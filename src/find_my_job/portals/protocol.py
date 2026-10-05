from __future__ import annotations

from typing import Protocol, runtime_checkable

from find_my_job.models import AppConfig, JobPosting, PortalConfig, ResumeProfile


@runtime_checkable
class PortalProvider(Protocol):
    name: str

    def search(
        self,
        profile: ResumeProfile | list[ResumeProfile],
        config: AppConfig,
        portal_cfg: PortalConfig,
        *,
        portal_key: str,
    ) -> list[JobPosting]: ...
