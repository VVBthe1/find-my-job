from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup

from find_my_job.models import JobPosting, JobType
from find_my_job.util import enrich_job_meta, infer_job_type, job_id_for, parse_listed_pay_lpa

logger = logging.getLogger("find_my_job.portals.http")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}


def fetch_html(url: str, *, timeout: float = 30.0) -> tuple[str, int]:
    with httpx.Client(follow_redirects=True, headers=DEFAULT_HEADERS, timeout=timeout) as client:
        resp = client.get(url)
        return resp.text, resp.status_code


def fetch_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[Any, int]:
    """GET JSON from a public board API. Returns (parsed_body_or_None, status)."""
    req_headers = {**DEFAULT_HEADERS, "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    with httpx.Client(follow_redirects=True, headers=req_headers, timeout=timeout) as client:
        resp = client.get(url, params=params)
        if resp.status_code >= 400:
            return None, resp.status_code
        try:
            return resp.json(), resp.status_code
        except ValueError:
            return None, resp.status_code


def looks_blocked(html: str, status: int) -> bool:
    if status in {401, 403, 429}:
        return True
    lower = html.lower()
    markers = [
        "captcha",
        "unusual traffic",
        "sign in to continue",
        "login to continue",
        "access denied",
        "cloudflare",
    ]
    return any(m in lower for m in markers)


def parse_relative_date(text: str) -> date | None:
    text = (text or "").strip().lower()
    if not text:
        return None
    if "today" in text or "just now" in text or "hour" in text:
        return date.today()
    if "yesterday" in text:
        return date.today() - timedelta(days=1)
    m = re.search(r"(\d+)\s*day", text)
    if m:
        return date.today() - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\s*week", text)
    if m:
        return date.today() - timedelta(days=7 * int(m.group(1)))
    m = re.search(r"(\d+)\s*month", text)
    if m:
        return date.today() - timedelta(days=30 * int(m.group(1)))
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def build_query(skills: list[str], locations: list[str]) -> tuple[str, str]:
    """Build a search query. Multiple skills → OR-ish query string for boards."""
    if not skills:
        query = "software engineer"
    elif len(skills) == 1:
        query = skills[0]
    else:
        # Boards differ; space-separated OR phrasing works reasonably on many IN boards
        query = " OR ".join(skills[:6])
    location = locations[0] if locations else "India"
    return query, location


def make_job(
    *,
    portal: str,
    url: str,
    title: str,
    company: str = "",
    location: str = "",
    posted_at: date | None = None,
    raw_text: str = "",
    listed_pay: float | None = None,
    job_type: JobType | None = None,
) -> JobPosting:
    blob = raw_text or f"{title} {company} {location}"
    pay = listed_pay if listed_pay is not None else parse_listed_pay_lpa(blob)
    jt = job_type or infer_job_type(title, blob)
    job = JobPosting(
        id=job_id_for(portal, url, title, company),
        portal=portal,
        url=url,
        title=title.strip(),
        company=company.strip(),
        location=location.strip(),
        posted_at=posted_at,
        raw_text=blob.strip(),
        listed_pay=pay,
        job_type=jt,
    )
    return enrich_job_meta(job)


def select_text(el: Any, selector: str) -> str:
    """Resolve CSS selector; support attr with 'sel@attr'."""
    if not selector:
        return ""
    attr = None
    css = selector
    if "@" in selector:
        css, attr = selector.split("@", 1)
    node = el.select_one(css) if css else el
    if node is None:
        return ""
    if attr:
        return (node.get(attr) or "").strip()
    return node.get_text(" ", strip=True)


def absolute_url(base: str, href: str) -> str:
    return urljoin(base, href)


def encode(s: str) -> str:
    return quote_plus(s)
