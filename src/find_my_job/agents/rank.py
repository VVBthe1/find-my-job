from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field

from find_my_job.agents.offline import offline_rank
from find_my_job.agents.rag import ResumeRAG
from find_my_job.llm import get_chat_model, resolve_llm_mode
from find_my_job.models import AppConfig, JobPosting, JobScore, ResumeProfile
from find_my_job.skills_logic import skills_for_prompt

logger = logging.getLogger("find_my_job.rank")


class RankResult(BaseModel):
    relevance: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    pay_lpa_estimate: float | None = None
    pay_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    resume_label: str = "default"


def _profile_summary(profiles: list[ResumeProfile]) -> str:
    parts = []
    for p in profiles:
        parts.append(
            f"[{p.label}] years={p.seniority_years} skills={', '.join(p.skills[:20])}\n"
            f"titles={', '.join(p.target_titles)}\n"
            f"excerpt:\n{p.text[:1500]}"
        )
    return "\n\n---\n\n".join(parts)


def _message_text(resp) -> str:
    content = getattr(resp, "content", resp)
    if isinstance(content, list):
        bits = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                bits.append(str(part["text"]))
            else:
                bits.append(str(part))
        return "".join(bits)
    return str(content)


def _parse_rank_json(text: str) -> RankResult:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    data = json.loads(text)
    return RankResult.model_validate(data)


def _invoke_rank(llm, prompt: str, *, provider: str) -> RankResult:
    # Gemini + with_structured_output can hang on tool/AFC loops; use plain JSON.
    if provider in {"google", "gemini"}:
        resp = llm.invoke(
            prompt
            + "\n\nRespond with ONLY a JSON object with keys: "
            "relevance (0-1 number), reasons (string array), "
            "pay_lpa_estimate (number|null), pay_confidence (0-1|null), "
            "resume_label (string)."
        )
        return _parse_rank_json(_message_text(resp))
    structured = llm.with_structured_output(RankResult)
    return structured.invoke(prompt)


def rank_job(
    job: JobPosting,
    profiles: list[ResumeProfile],
    config: AppConfig,
    rag: ResumeRAG,
    *,
    dry_run: bool = False,
) -> JobScore:
    rag_score, evidence = rag.score_job(job)
    mode = "offline" if dry_run else resolve_llm_mode(config)

    if mode == "offline":
        return offline_rank(job, profiles, config, rag_score, evidence)

    default_label = evidence[0].resume_label if evidence else (
        profiles[0].label if profiles else "default"
    )

    if rag_score < config.rag.min_similarity and not (
        rag_score <= 0.0 and not evidence
    ):
        return JobScore(
            job_id=job.id,
            relevance=0.0,
            rag_score=rag_score,
            final_score=rag_score,
            reasons=["Below RAG similarity floor"],
            resume_label=default_label,
            evidence=evidence,
            dropped=True,
            drop_reason="rag_min_similarity",
        )

    llm = get_chat_model(config)
    evidence_txt = "\n".join(f"- ({e.score:.2f}) {e.text[:300]}" for e in evidence[:5])
    labels = [p.label for p in profiles]
    prompt = f"""You are ranking a job for a senior engineer (~15+ years).

Candidate resumes:
{_profile_summary(profiles)}

Availability / notice: {config.availability}
Skills config:
{skills_for_prompt(config.skills)}
Min LPA: {config.pay.min_lpa}

Resume evidence (RAG):
{evidence_txt}

Job:
Title: {job.title}
Company: {job.company}
Location: {job.location}
Listed pay LPA: {job.listed_pay}
Type: {job.job_type.value}
URL: {job.url}
JD:
{job.raw_text[:3500]}

Instructions:
- relevance 0-1 for fit vs resumes (senior IC / eng roles preferred).
- If JD has no pay number, estimate pay_lpa_estimate in Indian LPA and set pay_confidence.
- If listed pay exists, set pay_lpa_estimate to that and pay_confidence=1.
- resume_label must be one of: {labels}
- Give 2-5 short reasons.
"""
    try:
        result = _invoke_rank(llm, prompt, provider=config.llm.provider)
    except Exception as exc:
        logger.warning("LLM rank failed for %s — falling back to offline: %s", job.id, exc)
        return offline_rank(job, profiles, config, rag_score, evidence)

    final = 0.45 * rag_score + 0.55 * float(result.relevance)
    dropped = False
    drop_reason = None
    if final < config.score_threshold:
        dropped = True
        drop_reason = "below_score_threshold"

    est = result.pay_lpa_estimate
    conf = result.pay_confidence or 0.0
    if (
        not dropped
        and config.pay.min_lpa is not None
        and est is not None
        and conf >= 0.6
        and est < config.pay.min_lpa
    ):
        dropped = True
        drop_reason = "estimated_pay_below_min"

    return JobScore(
        job_id=job.id,
        relevance=float(result.relevance),
        rag_score=rag_score,
        final_score=final,
        reasons=list(result.reasons),
        pay_lpa_estimate=est,
        pay_confidence=result.pay_confidence,
        resume_label=result.resume_label or default_label,
        evidence=evidence,
        dropped=dropped,
        drop_reason=drop_reason,
    )


def rank_jobs(
    jobs: list[JobPosting],
    profiles: list[ResumeProfile],
    config: AppConfig,
    rag: ResumeRAG,
    *,
    dry_run: bool = False,
) -> list[JobScore]:
    scores = [
        rank_job(job, profiles, config, rag, dry_run=dry_run) for job in jobs
    ]
    scores.sort(key=lambda s: s.final_score, reverse=True)
    return scores
