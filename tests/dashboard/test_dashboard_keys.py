"""Dashboard: the Keys page's API (/api/keys): list, save (checked first), check, remove; a refused key isn't saved;
a key never appears in a response or a log line; login and the CSRF token are still needed."""

from __future__ import annotations

import logging

import httpx
import pytest

from littlebot.core import keys

KEY = "fake-cartesia-key-DASHBOARD-TEST-7f3a"
ENV_KEY = "fake-cartesia-key-ENVFILE-TEST-91b2"


class _Service:
    def __init__(self) -> None:
        self.status = 200
        self.body = "{}"
        self.error: Exception | None = None
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, text=self.body)


@pytest.fixture
def service(dctx, tmp_path, monkeypatch) -> _Service:
    dctx.config.data_dir = str(tmp_path)
    dctx.privacy.set(cloud_enabled=True)
    monkeypatch.setattr(keys, "ENV_FILE", tmp_path / "test.env")  # never the real .env
    for s in keys.KEYS:
        monkeypatch.delenv(s.env, raising=False)
    svc = _Service()
    monkeypatch.setattr(keys, "check_transport", httpx.MockTransport(svc))
    return svc


async def test_list_shows_every_key_without_the_keys(authed, dctx, service):
    r = await authed.get("/api/keys")
    assert r.status_code == 200
    d = r.json()
    rows = {k["name"]: k for k in d["keys"]}
    assert set(rows) == {"anthropic", "openai", "deepgram", "cartesia", "elevenlabs", "home_assistant", "typesafe"}
    assert rows["anthropic"]["set"] and rows["anthropic"]["source"] == "env"
    assert rows["anthropic"]["ends_with"] == dctx.config.secret("anthropic_api_key")[-4:]
    assert rows["cartesia"] == {**rows["cartesia"], "set": False, "source": None, "ends_with": None,
                                "saved_here": False}
    assert rows["typesafe"]["checkable"] is False and rows["cartesia"]["get_url"].startswith("https://")
    assert dctx.config.secret("anthropic_api_key") not in r.text and dctx.config.secret("openai_api_key") not in r.text


async def test_save_check_and_remove(authed, dctx, service, tmp_path, caplog):
    (tmp_path / "test.env").write_text(f"CARTESIA_API_KEY={ENV_KEY}\n", encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        r = await authed.put("/api/keys/cartesia", json={"value": f"  {KEY}  "})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["check"]["result"] == "ok" and d["detail"] == "Saved. Cartesia accepted the key."
        assert d["key"]["source"] == "dashboard" and d["key"]["ends_with"] == "7f3a" and d["restart_needed"] is False
        assert dctx.config.secret("cartesia_api_key") == KEY  # used at once
        assert (tmp_path / "keys" / "cartesia").read_text(encoding="utf-8").strip() == KEY
        assert service.requests[-1].headers["authorization"] == f"Bearer {KEY}"

        r = await authed.post("/api/keys/cartesia/check")
        assert r.status_code == 200 and r.json()["ok"] is True

        r = await authed.delete("/api/keys/cartesia")
        assert r.status_code == 200
        d = r.json()
        assert d["key"]["source"] == "env" and "from the .env file" in d["detail"]
        assert dctx.config.secret("cartesia_api_key") == ENV_KEY and not (tmp_path / "keys" / "cartesia").exists()
        r2 = await authed.post("/api/keys/cartesia/remove")
        assert r2.status_code == 200 and "no Cartesia key saved here" in r2.json()["detail"]
        texts = [r.text, r2.text, (await authed.get("/api/keys")).text]
    assert "dashboard: cartesia key saved (checked OK)" in caplog.text
    assert "dashboard: cartesia key removed" in caplog.text
    for secret in (KEY, ENV_KEY):
        assert secret not in caplog.text and all(secret not in t for t in texts)


async def test_a_refused_key_is_not_saved(authed, dctx, service, tmp_path, caplog):
    service.status = 401
    with caplog.at_level(logging.DEBUG):
        r = await authed.post("/api/keys/cartesia", json={"value": KEY})
    assert r.status_code == 400
    assert r.json()["detail"] == "Cartesia refused that key. Nothing was saved."
    assert dctx.config.cartesia_api_key is None and not (tmp_path / "keys" / "cartesia").exists()
    assert KEY not in r.text and KEY not in caplog.text


async def test_no_answer_still_saves_with_a_note(authed, dctx, service):
    service.error = httpx.ConnectError("no network")
    r = await authed.put("/api/keys/deepgram", json={"value": "fake-deepgram-key-0042"})
    assert r.status_code == 200
    d = r.json()
    assert d["check"]["result"] == "unchecked" and "Couldn't reach Deepgram" in d["detail"]
    assert dctx.config.secret("deepgram_api_key") == "fake-deepgram-key-0042"
    service.error = None
    service.status = 500
    r = await authed.put("/api/keys/deepgram", json={"value": "fake-deepgram-key-0043"})
    assert r.status_code == 200 and r.json()["check"]["result"] == "unchecked"


async def test_cloud_off_saves_without_asking_anyone(authed, dctx, service):
    dctx.privacy.set(cloud_enabled=False)
    r = await authed.put("/api/keys/elevenlabs", json={"value": "fake-elevenlabs-key-0044"})
    assert r.status_code == 200 and "Cloud communication is off" in r.json()["detail"]
    assert not service.requests and dctx.config.secret("elevenlabs_api_key") == "fake-elevenlabs-key-0044"
    r = await authed.put("/api/keys/typesafe", json={"value": "fake-typesafe-key-0045"})
    assert r.status_code == 200 and "can't be checked" in r.json()["detail"]


async def test_bad_values_and_names(authed, dctx, service):
    for bad in ("short", "has a space in it", "tab\there-is-a-key", "x" * 301):
        r = await authed.put("/api/keys/cartesia", json={"value": bad})
        assert r.status_code == 422, bad
        assert bad not in r.text
    assert (await authed.put("/api/keys/cartesia", json={"value": "x" * 2000})).status_code == 422
    assert (await authed.put("/api/keys/cartesia", json={})).status_code == 422
    assert (await authed.put("/api/keys/made_up", json={"value": KEY})).status_code == 404
    assert (await authed.delete("/api/keys/made_up")).status_code == 404
    assert (await authed.post("/api/keys/made_up/check")).status_code == 404
    r = await authed.post("/api/keys/cartesia/check")
    assert r.status_code == 409 and "No Cartesia key" in r.json()["detail"]
    assert not service.requests and dctx.config.cartesia_api_key is None


async def test_login_and_csrf_are_needed(client, authed, dctx, service):
    assert (await client.get("/api/keys")).status_code == 401
    assert (await client.put("/api/keys/cartesia", json={"value": KEY})).status_code in (401, 403)
    token = authed.headers.pop("X-CSRF-Token")
    try:
        assert (await authed.put("/api/keys/cartesia", json={"value": KEY})).status_code == 403
        assert (await authed.delete("/api/keys/cartesia")).status_code == 403
        assert (await authed.post("/api/keys/cartesia/check")).status_code == 403
    finally:
        authed.headers["X-CSRF-Token"] = token
    assert dctx.config.cartesia_api_key is None and not service.requests


async def test_the_page_is_served_and_reachable(authed):
    r = await authed.get("/static/js/pages/keys.js")
    assert r.status_code == 200 and "/keys/" in r.text
    app_js = (await authed.get("/static/js/app.js")).text
    assert "import keys from './pages/keys.js'" in app_js and "id: 'keys'" in app_js
