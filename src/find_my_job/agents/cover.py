from __future__ import annotations

import logging

from find_my_job.agents.offline import offline_cover_letter
from find_my_job.llm import get_chat_model, resolve_llm_mode
from find_my_job.models import (
    AppConfig,
    CoverLetter,
    JobPosting,
    JobScore,
    ResumeProfile,
)

logger = logging.getLogger("find_my_job.cover")


def generate_cover_letter(
    job: JobPosting,
    score: JobScore,
    profiles: list[ResumeProfile],
    config: AppConfig,
    *,
    dry_run: bool = False,
) -> CoverLetter:
    profile = next((p for p in profiles if p.label == score.resume_label), None)
    if profile is None:
        profile = profiles[0]

    mode = "offline" if dry_run else resolve_llm_mode(config)
    if mode == "offline":
        text = offline_cover_letter(job, score, profiles, config)
        return CoverLetter(job_id=job.id, resume_label=profile.label, text=text)

    evidence = "\n".join(f"- {e.text[:250]}" for e in score.evidence[:4])
    llm = get_chat_model(config, temperature=0.4)
    prompt = f"""Write a concise, professional cover letter (200-350 words) for this job.
Use ONLY facts supported by the resume excerpt and RAG evidence. Do not invent employers.
Tone: senior engineer, direct, no fluff. Mention notice period: {config.availability}.

Resume label: {profile.label}
Resume excerpt:
{profile.text[:3000]}

RAG evidence:
{evidence}

Job title: {job.title}
Company: {job.company}
Location: {job.location}
JD:
{job.raw_text[:2500]}

Return the letter body only, plain text.
"""
    try:
        resp = llm.invoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        if isinstance(text, list):
            text = "".join(
                (t.get("text") if isinstance(t, dict) else str(t)) for t in text
            )
    except Exception as exc:
        logger.warning("Cover letter LLM failed — using offline template: %s", exc)
        text = offline_cover_letter(job, score, profiles, config)
    return CoverLetter(job_id=job.id, resume_label=profile.label, text=str(text).strip())
