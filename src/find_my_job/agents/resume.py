from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from find_my_job.models import AppConfig, ResumeProfile, ResumeRef
from find_my_job.skills_logic import must_have_names

logger = logging.getLogger("find_my_job.resume")

_RESUME_EXTS = {".pdf", ".docx", ".doc", ".md", ".txt"}


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from langchain_community.document_loaders import PyPDFLoader

        docs = PyPDFLoader(str(path)).load()
        return "\n".join(d.page_content for d in docs)
    if suffix in {".docx", ".doc"}:
        try:
            from langchain_community.document_loaders import Docx2txtLoader

            docs = Docx2txtLoader(str(path)).load()
            return "\n".join(d.page_content for d in docs)
        except Exception:
            import docx2txt

            return docx2txt.process(str(path)) or ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _extract_skills(text: str, hints: list[str]) -> list[str]:
    found: list[str] = []
    lower = text.lower()
    for skill in hints:
        if skill.lower() in lower:
            found.append(skill)
    m = re.search(r"(?is)skills?\s*[:\-]?\s*(.+?)(?:\n\n|\n[A-Z][a-z]+\:|$)", text)
    if m:
        blob = m.group(1)
        for part in re.split(r"[,|;/\n]", blob):
            s = part.strip(" -•\t")
            if 1 < len(s) < 40 and s not in found:
                found.append(s)
    return found[:40]


def _estimate_years(text: str) -> float | None:
    patterns = [
        r"(\d+)\+?\s*years?\s+(?:of\s+)?experience",
        r"experience\s*[:\-]?\s*(\d+)\+?\s*years?",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return float(m.group(1))
    return None


def _target_titles(text: str) -> list[str]:
    titles: list[str] = []
    for pat in [
        r"(?i)\b(senior|staff|principal|lead)?\s*(software|backend|fullstack|full[\s-]?stack|platform|devops|sre|data)\s*(engineer|developer|architect)\b",
        r"(?i)\b(engineering manager|tech lead|technical lead)\b",
    ]:
        for m in re.finditer(pat, text):
            t = re.sub(r"\s+", " ", m.group(0)).strip()
            if t and t not in titles:
                titles.append(t)
    return titles[:10]


def _label_from_filename(path: Path) -> str:
    stem = path.stem
    stem = re.sub(r"(?i)[_-]?(resume|cv)$", "", stem).strip("_- ")
    return (stem or path.stem).lower().replace(" ", "-")[:40]


def resolve_resume_path(ref: ResumeRef, config: AppConfig) -> Path:
    if ref.path:
        p = Path(ref.path).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        return p
    assert ref.file
    base = Path(config.resumes_dir)
    if not base.is_absolute():
        base = Path.cwd() / base
    return (base / ref.file).resolve()


def discover_resume_refs(config: AppConfig) -> list[ResumeRef]:
    """Use config.resumes if set; else scan resumes_dir for documents."""
    if config.resumes:
        return list(config.resumes)
    base = Path(config.resumes_dir)
    if not base.is_absolute():
        base = Path.cwd() / base
    if not base.is_dir():
        return []
    refs: list[ResumeRef] = []
    for path in sorted(base.iterdir()):
        if path.suffix.lower() not in _RESUME_EXTS:
            continue
        if path.name.startswith("."):
            continue
        if path.stem.lower() in {"readme", "license", "changelog"}:
            continue
        refs.append(ResumeRef(file=path.name, label=_label_from_filename(path)))
    return refs


def parse_resume(ref: ResumeRef, config: AppConfig) -> ResumeProfile:
    path = resolve_resume_path(ref, config)
    if not path.exists():
        raise FileNotFoundError(f"Resume not found: {path}")

    text = _load_text(path)
    hints = list(
        dict.fromkeys(
            must_have_names(config)
            + list(config.skills.include)
            + list(ref.include)
        )
    )
    skills = _extract_skills(text, hints)
    # Prefer explicit per-resume include for ranking bag
    include = list(dict.fromkeys(list(ref.include) + list(config.skills.include)))
    return ResumeProfile(
        label=ref.label or _label_from_filename(path),
        path=str(path),
        text=text,
        skills=skills,
        seniority_years=_estimate_years(text),
        target_titles=_target_titles(text),
        file_hash=_file_hash(path),
        include=include,
    )


def parse_all_resumes(config: AppConfig) -> list[ResumeProfile]:
    refs = discover_resume_refs(config)
    if not refs:
        raise ValueError(
            f"No resumes found. Put PDF/DOCX/MD files in '{config.resumes_dir}/' "
            "or list them under config.resumes."
        )
    profiles = [parse_resume(ref, config) for ref in refs]
    logger.info(
        "Loaded %d resume(s): %s",
        len(profiles),
        ", ".join(f"{p.label}({p.seniority_years or '?'}y)" for p in profiles),
    )
    return profiles
