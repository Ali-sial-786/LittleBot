"""Dashboard: phone mirroring (``/api/mirror``): status, start, stop, pairing (checked input), forget, the switch,
parents only, CSRF, and the card on the Assistant page."""

from __future__ import annotations

from typing import Any

import pytest

from littlebot.mirror import adb as A


class FakeMirror:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.calls: list[tuple[str, Any]] = []
        self.active = False

    def status(self) -> dict[str, Any]:
        return {"available": True, "enabled": bool(self.ctx.settings.get("mirror.enabled", True)),
                "active": self.active, "kind": "iphone" if self.active else None, "android_phones": [],
                "kinds": {"iphone": {"installed": True, "install": None},
                          "android": {"installed": False, "install": "sudo apt install adb"}}}

    async def start(self, kind: str, *, source: str = "voice") -> dict[str, Any]:
        self.calls.append(("start", (kind, source)))
        self.active = True
        return {"ok": True, "kind": kind, "state": "waiting", "error": None, "detail": "", "already": False}

    async def stop(self, reason: str) -> bool:
        self.calls.append(("stop", reason))
        was, self.active = self.active, False
        return was

    async def pair(self, address: str, code: str, *, name: str = "") -> dict[str, Any]:
        # the real service checks again; the route must have refused anything else already
        assert A.check_address(address) and A.check_code(code)
        self.calls.append(("pair", (address, code, name)))
        return {"ok": True, "error": None, "detail": "", "guid": "adb-x", "connected": True}

    async def forget(self, key: str) -> bool:
        self.calls.append(("forget", key))
        return key == "adb-x"


async def test_without_the_service(authed: Any) -> None:
    d = (await authed.get("/api/mirror")).json()
    assert d["available"] is False and d["active"] is False
    assert (await authed.post("/api/mirror/start", json={"phone": "iphone"})).status_code == 503
    assert (await authed.post("/api/mirror/stop")).status_code == 503
    r = await authed.post("/api/mirror/android/pair", json={"address": "192.168.1.23:37099", "code": "123456"})
    assert r.status_code == 503


async def test_start_and_stop(authed: Any, dctx: Any) -> None:
    dctx.mirror = FakeMirror(dctx)
    r = await authed.post("/api/mirror/start", json={"phone": "iphone"})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["status"]["active"] is True
    assert dctx.mirror.calls == [("start", ("iphone", "dashboard"))]
    r = await authed.post("/api/mirror/stop")
    assert r.status_code == 200 and r.json()["stopped"] is True and r.json()["active"] is False
    assert (await authed.post("/api/mirror/stop")).json()["stopped"] is False
    for bad in ({}, {"phone": "nokia"}, {"phone": "iphone", "x": 1}):
        assert (await authed.post("/api/mirror/start", json=bad)).status_code in (400, 422), bad
    d = (await authed.get("/api/mirror")).json()
    assert d["kinds"]["android"]["install"] == "sudo apt install adb"


async def test_pairing(authed: Any, dctx: Any) -> None:
    dctx.mirror = FakeMirror(dctx)
    r = await authed.post("/api/mirror/android/pair",
                          json={"address": "192.168.1.23:37099", "code": "123456", "name": "Ali's phone"})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["connected"] is True
    assert dctx.mirror.calls == [("pair", ("192.168.1.23:37099", "123456", "Ali's phone"))]


@pytest.mark.parametrize("body", [
    {"address": "192.168.1.23", "code": "123456"},
    {"address": "192.168.1.23:37099", "code": "12345"},
    {"address": "192.168.1.23:37099", "code": "abcdef"},
    {"address": "1.2.3.4:5;reboot", "code": "123456"},
    {"address": "$(id):12345", "code": "123456"},
    {"address": "192.168.1.23:37099 -t", "code": "123456"},
    {"address": "300.168.1.23:37099", "code": "123456"},
    {"address": "192.168.1.23:37099", "code": "123456", "extra": "x"},
    {"address": "192.168.1.23:37099"},
])
async def test_pairing_input_is_checked(authed: Any, dctx: Any, body: dict[str, Any]) -> None:
    dctx.mirror = FakeMirror(dctx)
    r = await authed.post("/api/mirror/android/pair", json=body)
    assert r.status_code in (400, 422), body
    assert dctx.mirror.calls == []


async def test_forget_and_the_switch(authed: Any, dctx: Any) -> None:
    dctx.mirror = FakeMirror(dctx)
    r = await authed.post("/api/mirror/android/forget", json={"key": "adb-x"})
    assert r.status_code == 200 and r.json()["forgot"] is True
    assert (await authed.post("/api/mirror/android/forget", json={"key": "other"})).json()["forgot"] is False
    r = await authed.put("/api/mirror/settings", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False and dctx.settings.get("mirror.enabled") is False
    assert (await authed.put("/api/mirror/settings", json={})).status_code in (400, 422)


async def test_parents_only(client: Any, dctx: Any) -> None:
    dctx.mirror = FakeMirror(dctx)
    assert (await client.get("/api/mirror")).status_code in (401, 403)
    assert (await client.post("/api/mirror/start", json={"phone": "iphone"})).status_code in (401, 403)
    assert (await client.post("/api/mirror/stop")).status_code in (401, 403)
    r = await client.post("/api/mirror/android/pair", json={"address": "192.168.1.23:37099", "code": "123456"})
    assert r.status_code in (401, 403)
    assert (await client.put("/api/mirror/settings", json={"enabled": False})).status_code in (401, 403)
    assert dctx.mirror.calls == [] and dctx.settings.get("mirror.enabled") is True


async def test_csrf_is_needed(authed: Any, dctx: Any) -> None:
    dctx.mirror = FakeMirror(dctx)
    token = authed.headers.pop("X-CSRF-Token")
    r = await authed.post("/api/mirror/start", json={"phone": "iphone"})
    assert r.status_code == 403
    r = await authed.post("/api/mirror/android/pair", json={"address": "192.168.1.23:37099", "code": "123456"})
    assert r.status_code == 403 and dctx.mirror.calls == []
    authed.headers["X-CSRF-Token"] = token
    assert (await authed.post("/api/mirror/start", json={"phone": "iphone"})).status_code == 200


async def test_the_assistant_page_has_the_card(authed: Any) -> None:
    js = await authed.get("/static/js/pages/assistant.js")
    assert js.status_code == 200 and "mirrorPanel" in js.text
    r = await authed.get("/static/js/pages/mirror_panel.js")
    assert r.status_code == 200
    for words in ("Mirror an iPhone", "Mirror an Android", "Pair device with pairing code", "Control Center",
                  "/mirror/android/pair", "/mirror/stop", "Copy the command"):
        assert words in r.text, words
