"""Dashboard: Android (``/api/android``): status, open (Android or an app), close, the switch, parents only, CSRF,
and the card on the Assistant page."""

from __future__ import annotations

from typing import Any


class FakeAndroid:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.calls: list[tuple[str, Any]] = []
        self.active = False

    async def refresh(self) -> None:
        self.calls.append(("refresh", None))

    def status(self) -> dict[str, Any]:
        return {"available": True, "enabled": bool(self.ctx.settings.get("android.enabled", True)),
                "set_up": True, "setup_step": None, "active": self.active,
                "state": "starting" if self.active else None, "apps": {"aosu": "com.glazero.android"}}

    async def open(self, *, source: str = "voice", app: str | None = None) -> dict[str, Any]:
        self.calls.append(("open", (source, app)))
        self.active = True
        return {"ok": True, "state": "starting", "error": None, "detail": "", "already": False}

    async def close(self, reason: str) -> bool:
        self.calls.append(("close", reason))
        was, self.active = self.active, False
        return was


async def test_without_the_service(authed: Any) -> None:
    d = (await authed.get("/api/android")).json()
    assert d["available"] is False and d["active"] is False
    assert (await authed.post("/api/android/open", json={})).status_code == 503
    assert (await authed.post("/api/android/close")).status_code == 503


async def test_open_and_close(authed: Any, dctx: Any) -> None:
    dctx.android = FakeAndroid(dctx)
    d = (await authed.get("/api/android")).json()
    assert d["set_up"] is True and dctx.android.calls == [("refresh", None)]
    r = await authed.post("/api/android/open", json={})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["status"]["active"] is True
    r = await authed.post("/api/android/close")
    assert r.status_code == 200 and r.json()["closed"] is True and r.json()["active"] is False
    r = await authed.post("/api/android/open", json={"app": "aosu"})
    assert r.status_code == 200
    assert [c for c in dctx.android.calls if c[0] != "refresh"] == [
        ("open", ("dashboard", None)), ("close", "closed on the dashboard"), ("open", ("dashboard", "aosu"))]
    for bad in ({"app": "x" * 61}, {"app": "aosu", "x": 1}):
        assert (await authed.post("/api/android/open", json=bad)).status_code in (400, 422), bad


async def test_the_switch(authed: Any, dctx: Any) -> None:
    dctx.android = FakeAndroid(dctx)
    r = await authed.put("/api/android/settings", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False and dctx.settings.get("android.enabled") is False
    assert (await authed.put("/api/android/settings", json={})).status_code in (400, 422)


async def test_parents_only(client: Any, dctx: Any) -> None:
    dctx.android = FakeAndroid(dctx)
    assert (await client.get("/api/android")).status_code in (401, 403)
    assert (await client.post("/api/android/open", json={})).status_code in (401, 403)
    assert (await client.post("/api/android/close")).status_code in (401, 403)
    assert (await client.put("/api/android/settings", json={"enabled": False})).status_code in (401, 403)
    assert dctx.android.calls == [] and dctx.settings.get("android.enabled") is True


async def test_csrf_is_needed(authed: Any, dctx: Any) -> None:
    dctx.android = FakeAndroid(dctx)
    token = authed.headers.pop("X-CSRF-Token")
    assert (await authed.post("/api/android/open", json={})).status_code == 403
    assert dctx.android.calls == []
    authed.headers["X-CSRF-Token"] = token
    assert (await authed.post("/api/android/open", json={})).status_code == 200


async def test_the_assistant_page_has_the_card(authed: Any) -> None:
    js = await authed.get("/static/js/pages/assistant.js")
    assert js.status_code == 200 and "androidPanel" in js.text
    r = await authed.get("/static/js/pages/android_panel.js")
    assert r.status_code == 200
    for words in ("Open Android", "Close Android", "/android/open", "/android/close", "/android/settings",
                  "Copy the command", "memory"):
        assert words in r.text, words
