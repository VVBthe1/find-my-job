from __future__ import annotations

import json
from pathlib import Path

from find_my_job.models import CoverLetter, JobPosting, JobScore
from find_my_job.util import slugify


class ArtifactWriter:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.jobs_dir = run_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._ranked_rows: list[dict] = []

    def write_job(
        self,
        job: JobPosting,
        score: JobScore,
        letter: CoverLetter | None = None,
    ) -> Path:
        folder = self.jobs_dir / f"{job.portal}__{slugify(job.company + '-' + job.title)}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "jd.md").write_text(
            f"# {job.title}\n\n"
            f"- Company: {job.company}\n"
            f"- Location: {job.location}\n"
            f"- Portal: {job.portal}\n"
            f"- URL: {job.url}\n"
            f"- Type: {job.job_type.value}\n"
            f"- Listed pay (LPA): {job.listed_pay}\n\n"
            f"## Description\n\n{job.raw_text}\n",
            encoding="utf-8",
        )
        (folder / "score.json").write_text(
            score.model_dump_json(indent=2),
            encoding="utf-8",
        )
        if letter:
            (folder / "cover_letter.md").write_text(letter.text + "\n", encoding="utf-8")

        pay = score.pay_lpa_estimate if score.pay_lpa_estimate is not None else job.listed_pay
        self._ranked_rows.append(
            {
                "final_score": score.final_score,
                "rag": score.rag_score,
                "relevance": score.relevance,
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "pay": pay,
                "url": job.url,
                "portal": job.portal,
                "dropped": score.dropped,
            }
        )
        self.write_ranked()
        return folder

    def write_ranked(self) -> None:
        rows = sorted(self._ranked_rows, key=lambda r: r["final_score"], reverse=True)
        lines = [
            "# Ranked jobs\n",
            "| Score | RAG | Title | Company | Location | Pay (LPA) | Portal | URL |",
            "|------:|----:|-------|---------|----------|----------|--------|-----|",
        ]
        kept = 0
        for r in rows:
            if r.get("dropped"):
                continue
            kept += 1
            lines.append(
                f"| {r['final_score']:.2f} | {r['rag']:.2f} | {r['title']} | {r['company']} | "
                f"{r['location']} | {r['pay']} | {r['portal']} | {r['url']} |"
            )
        if kept == 0:
            lines.append("")
            lines.append(
                f"_No kept jobs ({len(rows)} scored, all dropped). "
                "See per-job `score.json` under `jobs/` for drop reasons._"
            )
        (self.run_dir / "ranked.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (self.run_dir / "ranked.json").write_text(
            json.dumps(rows, indent=2), encoding="utf-8"
        )
