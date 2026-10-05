from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from find_my_job.agents.filter import hard_filter
from find_my_job.models import (
    AppConfig,
    ExperienceConfig,
    JobPosting,
    JobType,
    JobTypesConfig,
    MustHaveSkill,
    PayConfig,
    SkillsConfig,
)
from find_my_job.portals.builtin.http_list import Provider as HttpListProvider
from find_my_job.portals.discovery import resolve_provider
from find_my_job.util import (
    dedupe_jobs,
    infer_job_type,
    parse_experience_years,
    parse_listed_pay_lpa,
)


def test_infer_job_type():
    assert infer_job_type("Software Engineering Intern") == JobType.INTERN
    assert infer_job_type("Backend Engineer", "6 month contract") == JobType.CONTRACT
    assert infer_job_type("Staff Engineer") == JobType.FULL_TIME


def test_parse_pay():
    assert parse_listed_pay_lpa("30-40 LPA") == 35.0
    assert parse_listed_pay_lpa("no salary") is None


def test_parse_experience_years():
    lo, hi = parse_experience_years("python-developer-4-to-9-years-123")
    assert lo == 4 and hi == 9
    lo, hi = parse_experience_years("Looking for 8+ years of experience")
    assert lo == 8 and hi is None


def test_hard_filter_excludes_intern_and_skills():
    config = AppConfig(
        locations=["Bengaluru"],
        skills=SkillsConfig(
            must_have=[MustHaveSkill(skill="Python")],
            include=["Django", "AWS"],
            exclude=["SAP"],
        ),
        job_types=JobTypesConfig(full_time=True, intern=False, contract=False),
        pay=PayConfig(min_lpa=30),
        recency_days=14,
    )
    jobs = [
        JobPosting(
            id="1",
            portal="t",
            url="https://x/1",
            title="Intern Python",
            location="Bengaluru",
            job_type=JobType.INTERN,
            posted_at=date.today(),
        ),
        JobPosting(
            id="2",
            portal="t",
            url="https://x/2",
            title="Python SAP Consultant",
            location="Bengaluru",
            raw_text="Must have: SAP ABAP. Python is nice to have.",
            job_type=JobType.FULL_TIME,
            posted_at=date.today(),
        ),
        JobPosting(
            id="3",
            portal="t",
            url="https://x/3",
            title="Senior Python Engineer",
            location="Bengaluru",
            raw_text="Django AWS 40 LPA",
            job_type=JobType.FULL_TIME,
            posted_at=date.today(),
            listed_pay=40,
        ),
        JobPosting(
            id="4",
            portal="t",
            url="https://x/4",
            title="Old role",
            location="Bengaluru",
            raw_text="Python",
            job_type=JobType.FULL_TIME,
            posted_at=date.today() - timedelta(days=60),
        ),
        JobPosting(
            id="5",
            portal="t",
            url="https://x/5",
            title="Java Engineer",
            location="Bengaluru",
            raw_text="Must have Java Spring",
            job_type=JobType.FULL_TIME,
            posted_at=date.today(),
            listed_pay=50,
        ),
    ]
    kept, dropped = hard_filter(jobs, config)
    assert [j.id for j in kept] == ["3"]
    reasons = {j.id: r for j, r in dropped}
    assert "job_type" in reasons["1"]
    assert "excluded_mandatory" in reasons["2"]
    assert reasons["4"] == "stale"
    assert reasons["5"] == "missing_must_have"


def test_hard_filter_experience_and_pay():
    config = AppConfig(
        locations=["Pune"],
        skills=SkillsConfig(must_have=[MustHaveSkill(skill="Python", min_exp=6)]),
        experience=ExperienceConfig(min_years=10),
        pay=PayConfig(min_lpa=42, unknown="estimate"),
        job_types=JobTypesConfig(full_time=True),
        recency_days=14,
    )
    junior = JobPosting(
        id="j",
        portal="t",
        url="https://x/j",
        title="Python Developer 2-to-4-years",
        location="Pune",
        raw_text="Python",
        job_type=JobType.FULL_TIME,
        posted_at=date.today(),
    )
    low_pay = JobPosting(
        id="p",
        portal="t",
        url="https://x/p",
        title="Senior Python Engineer 10-to-15-years",
        location="Pune",
        raw_text="Python Django. CTC 20-25 LPA",
        job_type=JobType.FULL_TIME,
        posted_at=date.today(),
    )
    ok = JobPosting(
        id="ok",
        portal="t",
        url="https://x/ok",
        title="Staff Python Engineer 10-to-15-years",
        location="Pune",
        raw_text="Python Django. 45 LPA",
        job_type=JobType.FULL_TIME,
        posted_at=date.today(),
    )
    kept, dropped = hard_filter([junior, low_pay, ok], config)
    reasons = {j.id: r for j, r in dropped}
    assert "experience_junior" in reasons["j"]
    assert reasons["p"] == "pay_below_min"
    assert [j.id for j in kept] == ["ok"]


def test_exclude_allows_or_with_must_have():
    from find_my_job.skills_logic import excluded_mandatory_skill

    config = AppConfig(
        skills=SkillsConfig(
            must_have=[MustHaveSkill(skill="Python")],
            include=["Django"],
            exclude=["PHP"],
        )
    )
    or_job = JobPosting(
        id="or",
        portal="t",
        url="https://x/or",
        title="Backend Engineer",
        raw_text="Must have: Python or PHP experience building APIs.",
    )
    php_only = JobPosting(
        id="php",
        portal="t",
        url="https://x/php",
        title="PHP Developer",
        raw_text="Must have: PHP Laravel.",
    )
    assert excluded_mandatory_skill(or_job, config) is None
    assert excluded_mandatory_skill(php_only, config) == "PHP"


def test_dedupe():
    a = JobPosting(
        id="a", portal="p", url="https://x/a", title="Senior Engineer", company="Acme"
    )
    b = JobPosting(
        id="b", portal="p", url="https://x/b", title="Senior Engineer", company="Acme"
    )
    assert len(dedupe_jobs([a, b])) == 1


def test_must_have_yaml_strings_normalize():
    cfg = SkillsConfig.model_validate({"must_have": ["Python", {"skill": "PHP", "min_exp": 6}]})
    assert cfg.must_have[0].skill == "Python"
    assert cfg.must_have[1].min_exp == 6


def test_resolve_builtin_providers():
    for name in [
        "naukri",
        "indeed_in",
        "linkedin",
        "instahyre",
        "wellfound",
        "hirist",
        "http_list",
    ]:
        p = resolve_provider(name)
        assert p.name == name or hasattr(p, "search")


def test_http_list_parse_fixture():
    html = """
    <html><body>
      <article class="job">
        <h2>Senior Backend Engineer</h2>
        <a href="/jobs/1">link</a>
        <span class="company">Acme</span>
        <span class="loc">Bengaluru</span>
      </article>
      <article class="job">
        <h2>PHP Intern</h2>
        <a href="/jobs/2">link</a>
        <span class="company">Other</span>
        <span class="loc">Pune</span>
      </article>
    </body></html>
    """
    provider = HttpListProvider()
    jobs = provider._from_html(
        html,
        "demo",
        "https://example.com/jobs",
        "article.job",
        {
            "title": "h2",
            "url": "a@href",
            "company": ".company",
            "location": ".loc",
        },
    )
    assert len(jobs) == 2
    assert jobs[0].title == "Senior Backend Engineer"
    assert jobs[0].url.endswith("/jobs/1")
    assert jobs[0].company == "Acme"


def test_naukri_fixture_parse():
    from find_my_job.portals.builtin.naukri import Provider

    fixture = Path(__file__).parent / "fixtures" / "naukri_sample.html"
    html = fixture.read_text(encoding="utf-8")
    jobs = Provider()._parse(html, "https://www.naukri.com/python-jobs", "naukri")
    assert len(jobs) >= 1
    assert "Engineer" in jobs[0].title


def test_hirist_fixture_parse():
    from find_my_job.portals.builtin.hirist import Provider

    fixture = Path(__file__).parent / "fixtures" / "hirist_sample.html"
    html = fixture.read_text(encoding="utf-8")
    jobs = Provider()._parse(html, "https://www.hirist.tech/search", "hirist")
    assert len(jobs) == 1
    assert jobs[0].title == "Senior Python Developer"
    assert "hirist.tech" in jobs[0].url


def test_graph_compiles():
    from find_my_job.graph import build_graph

    g = build_graph()
    assert g is not None


def test_offline_rank_and_letter():
    from find_my_job.agents.cover import generate_cover_letter
    from find_my_job.agents.offline import offline_cover_letter, offline_rank
    from find_my_job.models import RagEvidence, ResumeProfile

    config = AppConfig(
        locations=["Bengaluru"],
        skills=SkillsConfig(include=["Python", "Django", "AWS"]),
        score_threshold=0.2,
        pay=PayConfig(min_lpa=20),
    )
    config.llm.mode = "offline"
    profile = ResumeProfile(
        label="backend",
        path="/tmp/r.md",
        text="Senior Python Django AWS engineer with 15 years experience.",
        skills=["Python", "Django", "AWS"],
        seniority_years=15,
        file_hash="x",
    )
    job = JobPosting(
        id="o1",
        portal="demo",
        url="https://x/1",
        title="Senior Python Engineer",
        company="Acme",
        location="Bengaluru",
        raw_text="Python Django AWS. 40 LPA.",
        listed_pay=40,
        job_type=JobType.FULL_TIME,
        posted_at=date.today(),
    )
    evidence = [
        RagEvidence(text="Built Django APIs on AWS", score=0.8, resume_label="backend")
    ]
    score = offline_rank(job, [profile], config, 0.7, evidence)
    assert not score.dropped
    assert "offline" in score.reasons[0]
    letter = generate_cover_letter(job, score, [profile], config, dry_run=False)
    assert "Acme" in letter.text
    assert "Python" in letter.text
    assert offline_cover_letter(job, score, [profile], config)


def test_llm_and_search_cache(tmp_path):
    from find_my_job.cache_keys import letter_cache_key, rank_cache_key
    from find_my_job.models import CoverLetter, JobScore, ResumeProfile
    from find_my_job.store import JobStore

    store = JobStore(tmp_path / "jobs.sqlite")
    job = JobPosting(
        id="c1",
        portal="demo",
        url="https://x/1",
        title="Eng",
        company="Acme",
        raw_text="Python",
        job_type=JobType.FULL_TIME,
    )
    profiles = [ResumeProfile(label="b", path="/x", text="hi", file_hash="abc")]
    cfg = AppConfig(skills=SkillsConfig(include=["Python"]))
    rk = rank_cache_key(job, profiles, cfg)
    score = JobScore(
        job_id="c1", relevance=0.9, rag_score=0.8, final_score=0.85, resume_label="b"
    )
    store.put_llm_cache(rk, "rank", "c1", score.model_dump(mode="json"))
    assert store.get_cached_score(rk).final_score == 0.85
    lk = letter_cache_key(job, profiles, cfg, "b")
    store.put_llm_cache(
        lk,
        "letter",
        "c1",
        CoverLetter(job_id="c1", resume_label="b", text="Hi").model_dump(mode="json"),
    )
    assert store.get_cached_letter(lk).text == "Hi"
    store.save_search_cache("demo", [job])
    assert len(store.load_search_cache("demo")) == 1


def test_rag_hash_embeddings(tmp_path):
    from find_my_job.agents.rag import ResumeRAG
    from find_my_job.models import RagConfig, ResumeProfile

    config = AppConfig(
        rag=RagConfig(persist_dir=str(tmp_path / "chroma"), collection="resumes_test"),
    )
    rag = ResumeRAG(config)
    profile = ResumeProfile(
        label="backend",
        path="/tmp/r.pdf",
        text="Senior Python Django AWS engineer with 15 years experience building platforms.",
        skills=["Python", "Django", "AWS"],
        file_hash="abc123",
    )
    n = rag.ingest([profile], force=True)
    assert n > 0
    job = JobPosting(
        id="j1",
        portal="t",
        url="https://x/1",
        title="Senior Python Engineer",
        raw_text="Looking for Django and AWS experience",
    )
    score, evidence = rag.score_job(job)
    assert score > 0
    assert evidence


def test_rank_and_cover_dry_run(tmp_path):
    from find_my_job.agents.cover import generate_cover_letter
    from find_my_job.agents.rag import ResumeRAG
    from find_my_job.agents.rank import rank_job
    from find_my_job.artifacts import ArtifactWriter
    from find_my_job.models import RagConfig, ResumeProfile

    config = AppConfig(
        locations=["Bengaluru"],
        skills=SkillsConfig(include=["Python"], exclude=["SAP"]),
        job_types=JobTypesConfig(full_time=True, intern=False),
        pay=PayConfig(min_lpa=20),
        score_threshold=0.1,
        rag=RagConfig(
            persist_dir=str(tmp_path / "chroma2"),
            collection="resumes_test",
            min_similarity=0.01,
        ),
    )
    profile = ResumeProfile(
        label="backend",
        path="/tmp/r.md",
        text="Senior Python Django AWS engineer with 15 years experience.",
        skills=["Python", "Django", "AWS"],
        file_hash="deadbeef",
    )
    rag = ResumeRAG(config)
    rag.ingest([profile], force=True)
    job = JobPosting(
        id="demo:1",
        portal="demo",
        url="https://example.com/jobs/1",
        title="Senior Python Engineer",
        company="Acme",
        location="Bengaluru",
        raw_text="Python Django AWS backend role. 40 LPA.",
        listed_pay=40,
        job_type=JobType.FULL_TIME,
        posted_at=date.today(),
    )
    score = rank_job(job, [profile], config, rag, dry_run=True)
    assert not score.dropped
    letter = generate_cover_letter(job, score, [profile], config, dry_run=True)
    assert "Acme" in letter.text or "Senior Python" in letter.text
    writer = ArtifactWriter(tmp_path / "out")
    folder = writer.write_job(job, score, letter)
    assert (folder / "cover_letter.md").exists()
    assert (tmp_path / "out" / "ranked.md").exists()
