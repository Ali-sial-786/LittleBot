"""Dashboard: the Nanit baby camera (``/api/nanit``): status, show, close, the switches, parents only, CSRF, 503
without the service, no secret in any response, and the card on the Assistant page."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from littlebot.nanit.service import NanitService

SECRET = "refresh-token-must-never-show"


class FakeNanit:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.calls: list[tuple[str, Any]] = []
        self.active = False
        self.refreshed = 0

    async def refresh(self, force: bool = False) -> str:
        self.refreshed += 1
        return "active"

    def status(self) -> dict[str, Any]:
        s = self.ctx.settings
        return {"available": True, "enabled": bool(s.get("nanit.enabled")), "sound": bool(s.get("nanit.sound")),
                "set_up": True, "bridge": "active", "bridge_running": True, "active": self.active,
                "camera": "Nursery" if self.active else None, "cameras": [{"name": "Nursery"}]}

    async def show(self, camera: Any = None, *, source: str = "voice") -> dict[str, Any]:
        self.calls.append(("show", (camera, source)))
        self.active = True
        return {"ok": True, "camera": "Nursery", "state": "starting", "error": None, "detail": "", "already": False}

    async def close(self, reason: str) -> bool:
        self.calls.append(("close", reason))
        was, self.active = self.active, False
        return was


async def test_without_the_service(authed: Any) -> None:
    d = (await authed.get("/api/nanit")).json()
    assert d["available"] is False and d["active"] is False and d["setup_command"].endswith("setup_nanit.sh")
    assert (await authed.post("/api/nanit/show", json={})).status_code == 503
    assert (await authed.post("/api/nanit/close")).status_code == 503


async def test_show_and_close(authed: Any, dctx: Any) -> None:
    dctx.nanit = FakeNanit(dctx)
    r = await authed.post("/api/nanit/show", json={"camera": "Nursery"})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["status"]["active"] is True
    assert dctx.nanit.calls == [("show", ("Nursery", "dashboard"))]
    assert (await authed.post("/api/nanit/show", json={})).status_code == 200
    assert dctx.nanit.calls[-1] == ("show", (None, "dashboard"))
    r = await authed.post("/api/nanit/close")
    assert r.status_code == 200 and r.json()["closed"] is True and r.json()["active"] is False
    assert (await authed.post("/api/nanit/close")).json()["closed"] is False
    for bad in ({"camera": "x" * 41}, {"camera": "Nursery", "x": 1}, {"camera": 5}):
        assert (await authed.post("/api/nanit/show", json=bad)).status_code in (400, 422), bad
    assert (await authed.get("/api/nanit")).json()["cameras"] == [{"name": "Nursery"}] and dctx.nanit.refreshed


async def test_the_switches(authed: Any, dctx: Any) -> None:
    dctx.nanit = FakeNanit(dctx)
    r = await authed.put("/api/nanit/settings", json={"sound": False})
    assert r.status_code == 200 and r.json()["sound"] is False and dctx.settings.get("nanit.sound") is False
    r = await authed.put("/api/nanit/settings", json={"enabled": False})
    assert r.json()["enabled"] is False and dctx.settings.get("nanit.enabled") is False
    assert (await authed.put("/api/nanit/settings", json={})).status_code in (400, 422)
    assert (await authed.put("/api/nanit/settings", json={"enabled": "maybe"})).status_code in (400, 422)


async def test_parents_only(client: Any, dctx: Any) -> None:
    dctx.nanit = FakeNanit(dctx)
    assert (await client.get("/api/nanit")).status_code in (401, 403)
    assert (await client.post("/api/nanit/show", json={})).status_code in (401, 403)
    assert (await client.post("/api/nanit/close")).status_code in (401, 403)
    assert (await client.put("/api/nanit/settings", json={"enabled": False})).status_code in (401, 403)
    assert dctx.nanit.calls == [] and dctx.settings.get("nanit.enabled") is True


async def test_csrf_is_needed(authed: Any, dctx: Any) -> None:
    dctx.nanit = FakeNanit(dctx)
    token = authed.headers.pop("X-CSRF-Token")
    assert (await authed.post("/api/nanit/show", json={})).status_code == 403
    assert (await authed.put("/api/nanit/settings", json={"sound": False})).status_code == 403
    assert dctx.nanit.calls == [] and dctx.settings.get("nanit.sound") is True
    authed.headers["X-CSRF-Token"] = token
    assert (await authed.post("/api/nanit/show", json={})).status_code == 200


async def test_no_secret_in_any_response(authed: Any, dctx: Any, tmp_path: Path) -> None:
    # The real service over a bridge folder whose session file holds a (pretend) token.
    folder = tmp_path / "nanit"
    folder.mkdir()
    (folder / "session.json").write_text(json.dumps({"refreshToken": SECRET, "authToken": SECRET}))
    (folder / "cameras.json").write_text(json.dumps({"cameras": [{"uid": "a1b2c3", "name": "Nursery"}]}))
    dctx.nanit = NanitService(dctx, screen=True, data_dir=folder, unit_dir=tmp_path / "units")
    texts = [(await authed.get("/api/nanit")).text, (await authed.post("/api/nanit/show", json={})).text,
             (await authed.post("/api/nanit/close")).text,
             (await authed.put("/api/nanit/settings", json={"sound": True})).text]
    for text in texts:
        assert SECRET not in text and "refreshToken" not in text and "a1b2c3" not in text, text
    d = json.loads(texts[0])
    assert d["set_up"] is False and d["setup"]["signed_in"] is True and d["cameras"] == [{"name": "Nursery"}]
    assert json.loads(texts[1])["error"] == "not_set_up"


async def test_the_assistant_page_has_the_card(authed: Any) -> None:
    js = await authed.get("/static/js/pages/assistant.js")
    assert js.status_code == 200 and "nanitPanel" in js.text
    r = await authed.get("/static/js/pages/nanit_panel.js")
    assert r.status_code == 200
    for words in ("/nanit/show", "/nanit/close", "/nanit/settings", "setup_nanit.sh", "Unofficial",
                  "cloud stream", "Copy the command", "sign in to Nanit"):
        assert words in r.text, words
    assert "session.json" not in r.text and "token" not in r.text.lower()
