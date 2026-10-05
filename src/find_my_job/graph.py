from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from find_my_job.agents.cover import generate_cover_letter
from find_my_job.agents.filter import hard_filter
from find_my_job.agents.rag import ResumeRAG
from find_my_job.agents.rank import rank_job
from find_my_job.agents.resume import parse_all_resumes
from find_my_job.artifacts import ArtifactWriter
from find_my_job.config import ensure_dirs
from find_my_job.models import (
    AppConfig,
    CoverLetter,
    JobPosting,
    JobScore,
    ResumeProfile,
)
from find_my_job.portals.discovery import get_provider_for_portal
from find_my_job.store import JobStore
from find_my_job.util import dedupe_jobs

logger = logging.getLogger("find_my_job.graph")


def _merge_list(left: list, right: list) -> list:
    return list(left or []) + list(right or [])


class PipelineState(TypedDict, total=False):
    config_data: dict
    dry_run: bool
    run_dir: str
    profiles_data: list[dict]
    jobs_data: Annotated[list[dict], _merge_list]
    filtered_data: list[dict]
    scores_data: list[dict]
    letters_data: list[dict]
    errors: Annotated[list[str], _merge_list]
    portal_failures: int


def _cfg(state: PipelineState) -> AppConfig:
    return AppConfig.model_validate(state["config_data"])


def _profiles(state: PipelineState) -> list[ResumeProfile]:
    return [ResumeProfile.model_validate(p) for p in state.get("profiles_data") or []]


def node_parse_resumes(state: PipelineState) -> dict[str, Any]:
    config = _cfg(state)
    profiles = parse_all_resumes(config)
    logger.info("Parsed %d resumes", len(profiles))
    return {"profiles_data": [p.model_dump(mode="json") for p in profiles]}


def node_ingest_rag(state: PipelineState) -> dict[str, Any]:
    config = _cfg(state)
    profiles = _profiles(state)
    dry_run = state.get("dry_run", False)
    try:
        rag = ResumeRAG(config, force_local=dry_run)
        rag.ingest(profiles)
    except Exception as exc:
        logger.warning("RAG ingest issue: %s", exc)
        return {"errors": [f"rag_ingest:{exc}"]}
    return {}


def node_poll_portals(state: PipelineState) -> dict[str, Any]:
    config = _cfg(state)
    store = JobStore(Path(config.data_dir) / "jobs.sqlite")
    profiles = _profiles(state)
    profile0 = profiles[0]
    all_jobs: list[JobPosting] = []
    errors: list[str] = []
    failures = 0
    force = config.cache.force_refresh
    reuse_search = config.cache.reuse_search and not force

    for portal_key, portal_cfg in config.portals.items():
        if not portal_cfg.enabled:
            continue
        max_fetches = portal_cfg.max_fetches_per_day or 1
        already = store.fetches_today(portal_key)

        if reuse_search and already >= max_fetches:
            cached = store.load_search_cache(portal_key)
            if cached is None:
                cached = store.load_jobs_for_portal(portal_key)
            logger.info(
                "Portal %s: using cached search (%d jobs, %d/%d fetches today)",
                portal_key,
                len(cached),
                already,
                max_fetches,
            )
            all_jobs.extend(cached)
            continue
        try:
            provider = get_provider_for_portal(portal_key, portal_cfg)
            jobs = provider.search(profile0, config, portal_cfg, portal_key=portal_key)
            store.log_fetch(portal_key, "ok", f"{len(jobs)} jobs")
            store.save_search_cache(portal_key, jobs)
            store.upsert_jobs(jobs)
            all_jobs.extend(jobs)
            logger.info("Portal %s: %d jobs (fresh fetch)", portal_key, len(jobs))
        except Exception as exc:
            failures += 1
            errors.append(f"{portal_key}:{exc}")
            store.log_fetch(portal_key, "error", str(exc))
            cached = store.load_search_cache(portal_key) or store.load_jobs_for_portal(
                portal_key
            )
            if cached:
                logger.warning(
                    "Portal %s failed (%s); reusing %d cached jobs",
                    portal_key,
                    exc,
                    len(cached),
                )
                all_jobs.extend(cached)
            else:
                logger.exception("Portal %s failed with no cache", portal_key)

    deduped = dedupe_jobs(all_jobs)
    store.upsert_jobs(deduped)
    return {
        "jobs_data": [j.model_dump(mode="json") for j in deduped],
        "errors": errors,
        "portal_failures": failures,
    }


def node_hard_filter(state: PipelineState) -> dict[str, Any]:
    config = _cfg(state)
    profiles = _profiles(state)
    jobs = [JobPosting.model_validate(j) for j in state.get("jobs_data") or []]
    kept, dropped = hard_filter(jobs, config, profiles=profiles)
    for job, reason in dropped[:20]:
        logger.debug("Dropped %s: %s", job.id, reason)

    # Similarity gate: keep top-N by resume match before expensive rank/letters
    dry_run = state.get("dry_run", False)
    top_n = config.rag.gate_top_n or 0
    if top_n > 0 and len(kept) > top_n and profiles:
        try:
            rag = ResumeRAG(config, force_local=dry_run)
            rag.ingest(profiles)
            scored: list[tuple[JobPosting, float]] = []
            for job in kept:
                sim, _ = rag.score_job(job)
                scored.append((job, sim))
            scored.sort(key=lambda x: x[1], reverse=True)
            # Prefer jobs above min_similarity, then fill to top_n
            above = [(j, s) for j, s in scored if s >= config.rag.min_similarity]
            rest = [(j, s) for j, s in scored if s < config.rag.min_similarity]
            gated = (above + rest)[:top_n]
            logger.info(
                "Similarity gate: %d → %d (top_n=%d, above_floor=%d)",
                len(kept),
                len(gated),
                top_n,
                len(above),
            )
            kept = [j for j, _ in gated]
        except Exception as exc:
            logger.warning("Similarity gate skipped: %s", exc)

    return {"filtered_data": [j.model_dump(mode="json") for j in kept]}


def node_rank_and_cover(state: PipelineState) -> dict[str, Any]:
    from find_my_job.cache_keys import letter_cache_key, rank_cache_key

    config = _cfg(state)
    dry_run = state.get("dry_run", False)
    profiles = _profiles(state)
    run_dir = Path(state["run_dir"])
    writer = ArtifactWriter(run_dir)
    store = JobStore(Path(config.data_dir) / "jobs.sqlite")
    reuse_llm = config.cache.reuse_llm and not config.cache.force_refresh
    from find_my_job.llm import resolve_llm_mode

    llm_mode = "offline" if dry_run else resolve_llm_mode(config)

    try:
        # Dry-run always uses local embeds; config.rag.embeddings=local is default
        # so live runs also avoid Gemini embed quota unless opted into cloud.
        rag = ResumeRAG(config, force_local=dry_run)
        rag.ingest(profiles, force=dry_run)
    except Exception as exc:
        logger.error("Cannot init RAG: %s", exc)
        return {"errors": [f"rag:{exc}"], "scores_data": [], "letters_data": []}

    scores: list[JobScore] = []
    letters: list[CoverLetter] = []
    filtered = [JobPosting.model_validate(j) for j in state.get("filtered_data") or []]
    cache_hits = 0

    for job in filtered:
        rkey = rank_cache_key(job, profiles, config, mode=llm_mode)
        score = store.get_cached_score(rkey) if reuse_llm else None
        if score is not None:
            cache_hits += 1
            logger.info("LLM rank cache hit: %s", job.id)
        else:
            score = rank_job(job, profiles, config, rag, dry_run=dry_run)
            if not dry_run:
                store.put_llm_cache(
                    rkey, "rank", job.id, score.model_dump(mode="json")
                )
        store.save_score(score)
        scores.append(score)
        if score.dropped:
            writer.write_job(job, score, None)
            continue

        letter = None
        lkey = letter_cache_key(
            job, profiles, config, score.resume_label, mode=llm_mode
        )
        if reuse_llm:
            letter = store.get_cached_letter(lkey)
        if letter is not None:
            cache_hits += 1
            logger.info("LLM letter cache hit: %s", job.id)
            letters.append(letter)
        elif not store.has_letter(job.id) or dry_run or not reuse_llm:
            letter = generate_cover_letter(
                job, score, profiles, config, dry_run=dry_run
            )
            if not dry_run:
                store.put_llm_cache(
                    lkey, "letter", job.id, letter.model_dump(mode="json")
                )
                store.mark_letter(job.id)
            letters.append(letter)

        writer.write_job(job, score, letter)
        logger.info(
            "Scored %.2f %s @ %s", score.final_score, job.title, job.company
        )

    logger.info("LLM cache hits this run: %d", cache_hits)
    writer.write_ranked()
    scores.sort(key=lambda s: s.final_score, reverse=True)
    return {
        "scores_data": [s.model_dump(mode="json") for s in scores],
        "letters_data": [lt.model_dump(mode="json") for lt in letters],
    }


def _add_nodes(graph: StateGraph) -> StateGraph:
    graph.add_node("parse_resumes", node_parse_resumes)
    graph.add_node("ingest_rag", node_ingest_rag)
    graph.add_node("poll_portals", node_poll_portals)
    graph.add_node("hard_filter", node_hard_filter)
    graph.add_node("rank_and_cover", node_rank_and_cover)
    graph.add_edge(START, "parse_resumes")
    graph.add_edge("parse_resumes", "ingest_rag")
    graph.add_edge("ingest_rag", "poll_portals")
    graph.add_edge("poll_portals", "hard_filter")
    graph.add_edge("hard_filter", "rank_and_cover")
    graph.add_edge("rank_and_cover", END)
    return graph


def build_graph():
    return _add_nodes(StateGraph(PipelineState)).compile()


def build_graph_with_checkpoint(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    checkpointer.setup()
    app = _add_nodes(StateGraph(PipelineState)).compile(checkpointer=checkpointer)
    return app, conn


def run_pipeline(
    config: AppConfig,
    *,
    dry_run: bool = False,
    thread_id: str | None = None,
) -> dict[str, Any]:
    ensure_dirs(config)
    run_id = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    run_dir = Path(config.out_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    from find_my_job.logging_setup import setup_logging

    setup_logging(run_dir / "run.log")

    ckpt_path = Path(config.data_dir) / "checkpoints.sqlite"
    app, conn = build_graph_with_checkpoint(ckpt_path)
    tid = thread_id or run_id
    initial: PipelineState = {
        "config_data": config.model_dump(mode="json"),
        "dry_run": dry_run,
        "run_dir": str(run_dir),
        "jobs_data": [],
        "errors": [],
        "portal_failures": 0,
    }
    try:
        result = app.invoke(
            initial,
            config={"configurable": {"thread_id": tid}},
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Convenience aliases for CLI
    out = dict(result)
    out["run_dir"] = str(run_dir)
    out["jobs"] = [
        JobPosting.model_validate(j) for j in result.get("jobs_data") or []
    ]
    out["filtered"] = [
        JobPosting.model_validate(j) for j in result.get("filtered_data") or []
    ]
    out["scores"] = [
        JobScore.model_validate(s) for s in result.get("scores_data") or []
    ]
    out["letters"] = [
        CoverLetter.model_validate(lt) for lt in result.get("letters_data") or []
    ]
    return out
