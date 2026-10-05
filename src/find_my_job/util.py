from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, urlunparse

from find_my_job.models import JobPosting, JobType

_INTERN = re.compile(r"\b(intern(ship)?|trainee)\b", re.I)
_CONTRACT = re.compile(r"\b(contract(or)?|consultancy|consultant)\b", re.I)
_FREELANCE = re.compile(r"\b(freelance|gig)\b", re.I)
_PART = re.compile(r"\b(part[\s-]?time)\b", re.I)
_PAY_LPA = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:-|to)?\s*(\d+(?:\.\d+)?)?\s*(?:lpa|lakh|lac)s?",
    re.I,
)
# Naukri slug + common JD: "4-to-9-years", "5 to 10 years", "8+ years", "3-5 yrs"
_EXP_RANGE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(\d{1,2})(?:\s*(?:-|–|to)\s*(\d{1,2}))?\s*"
    r"(?:\+?\s*)?(?:years?|yrs?)(?:\s+of\s+exp|\s+experience)?"
)
_EXP_SLUG = re.compile(r"(?i)(\d{1,2})-to-(\d{1,2})-years?")
_EXP_PLUS = re.compile(r"(?i)(\d{1,2})\+\s*(?:years?|yrs?)")


def infer_job_type(title: str, raw_text: str = "") -> JobType:
    blob = f"{title}\n{raw_text}"
    if _INTERN.search(blob):
        return JobType.INTERN
    if _FREELANCE.search(blob):
        return JobType.FREELANCE
    if _PART.search(blob):
        return JobType.PART_TIME
    if _CONTRACT.search(blob):
        return JobType.CONTRACT
    return JobType.FULL_TIME


def parse_listed_pay_lpa(text: str) -> float | None:
    m = _PAY_LPA.search(text or "")
    if not m:
        return None
    low = float(m.group(1))
    high = float(m.group(2)) if m.group(2) else low
    return (low + high) / 2.0


def parse_experience_years(text: str) -> tuple[float | None, float | None]:
    """Return (min_years, max_years) from title/JD/URL slug. Either may be None."""
    blob = text or ""
    m = _EXP_SLUG.search(blob.replace("_", "-"))
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (min(lo, hi), max(lo, hi))
    m = _EXP_PLUS.search(blob)
    if m:
        y = float(m.group(1))
        return (y, None)
    best: tuple[float | None, float | None] = (None, None)
    for m in _EXP_RANGE.finditer(blob):
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else lo
        if lo > 40 or hi > 40:
            continue  # skip dates / noise
        if best[0] is None or hi > (best[1] or 0):
            best = (min(lo, hi), max(lo, hi))
    return best


def enrich_job_meta(job: JobPosting) -> JobPosting:
    """Fill exp_* and listed_pay from title/url/text when missing."""
    blob = f"{job.title}\n{job.url}\n{job.raw_text}"
    if job.exp_min_years is None and job.exp_max_years is None:
        lo, hi = parse_experience_years(blob)
        job.exp_min_years = lo
        job.exp_max_years = hi
    if job.listed_pay is None:
        job.listed_pay = parse_listed_pay_lpa(blob)
    return job


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    # Keep identity query params used by job boards (Indeed etc.); drop tracking.
    keep = {
        "jk",
        "vjk",
        "id",
        "jobId",
        "jobid",
        "job_id",
        "posting_id",
        "currentJobId",
    }
    from urllib.parse import parse_qsl, urlencode

    q = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False) if k in keep]
    query = urlencode(q)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, ""))


def job_id_for(portal: str, url: str, title: str = "", company: str = "") -> str:
    canon = canonicalize_url(url)
    if not canon or canon.rstrip("/") in {
        "https://in.indeed.com/rc/clk",
        "https://in.indeed.com/viewjob",
        "https://www.indeed.com/viewjob",
    }:
        canon = f"{company}|{title}|{url}"
    digest = hashlib.sha256(f"{portal}|{canon}".encode()).hexdigest()[:16]
    return f"{portal}:{digest}"


def dedupe_jobs(jobs: list[JobPosting]) -> list[JobPosting]:
    by_id: dict[str, JobPosting] = {}
    fuzzy: set[str] = set()
    out: list[JobPosting] = []
    for job in jobs:
        if job.id in by_id:
            continue
        company = (job.company or "").strip()
        title = (job.title or "").strip()
        # Only fuzzy-merge when both sides are meaningful (empty company used to
        # collapse unrelated Indeed cards into one row).
        key = ""
        if len(company) >= 2 and len(title) >= 5:
            key = re.sub(r"\W+", "", f"{company}|{title}".lower())
            if key in fuzzy:
                continue
        by_id[job.id] = job
        if key:
            fuzzy.add(key)
        out.append(job)
    return out


def slugify(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (s or "job")[:max_len]
