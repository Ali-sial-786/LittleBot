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


# ------------------------------------------------------------ ask what LittleBot sees (camera look)
class FakeLook:
    """Stands in for littlebot.chatgpt.look.CameraLook (injected as ctx.extras["camera_look"])."""

    def __init__(self, result: dict[str, Any] | None = None, blocked: str | None = None) -> None:
        self.result = result or {"ok": True, "answer": "You are holding up three fingers.", "error": None,
                                 "detail": None, "latency_ms": 1234.0,
                                 "image_b64": "SECRET-PHOTO-BYTES"}  # must never reach the browser
        self.blocked = blocked
        self.asked: list[tuple[str, str]] = []
        self.photos_today = 0

    def blocked_reason(self) -> str | None:
        return self.blocked

    def status(self) -> dict[str, Any]:
        return {"enabled": self.blocked is None, "blocked": self.blocked,
                "blocked_detail": "Turn it on in Privacy." if self.blocked else None,
                "photos_today": self.photos_today, "last_at": None}

    async def look(self, question: str, *, asked_by: str = "parent") -> dict[str, Any]:
        self.asked.append((question, asked_by))
        if self.blocked:
            return {"ok": False, "answer": None, "error": self.blocked,
                    "detail": "Let the AI look through the camera in Privacy first.", "latency_ms": None}
        self.photos_today += 1
        return dict(self.result)


@pytest.fixture
def fake_look(dctx):
    look = FakeLook()
    dctx.extras["camera_look"] = look
    return look


async def test_look_returns_only_the_answer_text(authed, signed, fake_look):
    r = await authed.post("/api/chatgpt/look", json={"question": "How many fingers am I holding up?"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["result"]["ok"] is True
    assert d["result"]["answer"] == "You are holding up three fingers."
    assert d["spoken"] is False and d["look"]["photos_today"] == 1
    assert "SECRET-PHOTO-BYTES" not in r.text and "image" not in r.text
    assert fake_look.asked == [("How many fingers am I holding up?", "parent")]


async def test_look_without_a_question_leaves_the_default_to_camera_look(authed, signed, fake_look):
    r = await authed.post("/api/chatgpt/look", json={"question": "   "})
    assert r.status_code == 200
    assert fake_look.asked == [("", "parent")]


async def test_look_question_is_limited(authed, signed, fake_look):
    r = await authed.post("/api/chatgpt/look", json={"question": "x" * 301})
    assert r.status_code == 422 and fake_look.asked == []


async def test_look_speaks_the_answer_only_when_asked(authed, signed, fake_look, dctx):
    await authed.post("/api/chatgpt/look", json={"question": "What colour is my cup?"})
    assert "You are holding up three fingers." not in dctx.audio.spoken
    r = await authed.post("/api/chatgpt/look", json={"question": "What colour is my cup?", "speak": True})
    assert r.status_code == 200
    assert dctx.audio.spoken[-1] == "You are holding up three fingers."


async def test_look_blocked_is_403_with_a_friendly_reason(authed, signed, dctx):
    look = FakeLook(blocked="ai_camera_off")
    dctx.extras["camera_look"] = look
    r = await authed.post("/api/chatgpt/look", json={"question": "Hi", "speak": True})
    assert r.status_code == 403 and "Privacy" in r.json()["detail"]
    assert dctx.audio.spoken == []
    assert look.photos_today == 0


async def test_look_failure_is_502(authed, signed, dctx):
    dctx.extras["camera_look"] = FakeLook(result={"ok": False, "answer": None, "error": "request_failed",
                                                  "detail": "ChatGPT didn't answer. Try again.", "latency_ms": 900.0})
    r = await authed.post("/api/chatgpt/look", json={"question": "Hi"})
    assert r.status_code == 502 and r.json()["detail"] == "ChatGPT didn't answer. Try again."


async def test_chatgpt_view_includes_look_status(authed, signed, fake_look):
    fake_look.blocked = "camera_off"
    d = (await authed.get("/api/chatgpt")).json()
    assert d["look"]["blocked"] == "camera_off" and d["look"]["photos_today"] == 0
    assert d["privacy"]["ai_camera_enabled"] is False


async def test_look_needs_login(client, signed, fake_look):
    r = await client.post("/api/chatgpt/look", json={"question": "Hi"})
    assert r.status_code == 401 and fake_look.asked == []


async def test_privacy_switch_for_the_ai_camera(authed, dctx):
    if not hasattr(dctx.privacy, "ai_camera_enabled"):
        pytest.skip("privacy.ai_camera_enabled is not in this build yet")
    d = (await authed.get("/api/privacy")).json()
    assert d["ai_camera_enabled"] is False  # off by default
    r = await authed.put("/api/privacy", json={"ai_camera_enabled": True})
    assert r.status_code == 200 and r.json()["ai_camera_enabled"] is True
    assert dctx.privacy.ai_camera_enabled is True
    r = await authed.put("/api/privacy", json={"ai_camera_enabled": False})
    assert r.json()["ai_camera_enabled"] is False and dctx.privacy.ai_camera_enabled is False


async def test_real_camera_look_stays_off_until_the_parent_allows_it(authed, signed, dctx):
    pytest.importorskip("littlebot.chatgpt.look")
    d = (await authed.get("/api/chatgpt")).json()
    assert d["look"]["enabled"] is False and d["look"]["blocked"] == "cloud_off" and d["look"]["photos_today"] == 0
    dctx.privacy.set(cloud_enabled=True, camera_enabled=True)
    d = (await authed.get("/api/chatgpt")).json()
    assert d["look"]["blocked"] == "ai_camera_off" and "Privacy" in d["look"]["blocked_detail"]
    r = await authed.post("/api/chatgpt/look", json={"question": "How many fingers am I holding up?"})
    assert r.status_code == 403 and "Privacy" in r.json()["detail"]
    assert (await authed.get("/api/chatgpt")).json()["look"]["photos_today"] == 0
