from __future__ import annotations

import logging
import re
from datetime import datetime
from html import unescape

from bs4 import BeautifulSoup

from find_my_job.models import AppConfig, JobPosting, PortalConfig, ResumeProfile
from find_my_job.portals.http_utils import (
    absolute_url,
    fetch_html,
    fetch_json,
    looks_blocked,
    make_job,
    parse_relative_date,
)
from find_my_job.skills_logic import search_skills

logger = logging.getLogger("find_my_job.portals.hirist")

# Public unauthenticated feed (main /job/jobfeed requires login).
ALUMNI_API = "https://gladiator.hirist.tech/job/alumni-jobs"
MAX_PAGES = 8
PAGE_SIZE = 50


def _strip_html(text: str) -> str:
    if not text:
        return ""
    plain = re.sub(r"<[^>]+>", " ", text)
    return unescape(re.sub(r"\s+", " ", plain)).strip()


class Provider:
    name = "hirist"

    def search(
        self,
        profile: ResumeProfile | list[ResumeProfile],
        config: AppConfig,
        portal_cfg: PortalConfig,
        *,
        portal_key: str,
    ) -> list[JobPosting]:
        skills = [s.lower() for s in search_skills(config)]
        api_jobs = self._from_api_pages(config, portal_key, skills)
        if api_jobs:
            return api_jobs
        logger.info("Hirist alumni API empty/unavailable; falling back to HTML")
        return self._search_html(config, portal_cfg, portal_key)

    def _from_api_pages(
        self, config: AppConfig, portal_key: str, skills: list[str]
    ) -> list[JobPosting]:
        out: list[JobPosting] = []
        for page in range(MAX_PAGES):
            data, status = fetch_json(
                ALUMNI_API,
                params={"page": page, "limit": PAGE_SIZE},
                headers={
                    "Origin": "https://www.hirist.tech",
                    "Referer": "https://www.hirist.tech/",
                    "version": "2",
                },
            )
            if status != 200 or not isinstance(data, dict):
                break
            payload = data.get("data") or data
            raw_jobs = payload.get("jobs") or []
            if not raw_jobs:
                break
            for obj in raw_jobs:
                job = self._from_obj(obj, portal_key, config, skills)
                if job:
                    out.append(job)
            total = int(payload.get("total") or 0)
            if (page + 1) * PAGE_SIZE >= total:
                break
        return out

    def _from_obj(
        self,
        obj: dict,
        portal_key: str,
        config: AppConfig,
        skills: list[str],
    ) -> JobPosting | None:
        title = (obj.get("title") or "").strip()
        url = (obj.get("jobDetailUrl") or "").strip()
        if not title or not url:
            return None

        tags = obj.get("tags") or []
        tag_names = [
            (t.get("name") if isinstance(t, dict) else str(t)) or "" for t in tags
        ]
        intro = _strip_html(obj.get("introText") or "")
        blob = f"{title} {' '.join(tag_names)} {intro}".lower()
        if skills:
            # Word-boundary so "php" doesn't match random substrings
            if not any(re.search(rf"\b{re.escape(s)}\b", blob) for s in skills):
                return None

        locs = obj.get("locations") or obj.get("location") or []
        if isinstance(locs, list):
            loc_names = [
                (x.get("name") if isinstance(x, dict) else str(x)) or "" for x in locs
            ]
            location = ", ".join(n for n in loc_names if n)
        else:
            location = str(locs)

        if config.locations and location:
            loc_l = location.lower()
            wanted = [x.lower() for x in config.locations]
            remoteish = any(w in loc_l for w in ("remote", "work from home", "wfh"))
            if not remoteish and not any(w in loc_l for w in wanted):
                if not any("india" == w or "remote" in w for w in wanted):
                    return None

        company = ""
        cd = obj.get("companyData") or {}
        if isinstance(cd, dict):
            company = (cd.get("companyName") or "").strip()

        posted = None
        created = obj.get("createdTime") or obj.get("createdTimeNoMillis")
        if created:
            try:
                posted = datetime.fromisoformat(str(created).replace("Z", "+00:00")).date()
            except ValueError:
                posted = parse_relative_date(str(created))

        min_sal = obj.get("minSal") or 0
        max_sal = obj.get("maxSal") or 0
        listed = None
        # Hirist salaries are often annual INR lakhs * 100000 or raw LPA; only use when > 0
        if max_sal and max_sal < 200:
            listed = float(max_sal)
        elif min_sal and min_sal < 200:
            listed = float(min_sal)

        return make_job(
            portal=portal_key,
            url=url,
            title=title,
            company=company,
            location=location,
            posted_at=posted,
            listed_pay=listed,
            raw_text=intro or f"{title}. Tags: {', '.join(tag_names)}",
        )

    def _search_html(
        self, config: AppConfig, portal_cfg: PortalConfig, portal_key: str
    ) -> list[JobPosting]:
        from find_my_job.portals.http_utils import build_query, encode

        skills = search_skills(config)
        query, _ = build_query(skills, config.locations)
        urls = list(portal_cfg.search_urls)
        if not urls:
            urls.append(f"https://www.hirist.tech/search?keyword={encode(query)}")
            for loc in config.locations or []:
                urls.append(
                    f"https://www.hirist.tech/search?keyword={encode(query)}&location={encode(loc)}"
                )

        jobs: list[JobPosting] = []
        seen: set[str] = set()
        for url in urls:
            html, status = fetch_html(url)
            if looks_blocked(html, status):
                logger.warning("Hirist blocked: %s (%s)", url, status)
                continue
            for job in self._parse(html, url, portal_key):
                if job.id in seen:
                    continue
                seen.add(job.id)
                jobs.append(job)
        return jobs

    def _parse(self, html: str, page_url: str, portal_key: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "lxml")
        out: list[JobPosting] = []
        seen_urls: set[str] = set()
        cards = soup.select(
            "div.job-card, div.jobCard, article.job, div[class*='JobCard'], "
            "div.listing, li.job-list-item"
        )
        if not cards:
            cards = soup.select("a[href*='/j/'], a[href*='/job/']")
        for el in cards:
            a = el if getattr(el, "name", None) == "a" else el.select_one(
                "a[href*='/j/'], a[href*='/job'], a[href*='/jobs/'], h2 a, h3 a"
            )
            if not a:
                continue
            href = a.get("href") or ""
            title_el = el.select_one("h2, h3, .job-title, [class*='title']") or a
            title = title_el.get_text(" ", strip=True)
            if not title or not href:
                continue
            url = absolute_url("https://www.hirist.tech", href)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            company = el.select_one(".company, .company-name, [class*='company']")
            loc = el.select_one(".location, [class*='location']")
            date_el = el.select_one("time, .posted, [class*='posted']")
            out.append(
                make_job(
                    portal=portal_key,
                    url=url,
                    title=title,
                    company=company.get_text(" ", strip=True) if company else "",
                    location=loc.get_text(" ", strip=True) if loc else "",
                    posted_at=parse_relative_date(
                        date_el.get_text(" ", strip=True) if date_el else ""
                    ),
                    raw_text=title,
                )
            )
        if not out:
            logger.info("Hirist parse found 0 cards for %s", page_url)
        return out
