from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path

from find_my_job.models import PortalConfig
from find_my_job.portals.protocol import PortalProvider

logger = logging.getLogger("find_my_job.portals")

_BUILTIN = {
    "naukri": "find_my_job.portals.builtin.naukri",
    "indeed_in": "find_my_job.portals.builtin.indeed_in",
    "linkedin": "find_my_job.portals.builtin.linkedin",
    "instahyre": "find_my_job.portals.builtin.instahyre",
    "wellfound": "find_my_job.portals.builtin.wellfound",
    "hirist": "find_my_job.portals.builtin.hirist",
    "http_list": "find_my_job.portals.builtin.http_list",
}


def _load_module_from_path(path: Path):
    mod_name = f"find_my_job_user_portal_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load portal plugin: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _instantiate(provider: object) -> PortalProvider:
    if isinstance(provider, type):
        return provider()  # type: ignore[return-value]
    return provider  # type: ignore[return-value]


def _user_plugin_dirs() -> list[Path]:
    return [
        Path.cwd() / "portals",
        Path.home() / ".config" / "find-my-job" / "portals",
    ]


def resolve_provider(name: str) -> PortalProvider:
    for directory in _user_plugin_dirs():
        candidate = directory / f"{name}.py"
        if candidate.exists():
            module = _load_module_from_path(candidate)
            provider = getattr(module, "Provider", None)
            if provider is None:
                raise AttributeError(f"{candidate} must export Provider")
            return _instantiate(provider)

    if name in _BUILTIN:
        module = importlib.import_module(_BUILTIN[name])
        return _instantiate(getattr(module, "Provider"))

    raise KeyError(
        f"Unknown portal provider '{name}'. "
        f"Add portals/{name}.py with Provider, use a builtin "
        f"({', '.join(sorted(_BUILTIN))}), or set provider: http_list."
    )


def get_provider_for_portal(portal_key: str, portal_cfg: PortalConfig) -> PortalProvider:
    return resolve_provider(portal_cfg.provider or portal_key)
