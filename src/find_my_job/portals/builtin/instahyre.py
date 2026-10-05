from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urljoin

from find_my_job.models import AppConfig, JobPosting, PortalConfig, ResumeProfile
from find_my_job.portals.http_utils import fetch_json, make_job
from find_my_job.skills_logic import search_skills

logger = logging.getLogger("find_my_job.portals.instahyre")

API = "https://www.instahyre.com/api/v1/job_search/"
MAX_PAGES = 5


class Provider:
    name = "instahyre"

    def search(
        self,
        profile: ResumeProfile | list[ResumeProfile],
        config: AppConfig,
        portal_cfg: PortalConfig,
        *,
        portal_key: str,
    ) -> list[JobPosting]:
        skills = search_skills(config)
        if not skills:
            skills = ["software engineer"]

        jobs: list[JobPosting] = []
        seen: set[str] = set()
        # OR across must_have skills (API accepts one skills= filter per call)
        for skill in skills[:6]:
            for job in self._search_skill(skill, config, portal_key):
                if job.id in seen:
                    continue
                seen.add(job.id)
                jobs.append(job)
        return jobs

    def _search_skill(
        self, skill: str, config: AppConfig, portal_key: str
    ) -> list[JobPosting]:
        out: list[JobPosting] = []
        offset = 0
        for _ in range(MAX_PAGES):
            data, status = fetch_json(
                API,
                params={"skills": skill, "limit": 35, "offset": offset},
                headers={"Referer": "https://www.instahyre.com/"},
            )
            if status != 200 or not isinstance(data, dict):
                logger.warning("Instahyre API %s for skill=%s", status, skill)
                break
            objects = data.get("objects") or []
            if not objects:
                break
            for obj in objects:
                job = self._from_obj(obj, portal_key, config)
                if job:
                    out.append(job)
            meta = data.get("meta") or {}
            nxt = meta.get("next")
            if not nxt:
                break
            # next is a path like /api/v1/job_search?skills=...&offset=35
            if "offset=" in str(nxt):
                try:
                    offset = int(str(nxt).rsplit("offset=", 1)[-1].split("&", 1)[0])
                except ValueError:
                    offset += len(objects)
            else:
                offset += len(objects)
        return out

    def _from_obj(
        self, obj: dict, portal_key: str, config: AppConfig
    ) -> JobPosting | None:
        title = (obj.get("title") or "").strip()
        url = (obj.get("public_url") or "").strip()
        if not title or not url:
            return None
        if url.startswith("/"):
            url = urljoin("https://www.instahyre.com/", url)

        loc = (obj.get("locations") or "").strip()
        if config.locations and loc:
            loc_l = loc.lower()
            wanted = [x.lower() for x in config.locations]
            # Keep if any configured location substring matches, or WFH/remote
            remoteish = any(w in loc_l for w in ("remote", "work from home", "wfh"))
            if not remoteish and not any(w in loc_l for w in wanted):
                # Soft filter: still keep if "India" only wanted; otherwise drop mismatch
                if not any("india" == w or "remote" in w for w in wanted):
                    return None

        emp = obj.get("employer") or {}
        company = (emp.get("company_name") or "").strip()
        keywords = obj.get("keywords") or []
        kw_text = ", ".join(keywords) if isinstance(keywords, list) else str(keywords)
        posted = None
        reviewed = obj.get("reviewed_at")
        if reviewed:
            try:
                posted = datetime.fromisoformat(str(reviewed).replace("Z", "+00:00")).date()
            except ValueError:
                posted = None

        return make_job(
            portal=portal_key,
            url=url,
            title=title,
            company=company,
            location=loc,
            posted_at=posted,
            raw_text=f"{title}. Skills: {kw_text}".strip(),
        )
