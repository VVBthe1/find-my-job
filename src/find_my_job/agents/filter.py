from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from find_my_job.models import AppConfig, JobPosting, ResumeProfile
from find_my_job.skills_logic import excluded_mandatory_skill, matches_must_have
from find_my_job.util import enrich_job_meta, parse_listed_pay_lpa

logger = logging.getLogger("find_my_job.filter")


def _to_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def location_matches(job: JobPosting, locations: list[str]) -> bool:
    if not locations:
        return True
    blob = (job.location or "").lower()
    for loc in locations:
        l = loc.lower()
        if l in blob or blob in l:
            return True
        if "remote" in l and "remote" in blob:
            return True
    return not blob


def candidate_min_years(
    config: AppConfig, profiles: list[ResumeProfile] | None = None
) -> float | None:
    if config.experience.drop_if_jd_max_below is not None:
        return float(config.experience.drop_if_jd_max_below)
    if config.experience.min_years is not None:
        return float(config.experience.min_years)
    if profiles:
        years = [p.seniority_years for p in profiles if p.seniority_years]
        if years:
            return float(max(years))
    return None


def _pay_decision(
    job: JobPosting, config: AppConfig
) -> tuple[bool, str | None, float | None, float]:
    """Return (ok, drop_reason, effective_pay, confidence)."""
    pay = job.listed_pay
    conf = 1.0 if pay is not None else 0.0
    if pay is None:
        est = parse_listed_pay_lpa(f"{job.title} {job.raw_text}")
        if est is not None:
            pay = est
            conf = 0.7
        elif config.pay.unknown == "drop":
            return False, "pay_unknown", None, 0.0
        elif config.pay.unknown == "keep":
            return True, None, None, 0.0
        else:
            # estimate mode but nothing found — keep for later LLM estimate
            return True, None, None, 0.0

    min_c = config.pay.estimate_confidence_min
    if (
        config.pay.min_lpa is not None
        and pay is not None
        and conf >= min_c
        and pay < config.pay.min_lpa
    ):
        return False, "pay_below_min", pay, conf
    if (
        config.pay.max_lpa is not None
        and pay is not None
        and conf >= min_c
        and pay > config.pay.max_lpa
    ):
        return False, "pay_above_max", pay, conf
    return True, None, pay, conf


def hard_filter(
    jobs: list[JobPosting],
    config: AppConfig,
    profiles: list[ResumeProfile] | None = None,
) -> tuple[list[JobPosting], list[tuple[JobPosting, str]]]:
    allowed = config.job_types.allowed()
    kept: list[JobPosting] = []
    dropped: list[tuple[JobPosting, str]] = []
    cutoff = date.today() - timedelta(days=config.recency_days)
    floor = candidate_min_years(config, profiles)

    for job in jobs:
        enrich_job_meta(job)

        if job.job_type not in allowed:
            dropped.append((job, f"job_type:{job.job_type.value}"))
            continue
        if not location_matches(job, config.locations):
            dropped.append((job, "location"))
            continue

        # Experience: drop clearly junior JDs vs your floor
        jd_max = job.exp_max_years
        jd_min = job.exp_min_years
        if floor is not None:
            if jd_max is not None and jd_max < floor:
                dropped.append((job, f"experience_junior:jd_max={jd_max}<{floor}"))
                continue
            if (
                jd_max is None
                and jd_min is not None
                and jd_min < floor
                and jd_min <= floor * 0.4
            ):
                dropped.append((job, f"experience_junior:jd_min={jd_min}<{floor}"))
                continue
            if jd_max is None and jd_min is None and config.experience.unknown == "drop":
                dropped.append((job, "experience_unknown"))
                continue

        if not matches_must_have(job, config):
            dropped.append((job, "missing_must_have"))
            continue
        excl = excluded_mandatory_skill(job, config)
        if excl:
            dropped.append((job, f"excluded_mandatory:{excl}"))
            continue
        posted = _to_date(job.posted_at)
        if posted is None:
            if config.unknown_recency == "drop":
                dropped.append((job, "unknown_recency"))
                continue
        elif posted < cutoff:
            dropped.append((job, "stale"))
            continue

        ok_pay, pay_reason, pay_val, _conf = _pay_decision(job, config)
        if pay_val is not None:
            job.listed_pay = pay_val
        if not ok_pay:
            dropped.append((job, pay_reason or "pay"))
            continue

        kept.append(job)

    logger.info("Hard filter: kept %d / dropped %d", len(kept), len(dropped))
    return kept, dropped
