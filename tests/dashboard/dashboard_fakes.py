"""Fakes for the dashboard tests: activities, media, brain, vision and a robot facade.

They implement the protocols in ``littlebot.core.interfaces`` and record calls
like the fakes in ``littlebot.testing``.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Any

from littlebot.core.context import RobotContext
from littlebot.core.events import Topics
from littlebot.core.state import RobotState
from littlebot.testing import _Recorder

ACTIVITIES = [
    {"id": "peekaboo", "name": "Peekaboo", "description": "Eyes hide and reappear.", "min_age_months": 6,
     "max_age_months": 24, "age_groups": ["infant", "young_toddler"], "category": "social",
     "participation": "Watch and giggle", "parent_instructions": "Sit with your child in front of LittleBot.",
     "safety_notes": "Keep the robot on a stable surface.", "duration_seconds": 90, "pausable": True},
    {"id": "animal_sounds", "name": "Animal sounds", "description": "Moo, baa and quack.", "min_age_months": 9,
     "max_age_months": 36, "age_groups": ["infant", "young_toddler", "toddler", "two_year"], "category": "language",
     "participation": "Copy the sounds", "parent_instructions": "Make the sounds together.",
     "safety_notes": "", "duration_seconds": 120, "pausable": True},
    {"id": "counting", "name": "Counting fingers", "description": "Count to five.", "min_age_months": 30,
     "max_age_months": 60, "age_groups": ["two_year", "preschool"], "category": "numeracy",
     "participation": "Hold up fingers", "parent_instructions": "Help with the fingers.",
     "safety_notes": "", "duration_seconds": 120, "pausable": False},
]


class FakeActivities(_Recorder):
    def __init__(self, bus: Any = None) -> None:
        super().__init__()
        self.bus = bus
        self.running: dict[str, Any] | None = None
        self.paused = False
        self.prompt: dict[str, Any] | None = None
        self.refuse_start: str | None = None
        self.history_cleared = False

    def list(self, age_months: int | None = None, category: str | None = None) -> list[dict[str, Any]]:
        self._rec("list", age_months=age_months, category=category)
        items = [dict(a) for a in ACTIVITIES]
        if age_months is not None:
            items = [a for a in items if a["min_age_months"] <= age_months < a["max_age_months"]]
        if category is not None:
            items = [a for a in items if a["category"] == category]
        return items

    def get(self, activity_id: str) -> dict[str, Any] | None:
        return next((dict(a) for a in ACTIVITIES if a["id"] == activity_id), None)

    async def start(self, activity_id: str, *, source: str = "dashboard") -> dict[str, Any]:
        self._rec("start", activity_id, source=source)
        item = self.get(activity_id)
        if item is None:
            raise KeyError(f"unknown activity {activity_id!r}")
        if self.refuse_start:
            raise PermissionError(self.refuse_start)
        self.running, self.paused = item, False
        self.prompt = {"prompt_id": "p1", "text": "Did your child clap?", "options": ["done", "skip"]}
        if self.bus:
            self.bus.publish(Topics.ACTIVITY_STARTED, {"id": activity_id, "name": item["name"]}, source="activities")
        await asyncio.sleep(0)
        return self.status()

    async def stop(self, reason: str = "stopped") -> None:
        self._rec("stop", reason)
        self.running, self.prompt = None, None

    async def pause(self) -> bool:
        self._rec("pause")
        self.paused = self.running is not None
        return self.paused

    async def resume(self) -> bool:
        self._rec("resume")
        ok = self.running is not None and self.paused
        self.paused = False
        return ok

    def respond(self, response: str, prompt_id: str | None = None) -> bool:
        self._rec("respond", response, prompt_id)
        if self.prompt is None:
            return False
        self.prompt = None
        return True

    def clear_history(self) -> None:
        self._rec("clear_history")
        self.history_cleared = True

    def status(self) -> dict[str, Any]:
        r = self.running
        return {"running": r is not None, "paused": self.paused, "id": r["id"] if r else None,
                "name": r["name"] if r else None, "step_index": 0 if r else None, "total_steps": 4 if r else None,
                "step_text": "Clap your hands!" if r else None, "prompt": self.prompt}


_YT = re.compile(r"^https://(www\.)?(youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})$")


class FakeMedia(_Recorder):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict[str, Any]] = []
        self._next = 1
        self.state = "idle"
        self.current: dict[str, Any] | None = None
        self.volume = 0.5
        self.under_age = False
        self.player_url = "http://127.0.0.1:8765/player?token=kiosk-token-123"
        self._lock = threading.Lock()

    def list_items(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(i) for i in self.items]

    def add_item(self, url: str, title: str | None = None, **kwargs: Any) -> dict[str, Any]:
        self._rec("add_item", url, title, **kwargs)
        m = _YT.match(url)
        if not m:
            raise ValueError("That doesn't look like a YouTube video link.")
        with self._lock:
            item = {"id": self._next, "kind": "video", "youtube_id": m.group(3), "title": title or "Untitled video",
                    "url": url, "added_at": time.time()}
            self._next += 1
            self.items.append(item)
        return dict(item)

    def remove_item(self, item_id: int) -> bool:
        self._rec("remove_item", item_id)
        with self._lock:
            before = len(self.items)
            self.items = [i for i in self.items if i["id"] != item_id]
            return len(self.items) != before

    async def play(self, item_id: int, *, parent_override: bool = False) -> dict[str, Any]:
        self._rec("play", item_id, parent_override=parent_override)
        item = next((i for i in self.items if i["id"] == item_id), None)
        if item is None:
            raise KeyError("That video is not in the approved list.")
        if self.under_age and not parent_override:
            raise PermissionError("Video is off for children under 18 months.")
        self.state, self.current = "playing", item
        return self.status()

    async def pause(self) -> bool:
        self._rec("pause")
        if self.state == "playing":
            self.state = "paused"
            return True
        return False

    async def resume(self) -> bool:
        self._rec("resume")
        if self.state == "paused":
            self.state = "playing"
            return True
        return False

    async def stop(self, reason: str = "stopped") -> None:
        self._rec("stop", reason)
        self.state, self.current = "idle", None

    async def skip(self) -> dict[str, Any]:
        self._rec("skip")
        return self.status()

    def set_volume(self, volume: float) -> float:
        self._rec("set_volume", volume)
        self.volume = max(0.0, min(0.8, volume))
        return self.volume

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "item": self.current, "session_seconds": 0, "session_limit_seconds": 900,
                "daily_used_seconds": 0, "daily_limit_seconds": 1800, "volume": self.volume,
                "player": {"mode": "kiosk", "url": self.player_url, "path": "/player?token=kiosk-token-123",
                           "connected": False, "last_event": {"type": "autoplay_blocked", "current": 0.0}}}


class FakeMediaFull(FakeMedia):
    """FakeMedia plus the optional update_item / reorder_items extras of the real controller."""

    def update_item(self, item_id: int, *, title: str | None = None, enabled: bool | None = None) -> dict[str, Any]:
        self._rec("update_item", item_id, title=title, enabled=enabled)
        for it in self.items:
            if it["id"] == item_id:
                if title is not None:
                    it["title"] = title
                if enabled is not None:
                    it["enabled"] = enabled
                return dict(it)
        raise KeyError(f"unknown media item {item_id}")

    def reorder_items(self, ids: list[int]) -> list[dict[str, Any]]:
        self._rec("reorder_items", list(ids))
        order = {i: n for n, i in enumerate(ids)}
        self.items.sort(key=lambda it: order.get(it["id"], len(order)))
        return self.list_items()


class FakeBrain(_Recorder):
    def __init__(self) -> None:
        super().__init__()

    async def handle_utterance(self, text: str, *, source: str = "simulated", speak: bool = True) -> dict[str, Any]:
        self._rec("handle_utterance", text, source=source, speak=speak)
        return {"reply": f"You said {text}!", "provider": "mock", "filtered": False, "error": None}

    async def converse_once(self) -> dict[str, Any]:
        self._rec("converse_once")
        return {"heard": None, "reply": None, "provider": "mock"}

    def providers(self) -> list[dict[str, Any]]:
        return [{"name": "mock", "configured": True, "available": True, "model": "scripted"},
                {"name": "anthropic", "configured": True, "available": True, "model": "claude-haiku-5-5"},
                {"name": "openai", "configured": False, "available": False, "model": "gpt-4o-mini"}]

    async def test_provider(self, name: str) -> dict[str, Any]:
        self._rec("test_provider", name)
        return {"ok": True, "latency_ms": 12, "error": None}

    def clear_history(self) -> None:
        self._rec("clear_history")

    def status(self) -> dict[str, Any]:
        return {"provider": "mock", "active_provider": "mock", "cloud_allowed": False, "reason": "age",
                "usage": {"requests_today": 3, "requests_this_hour": 1, "cost_today_usd": 0.0},
                "last_error": None, "busy": False}


class FakeVision(_Recorder):
    def __init__(self) -> None:
        super().__init__()
        self.face: tuple[float, float] | None = None

    def simulate_face(self, x: float, y: float, size: float = 0.25) -> None:
        self._rec("simulate_face", x, y, size)
        self.face = (x, y)

    def simulate_no_face(self) -> None:
        self._rec("simulate_no_face")
        self.face = None

    def status(self) -> dict[str, Any]:
        return {"camera": "ok", "backend": "fake", "detector": "fake", "fps": 6.0,
                "faces": 1 if self.face else 0, "person_present": self.face is not None,
                "gaze": {"x": self.face[0], "y": self.face[1]} if self.face else None}


class FakeRobot(_Recorder):
    """Minimal LittleBot facade: status / stop_all / sleep / wake / request_shutdown."""

    def __init__(self, ctx: RobotContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.started_at = time.time() - 125
        self.services: list[tuple[str, Any]] = []
        self.dashboard = None
        self.shutdown_requested: str | None = None
        ctx.extras["robot"] = self

    def status(self) -> dict[str, Any]:
        ctx = self.ctx
        out: dict[str, Any] = {
            "name": ctx.config.robot_name, "version": "test", "mode": ctx.config.mode.value,
            "state": ctx.state.snapshot(), "leases": ctx.arbiter.snapshot(), "privacy": ctx.privacy.snapshot(),
            "system": ctx.monitor.status(), "age_months": ctx.settings.child_age_months(),
            "age_group": ctx.age_policy().group, "services": {},
        }
        for attr in ("face", "audio", "vision", "motion", "media", "brain", "activities"):
            svc = getattr(ctx, attr)
            out[attr] = svc.status() if svc is not None else {"status": "unavailable"}
        return out

    async def stop_all(self, source: str = "parent") -> dict[str, Any]:
        self._rec("stop_all", source)
        ctx = self.ctx
        ctx.bus.publish(Topics.STOP_ALL, {"source": source})
        if ctx.motion is not None:
            ctx.motion.cancel()
        if ctx.audio is not None:
            ctx.audio.stop_output()
        if ctx.activities is not None:
            await ctx.activities.stop("parent stop")
        if ctx.media is not None:
            await ctx.media.stop("stopped")
        ctx.state.transition(RobotState.IDLE, "stop all", force=True)
        return self.status()

    def sleep(self, source: str = "parent") -> bool:
        self._rec("sleep", source)
        return self.ctx.state.transition(RobotState.SLEEPING, "sleep")

    def wake(self, source: str = "parent") -> bool:
        self._rec("wake", source)
        return self.ctx.state.transition(RobotState.IDLE, "wake")

    def request_shutdown(self, reason: str = "requested") -> None:
        self._rec("request_shutdown", reason)
        self.shutdown_requested = reason


def install_fakes(ctx: RobotContext) -> dict[str, Any]:
    """Attach all dashboard fakes to ``ctx`` (face/audio/motion fakes come from make_test_context)."""
    ctx.activities = FakeActivities(ctx.bus)
    ctx.media = FakeMedia()
    ctx.brain = FakeBrain()
    ctx.vision = FakeVision()
    robot = FakeRobot(ctx)
    return {"activities": ctx.activities, "media": ctx.media, "brain": ctx.brain, "vision": ctx.vision,
            "robot": robot}


# ---------------------------------------------------------------------------
# HTTP helpers shared by the dashboard tests
# ---------------------------------------------------------------------------

FAKE_ANTHROPIC_KEY = "sk-ant-api03-TESTSECRETdoNOTleak1234567890abcdef"
FAKE_OPENAI_KEY = "sk-proj-TESTSECREToPENAIdoNOTleak0987654321"
PASSWORD = "correct horse battery"
BASE_URL = "http://testserver"


def make_client(app: Any, ip: str = "127.0.0.1") -> "httpx.AsyncClient":
    import httpx

    transport = httpx.ASGITransport(app=app, client=(ip, 50000))
    return httpx.AsyncClient(transport=transport, base_url=BASE_URL)


async def do_setup(client: Any, app: Any, password: str = PASSWORD) -> dict[str, Any]:
    """Complete first-run setup with a freshly issued code; leaves the client logged in with CSRF set."""
    code = app.state.auth.issue_setup_code()
    r = await client.post("/api/setup", json={"setup_code": code, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()
    client.headers["X-CSRF-Token"] = data["csrf_token"]
    return data
