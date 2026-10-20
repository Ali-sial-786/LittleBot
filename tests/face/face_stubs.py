"""Small stand-ins used by the face tests (stub robot, recording subsystems)."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from littlebot.core.config import FaceConfig
from littlebot.face.animator import FaceAnimator


def make_animator(seed: int = 1, **cfg: Any) -> FaceAnimator:
    """Deterministic animator; ``cfg`` overrides FaceConfig fields."""
    return FaceAnimator(FaceConfig(**cfg), seed=seed)


def step(a: Any, seconds: float, dt: float = 1 / 60) -> None:
    """Advance an animator / controller by ``seconds`` in fixed steps."""
    n = int(round(seconds / dt))
    for _ in range(n):
        a.update(dt)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


class StubVision(Recorder):
    def simulate_face(self, x: float, y: float, size: float = 0.25) -> None:
        self.calls.append(("simulate_face", (x, y), {"size": size}))

    def simulate_no_face(self) -> None:
        self.calls.append(("simulate_no_face", (), {}))

    def status(self) -> dict[str, Any]:
        return {"camera": "ok", "backend": "stub", "faces": 0}


class StubActivities(Recorder):
    def __init__(self, refuse: bool = False) -> None:
        super().__init__()
        self.refuse = refuse

    async def start(self, activity_id: str, *, source: str = "dashboard") -> dict[str, Any]:
        self.calls.append(("start", (activity_id,), {"source": source}))
        if self.refuse:
            raise PermissionError("not now")
        return {"running": True, "id": activity_id}

    def respond(self, response: str, prompt_id: str | None = None) -> bool:
        self.calls.append(("respond", (response,), {}))
        return True

    def status(self) -> dict[str, Any]:
        return {"running": False}


class StubBrain(Recorder):
    async def converse_once(self) -> dict[str, Any]:
        self.calls.append(("converse_once", (), {}))
        await asyncio.sleep(0)
        return {"heard": None}

    def status(self) -> dict[str, Any]:
        return {"provider": "stub"}


class StubRobot:
    """Just what face.display.run_display needs from app.robot.LittleBot."""

    def __init__(self, ctx: Any, runtime: Any = None) -> None:
        self.ctx = ctx
        self.runtime = runtime
        self.shutdown_event = threading.Event()
        self.shutdown_reasons: list[str] = []
        self.stop_all_calls: list[str] = []

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": "LittleBot", "mode": "test", "state": self.ctx.state.snapshot()}
        for attr in ("face", "audio", "vision", "motion", "media", "brain", "activities"):
            svc = getattr(self.ctx, attr)
            out[attr] = svc.status() if svc is not None else {"status": "unavailable"}
        return out

    async def stop_all(self, source: str = "parent") -> dict[str, Any]:
        self.stop_all_calls.append(source)
        return {}

    def request_shutdown(self, reason: str = "requested") -> None:
        self.shutdown_reasons.append(reason)
        self.shutdown_event.set()
