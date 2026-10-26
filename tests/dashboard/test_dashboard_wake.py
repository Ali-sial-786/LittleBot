"""Dashboard: the wake word switch and status on the AI page (``/api/chatgpt``)."""

from __future__ import annotations

from typing import Any


class FakeWake:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def status(self) -> dict[str, Any]:
        enabled = bool(self.ctx.settings.get("wake.enabled"))
        return {"enabled": enabled, "available": True, "listening": enabled,
                "state": "listening" if enabled else "disabled", "paused": None if enabled else "disabled",
                "detail": "Listening for “hey little bot” or “hey bot”.",
                "unavailable": None, "model_missing": False, "phrases": ["hey little bot", "hey bot"], "triggers": 2,
                "last_heard": {"phrase": "hey bot", "at": 1.0, "result": "blocked", "error": "not_signed_in",
                               "detail": "Sign in with ChatGPT first."}}


async def test_wake_status_without_the_service(authed, dctx):
    d = (await authed.get("/api/chatgpt")).json()
    assert d["wake"]["enabled"] is True  # on by default
    assert d["wake"]["available"] is False and d["wake"]["unavailable"] == "not_running"


async def test_wake_switch(authed, dctx):
    dctx.wake = FakeWake(dctx)
    d = (await authed.get("/api/chatgpt")).json()
    assert d["wake"]["listening"] is True and d["wake"]["last_heard"]["result"] == "blocked"
    r = await authed.put("/api/chatgpt/wake", json={"enabled": False})
    assert r.status_code == 200
    assert dctx.settings.get("wake.enabled") is False
    body = r.json()
    assert body["ok"] is True and body["wake"]["enabled"] is False and body["wake"]["state"] == "disabled"
    assert "live" in body and "account" in body  # the whole AI-page view, like the other routes
    r = await authed.put("/api/chatgpt/wake", json={"enabled": True})
    assert r.status_code == 200 and dctx.settings.get("wake.enabled") is True


async def test_wake_switch_validates_and_needs_login(authed, client, dctx):
    for bad in ({}, {"enabled": "maybe"}, {"enabled": True, "phrases": ["open sesame"]}):
        r = await authed.put("/api/chatgpt/wake", json=bad)
        assert r.status_code in (400, 422), bad
    assert dctx.settings.get("wake.enabled") is True
    r = await client.put("/api/chatgpt/wake", json={"enabled": False})
    assert r.status_code == 401
    assert dctx.settings.get("wake.enabled") is True


async def test_real_wake_service_status_reaches_the_page(authed, dctx):
    from littlebot.audio.wakeword import WakeWordService

    dctx.wake = WakeWordService(dctx)  # not started; FakeAudio can't share a microphone
    d = (await authed.get("/api/chatgpt")).json()["wake"]
    for key in ("enabled", "available", "listening", "state", "paused", "detail", "model_missing", "phrases",
                "last_heard"):
        assert key in d
    assert d["phrases"] == ["hey little bot", "hey lil bot", "hey bot"] and d["listening"] is False
