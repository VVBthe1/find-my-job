from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

from find_my_job.models import AppConfig, JobPosting, PortalConfig, ResumeProfile
from find_my_job.portals.http_utils import (
    DEFAULT_HEADERS,
    absolute_url,
    fetch_html,
    looks_blocked,
    make_job,
    parse_relative_date,
)
from find_my_job.skills_logic import search_skills

logger = logging.getLogger("find_my_job.portals.naukri")

NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# City name (lower) → sitemap filename fragment after jobDescPages
CITY_SITEMAPS: dict[str, list[str]] = {
    "pune": ["jobDescPagesPune.xml"],
    "ahmedabad": ["jobDescPagesAhmedabad.xml"],
    "kolkata": ["jobDescPagesKolkata.xml"],
    "mumbai": ["jobDescPagesMumbai-1.xml.gz", "jobDescPagesMumbai-2.xml.gz"],
    "noida": ["jobDescPagesNoida.xml"],
    "delhi": ["jobDescPagesDelhi.xml"],
    "new delhi": ["jobDescPagesDelhi.xml"],
    "gurgaon": ["jobDescPagesDelhi.xml"],
    "gurugram": ["jobDescPagesDelhi.xml"],
    "bangalore": ["jobDescPagesBangalore.xml"],
    "bengaluru": ["jobDescPagesBangalore.xml"],
    "chennai": ["jobDescPagesChennai.xml"],
    "hyderabad": ["jobDescPagesHyderabad.xml"],
}

KNOWN_CITIES = {
    "pune",
    "mumbai",
    "delhi",
    "noida",
    "gurgaon",
    "gurugram",
    "bangalore",
    "bengaluru",
    "chennai",
    "hyderabad",
    "kolkata",
    "ahmedabad",
    "nashik",
    "nagpur",
    "indore",
    "jaipur",
    "chandigarh",
    "remote",
    "india",
}

# Cap per run so we don't ingest entire city catalogs
DEFAULT_MAX_JOBS = 250


class Provider:
    name = "naukri"

    def search(
        self,
        profile: ResumeProfile | list[ResumeProfile],
        config: AppConfig,
        portal_cfg: PortalConfig,
        *,
        portal_key: str,
    ) -> list[JobPosting]:
        # Prefer public city JD sitemaps — jobapi/v3/search needs browser nkparam (406 otherwise)
        jobs = self._search_sitemaps(config, portal_cfg, portal_key)
        if jobs:
            return jobs
        logger.warning("Naukri sitemap returned 0; falling back to HTML search pages")
        return self._search_html(config, portal_cfg, portal_key)

    def _search_sitemaps(
        self, config: AppConfig, portal_cfg: PortalConfig, portal_key: str
    ) -> list[JobPosting]:
        skills = [s.lower() for s in search_skills(config)]
        max_jobs = int(portal_cfg.extra.get("max_jobs", DEFAULT_MAX_JOBS))
        cutoff = date.today() - timedelta(days=max(1, config.recency_days or 14))

        sitemap_urls = self._sitemap_urls_for(config)
        if not sitemap_urls:
            return []

        seen: set[str] = set()
        out: list[JobPosting] = []
        with httpx.Client(
            follow_redirects=True, headers=DEFAULT_HEADERS, timeout=90.0
        ) as client:
            for sm_url in sitemap_urls:
                try:
                    entries = self._load_sitemap(client, sm_url)
                except Exception as exc:
                    logger.warning("Naukri sitemap failed %s: %s", sm_url, exc)
                    continue
                for loc, lastmod in entries:
                    if loc in seen:
                        continue
                    if not self._slug_matches_skills(loc, skills):
                        continue
                    posted = self._parse_lastmod(lastmod)
                    if posted and posted < cutoff:
                        continue
                    job = self._job_from_url(loc, posted, portal_key)
                    if not job:
                        continue
                    seen.add(loc)
                    out.append(job)
                    if len(out) >= max_jobs:
                        return out
        logger.info("Naukri sitemap matched %s jobs (skills=%s)", len(out), skills)
        return out

    def _sitemap_urls_for(self, config: AppConfig) -> list[str]:
        files: list[str] = []
        locs = [x.lower() for x in (config.locations or [])]
        for loc in locs:
            # "Remote India" → skip city map; pull other-cities later
            for city, names in CITY_SITEMAPS.items():
                if city in loc or loc in city:
                    files.extend(names)
        # Always include Pune/Bangalore defaults if nothing matched but India/remote
        if not files:
            if any("india" in x or "remote" in x for x in locs) or not locs:
                files = [
                    "jobDescPagesPune.xml",
                    "jobDescPagesBangalore.xml",
                    "jobDescPagesHyderabad.xml",
                    "jobDescPagesOtherCities-1.xml.gz",
                ]
            else:
                files = ["jobDescPagesPune.xml"]
        # Deduplicate preserving order
        seen: set[str] = set()
        urls: list[str] = []
        for name in files:
            if name in seen:
                continue
            seen.add(name)
            urls.append(f"https://www.naukri.com/sitemap/{name}")
        return urls

    def _load_sitemap(
        self, client: httpx.Client, url: str
    ) -> list[tuple[str, str | None]]:
        resp = client.get(url)
        resp.raise_for_status()
        content = resp.content
        if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
            import gzip

            content = gzip.decompress(content)
        root = ET.fromstring(content)
        out: list[tuple[str, str | None]] = []
        for node in root.findall("s:url", NS):
            loc_el = node.find("s:loc", NS)
            if loc_el is None or not loc_el.text:
                continue
            lm_el = node.find("s:lastmod", NS)
            out.append((loc_el.text.strip(), lm_el.text if lm_el is not None else None))
        return out

    def _slug_matches_skills(self, url: str, skills: list[str]) -> bool:
        if not skills:
            return True
        slug = urlparse(url).path.rsplit("/", 1)[-1].lower()
        # hyphen-bounded so "python" matches -python- / python- at start of title
        for skill in skills:
            s = skill.lower().strip()
            if not s:
                continue
            token = s.replace(" ", "-")
            if re.search(rf"(^|-){re.escape(token)}(-|$)", slug):
                return True
            # also allow plain word in slug for multi-word already hyphenated
            if re.search(rf"(^|-){re.escape(s)}(-|$)", slug):
                return True
        return False

    def _parse_lastmod(self, text: str | None) -> date | None:
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None

    def _job_from_url(
        self, url: str, posted: date | None, portal_key: str
    ) -> JobPosting | None:
        path = urlparse(url).path.rsplit("/", 1)[-1]
        m = re.match(r"job-listings-(.+)-(\d+)$", path, re.I)
        if not m:
            return None
        slug, _jid = m.group(1), m.group(2)
        slug = re.sub(r"-\d+-to-\d+-years?$", "", slug, flags=re.I)
        parts = slug.split("-")
        locs: list[str] = []
        while parts and parts[-1].lower() in KNOWN_CITIES:
            locs.append(parts.pop())
        location = ", ".join(reversed([x.title() for x in locs]))
        # Remaining slug ≈ title + company; keep as title for ranking
        title = " ".join(parts).strip().title() if parts else path
        if not title:
            return None
        return make_job(
            portal=portal_key,
            url=url,
            title=title,
            company="",
            location=location,
            posted_at=posted,
            raw_text=title,
        )

    def _search_html(
        self, config: AppConfig, portal_cfg: PortalConfig, portal_key: str
    ) -> list[JobPosting]:
        from find_my_job.portals.http_utils import build_query, encode

        skills = search_skills(config)
        query, _ = build_query(skills, config.locations)
        urls = list(portal_cfg.search_urls)
        if not urls:
            for loc in config.locations or ["India"]:
                slug_q = encode(query).replace("+", "-")
                slug_l = encode(loc).replace("+", "-")
                urls.append(f"https://www.naukri.com/{slug_q}-jobs-in-{slug_l}")

        jobs: list[JobPosting] = []
        for url in urls:
            html, status = fetch_html(url)
            if looks_blocked(html, status):
                logger.warning("Naukri blocked: %s (%s)", url, status)
                continue
            jobs.extend(self._parse(html, url, portal_key))
        return jobs

    def _parse(self, html: str, page_url: str, portal_key: str) -> list[JobPosting]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        out: list[JobPosting] = []
        cards = soup.select(
            "article.jobTuple, div.srp-jobtuple-wrapper, div.cust-job-tuple, article"
        )
        for el in cards:
            a = el.select_one("a.title, a[class*='title'], h2 a, a[href*='job-listings']")
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            href = a.get("href") or ""
            if not title or not href:
                continue
            company_el = el.select_one(
                "a.comp-name, a[class*='company'], span.comp-name, .companyInfo a"
            )
            loc_el = el.select_one(".locWdth, span.loc, .location, [class*='location']")
            date_el = el.select_one(".job-post-day, span.job-post-day, [class*='posted']")
            desc_el = el.select_one(".job-desc, .jobDescription, [class*='description']")
            out.append(
                make_job(
                    portal=portal_key,
                    url=absolute_url(page_url, href),
                    title=title,
                    company=company_el.get_text(" ", strip=True) if company_el else "",
                    location=loc_el.get_text(" ", strip=True) if loc_el else "",
                    posted_at=parse_relative_date(
                        date_el.get_text(" ", strip=True) if date_el else ""
                    ),
                    raw_text=desc_el.get_text(" ", strip=True) if desc_el else title,
                )
            )
        if not out:
            logger.info(
                "Naukri parse found 0 cards for %s (selectors may need update)", page_url
            )
        return out
