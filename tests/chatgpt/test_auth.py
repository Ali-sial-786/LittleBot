"""ChatGPT device-code sign-in, token storage/refresh and sign-out (mocked auth.openai.com)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from typing import Any

import httpx
import pytest
from chatgpt_helpers import access_token, jwt, signed_in_auth

from littlebot.chatgpt.auth import AUTH_CLAIM, CLIENT_ID, ChatGPTAuth, ChatGPTAuthError, jwt_claims, mask_email


class Auth0:
    """A scripted auth.openai.com."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.polls_pending = 2
        self.usercode_status = 200
        self.refresh_response: tuple[int, dict[str, Any]] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        ctype = request.headers.get("content-type", "")
        body: Any = json.loads(request.content) if "json" in ctype else dict(
            p.split("=", 1) for p in request.content.decode().split("&"))
        self.calls.append((path, body))
        if path == "/api/accounts/deviceauth/usercode":
            if self.usercode_status != 200:
                return httpx.Response(self.usercode_status)
            return httpx.Response(200, json={"device_auth_id": "dev-1", "user_code": "ABCD-1234", "interval": "1"})
        if path == "/api/accounts/deviceauth/token":
            if self.polls_pending > 0:
                self.polls_pending -= 1
                return httpx.Response(403, json={"error": {"code": "deviceauth_authorization_pending"}})
            return httpx.Response(200, json={"authorization_code": "code-1", "code_verifier": "verifier-1"})
        if path == "/oauth/token" and body.get("grant_type") == "authorization_code":
            return httpx.Response(200, json={
                "access_token": access_token(), "refresh_token": "refresh-1", "expires_in": 3600,
                "id_token": jwt({"email": "parent@example.com", AUTH_CLAIM: {"chatgpt_account_id": "acct-123"}})})
        if path == "/oauth/token" and body.get("grant_type") == "refresh_token":
            code, data = self.refresh_response or (200, {"access_token": access_token(), "refresh_token": "refresh-2"})
            return httpx.Response(code, json=data)
        if path == "/oauth/revoke":
            return httpx.Response(200, json={})
        return httpx.Response(404)


def make(tmp_path, server: Auth0, **kw: Any) -> ChatGPTAuth:
    async def no_sleep(_s: float) -> None:
        await asyncio.sleep(0)

    return ChatGPTAuth(tmp_path / "data" / "chatgpt_auth.json", transport=httpx.MockTransport(server),
                       sleep=no_sleep, **kw)


async def test_device_code_sign_in_end_to_end(tmp_path):
    server = Auth0()
    auth = make(tmp_path, server)
    st = await auth.start_sign_in()
    assert st["pending"]["user_code"] == "ABCD-1234"
    assert st["pending"]["verification_url"] == "https://auth.openai.com/codex/device"
    await asyncio.wait_for(auth._poll_task, 5)
    st = auth.status()
    assert st == {"signed_in": True, "email": "p***@example.com", "plan": "plus", "pending": None, "error": None}
    # the exchange used the device redirect and the verifier OpenAI handed back
    exchange = next(b for p, b in server.calls if p == "/oauth/token")
    assert exchange["grant_type"] == "authorization_code" and exchange["code_verifier"] == "verifier-1"
    assert "deviceauth%2Fcallback" in exchange["redirect_uri"] or exchange["redirect_uri"].endswith("deviceauth/callback")
    assert server.calls[0] == ("/api/accounts/deviceauth/usercode", {"client_id": CLIENT_ID})
    # stored for the owner only, and no token ever appears in the status
    path = tmp_path / "data" / "chatgpt_auth.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "refresh" not in json.dumps(st) and "token" not in json.dumps(st)
    bearer = await auth.bearer()
    assert bearer.account_id == "acct-123" and "eyJ" not in repr(bearer)


async def test_device_code_disabled_is_explained(tmp_path):
    server = Auth0()
    server.usercode_status = 404
    with pytest.raises(ChatGPTAuthError) as e:
        await make(tmp_path, server).start_sign_in()
    assert e.value.kind == "device_code_disabled" and "Settings > Security" in str(e.value)


async def test_cancel_sign_in(tmp_path):
    server = Auth0()
    server.polls_pending = 10_000
    auth = make(tmp_path, server)
    await auth.start_sign_in()
    auth.cancel_sign_in()
    await asyncio.sleep(0)
    assert auth.status()["pending"] is None and not auth.signed_in


async def test_expired_token_refreshes_and_rotates(tmp_path):
    server = Auth0()
    auth = signed_in_auth(tmp_path, transport=httpx.MockTransport(server))
    tokens = auth.store.read()
    tokens.expires_at = time.time() + 60  # within the 5-minute early-refresh window
    auth.store.write(tokens)
    await auth.bearer()
    assert auth.store.read().refresh_token == "refresh-2"
    assert ("/oauth/token", {"client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": "refresh-1"}) in server.calls


async def test_concurrent_refreshes_happen_once(tmp_path):
    server = Auth0()
    auth = signed_in_auth(tmp_path, transport=httpx.MockTransport(server))
    tokens = auth.store.read()
    tokens.expires_at = 0
    auth.store.write(tokens)
    await asyncio.gather(*(auth.bearer() for _ in range(5)))
    assert sum(1 for p, b in server.calls if b.get("grant_type") == "refresh_token") == 1


@pytest.mark.parametrize("code,data", [(400, {"error": "invalid_grant"}), (401, {}),
                                       (400, {"error": {"code": "refresh_token_reused"}})])
async def test_permanent_refresh_failure_signs_out(tmp_path, code, data):
    server = Auth0()
    server.refresh_response = (code, data)
    auth = signed_in_auth(tmp_path, transport=httpx.MockTransport(server))
    tokens = auth.store.read()
    tokens.expires_at = 0
    auth.store.write(tokens)
    with pytest.raises(ChatGPTAuthError) as e:
        await auth.bearer()
    assert e.value.kind == "expired" and not auth.signed_in and not auth.store.path.exists()


async def test_transient_refresh_failure_keeps_the_sign_in(tmp_path):
    server = Auth0()
    server.refresh_response = (503, {})
    auth = signed_in_auth(tmp_path, transport=httpx.MockTransport(server))
    tokens = auth.store.read()
    tokens.expires_at = 0
    auth.store.write(tokens)
    with pytest.raises(ChatGPTAuthError):
        await auth.bearer()
    assert auth.signed_in


async def test_sign_out_revokes_and_deletes(tmp_path):
    server = Auth0()
    auth = signed_in_auth(tmp_path, transport=httpx.MockTransport(server))
    assert await auth.sign_out() is True
    assert ("/oauth/revoke", {"token": "refresh-1", "token_type_hint": "refresh_token", "client_id": CLIENT_ID}) in server.calls
    assert not auth.signed_in and not auth.store.path.exists()
    with pytest.raises(ChatGPTAuthError):
        await auth.bearer()


async def test_sign_out_offline_still_forgets(tmp_path):
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    auth = signed_in_auth(tmp_path, transport=httpx.MockTransport(boom))
    assert await auth.sign_out() is False
    assert not auth.signed_in


def test_claims_and_masking():
    c = jwt_claims(access_token(account="a1", plan="pro"))
    assert c["account_id"] == "a1" and c["plan_type"] == "pro" and c["exp"]
    assert jwt_claims("not.a.jwt") == {} or jwt_claims("not.a.jwt")["account_id"] is None
    assert mask_email("parent@example.com") == "p***@example.com"
    assert mask_email(None) is None


def test_corrupt_token_file_means_signed_out(tmp_path):
    p = tmp_path / "chatgpt_auth.json"
    p.write_text("{nope")
    assert not ChatGPTAuth(p).signed_in
