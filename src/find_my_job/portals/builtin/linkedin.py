from __future__ import annotations

import logging
import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from find_my_job.models import AppConfig, JobPosting, PortalConfig, ResumeProfile
from find_my_job.portals.http_utils import (
    absolute_url,
    build_query,
    encode,
    fetch_html,
    looks_blocked,
    make_job,
    parse_relative_date,
)
from find_my_job.skills_logic import search_skills

logger = logging.getLogger("find_my_job.portals.linkedin")

# Public logged-out guest endpoint (HTML job cards). No OAuth / partner token.
GUEST_SEARCH = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)
PAGE_SIZE = 25
DEFAULT_MAX_PAGES = 2  # per location × skill combo; keep low to avoid 429


class Provider:
    name = "linkedin"

    def search(
        self,
        profile: ResumeProfile | list[ResumeProfile],
        config: AppConfig,
        portal_cfg: PortalConfig,
        *,
        portal_key: str,
    ) -> list[JobPosting]:
        skills = search_skills(config)
        query, _ = build_query(skills, config.locations)
        max_pages = int(portal_cfg.extra.get("max_pages", DEFAULT_MAX_PAGES))
        # LinkedIn f_TPR is seconds: r{seconds}
        tpr_secs = max(86400, int(config.recency_days or 14) * 86400)

        locations = list(config.locations) or ["India"]
        # Prefer one combined query; also try each must_have alone if OR query is awkward
        queries = [query]
        if " OR " in query and len(skills) > 1:
            queries = list(skills[:4])

        jobs: list[JobPosting] = []
        seen: set[str] = set()
        for loc in locations:
            for q in queries:
                for page in range(max_pages):
                    params = {
                        "keywords": q,
                        "location": loc,
                        "f_TPR": f"r{tpr_secs}",
                        "start": page * PAGE_SIZE,
                    }
                    url = f"{GUEST_SEARCH}?{urlencode(params)}"
                    html, status = fetch_html(url)
                    if status in {429, 999} or looks_blocked(html, status):
                        logger.warning(
                            "LinkedIn guest blocked/rate-limited: %s (%s)", url, status
                        )
                        return jobs
                    if status != 200 or not html.strip():
                        break
                    batch = self._parse(html, url, portal_key)
                    if not batch:
                        break
                    for job in batch:
                        key = job.url.split("?")[0]
                        # normalize view id
                        m = re.search(r"/jobs/view/[^/]*?(\d{8,})", key)
                        dedupe = m.group(1) if m else key
                        if dedupe in seen:
                            continue
                        seen.add(dedupe)
                        jobs.append(job)
        logger.info("LinkedIn guest returned %s jobs", len(jobs))
        return jobs

    def _parse(self, html: str, page_url: str, portal_key: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "lxml")
        out: list[JobPosting] = []
        cards = soup.select(
            "div.base-card, li.jobs-search-results__list-item, "
            "div.job-search-card, ul.jobs-search__results-list li"
        )
        for el in cards:
            a = el.select_one("a.base-card__full-link, a[href*='/jobs/view/'], h3 a")
            if not a:
                continue
            title_el = el.select_one("h3, .base-search-card__title")
            title = (title_el or a).get_text(" ", strip=True)
            href = a.get("href") or ""
            if not title or not href:
                continue
            company = el.select_one(
                "h4, .base-search-card__subtitle, .job-search-card__subtitle"
            )
            loc = el.select_one(".job-search-card__location")
            date_el = el.select_one("time")
            posted = None
            if date_el and date_el.get("datetime"):
                posted = parse_relative_date(date_el["datetime"])
            elif date_el:
                posted = parse_relative_date(date_el.get_text(" ", strip=True))
            location = loc.get_text(" ", strip=True) if loc else ""
            # Strip trailing relative-date noise if a broader selector ever matches
            location = re.sub(
                r"\s*\d+\s+(minute|hour|day|week|month)s?\s+ago\s*$",
                "",
                location,
                flags=re.I,
            ).strip()
            clean = absolute_url(page_url, href.split("?")[0])
            out.append(
                make_job(
                    portal=portal_key,
                    url=clean,
                    title=title,
                    company=company.get_text(" ", strip=True) if company else "",
                    location=location,
                    posted_at=posted,
                    raw_text=title,
                )
            )
        return out
