from __future__ import annotations

import hashlib
import json

from find_my_job.models import AppConfig, JobPosting, ResumeProfile


def job_content_hash(job: JobPosting) -> str:
    blob = "|".join(
        [
            job.id,
            job.url,
            job.title,
            job.company,
            job.location,
            job.raw_text or "",
            str(job.listed_pay),
            job.job_type.value,
        ]
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def resumes_hash(profiles: list[ResumeProfile]) -> str:
    parts = sorted(f"{p.label}:{p.file_hash}" for p in profiles)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def rank_cache_key(
    job: JobPosting,
    profiles: list[ResumeProfile],
    config: AppConfig,
    *,
    mode: str | None = None,
) -> str:
    """Stable key for LLM rank results (invalidates on JD/resume/threshold/model change)."""
    material = {
        "kind": "rank",
        "job": job_content_hash(job),
        "resumes": resumes_hash(profiles),
        "model": config.llm.model,
        "provider": config.llm.provider,
        "mode": mode or config.llm.mode,
        "score_threshold": config.score_threshold,
        "min_similarity": config.rag.min_similarity,
        "min_lpa": config.pay.min_lpa,
        "max_lpa": config.pay.max_lpa,
        "skills_must_have": [
            m.model_dump() if hasattr(m, "model_dump") else m
            for m in config.skills.must_have
        ],
        "skills_include": config.skills.include,
        "skills_exclude": config.skills.exclude,
        "experience_min": config.experience.min_years,
        "pay_unknown": config.pay.unknown,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True).encode()
    ).hexdigest()


def letter_cache_key(
    job: JobPosting,
    profiles: list[ResumeProfile],
    config: AppConfig,
    resume_label: str,
    *,
    mode: str | None = None,
) -> str:
    material = {
        "kind": "letter",
        "job": job_content_hash(job),
        "resumes": resumes_hash(profiles),
        "resume_label": resume_label,
        "model": config.llm.model,
        "provider": config.llm.provider,
        "mode": mode or config.llm.mode,
        "availability": config.availability,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True).encode()
    ).hexdigest()
