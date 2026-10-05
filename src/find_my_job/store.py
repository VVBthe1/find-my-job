from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

from find_my_job.models import CoverLetter, JobPosting, JobScore, JobType


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    portal TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    posted_at TEXT,
    raw_text TEXT,
    listed_pay REAL,
    job_type TEXT,
    content_hash TEXT,
    payload_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
    job_id TEXT PRIMARY KEY,
    relevance REAL,
    rag_score REAL,
    final_score REAL,
    reasons_json TEXT,
    pay_lpa_estimate REAL,
    pay_confidence REAL,
    resume_label TEXT,
    dropped INTEGER,
    drop_reason TEXT,
    scored_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS portal_fetch_log (
    portal TEXT NOT NULL,
    fetch_date TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    PRIMARY KEY (portal, fetch_date)
);

CREATE TABLE IF NOT EXISTS seen_letters (
    job_id TEXT PRIMARY KEY,
    written_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    job_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_cache (
    portal TEXT NOT NULL,
    fetch_date TEXT NOT NULL,
    jobs_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (portal, fetch_date)
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Lightweight migrations for older DBs
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "content_hash" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN content_hash TEXT")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_jobs(self, jobs: list[JobPosting], *, content_hashes: dict[str, str] | None = None) -> None:
        from find_my_job.cache_keys import job_content_hash

        now = _utc_now()
        content_hashes = content_hashes or {}
        with self._conn() as conn:
            for job in jobs:
                ch = content_hashes.get(job.id) or job_content_hash(job)
                existing = conn.execute(
                    "SELECT id FROM jobs WHERE id = ?", (job.id,)
                ).fetchone()
                payload = job.model_dump(mode="json")
                posted = None
                if job.posted_at is not None:
                    posted = (
                        job.posted_at.isoformat()
                        if hasattr(job.posted_at, "isoformat")
                        else str(job.posted_at)
                    )
                if existing:
                    conn.execute(
                        """
                        UPDATE jobs SET url=?, title=?, company=?, location=?,
                        posted_at=?, raw_text=?, listed_pay=?, job_type=?,
                        content_hash=?, payload_json=?, last_seen_at=?
                        WHERE id=?
                        """,
                        (
                            job.url,
                            job.title,
                            job.company,
                            job.location,
                            posted,
                            job.raw_text,
                            job.listed_pay,
                            job.job_type.value,
                            ch,
                            json.dumps(payload),
                            now,
                            job.id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO jobs (
                            id, portal, url, title, company, location, posted_at,
                            raw_text, listed_pay, job_type, content_hash, payload_json,
                            first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job.id,
                            job.portal,
                            job.url,
                            job.title,
                            job.company,
                            job.location,
                            posted,
                            job.raw_text,
                            job.listed_pay,
                            job.job_type.value,
                            ch,
                            json.dumps(payload),
                            now,
                            now,
                        ),
                    )

    def save_score(self, score: JobScore) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO scores (
                    job_id, relevance, rag_score, final_score, reasons_json,
                    pay_lpa_estimate, pay_confidence, resume_label, dropped,
                    drop_reason, scored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    relevance=excluded.relevance,
                    rag_score=excluded.rag_score,
                    final_score=excluded.final_score,
                    reasons_json=excluded.reasons_json,
                    pay_lpa_estimate=excluded.pay_lpa_estimate,
                    pay_confidence=excluded.pay_confidence,
                    resume_label=excluded.resume_label,
                    dropped=excluded.dropped,
                    drop_reason=excluded.drop_reason,
                    scored_at=excluded.scored_at
                """,
                (
                    score.job_id,
                    score.relevance,
                    score.rag_score,
                    score.final_score,
                    json.dumps(score.reasons),
                    score.pay_lpa_estimate,
                    score.pay_confidence,
                    score.resume_label,
                    1 if score.dropped else 0,
                    score.drop_reason,
                    _utc_now(),
                ),
            )

    def get_llm_cache(self, cache_key: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload_json FROM llm_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"])

    def put_llm_cache(self, cache_key: str, kind: str, job_id: str, payload: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO llm_cache (cache_key, kind, job_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at
                """,
                (cache_key, kind, job_id, json.dumps(payload), _utc_now()),
            )

    def get_cached_score(self, cache_key: str) -> JobScore | None:
        data = self.get_llm_cache(cache_key)
        if not data:
            return None
        try:
            return JobScore.model_validate(data)
        except Exception:
            return None

    def get_cached_letter(self, cache_key: str) -> CoverLetter | None:
        data = self.get_llm_cache(cache_key)
        if not data:
            return None
        try:
            return CoverLetter.model_validate(data)
        except Exception:
            return None

    def save_search_cache(self, portal: str, jobs: list[JobPosting]) -> None:
        payload = [j.model_dump(mode="json") for j in jobs]
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO search_cache (portal, fetch_date, jobs_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(portal, fetch_date) DO UPDATE SET
                    jobs_json=excluded.jobs_json,
                    created_at=excluded.created_at
                """,
                (portal, date.today().isoformat(), json.dumps(payload), _utc_now()),
            )

    def load_search_cache(self, portal: str, on: date | None = None) -> list[JobPosting] | None:
        d = (on or date.today()).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT jobs_json FROM search_cache WHERE portal = ? AND fetch_date = ?",
                (portal, d),
            ).fetchone()
        if not row:
            return None
        jobs: list[JobPosting] = []
        for item in json.loads(row["jobs_json"]):
            if isinstance(item.get("job_type"), str):
                item["job_type"] = JobType(item["job_type"])
            jobs.append(JobPosting(**item))
        return jobs

    def fetches_today(self, portal: str, on: date | None = None) -> int:
        d = (on or date.today()).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM portal_fetch_log
                WHERE portal = ? AND fetch_date = ? AND status IN ('ok', 'blocked')
                """,
                (portal, d),
            ).fetchone()
            return int(row["c"] if row else 0)

    def log_fetch(self, portal: str, status: str, detail: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO portal_fetch_log (portal, fetch_date, status, detail)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(portal, fetch_date) DO UPDATE SET
                    status=excluded.status,
                    detail=excluded.detail
                """,
                (portal, date.today().isoformat(), status, detail),
            )

    def has_letter(self, job_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_letters WHERE job_id = ?", (job_id,)
            ).fetchone()
            return row is not None

    def mark_letter(self, job_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO seen_letters (job_id, written_at) VALUES (?, ?)
                ON CONFLICT(job_id) DO UPDATE SET written_at=excluded.written_at
                """,
                (job_id, _utc_now()),
            )

    def load_jobs_for_portal(self, portal: str) -> list[JobPosting]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM jobs WHERE portal = ?", (portal,)
            ).fetchall()
        jobs: list[JobPosting] = []
        for row in rows:
            data = json.loads(row["payload_json"])
            if isinstance(data.get("job_type"), str):
                data["job_type"] = JobType(data["job_type"])
            jobs.append(JobPosting(**data))
        return jobs
