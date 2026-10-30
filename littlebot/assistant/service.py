"""AssistantService (``ctx.assistant``): the fast voice pipeline's front door (being built: docs/rethink/PLAN.md)."""

from __future__ import annotations

import logging
from typing import Any

from littlebot.core.service import Service, ServiceStatus

log = logging.getLogger("littlebot.assistant")


class AssistantService(Service):
    name = "assistant"

    def __init__(self, ctx: Any) -> None:
        super().__init__()
        self.ctx = ctx

    async def start(self) -> None:
        self.set_status(ServiceStatus.RUNNING, "")

    def status(self) -> dict[str, Any]:
        return {"active": False, "pipeline": self.ctx.config.assistant.pipeline}
