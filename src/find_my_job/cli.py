from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from find_my_job.config import load_config
from find_my_job.graph import run_pipeline

app = typer.Typer(
    name="find-my-job",
    help="Local multi-agent job filter with LangGraph + ChromaDB RAG",
    add_completion=False,
)
console = Console()


@app.command()
def run(
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to config.yaml"
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Skip live LLM calls where possible; still poll portals",
    ),
    thread_id: Optional[str] = typer.Option(
        None, "--thread-id", help="LangGraph checkpoint thread id"
    ),
) -> None:
    """Run the full pipeline and write artifacts under out/<run-id>/."""
    try:
        cfg = load_config(config)
    except Exception as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    console.print("[bold]find-my-job[/bold] starting…")
    from find_my_job.llm import has_cloud_key, resolve_llm_mode

    try:
        mode = "offline" if dry_run else resolve_llm_mode(cfg)
    except Exception as exc:
        console.print(f"[red]LLM config:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    if mode == "offline":
        console.print(
            "[yellow]offline mode[/yellow] — no cloud API "
            "(local RAG rank + template cover letters)"
            + (" [dry-run]" if dry_run else "")
        )
    else:
        console.print(
            f"[green]{cfg.llm.provider}[/green] cloud mode ({cfg.llm.model})"
        )
        if not has_cloud_key(cfg):
            console.print("[red]API key missing for configured provider[/red]")
            raise typer.Exit(code=2)

    result = run_pipeline(cfg, dry_run=dry_run, thread_id=thread_id)
    run_dir = result.get("run_dir")
    n_jobs = len(result.get("jobs") or [])
    n_filt = len(result.get("filtered") or [])
    n_scores = len(result.get("scores") or [])
    n_letters = len(result.get("letters") or [])
    failures = int(result.get("portal_failures") or 0)
    errors = result.get("errors") or []

    console.print(f"Jobs fetched: {n_jobs}")
    console.print(f"After hard filter: {n_filt}")
    console.print(f"Scored: {n_scores}")
    console.print(f"Cover letters: {n_letters}")
    console.print(f"Artifacts: {run_dir}")
    if errors:
        console.print(f"[yellow]Warnings/errors ({len(errors)}):[/yellow]")
        for e in errors[:10]:
            console.print(f"  - {e}")

    if failures:
        raise typer.Exit(code=1)


@app.command("list-portals")
def list_portals() -> None:
    """List builtin portal provider names."""
    from find_my_job.portals.discovery import _BUILTIN

    for name in sorted(_BUILTIN):
        console.print(name)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
