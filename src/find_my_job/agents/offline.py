from __future__ import annotations

import logging
import re

from find_my_job.models import AppConfig, JobPosting, JobScore, RagEvidence, ResumeProfile
from find_my_job.skills_logic import ranking_skills_for_profiles
from find_my_job.util import parse_listed_pay_lpa

logger = logging.getLogger("find_my_job.offline")


def _skill_overlap(job: JobPosting, config: AppConfig, profiles: list[ResumeProfile]) -> tuple[float, list[str]]:
    blob = f"{job.title}\n{job.raw_text}".lower()
    wanted = ranking_skills_for_profiles(config, profiles)
    hits: list[str] = []
    for skill in wanted:
        s = skill.strip()
        if not s:
            continue
        if len(s) <= 2:
            if re.search(rf"\b{re.escape(s.lower())}\b", blob):
                hits.append(s)
        elif s.lower() in blob:
            hits.append(s)
    if not wanted:
        return 0.5, []
    return min(1.0, len(hits) / max(3, min(8, len(wanted)))), hits


def _title_fit(job: JobPosting, profiles: list[ResumeProfile]) -> float:
    title = job.title.lower()
    senior_bits = ("senior", "staff", "principal", "lead", "architect", "manager")
    eng_bits = ("engineer", "developer", "architect", "sre", "devops", "backend", "platform")
    score = 0.4
    if any(b in title for b in eng_bits):
        score += 0.25
    if any(b in title for b in senior_bits):
        score += 0.2
    for p in profiles:
        for t in p.target_titles:
            if t.lower() in title or any(w in title for w in t.lower().split() if len(w) > 3):
                score += 0.15
                break
    return min(1.0, score)


def offline_rank(
    job: JobPosting,
    profiles: list[ResumeProfile],
    config: AppConfig,
    rag_score: float,
    evidence: list[RagEvidence],
) -> JobScore:
    """Rank without any paid LLM — RAG + skill/title heuristics."""
    default_label = evidence[0].resume_label if evidence else (
        profiles[0].label if profiles else "default"
    )
    # RAG miss (query failed / empty index): don't hard-drop — fall back to skills/title.
    rag_unavailable = rag_score <= 0.0 and not evidence
    effective_rag = 0.45 if rag_unavailable else rag_score

    if not rag_unavailable and rag_score < config.rag.min_similarity:
        return JobScore(
            job_id=job.id,
            relevance=0.0,
            rag_score=rag_score,
            final_score=rag_score,
            reasons=["Below RAG similarity floor (offline)"],
            resume_label=default_label,
            evidence=evidence,
            dropped=True,
            drop_reason="rag_min_similarity",
        )

    skill_score, skill_hits = _skill_overlap(job, config, profiles)
    title_score = _title_fit(job, profiles)
    relevance = 0.45 * effective_rag + 0.35 * skill_score + 0.20 * title_score
    final = 0.50 * effective_rag + 0.50 * relevance

    reasons = [
        "offline rank (local heuristics)",
        f"rag={rag_score:.2f}" + (" (unavailable→neutral)" if rag_unavailable else ""),
        f"skills matched: {', '.join(skill_hits[:8]) or 'none'}",
        f"title_fit={title_score:.2f}",
    ]

    pay = job.listed_pay
    pay_conf = 1.0 if pay is not None else 0.0
    if pay is None:
        pay = parse_listed_pay_lpa(f"{job.title} {job.raw_text}")
        pay_conf = 0.7 if pay is not None else 0.0

    dropped = False
    drop_reason = None
    if final < config.score_threshold:
        dropped = True
        drop_reason = "below_score_threshold"
    if (
        not dropped
        and config.pay.min_lpa is not None
        and pay is not None
        and pay_conf >= 0.6
        and pay < config.pay.min_lpa
    ):
        dropped = True
        drop_reason = "pay_below_min"

    return JobScore(
        job_id=job.id,
        relevance=relevance,
        rag_score=rag_score,
        final_score=final,
        reasons=reasons,
        pay_lpa_estimate=pay,
        pay_confidence=pay_conf,
        resume_label=default_label,
        evidence=evidence,
        dropped=dropped,
        drop_reason=drop_reason,
    )


def offline_cover_letter(
    job: JobPosting,
    score: JobScore,
    profiles: list[ResumeProfile],
    config: AppConfig,
) -> str:
    """Draft cover letter from resume/RAG snippets — no LLM."""
    profile = next((p for p in profiles if p.label == score.resume_label), None)
    if profile is None:
        profile = profiles[0]

    bullets = []
    for e in score.evidence[:4]:
        line = " ".join(e.text.split())
        if line:
            bullets.append(f"- {line[:220]}")
    if not bullets:
        excerpt = " ".join(profile.text.split())[:400]
        bullets = [f"- {excerpt}"]

    skills = ", ".join(
        (ranking_skills_for_profiles(config, profiles) or profile.skills)[:8]
    ) or "relevant technologies"
    company = job.company or "your team"
    return (
        f"Dear Hiring Team at {company},\n\n"
        f"I am applying for the {job.title} role"
        f"{f' in {job.location}' if job.location else ''}. "
        f"I am a senior engineer with "
        f"{f'{int(profile.seniority_years)}+ years of experience' if profile.seniority_years else 'extensive experience'} "
        f"and strong alignment with this position around {skills}.\n\n"
        f"Highlights from my background that map to this role:\n"
        + "\n".join(bullets)
        + "\n\n"
        f"My notice period / availability: {config.availability}. "
        f"I would welcome a conversation about how I can contribute at {company}.\n\n"
        f"Thank you for your consideration.\n\n"
        f"Best regards\n"
    )
