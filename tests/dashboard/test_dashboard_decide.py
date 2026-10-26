"""Dashboard: the fast decisions panel (littlebot/decide): the provider, each feature's mode, the key never shown."""

from __future__ import annotations

from pydantic import SecretStr


async def test_decide_status_and_settings(authed, dctx):
    dctx.config.typesafe_api_key = SecretStr("ts-secret-key-123")
    r = await authed.get("/api/decide")
    assert r.status_code == 200
    d = r.json()
    assert d["provider"] == "off" and d["why_off"] == "the provider is off" and d["has_typesafe_key"] is True
    assert set(d["features"]) == {"cards", "routing", "answered", "browser", "naming", "videos", "names", "concern",
                                  "addressed"}
    assert "ts-secret-key-123" not in r.text
    r = await authed.put("/api/decide/settings", json={"provider": "jev", "modes": {"cards": "on"}})
    assert r.status_code == 200
    d = r.json()
    assert d["provider"] == "jev" and d["features"]["cards"]["chosen"] == "on"
    assert d["features"]["routing"]["chosen"] == "shadow"  # the default
    r = await authed.put("/api/decide/settings", json={"modes": {"routing": "off"}})
    assert r.json()["features"]["cards"]["chosen"] == "on" and r.json()["features"]["routing"]["chosen"] == "off"
    assert r.json()["log_context"] is True
    r = await authed.put("/api/decide/settings", json={"log_context": False})
    assert r.status_code == 200 and r.json()["log_context"] is False


async def test_bad_settings_are_refused(authed):
    assert (await authed.put("/api/decide/settings", json={})).status_code == 422
    assert (await authed.put("/api/decide/settings", json={"provider": "gpt"})).status_code == 422
    assert (await authed.put("/api/decide/settings", json={"modes": {"cards": "maybe"}})).status_code == 422
    assert (await authed.put("/api/decide/settings", json={"modes": {"made_up": "on"}})).status_code == 422


async def test_it_needs_a_login(client):
    assert (await client.get("/api/decide")).status_code == 401
