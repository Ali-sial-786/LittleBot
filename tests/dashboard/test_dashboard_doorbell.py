"""The doorbell hook (POST /hooks/doorbell/{secret}, no login) and the dashboard's /api/doorbell (behind the login):
right and wrong secrets, body size, JSON / form / text bodies, a new address, the test ring, the settings."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from littlebot.skills.base import ANNOUNCE
from littlebot.skills.doorbell import Doorbell, hook_token
from littlebot.skills.hub import SkillHub

from dashboard_fakes import make_client


@pytest.fixture
def said(dctx) -> list[dict[str, Any]]:
    dctx.skills = SkillHub(dctx, [Doorbell(dctx)])
    dctx.settings.set("heads_up.quiet_hours", "")  # whatever the time the tests run
    got: list[dict[str, Any]] = []
    dctx.bus.subscribe(ANNOUNCE, lambda e: got.append(dict(e.data)))
    return got


async def test_the_hook_with_the_right_secret_says_it(client, dctx, said, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    token = hook_token(dctx)
    r = await client.post(f"/hooks/doorbell/{token}", json={"title": "aosu", "text": "Someone is at your Front Door"})
    assert r.status_code == 200 and r.json() == {"ok": True, "said": True}
    assert said[-1]["text"] == "Someone's at the front door."
    assert r.headers["cache-control"] == "no-store"
    # (the test's own client, httpx, logs the address it calls: LittleBot's side never does)
    logged = " ".join(rec.getMessage() for rec in caplog.records if not rec.name.startswith(("httpx", "httpcore")))
    assert token not in logged and "Someone is at" not in logged and "person at Front Door" in logged


@pytest.mark.parametrize("token", ["wrong", "x" * 43, ""])
async def test_a_wrong_secret_is_just_not_found(client, dctx, said, token) -> None:
    good = hook_token(dctx)
    r = await client.post(f"/hooks/doorbell/{token}", json={"event": "doorbell"})
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"} and good not in r.text
    assert said == []
    nowhere = await client.post("/hooks/nothing-here", json={})
    assert nowhere.status_code == 404 and nowhere.json() == r.json()  # the same as a page that doesn't exist


async def test_no_secret_yet_and_switched_off(client, dctx, said) -> None:
    r = await client.post("/hooks/doorbell/" + "a" * 43, json={"event": "doorbell"})
    assert r.status_code == 404  # no address has been made yet
    token = hook_token(dctx)
    dctx.skills = SkillHub(dctx, [])
    r = await client.post(f"/hooks/doorbell/{token}", json={"event": "doorbell"})
    assert r.status_code == 404 and said == []


async def test_only_post(client, dctx, said) -> None:
    r = await client.get(f"/hooks/doorbell/{hook_token(dctx)}")
    assert r.status_code in (404, 405) and said == []


async def test_bodies_json_form_and_text(client, dctx, said) -> None:
    url = f"/hooks/doorbell/{hook_token(dctx)}"
    r = await client.post(url, data={"title": "aosu", "text": "Your Doorbell is ringing"})
    assert r.json()["said"] and said[-1]["text"] == "Someone rang the doorbell."
    r = await client.post(url, content=b"A package was detected at Front Door",
                          headers={"Content-Type": "text/plain"})
    assert r.json()["said"] and said[-1]["text"] == "A package was left at the front door."
    r = await client.post(url, content=b'{"event": "person", "camera": "Backyard"}',
                          headers={"Content-Type": "text/plain; charset=utf-8"})  # JSON sent as text (MacroDroid)
    assert r.json()["said"] and said[-1]["text"] == "The backyard camera saw someone."
    r = await client.post(url, content=b"", headers={"Content-Type": "application/json"})
    assert r.status_code == 200 and r.json()["said"] is False  # nothing known: "other" is not said
    r = await client.post(url, content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 200


async def test_the_body_is_small(client, dctx, said) -> None:
    url = f"/hooks/doorbell/{hook_token(dctx)}"
    r = await client.post(url, content=b"x" * 5000, headers={"Content-Type": "text/plain"})
    assert r.status_code == 413 and said == []
    r = await client.post(url, content=b"Someone rang " + b"x" * 3000, headers={"Content-Type": "text/plain"})
    assert r.status_code == 200


async def test_the_host_allowlist_still_applies(client, dctx, said) -> None:
    r = await client.post(f"/hooks/doorbell/{hook_token(dctx)}", json={"event": "doorbell"},
                          headers={"Host": "evil.example.com"})
    assert r.status_code == 400 and said == []


async def test_the_dashboard_needs_the_login(client, dctx, said) -> None:
    for method, path in (("GET", "/api/doorbell"), ("POST", "/api/doorbell/test"), ("POST", "/api/doorbell/rotate"),
                         ("PUT", "/api/doorbell/settings")):
        r = await client.request(method, path, json={})
        assert r.status_code == 401, path
    assert said == []


async def test_address_test_rotate_and_settings(authed, app, dctx, said) -> None:
    r = await authed.get("/api/doorbell")
    body = r.json()
    token = hook_token(dctx)
    assert r.status_code == 200 and body["url"].endswith(f"/hooks/doorbell/{token}")
    assert body["reachable"] is False and "lan_access" in body["lan_help"]  # LAN access is off in the tests
    assert body["events"] == ["doorbell", "person", "package"] and body["at_night"] is True
    r = await authed.post("/api/doorbell/test")
    assert r.json() == {"ok": True, "said": True, "why": None}
    assert said[-1]["text"].startswith("This is a test.")
    r = await authed.post("/api/doorbell/rotate")
    new = hook_token(dctx)
    assert new != token and r.json()["url"].endswith(new)
    async with make_client(app) as phone:  # no login, like a phone
        assert (await phone.post(f"/hooks/doorbell/{token}", json={"event": "doorbell"})).status_code == 404
        assert (await phone.post(f"/hooks/doorbell/{new}", json={"event": "doorbell"})).status_code == 200
    r = await authed.put("/api/doorbell/settings", json={"events": ["doorbell", "motion"], "at_night": False})
    assert r.status_code == 200 and r.json()["events"] == ["doorbell", "motion"] and r.json()["at_night"] is False
    assert dctx.settings.get("doorbell.events") == ["doorbell", "motion"]
    r = await authed.put("/api/doorbell/settings", json={"events": ["fireworks"]})
    assert r.status_code == 422
    r = await authed.put("/api/doorbell/settings", json={"enabled": False})
    assert r.json()["enabled"] is False
    assert (await authed.post("/api/doorbell/test")).json()["why"] == "off"


async def test_lan_urls_come_first(authed, app, dctx, said) -> None:
    dctx.config.dashboard.lan_access = True
    app.state.dash.urls = ["http://192.168.1.20:8765/", "http://littlebot.local:8765/", "http://127.0.0.1:8765/"]
    body = (await authed.get("/api/doorbell")).json()
    token = hook_token(dctx)
    assert body["url"] == f"http://192.168.1.20:8765/hooks/doorbell/{token}" and body["reachable"] is True
    assert body["urls"][-1].startswith("http://127.0.0.1:8765/") and body["lan_help"] is None
