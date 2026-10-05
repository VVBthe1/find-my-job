from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from find_my_job.models import AppConfig, PortalConfig


def load_env(env_path: str | Path | None = None) -> None:
    """Load .env from project root or an explicit path."""
    if env_path:
        load_dotenv(env_path, override=False)
        return
    cwd = Path.cwd() / ".env"
    if cwd.exists():
        load_dotenv(cwd, override=False)
    else:
        load_dotenv(override=False)


def load_config(path: str | Path | None = None) -> AppConfig:
    load_env()
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    else:
        candidates.extend(
            [
                Path.cwd() / "config.yaml",
                Path.home() / ".config" / "find-my-job" / "config.yaml",
                Path.cwd() / "config.example.yaml",
            ]
        )

    config_path: Path | None = None
    for c in candidates:
        if c.exists():
            config_path = c
            break
    if config_path is None:
        raise FileNotFoundError(
            "No config found. Copy config.example.yaml to config.yaml and edit it."
        )

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    portals_raw = raw.pop("portals", {}) or {}
    portals: dict[str, PortalConfig] = {}
    for name, pcfg in portals_raw.items():
        if not isinstance(pcfg, dict):
            continue
        data = dict(pcfg)
        if "provider" not in data:
            data["provider"] = name
        # Allow arbitrary YAML keys for http_list / plugin extras
        known = {
            "enabled",
            "provider",
            "max_fetches_per_day",
            "search_urls",
            "search_url",
            "item_selector",
            "fields",
            "format",
            "extra",
        }
        extras = {k: v for k, v in data.items() if k not in known}
        if extras:
            data["extra"] = {**data.get("extra", {}), **extras}
            for k in extras:
                data.pop(k, None)
        portals[name] = PortalConfig(**data)

    return AppConfig(**raw, portals=portals)


def project_path(*parts: str) -> Path:
    return Path.cwd().joinpath(*parts)


def ensure_dirs(config: AppConfig) -> None:
    Path(config.data_dir).mkdir(parents=True, exist_ok=True)
    Path(config.out_dir).mkdir(parents=True, exist_ok=True)
    Path(config.rag.persist_dir).mkdir(parents=True, exist_ok=True)
    resumes = Path(config.resumes_dir)
    if not resumes.is_absolute():
        resumes = Path.cwd() / resumes
    resumes.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
