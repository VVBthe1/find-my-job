# find-my-job

Local multi-agent CLI that polls configured Indian job portals, filters by your criteria, ranks with resume RAG (ChromaDB) + LLM, and writes cover letters so you can run it and walk away.

## Stack

- **LangGraph** — pipeline orchestration + SQLite checkpoints
- **LangChain** — loaders, splitters, chat models, structured output
- **ChromaDB** — resume embeddings / retrieval

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# Add GOOGLE_API_KEY from https://aistudio.google.com/apikey
cp config.example.yaml config.yaml
# edit config.yaml: resume paths, locations, skills, portals
```

**LLM (default: Google Gemini, free tier via AI Studio):** put `GOOGLE_API_KEY` in `.env`.
Resume text and job descriptions are sent to Google’s API for ranking/letters — same as any cloud LLM.
Contact details in the resume go with that; Google already has your account on Pixel/AI Studio.

With **no API key**, `llm.mode: auto` runs **offline**: local embeddings, heuristic rank, template letters.

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
cp .env.example .env
cp config.example.yaml config.yaml
```

## Run

```bash
find-my-job run
find-my-job run --dry-run
find-my-job run --config /path/to/config.yaml
```

Artifacts land in `out/<run-id>/` (ranked.md, per-job folders, run.log). Job metadata and fetch throttle live in `data/jobs.sqlite`; vectors in `data/chroma/`.

## Add a portal without changing core

1. **YAML `http_list`** — set `provider: http_list` plus `search_url`, `item_selector`, and `fields` in config.
2. **Drop-in Python** — put `Provider` in `./portals/myboard.py` or `~/.config/find-my-job/portals/myboard.py`, then `provider: myboard` in config.

## Notes

- Pollers only GET public search URLs. No portal logins or cookies.
- LinkedIn (and others) honor `max_fetches_per_day` (default 1) via SQLite fetch log.
- Does not auto-apply.
