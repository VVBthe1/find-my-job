from __future__ import annotations

import json
import logging
from typing import Any

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
    select_text,
)

logger = logging.getLogger("find_my_job.portals.http_list")


class Provider:
    name = "http_list"

    def search(
        self,
        profile: ResumeProfile | list[ResumeProfile],
        config: AppConfig,
        portal_cfg: PortalConfig,
        *,
        portal_key: str,
    ) -> list[JobPosting]:
        skills = search_skills(config)
        query, location = build_query(skills, config.locations)

        template = portal_cfg.search_url or (portal_cfg.extra or {}).get("search_url")
        if not template:
            raise ValueError(f"Portal {portal_key}: http_list requires search_url")

        urls = portal_cfg.search_urls or []
        if not urls:
            for loc in config.locations or [location]:
                urls.append(
                    template.format(
                        query=encode(query),
                        location=encode(loc),
                        q=encode(query),
                        l=encode(loc),
                    )
                )

        item_selector = portal_cfg.item_selector or portal_cfg.extra.get("item_selector")
        fields = portal_cfg.fields or portal_cfg.extra.get("fields") or {}
        if not item_selector or not fields.get("title") or not fields.get("url"):
            raise ValueError(
                f"Portal {portal_key}: http_list needs item_selector and fields.title/url"
            )

        jobs: list[JobPosting] = []
        for url in urls:
            html, status = fetch_html(url)
            if looks_blocked(html, status):
                logger.warning("%s blocked fetching %s (%s)", portal_key, url, status)
                continue
            if (portal_cfg.format or "html").lower() == "json":
                jobs.extend(self._from_json(html, portal_key, url, fields))
            else:
                jobs.extend(
                    self._from_html(html, portal_key, url, item_selector, fields)
                )
        return jobs

    def _from_html(
        self,
        html: str,
        portal_key: str,
        page_url: str,
        item_selector: str,
        fields: dict[str, str],
    ) -> list[JobPosting]:
        soup = BeautifulSoup(html, "lxml")
        out: list[JobPosting] = []
        for el in soup.select(item_selector):
            title = select_text(el, fields.get("title", ""))
            href = select_text(el, fields.get("url", ""))
            if not title or not href:
                continue
            job_url = absolute_url(page_url, href)
            company = select_text(el, fields.get("company", "")) if fields.get("company") else ""
            location = select_text(el, fields.get("location", "")) if fields.get("location") else ""
            posted_raw = select_text(el, fields.get("posted_at", "")) if fields.get("posted_at") else ""
            desc = select_text(el, fields.get("description", "")) if fields.get("description") else ""
            out.append(
                make_job(
                    portal=portal_key,
                    url=job_url,
                    title=title,
                    company=company,
                    location=location,
                    posted_at=parse_relative_date(posted_raw),
                    raw_text=desc or f"{title} at {company} — {location}",
                )
            )
        return out

    def _from_json(
        self,
        body: str,
        portal_key: str,
        page_url: str,
        fields: dict[str, str],
    ) -> list[JobPosting]:
        try:
            data: Any = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from %s", page_url)
            return []
        items = data
        # optional path like "results.jobs"
        path = fields.get("_items") or fields.get("items_path")
        if path:
            cur = data
            for part in path.split("."):
                cur = cur[part] if isinstance(cur, dict) else []
            items = cur
        if not isinstance(items, list):
            return []
        out: list[JobPosting] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get(fields.get("title", "title"), "") or "")
            href = str(item.get(fields.get("url", "url"), "") or "")
            if not title or not href:
                continue
            out.append(
                make_job(
                    portal=portal_key,
                    url=absolute_url(page_url, href),
                    title=title,
                    company=str(item.get(fields.get("company", "company"), "") or ""),
                    location=str(item.get(fields.get("location", "location"), "") or ""),
                    raw_text=str(item.get(fields.get("description", "description"), "") or title),
                )
            )
        return out
