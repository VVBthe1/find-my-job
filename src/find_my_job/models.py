from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class JobType(str, Enum):
    FULL_TIME = "full_time"
    INTERN = "intern"
    CONTRACT = "contract"
    FREELANCE = "freelance"
    PART_TIME = "part_time"


class MustHaveSkill(BaseModel):
    """One must-have skill; portal search is OR across the list."""

    skill: str
    min_exp: float | None = None  # JD should ask for ~this many years (when known)


class ResumeRef(BaseModel):
    """Resume file reference. Prefer `file` under resumes_dir; `path` may be absolute."""

    path: str | None = None
    file: str | None = None
    label: str = "default"
    # Ranking boost for this resume; falls back to global skills.include + parsed skills
    include: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _need_path_or_file(self) -> ResumeRef:
        if not self.path and not self.file:
            raise ValueError("ResumeRef needs path or file")
        return self


class SkillsConfig(BaseModel):
    """Skill buckets for search, ranking, and hard filters.

    - must_have: OR for portals + JD should mention at least one (optional min_exp each)
    - include: global ranking boost (also merged with per-resume include)
    - exclude: reject if mandatory, unless JD ORs with a must_have/include skill
    """

    must_have: list[MustHaveSkill] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

    @field_validator("must_have", mode="before")
    @classmethod
    def _normalize_must_have(cls, value: Any) -> list:
        if value is None:
            return []
        out: list = []
        for item in value:
            if isinstance(item, str):
                out.append({"skill": item})
            elif isinstance(item, dict):
                # allow {Python: 6} shorthand? no — stick to skill/min_exp
                if "skill" in item:
                    out.append(item)
                elif "name" in item:
                    out.append({"skill": item["name"], "min_exp": item.get("min_exp")})
                else:
                    raise ValueError(f"Invalid must_have entry: {item!r}")
            else:
                out.append(item)
        return out


class JobTypesConfig(BaseModel):
    full_time: bool = True
    intern: bool = False
    contract: bool = False
    freelance: bool = False
    part_time: bool = False

    def allowed(self) -> set[JobType]:
        mapping = {
            JobType.FULL_TIME: self.full_time,
            JobType.INTERN: self.intern,
            JobType.CONTRACT: self.contract,
            JobType.FREELANCE: self.freelance,
            JobType.PART_TIME: self.part_time,
        }
        return {jt for jt, ok in mapping.items() if ok}


class PayConfig(BaseModel):
    min_lpa: float | None = None
    max_lpa: float | None = None
    # When JD has no pay: estimate from text, keep, or drop
    unknown: str = "estimate"  # estimate | keep | drop
    estimate_confidence_min: float = 0.55


class ExperienceConfig(BaseModel):
    """Drop roles that are clearly junior vs your seniority."""

    # Candidate floor (years). If null, use max(resume.seniority_years).
    min_years: float | None = None
    # Drop when JD max years is strictly below this (overrides soft compare).
    # If null, use min_years (or resume-derived).
    drop_if_jd_max_below: float | None = None
    # If JD years unknown: keep | drop
    unknown: str = "keep"  # keep | drop


class RagConfig(BaseModel):
    store: str = "chroma"
    persist_dir: str = "data/chroma"
    collection: str = "resumes"
    # local = free in-process embeds (default). cloud = Gemini/OpenAI — rate-limits hard.
    embeddings: str = "local"  # local | cloud
    embedding_model: str = "gemini-embedding-001"
    rebuild_on_resume_change: bool = True
    min_similarity: float = 0.20
    top_k: int = 5
    # After hard filter: keep only top N by resume similarity before LLM rank/letters
    gate_top_n: int = 80


class LlmConfig(BaseModel):
    # auto = use cloud API if key present, else offline
    # offline = never call paid/cloud APIs
    # cloud / google / openai = require the matching key
    provider: str = "google"  # google | gemini | openai | openai_compatible
    mode: str = "auto"  # auto | offline | cloud | google | openai
    model: str = "gemini-3.5-flash-lite"
    base_url: str | None = None


class PortalConfig(BaseModel):
    enabled: bool = True
    provider: str
    max_fetches_per_day: int = 1
    search_urls: list[str] = Field(default_factory=list)
    search_url: str | None = None
    item_selector: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    format: str = "html"  # html | json
    extra: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class CacheConfig(BaseModel):
    """Reuse portal listings and LLM outputs to avoid repeat API spend."""

    reuse_search: bool = True  # honor max_fetches_per_day + SQLite job cache
    reuse_llm: bool = True  # skip rank/letter LLM if content hash matches
    force_refresh: bool = False  # ignore caches for this run


class AppConfig(BaseModel):
    locations: list[str] = Field(default_factory=list)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    job_types: JobTypesConfig = Field(default_factory=JobTypesConfig)
    pay: PayConfig = Field(default_factory=PayConfig)
    experience: ExperienceConfig = Field(default_factory=ExperienceConfig)
    availability: str = "30 days notice"
    recency_days: int = 14
    score_threshold: float = 0.72
    unknown_recency: str = "keep_with_penalty"  # drop | keep_with_penalty
    rag: RagConfig = Field(default_factory=RagConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    resumes_dir: str = "resumes"
    resumes: list[ResumeRef] = Field(default_factory=list)
    portals: dict[str, PortalConfig] = Field(default_factory=dict)
    data_dir: str = "data"
    out_dir: str = "out"


class ResumeProfile(BaseModel):
    label: str
    path: str
    text: str
    skills: list[str] = Field(default_factory=list)
    seniority_years: float | None = None
    target_titles: list[str] = Field(default_factory=list)
    file_hash: str = ""
    include: list[str] = Field(default_factory=list)


class JobPosting(BaseModel):
    id: str
    portal: str
    url: str
    title: str
    company: str = ""
    location: str = ""
    posted_at: date | datetime | None = None
    raw_text: str = ""
    listed_pay: float | None = None  # LPA if known
    job_type: JobType = JobType.FULL_TIME
    # Parsed from title/JD/URL when available
    exp_min_years: float | None = None
    exp_max_years: float | None = None


class RagEvidence(BaseModel):
    text: str
    score: float
    resume_label: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobScore(BaseModel):
    job_id: str
    relevance: float
    rag_score: float
    final_score: float
    reasons: list[str] = Field(default_factory=list)
    pay_lpa_estimate: float | None = None
    pay_confidence: float | None = None
    resume_label: str = "default"
    evidence: list[RagEvidence] = Field(default_factory=list)
    dropped: bool = False
    drop_reason: str | None = None


class CoverLetter(BaseModel):
    job_id: str
    resume_label: str
    text: str
