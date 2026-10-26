"""Dashboard: the browser view (``/api/browser``): what it shows, Close, the parent's switch, parent-only."""

from __future__ import annotations

from typing import Any


class FakeBrowser:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.active = True
        self.closed: list[str] = []

    def status(self) -> dict[str, Any]:
        return {"available": True, "enabled": bool(self.ctx.settings.get("browser.enabled", True)), "detail": None,
                "active": self.active, "showing": "Google results for “giraffes”" if self.active else None}

    def close(self, reason: str) -> bool:
        self.closed.append(reason)
        was, self.active = self.active, False
        return was

    forgot = 0
    saved = True

    async def forget(self) -> bool:
        self.forgot += 1
        was, self.saved = self.saved, False
        return was


async def test_status_without_the_service(authed: Any) -> None:
    d = (await authed.get("/api/browser")).json()
    assert d["available"] is False and d["enabled"] is True and d["active"] is False
    assert (await authed.post("/api/browser/close")).status_code == 503


async def test_status_and_close(authed: Any, dctx: Any) -> None:
    dctx.browser = FakeBrowser(dctx)
    d = (await authed.get("/api/browser")).json()
    assert d["active"] is True and d["showing"] == "Google results for “giraffes”"
    r = await authed.post("/api/browser/close")
    assert r.status_code == 200 and r.json()["closed"] is True and r.json()["active"] is False
    assert dctx.browser.closed == ["parent"]
    assert (await authed.post("/api/browser/close")).json()["closed"] is False


async def test_the_parents_switch(authed: Any, dctx: Any) -> None:
    dctx.browser = FakeBrowser(dctx)
    r = await authed.put("/api/browser/settings", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert dctx.settings.get("browser.enabled") is False
    r = await authed.put("/api/browser/settings", json={"enabled": True})
    assert r.json()["enabled"] is True and dctx.settings.get("browser.enabled") is True
    for bad in ({}, {"enabled": "maybe"}, {"colour": "pink"}):
        assert (await authed.put("/api/browser/settings", json=bad)).status_code in (400, 422), bad


async def test_remembering_google_and_forgetting_it(authed: Any, dctx: Any) -> None:
    dctx.browser = FakeBrowser(dctx)
    assert dctx.settings.get("browser.keep_signins") is False  # off by default
    r = await authed.put("/api/browser/settings", json={"keep_signins": True})
    assert r.status_code == 200 and dctx.settings.get("browser.keep_signins") is True and dctx.browser.forgot == 0
    r = await authed.post("/api/browser/forget")
    assert r.status_code == 200 and r.json()["forgot"] is True and dctx.browser.forgot == 1
    assert (await authed.post("/api/browser/forget")).json()["forgot"] is False
    r = await authed.put("/api/browser/settings", json={"keep_signins": False})  # off: what was kept goes
    assert dctx.settings.get("browser.keep_signins") is False and dctx.browser.forgot == 3


async def test_parents_only(client: Any, dctx: Any) -> None:
    dctx.browser = FakeBrowser(dctx)
    assert (await client.get("/api/browser")).status_code in (401, 403)
    assert (await client.post("/api/browser/close")).status_code in (401, 403)
    assert (await client.put("/api/browser/settings", json={"enabled": False})).status_code in (401, 403)
    assert (await client.post("/api/browser/forget")).status_code in (401, 403)
    assert dctx.browser.closed == [] and dctx.settings.get("browser.enabled") is True


async def test_the_privacy_page_has_the_switch(authed: Any) -> None:
    r = await authed.get("/static/js/pages/privacy.js")
    assert r.status_code == 200
    assert "Allow the browser view (Google with SafeSearch, grown-ups only)" in r.text
    assert "/browser/close" in r.text
    assert "Let the browser remember Google" in r.text and "/browser/forget" in r.text
