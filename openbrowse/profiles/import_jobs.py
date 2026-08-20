"""In-memory staging for the BU Cloud import flow (fetch → confirm → import).

A job holds the fetched storage states in server memory only, between the fetch and confirm
steps, and is cleared once imported or once it goes stale. Cookie values are never included in
the client-facing summary, and the BU Cloud token is never stored on the job at all.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

_TTL_SECONDS = 900


@dataclass
class ImportJob:
    id: str
    status: str = "fetching"  # fetching → ready → importing → done | error
    phase: str = "Starting"
    error: str | None = None
    profiles: list[dict[str, Any]] = field(default_factory=list)
    staged: dict[str, dict[str, Any]] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    created: float = field(default_factory=time.monotonic)

    @property
    def terminal(self) -> bool:
        return self.status in ("done", "error")

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "phase": self.phase,
            "error": self.error,
            "profiles": [
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "status": p.get("status"),
                    "cookieCount": p.get("cookie_count"),
                    "originCount": p.get("origin_count"),
                    "domains": p.get("domains") or [],
                    "progress": p.get("progress"),
                    "error": p.get("error"),
                }
                for p in self.profiles
            ],
            "results": self.results,
        }


class ImportJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, ImportJob] = {}

    def new(self) -> ImportJob:
        self._purge()
        job = ImportJob(id=uuid.uuid4().hex)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> ImportJob | None:
        self._purge()
        return self._jobs.get(job_id)

    def drop(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)

    def _purge(self) -> None:
        now = time.monotonic()
        for jid in [j for j, job in self._jobs.items() if now - job.created > _TTL_SECONDS]:
            self._jobs.pop(jid, None)


jobs = ImportJobStore()
