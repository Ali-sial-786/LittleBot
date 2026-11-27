"""Dashboard: WhatsApp calls (``/api/calls``): status, open (WhatsApp, a call, to answer), close, the switches (calls
allowed; the chats hidden), the list of people to call, signing out, parents only, CSRF, and the card on the
Assistant page."""

from __future__ import annotations

from typing import Any

from littlebot.calls import contacts as C


class FakeCalls:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.log: list[tuple[str, Any]] = []
        self.active = False
        self.profile = True

    def status(self) -> dict[str, Any]:
        s = self.ctx.settings
        return {"available": True, "enabled": bool(s.get("calls.enabled", True)),
                "hide_chats": bool(s.get("calls.hide_chats", True)), "active": self.active,
                "state": "ready" if self.active else None, "call": "none", "linked": True if self.active else None,
                "profile": self.profile, "memory_free_mb": 1800, "min_free_mb": 700,
                "contacts": [{"name": k, "whatsapp": v} for k, v in (s.get("calls.contacts") or {}).items()]}

    async def open(self, **kw: Any) -> dict[str, Any]:
        self.log.append(("open", kw))
        if kw.get("contact") == "Sam":
            return {"ok": False, "state": None, "error": "unknown_contact", "detail": "You can call Dad.",
                    "already": False, "contact": None}
        self.active = True
        return {"ok": True, "state": "loading", "error": None, "detail": "", "already": False,
                "contact": kw.get("contact")}

    async def hang_up(self, reason: str) -> dict[str, Any]:
        self.log.append(("hang_up", reason))
        was, self.active = self.active, False
        return {"ok": True, "was_call": False, "closed": was}

    def set_contacts(self, raw: Any, *, source: str = "dashboard") -> dict[str, str]:
        cleaned = C.clean(raw)
        self.ctx.settings.set("calls.contacts", cleaned, source=source)
        return cleaned

    async def forget(self) -> bool:
        self.log.append(("forget", None))
        was, self.profile, self.active = self.profile, False, False
        return was


async def test_without_the_service(authed: Any, dctx: Any) -> None:
    d = (await authed.get("/api/calls")).json()
    assert d["available"] is False and d["active"] is False and d["hide_chats"] is True and d["contacts"] == []
    assert (await authed.post("/api/calls/open", json={})).status_code == 503
    assert (await authed.post("/api/calls/close")).status_code == 503
    assert (await authed.post("/api/calls/signout")).status_code == 503
    # the list and the switches are settings: they work all the same
    r = await authed.put("/api/calls/contacts", json={"contacts": [{"name": "Dad", "whatsapp": "Ali Khan"}]})
    assert r.status_code == 200 and dctx.settings.get("calls.contacts") == {"Dad": "Ali Khan"}


async def test_open_call_and_close(authed: Any, dctx: Any) -> None:
    dctx.calls = FakeCalls(dctx)
    d = (await authed.get("/api/calls")).json()
    assert d["available"] is True and d["memory_free_mb"] == 1800 and d["profile"] is True
    r = await authed.post("/api/calls/open", json={})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["status"]["active"] is True
    r = await authed.post("/api/calls/close")
    assert r.status_code == 200 and r.json()["closed"] is True and r.json()["active"] is False
    r = await authed.post("/api/calls/open", json={"contact": "Dad", "kind": "voice"})
    assert r.status_code == 200 and r.json()["contact"] == "Dad"
    r = await authed.post("/api/calls/open", json={"answer": True})
    assert r.status_code == 200
    r = await authed.post("/api/calls/open", json={"contact": "Sam"})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["detail"] == "You can call Dad."
    assert dctx.calls.log == [
        ("open", {"source": "dashboard", "contact": None, "kind": "video", "answer": False}),
        ("hang_up", "closed on the dashboard"),
        ("open", {"source": "dashboard", "contact": "Dad", "kind": "voice", "answer": False}),
        ("open", {"source": "dashboard", "contact": None, "kind": "video", "answer": True}),
        ("open", {"source": "dashboard", "contact": "Sam", "kind": "video", "answer": False})]
    for bad in ({"contact": "x" * 41}, {"kind": "fax"}, {"contact": "Dad", "x": 1}):
        assert (await authed.post("/api/calls/open", json=bad)).status_code in (400, 422), bad


async def test_the_switches(authed: Any, dctx: Any) -> None:
    dctx.calls = FakeCalls(dctx)
    assert dctx.settings.get("calls.hide_chats") is True  # the chats are hidden unless a grown-up says otherwise
    r = await authed.put("/api/calls/settings", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False and dctx.settings.get("calls.enabled") is False
    assert dctx.settings.get("calls.hide_chats") is True
    r = await authed.put("/api/calls/settings", json={"hide_chats": False})
    assert r.status_code == 200 and r.json()["hide_chats"] is False and dctx.settings.get("calls.hide_chats") is False
    assert dctx.settings.get("calls.enabled") is False
    assert (await authed.put("/api/calls/settings", json={})).status_code in (400, 422)
    assert (await authed.put("/api/calls/settings", json={"hide_chats": "maybe"})).status_code in (400, 422)


async def test_the_contacts_editor(authed: Any, dctx: Any) -> None:
    dctx.calls = FakeCalls(dctx)
    r = await authed.put("/api/calls/contacts", json={"contacts": [
        {"name": " Dad ", "whatsapp": "Ali Khan"}, {"name": "Mum", "whatsapp": ""}]})
    assert r.status_code == 200
    assert r.json()["contacts"] == [{"name": "Dad", "whatsapp": "Ali Khan"}, {"name": "Mum", "whatsapp": "Mum"}]
    assert dctx.settings.get("calls.contacts") == {"Dad": "Ali Khan", "Mum": "Mum"}
    r = await authed.put("/api/calls/contacts", json={"contacts": [{"name": "Dad"}, {"name": "dad"}]})
    assert r.status_code == 422 and "twice" in r.json()["detail"]
    assert dctx.settings.get("calls.contacts") == {"Dad": "Ali Khan", "Mum": "Mum"}  # unchanged
    for bad in ({"contacts": [{"name": "x" * 41}]}, {"contacts": [{"whatsapp": "Ali"}]}, {"contacts": "Dad"}, {}):
        assert (await authed.put("/api/calls/contacts", json=bad)).status_code in (400, 422), bad
    assert (await authed.put("/api/calls/contacts", json={"contacts": []})).status_code == 200
    assert dctx.settings.get("calls.contacts") == {}


async def test_sign_out_deletes_the_saved_profile(authed: Any, dctx: Any) -> None:
    dctx.calls = FakeCalls(dctx)
    r = await authed.post("/api/calls/signout")
    assert r.status_code == 200 and r.json()["removed"] is True and r.json()["profile"] is False
    assert (await authed.post("/api/calls/signout")).json()["removed"] is False
    assert [c[0] for c in dctx.calls.log] == ["forget", "forget"]


async def test_parents_only(client: Any, dctx: Any) -> None:
    dctx.calls = FakeCalls(dctx)
    assert (await client.get("/api/calls")).status_code in (401, 403)
    assert (await client.post("/api/calls/open", json={})).status_code in (401, 403)
    assert (await client.post("/api/calls/close")).status_code in (401, 403)
    assert (await client.post("/api/calls/signout")).status_code in (401, 403)
    assert (await client.put("/api/calls/settings", json={"hide_chats": False})).status_code in (401, 403)
    assert (await client.put("/api/calls/contacts", json={"contacts": []})).status_code in (401, 403)
    assert dctx.calls.log == [] and dctx.settings.get("calls.hide_chats") is True


async def test_csrf_is_needed(authed: Any, dctx: Any) -> None:
    dctx.calls = FakeCalls(dctx)
    token = authed.headers.pop("X-CSRF-Token")
    assert (await authed.post("/api/calls/open", json={})).status_code == 403
    assert (await authed.put("/api/calls/settings", json={"hide_chats": False})).status_code == 403
    assert (await authed.post("/api/calls/signout")).status_code == 403
    assert dctx.calls.log == [] and dctx.settings.get("calls.hide_chats") is True
    authed.headers["X-CSRF-Token"] = token
    assert (await authed.post("/api/calls/open", json={})).status_code == 200


async def test_the_real_service_in_a_test_never_launches(authed: Any, dctx: Any) -> None:
    from littlebot.calls import create_service

    dctx.calls = create_service(dctx)
    d = (await authed.get("/api/calls")).json()
    assert d["available"] is False and d["active"] is False
    r = await authed.post("/api/calls/open", json={})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["error"] == "unavailable"
    assert (await authed.post("/api/calls/signout")).json()["removed"] is False


async def test_the_assistant_page_has_the_card(authed: Any) -> None:
    js = await authed.get("/static/js/pages/assistant.js")
    assert js.status_code == 200 and "callsPanel" in js.text
    assert js.text.index("android.el") < js.text.index("calls.el")  # next to Android
    r = await authed.get("/static/js/pages/calls_panel.js")
    assert r.status_code == 200
    for words in ("Open WhatsApp", "Close WhatsApp", "/calls/open", "/calls/close", "/calls/settings",
                  "/calls/contacts", "/calls/signout", "Sign out of WhatsApp on the robot", "Hide my chats on the robot",
                  "Who can be called", "memory", "Not tried on the robot yet"):
        assert words in r.text, words
