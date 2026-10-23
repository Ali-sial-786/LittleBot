"""Dashboard: Sign in with ChatGPT + live voice routes. Tokens must never reach the browser."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chatgpt"))
from chatgpt_helpers import access_token  # noqa: E402

from littlebot.chatgpt.auth import ChatGPTAuth, TokenSet  # noqa: E402

SECRET_ACCESS = access_token()
SECRET_REFRESH = "rt-TESTSECRETrefreshDONOTleak"


class FakeLive:
    def __init__(self) -> None:
        self.started: list[bool] = []
        self.active = False

    def status(self) -> dict[str, Any]:
        return {"available": True, "active": self.active, "can_start": not self.active, "blocked": None}

    async def start_session(self, *, parent_present: bool) -> dict[str, Any]:
        self.started.append(parent_present)
        if not parent_present:
            return {"ok": False, "error": "parent_not_present", "detail": "Tick the box."}
        self.active = True
        return {"ok": True}

    async def stop_session(self, reason: str = "") -> dict[str, Any]:
        self.active = False
        return {"ok": True}


def auth_server(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/usercode"):
        return httpx.Response(200, json={"device_auth_id": "dev-1", "user_code": "WXYZ-9876", "interval": 5})
    return httpx.Response(403, json={"error": {"code": "deviceauth_authorization_pending"}})


@pytest.fixture
def signed(dctx, tmp_path):
    auth = ChatGPTAuth(tmp_path / "chatgpt_auth.json", transport=httpx.MockTransport(auth_server))
    dctx.extras["chatgpt_auth"] = auth
    dctx.live = FakeLive()
    return auth


async def test_status_without_sign_in(authed, signed):
    r = await authed.get("/api/chatgpt")
    assert r.status_code == 200
    d = r.json()
    assert d["account"]["signed_in"] is False and d["live"]["available"] is True


async def test_sign_in_needs_cloud_on(authed, signed):
    r = await authed.post("/api/chatgpt/sign-in", json={})
    assert r.status_code == 403 and "Privacy" in r.json()["detail"]


async def test_sign_in_shows_code_and_cancel(authed, signed, dctx):
    dctx.privacy.set(cloud_enabled=True)
    r = await authed.post("/api/chatgpt/sign-in", json={})
    assert r.status_code == 200
    pending = r.json()["account"]["pending"]
    assert pending["user_code"] == "WXYZ-9876" and pending["verification_url"] == "https://auth.openai.com/codex/device"
    r = await authed.post("/api/chatgpt/sign-in/cancel", json={})
    assert r.json()["account"]["pending"] is None


async def test_tokens_never_reach_the_browser(authed, signed, dctx):
    signed.store.write(TokenSet(access_token=SECRET_ACCESS, refresh_token=SECRET_REFRESH, expires_at=time.time() + 3600,
                                account_id="acct-SECRET", plan_type="plus", email_masked="p***@example.com"))
    signed._loaded = False
    for path in ("/api/chatgpt", "/api/ai", "/api/status", "/api/config"):
        r = await authed.get(path)
        text = r.text
        assert SECRET_ACCESS not in text and SECRET_REFRESH not in text and "acct-SECRET" not in text, path
    d = (await authed.get("/api/chatgpt")).json()
    assert d["account"] == {"signed_in": True, "email": "p***@example.com", "plan": "plus", "pending": None, "error": None}


async def test_live_start_requires_parent_present(authed, signed, dctx):
    r = await authed.post("/api/chatgpt/live/start", json={})
    assert r.status_code == 403
    r = await authed.post("/api/chatgpt/live/start", json={"parent_present": True})
    assert r.status_code == 200 and r.json()["live"]["active"] is True
    r = await authed.post("/api/chatgpt/live/stop", json={})
    assert r.status_code == 200 and r.json()["live"]["active"] is False
    assert dctx.live.started == [False, True]


async def test_routes_need_login(client, signed):
    for method, path in (("get", "/api/chatgpt"), ("post", "/api/chatgpt/sign-in"), ("post", "/api/chatgpt/live/start")):
        r = await getattr(client, method)(path, **({} if method == "get" else {"json": {}}))
        assert r.status_code == 401, path


async def test_sign_out(authed, signed, dctx):
    signed.store.write(TokenSet(access_token=SECRET_ACCESS, refresh_token=SECRET_REFRESH, expires_at=time.time() + 3600,
                                account_id="acct-1"))
    signed._loaded = False
    r = await authed.post("/api/chatgpt/sign-out", json={})
    assert r.status_code == 200 and r.json()["account"]["signed_in"] is False
    assert not signed.store.path.exists()


async def test_live_safety_stop_becomes_a_parent_alert(authed, signed, dctx):
    from littlebot.core.events import Topics

    dctx.bus.publish(Topics.BRAIN_STATUS, {"provider": "live", "status": "live_safety_stop", "detail": "secrecy"})
    alerts = (await authed.get("/api/status")).json()["alerts"]
    assert any(a["title"] == "Live voice stopped" and "secrecy" in a["detail"] for a in alerts)
