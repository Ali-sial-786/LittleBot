"""Dashboard: info cards (``/api/screens``): status, close, show a card to try it, the parent's switches."""

from __future__ import annotations

from typing import Any

import pytest

from littlebot.screens import base
from littlebot.screens.base import Card, CardType

KIND = "test_dash_card"


class _Card(Card):
    kind = KIND

    def draw(self, surf: Any) -> None:
        surf.fill((255, 255, 255))


async def _build(spec: dict[str, Any], ctx: Any, host: Any) -> Card:
    return _Card(spec, host)


@pytest.fixture
def screens(dctx):
    from littlebot.screens.service import ScreenService

    base.card_types()
    base.register(CardType(kind=KIND, description="test", fields={"title": {"type": "string"}}, build=_build,
                           required=("title",)))
    svc = ScreenService(dctx)
    dctx.screens = svc
    yield svc
    svc.detach()
    base._TYPES.pop(KIND, None)


async def test_status_without_the_service(authed):
    d = (await authed.get("/api/screens")).json()
    assert d["available"] is False and d["enabled"] is True and d["web_pictures"] is True and d["active"] is False
    assert (await authed.post("/api/screens/close")).status_code == 503
    assert (await authed.post("/api/screens/show", json={"spec": {"card": KIND}})).status_code == 503


async def test_show_status_and_close(authed, screens):
    r = await authed.post("/api/screens/show", json={"spec": {"card": KIND, "title": "Hello"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["showing"] == "Hello" and body["active"] and body["source"] == "dashboard"
    d = (await authed.get("/api/screens")).json()
    assert d["active"] and d["card"]["kind"] == KIND and d["closes_in"] == 30.0
    r = await authed.post("/api/screens/close")
    assert r.status_code == 200 and r.json()["closed"] is True and r.json()["active"] is False
    assert screens.status()["last"]["reason"] == "parent"
    assert (await authed.post("/api/screens/close")).json()["closed"] is False


async def test_show_errors(authed, screens, dctx):
    r = await authed.post("/api/screens/show", json={"spec": {"card": "no_such_card"}})
    assert r.status_code == 422 and "couldn't be made" in r.json()["detail"]
    r = await authed.post("/api/screens/show", json={"spec": {"card": KIND, "title": "x" * 5000}})
    assert r.status_code == 413
    r = await authed.post("/api/screens/show", json={})
    assert r.status_code in (400, 422)
    dctx.settings.set("screens.enabled", False)
    r = await authed.post("/api/screens/show", json={"spec": {"card": KIND, "title": "Hello"}})
    assert r.status_code == 409 and "switched off" in r.json()["detail"]


async def test_parent_switches(authed, screens, dctx):
    r = await authed.put("/api/screens/settings", json={"web_pictures": False})
    assert r.status_code == 200 and r.json()["web_pictures"] is False and r.json()["enabled"] is True
    assert dctx.settings.get("screens.web_pictures") is False
    screens.show_card(_Card({"title": "x"}))
    r = await authed.put("/api/screens/settings", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert dctx.settings.get("screens.enabled") is False and not screens.active  # switching off closes the card
    for bad in ({}, {"enabled": "maybe"}, {"colour": "pink"}):
        assert (await authed.put("/api/screens/settings", json=bad)).status_code in (400, 422), bad


async def test_routes_need_the_parent(client, screens, dctx):
    assert (await client.get("/api/screens")).status_code == 401
    assert (await client.post("/api/screens/close")).status_code == 401
    r = await client.post("/api/screens/show", json={"spec": {"card": KIND, "title": "Hello"}})
    assert r.status_code == 401 and not screens.active
    assert (await client.put("/api/screens/settings", json={"enabled": False})).status_code == 401
    assert dctx.settings.get("screens.enabled") is True


async def test_privacy_page_has_the_switches(authed):
    js = (await authed.get("/static/js/pages/privacy.js")).text
    assert "Show pictures from the internet on LittleBot" in js and "strict safe search" in js
    assert "Info cards on the screen during voice chats" in js and "/screens/settings" in js
