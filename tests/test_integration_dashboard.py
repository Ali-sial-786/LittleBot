"""The parent dashboard controlling the REAL robot over real HTTP.

The robot is built through the composition root with every real subsystem (mock
devices), the dashboard's uvicorn server runs on the robot's own asyncio runtime
(exactly as in production) on an ephemeral port, and this test talks to it with
an ordinary HTTP client: first-run setup, then each dashboard section.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx
import pytest
from pydantic import SecretStr

from littlebot.app.robot import LittleBot
from littlebot.core.context import build_core_context
from littlebot.core.events import Topics, collect
from littlebot.core.state import RobotState
from littlebot.hardware.system import MockSystemMonitor
from littlebot.testing import make_test_config

FAKE_KEY = "sk-ant-integration-test-secret-0123456789"


def wait_until(pred: Callable[[], Any], timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.03)
    return pred()


@pytest.fixture
def live():
    cfg = make_test_config(dashboard={"enabled": True, "port": 0})
    cfg.anthropic_api_key = SecretStr(FAKE_KEY)
    ctx = build_core_context(cfg, db_path=":memory:", monitor=MockSystemMonitor())
    seen = collect(ctx.bus, [Topics.DASHBOARD_SETUP])
    bot = LittleBot(ctx)
    bot.build_subsystems()
    bot.start()
    assert bot.dashboard is not None and bot.dashboard.port
    base = f"http://127.0.0.1:{bot.dashboard.port}"
    client = httpx.Client(base_url=base, headers={"Origin": base}, timeout=20)
    yield bot, client, seen
    client.close()
    bot.shutdown("test end")


def _setup(bot: LittleBot, client: httpx.Client) -> None:
    code = bot.dashboard.auth.issue_setup_code()
    r = client.post("/api/setup", json={"setup_code": code, "password": "a-good-parent-password"})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]


def test_first_run_setup_and_protection(live):
    bot, client, seen = live
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/session").json()["setup_required"] is True
    assert client.get("/api/status").status_code == 401
    # The setup code was announced for the robot's own screen.
    assert seen and seen[0].data["active"] is True and len(seen[0].data["code"]) == 9
    r = client.post("/api/setup", json={"setup_code": "WRON-GCOD", "password": "a-good-parent-password"})
    assert r.status_code in (401, 403)
    _setup(bot, client)
    assert seen[-1].data["active"] is False  # banner removed from the robot screen
    assert client.get("/api/status").status_code == 200
    # Mutations need the CSRF header.
    no_csrf = httpx.Client(base_url=str(client.base_url), headers={"Origin": str(client.base_url)},
                           cookies=client.cookies)
    try:
        assert no_csrf.post("/api/face/blink", json={}).status_code == 403
    finally:
        no_csrf.close()


def test_dashboard_drives_every_subsystem(live):
    bot, client, _ = live
    ctx = bot.ctx
    _setup(bot, client)

    def ok(r: httpx.Response) -> dict[str, Any]:
        assert r.status_code in (200, 201), (r.request.url, r.status_code, r.text)
        assert FAKE_KEY not in r.text
        return r.json()

    status = ok(client.get("/api/status"))
    assert status["state"]["state"] == "IDLE"

    # Child profile -> age-aware behaviour.
    ok(client.put("/api/profile", json={"age_months": 30, "favourite_animals": ["duck"]}))
    assert ctx.settings.child_age_months() == 30

    # Face.
    ok(client.post("/api/face/expression", json={"name": "happy"}))
    assert wait_until(lambda: ctx.face.status()["expression"] == "happy", 3)
    assert client.post("/api/face/expression", json={"name": "grumpy"}).status_code == 422

    # Activities: start a real-time peekaboo, see it running, STOP everything.
    ctx.clock.scale = 1.0
    ok(client.post("/api/activities/peekaboo/start", json={}))
    assert wait_until(lambda: ctx.activities.status()["running"], 3)
    assert ctx.state.state == RobotState.PLAYING_ACTIVITY
    ok(client.post("/api/stop", json={}))
    assert wait_until(lambda: not ctx.activities.status()["running"], 3)
    assert ctx.state.state == RobotState.IDLE
    ctx.clock.scale = 0.0

    # Media: allowlist + play + stop.
    item = ok(client.post("/api/media/items", json={"url": "https://youtu.be/jNQXAC9IVRw", "title": "Song"}))
    item_id = item.get("id") or item.get("item", {}).get("id")
    assert client.post("/api/media/items", json={"url": "https://evil.example/watch?v=jNQXAC9IVRw"}).status_code == 422
    ok(client.post("/api/media/play", json={"item_id": item_id}))
    assert ctx.state.state == RobotState.PLAYING_MEDIA
    assert wait_until(lambda: ctx.face.status()["visible"] is False, 3)
    ok(client.post("/api/media/stop", json={}))
    assert wait_until(lambda: ctx.state.state == RobotState.IDLE, 3)
    assert wait_until(lambda: ctx.face.status()["visible"] is True, 3)

    # Simulator camera: tap the left of the pad (screen coords) -> eyes look left.
    for _ in range(10):
        ok(client.post("/api/sim/camera", json={"event": "face", "x": 0.15, "y": 0.5}))
        time.sleep(0.05)
    assert wait_until(lambda: ctx.face.status()["gaze"]["x"] < 0.42, 4), ctx.face.status()

    # Simulated speech -> mock conversation.
    reply = ok(client.post("/api/sim/speech", json={"text": "what does a duck say?", "route": "brain"}))
    assert "reply" in str(reply).lower()
    assert wait_until(lambda: ctx.state.state == RobotState.IDLE, 5)

    # Motion: gesture, e-stop latch, reset.
    ok(client.post("/api/motion/gesture", json={"name": "wave"}))
    ok(client.post("/api/motion/estop", json={}))
    assert ctx.motion.status()["estop"] is True
    ok(client.post("/api/motion/estop/reset", json={}))
    assert wait_until(lambda: ctx.motion.status()["estop"] is False, 3)

    # Privacy: camera off releases the camera.
    ok(client.put("/api/privacy", json={"camera_enabled": False}))
    assert wait_until(lambda: ctx.vision.status()["camera"] == "disabled", 3)

    # AI page never exposes the key, only that one exists.
    ai = ok(client.get("/api/ai"))
    assert FAKE_KEY not in str(ai)

    # Final status still sane and secret-free.
    final = ok(client.get("/api/status"))
    assert final["state"]["state"] == "IDLE"
