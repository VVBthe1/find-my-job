from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from find_my_job.models import AppConfig, JobPosting, PortalConfig, ResumeProfile
from find_my_job.skills_logic import search_skills
from find_my_job.portals.http_utils import (
    absolute_url,
    build_query,
    encode,
    fetch_html,
    looks_blocked,
    make_job,
)

logger = logging.getLogger("find_my_job.portals.wellfound")


class Provider:
    name = "wellfound"

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
        urls = list(portal_cfg.search_urls)
        if not urls:
            for loc in config.locations or ["India"]:
                urls.append(
                    f"https://wellfound.com/role/l/software-engineer/{encode(loc).replace('+', '-').lower()}"
                    if False
                    else f"https://wellfound.com/jobs?query={encode(query)}&location={encode(loc)}"
                )

        jobs: list[JobPosting] = []
        for url in urls:
            html, status = fetch_html(url)
            if looks_blocked(html, status):
                logger.warning("Wellfound blocked: %s (%s)", url, status)
                continue
            jobs.extend(self._parse(html, url, portal_key))
        return jobs

    def _parse(self, html: str, page_url: str, portal_key: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "lxml")
        out: list[JobPosting] = []
        for el in soup.select(
            "div[class*='styles_result'], a[href*='/jobs/'], div.job-listing, article"
        ):
            a = el if (getattr(el, "name", None) == "a" and el.get("href")) else el.select_one(
                "a[href*='/jobs/'], a[href*='/role/']"
            )
            if not a:
                continue
            href = a.get("href") or ""
            title_el = el.select_one("h2, h3, [class*='title']") or a
            title = title_el.get_text(" ", strip=True)
            if not title or not href or "/jobs/" not in href and "/role/" not in href:
                continue
            company = el.select_one("[class*='company'], h4, .company-name")
            loc = el.select_one("[class*='location'], .location")
            out.append(
                make_job(
                    portal=portal_key,
                    url=absolute_url("https://wellfound.com", href),
                    title=title,
                    company=company.get_text(" ", strip=True) if company else "",
                    location=loc.get_text(" ", strip=True) if loc else "",
                    raw_text=title,
                )
            )
        seen: set[str] = set()
        unique: list[JobPosting] = []
        for j in out:
            if j.id in seen:
                continue
            seen.add(j.id)
            unique.append(j)
        return unique
