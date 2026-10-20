"""Common lifecycle for long-running modules."""

from __future__ import annotations

import abc
import enum
import logging
from typing import Any


class ServiceStatus(str, enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"  # running with reduced function (e.g. device missing -> mock / disabled)
    DISABLED = "disabled"  # turned off by config or privacy controls
    FAILED = "failed"


class Service(abc.ABC):
    """Base class: async start/stop on the runtime loop plus a health snapshot.

    ``start`` must not block the loop: spawn threads for blocking device I/O.
    ``stop`` must be idempotent and safe to call after a failed start.
    """

    name: str = "service"

    def __init__(self) -> None:
        self.status = ServiceStatus.STOPPED
        self.status_detail = ""
        self.log = logging.getLogger(f"littlebot.{self.name}")

    async def start(self) -> None:  # pragma: no cover - default no-op
        self.status = ServiceStatus.RUNNING

    async def stop(self) -> None:  # pragma: no cover - default no-op
        self.status = ServiceStatus.STOPPED

    def set_status(self, status: ServiceStatus, detail: str = "") -> None:
        if status != self.status or detail != self.status_detail:
            self.log.info("status %s%s", status.value, f" ({detail})" if detail else "")
        self.status, self.status_detail = status, detail

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status.value, "detail": self.status_detail}
