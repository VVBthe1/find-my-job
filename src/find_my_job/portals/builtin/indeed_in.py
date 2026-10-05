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
    parse_relative_date,
)

logger = logging.getLogger("find_my_job.portals.indeed")


class Provider:
    name = "indeed_in"

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
                    f"https://in.indeed.com/jobs?q={encode(query)}&l={encode(loc)}&fromage=14"
                )

        jobs: list[JobPosting] = []
        for url in urls:
            html, status = fetch_html(url)
            if looks_blocked(html, status):
                logger.warning("Indeed blocked: %s (%s)", url, status)
                continue
            jobs.extend(self._parse(html, url, portal_key))
        return jobs

    def _parse(self, html: str, page_url: str, portal_key: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "lxml")
        out: list[JobPosting] = []
        for el in soup.select("div.job_seen_beacon, li.css-5lfssg, div.jobsearch-ResultsList > div, .result"):
            a = el.select_one("h2.jobTitle a, a.jcs-JobTitle, h2 a")
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            href = a.get("href") or ""
            if not title or not href:
                continue
            company = el.select_one("[data-testid='company-name'], span.companyName, .companyName")
            loc = el.select_one("[data-testid='text-location'], .companyLocation")
            date_el = el.select_one("span.date, [data-testid='myJobsStateDate']")
            snip = el.select_one(".job-snippet, .jobCardShelfContainer")
            out.append(
                make_job(
                    portal=portal_key,
                    url=absolute_url("https://in.indeed.com", href),
                    title=title,
                    company=company.get_text(" ", strip=True) if company else "",
                    location=loc.get_text(" ", strip=True) if loc else "",
                    posted_at=parse_relative_date(
                        date_el.get_text(" ", strip=True) if date_el else ""
                    ),
                    raw_text=snip.get_text(" ", strip=True) if snip else title,
                )
            )
        return out
