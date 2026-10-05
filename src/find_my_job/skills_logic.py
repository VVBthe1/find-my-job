from __future__ import annotations

import re

from find_my_job.models import AppConfig, JobPosting, MustHaveSkill, SkillsConfig


_MANDATORY_HINT = re.compile(
    r"(?i)\b(must[\s-]?have|mandatory|required|essential|compulsory|key skills?|"
    r"primary skills?|core skills?|tech(nical)? requirements?)\b"
)
_OPTIONAL_HINT = re.compile(
    r"(?i)\b(nice[\s-]?to[\s-]?have|good[\s-]?to[\s-]?have|optional|preferred|"
    r"bonus|plus points?)\b"
)
_OR_SPLIT = re.compile(r"(?i)\s+(?:or|/|,|\|)\s+")


def must_have_names(skills: SkillsConfig | AppConfig) -> list[str]:
    cfg = skills.skills if isinstance(skills, AppConfig) else skills
    return [m.skill for m in cfg.must_have if m.skill]


def search_skills(config: AppConfig) -> list[str]:
    """Skills used to build portal search queries (OR semantics)."""
    names = must_have_names(config)
    if names:
        return names
    return list(config.skills.include) or ["software engineer"]


def ranking_skills(config: AppConfig, extra: list[str] | None = None) -> list[str]:
    """Skills used for relevance ranking."""
    skills = list(config.skills.include)
    if extra:
        skills = list(dict.fromkeys(skills + extra))
    return skills


def ranking_skills_for_profiles(
    config: AppConfig, profiles: list
) -> list[str]:
    extra: list[str] = []
    for p in profiles:
        extra.extend(getattr(p, "include", None) or [])
        extra.extend(getattr(p, "skills", None) or [])
    return ranking_skills(config, extra)


def _contains_skill(text: str, skill: str) -> bool:
    s = skill.strip().lower()
    if not s:
        return False
    if len(s) <= 3:
        return bool(re.search(rf"\b{re.escape(s)}\b", text))
    return s in text


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?\n])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _has_or_escape(sentence: str, excluded: str, allowed: list[str]) -> bool:
    """True if excluded skill is offered as an alternative to an allowed skill."""
    lower = sentence.lower()
    if not _contains_skill(lower, excluded):
        return False
    pieces = _OR_SPLIT.split(sentence)
    if len(pieces) < 2 and "/" not in sentence:
        pieces = re.split(r"[/,|]", sentence)
    has_excluded = any(_contains_skill(p.lower(), excluded) for p in pieces)
    has_allowed = any(
        _contains_skill(p.lower(), a) for p in pieces for a in allowed if a
    )
    if has_excluded and has_allowed and (
        re.search(r"(?i)\bor\b", sentence) or "/" in sentence or "|" in sentence
    ):
        return True
    if re.search(
        rf"(?i)\b{re.escape(excluded)}\b.{{0,40}}\bor\b|\bor\b.{{0,40}}\b{re.escape(excluded)}\b",
        sentence,
    ):
        if any(_contains_skill(lower, a) for a in allowed):
            return True
    return False


def _in_optional_section(sentence: str) -> bool:
    return bool(_OPTIONAL_HINT.search(sentence))


def _in_mandatory_section(sentence: str, *, title: bool = False) -> bool:
    if title:
        return True
    return bool(_MANDATORY_HINT.search(sentence))


def _skill_satisfies(job: JobPosting, entry: MustHaveSkill) -> bool:
    blob = f"{job.title}\n{job.raw_text}".lower()
    if not _contains_skill(blob, entry.skill):
        return False
    if entry.min_exp is None:
        return True
    # When JD publishes a max experience, it should reach the skill floor.
    if job.exp_max_years is not None and job.exp_max_years < entry.min_exp:
        return False
    if (
        job.exp_max_years is None
        and job.exp_min_years is not None
        and job.exp_min_years < entry.min_exp
        and job.exp_min_years <= entry.min_exp * 0.5
    ):
        # e.g. "2+ years" vs min_exp 6 — too junior for this skill match
        return False
    return True


def matches_must_have(job: JobPosting, config: AppConfig) -> bool:
    """At least one must_have skill matches (OR). Empty must_have = pass."""
    must = config.skills.must_have
    if not must:
        return True
    return any(_skill_satisfies(job, entry) for entry in must)


def excluded_mandatory_skill(job: JobPosting, config: AppConfig) -> str | None:
    """
    Drop if an excluded skill is required as mandatory, unless the JD offers it
    as an OR alternative alongside a must_have/include skill.
    """
    excluded = config.skills.exclude
    if not excluded:
        return None

    allowed = list(
        dict.fromkeys(must_have_names(config) + list(config.skills.include))
    )
    title = job.title or ""
    body = job.raw_text or ""

    for skill in excluded:
        if _contains_skill(title.lower(), skill):
            if _has_or_escape(title, skill, allowed):
                continue
            return skill

    sentences = _sentences(body)
    for skill in excluded:
        hit_mandatory = False
        escaped = False
        for sent in sentences:
            if not _contains_skill(sent.lower(), skill):
                continue
            if _in_optional_section(sent):
                continue
            if _has_or_escape(sent, skill, allowed):
                escaped = True
                continue
            if _in_mandatory_section(sent):
                hit_mandatory = True
                break
        if hit_mandatory and not escaped:
            return skill

        if not escaped and not hit_mandatory:
            for sent in sentences:
                if _contains_skill(sent.lower(), skill) and _MANDATORY_HINT.search(sent):
                    if not _has_or_escape(sent, skill, allowed):
                        return skill
                    escaped = True

    return None


def skills_for_prompt(skills: SkillsConfig) -> str:
    must_bits = []
    for m in skills.must_have:
        if m.min_exp is not None:
            must_bits.append(f"{m.skill} (>={m.min_exp}y)")
        else:
            must_bits.append(m.skill)
    return (
        f"must_have (OR for search/match): {', '.join(must_bits) or '(none)'}\n"
        f"include (ranking): {', '.join(skills.include) or '(none)'}\n"
        f"exclude (not mandatory unless OR): {', '.join(skills.exclude) or '(none)'}"
    )
