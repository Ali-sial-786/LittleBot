"""Sign in with ChatGPT: the parent's own ChatGPT plan instead of a paid API key.

Uses OpenAI's public Codex sign-in (the one the Codex CLI and WatchGPT use) with the
device-code flow, which suits a robot without a keyboard:

    1. POST {issuer}/api/accounts/deviceauth/usercode   {"client_id"}         -> device_auth_id, user_code
    2. the parent opens {issuer}/codex/device on their phone and types the code
    3. POST {issuer}/api/accounts/deviceauth/token      {device_auth_id, user_code}
                                                       -> authorization_code + code_verifier once approved
    4. POST {issuer}/oauth/token (form) grant_type=authorization_code ... -> access/refresh/id tokens

Tokens stay on the robot: a 0600 JSON file in the data folder, read and refreshed here only.
They are never returned by the dashboard API, logged, or sent anywhere but auth.openai.com
and chatgpt.com. Sign out revokes the refresh token (best effort) and deletes the file.

UNDOCUMENTED: these endpoints are not a public OpenAI API. They are what the Codex CLI uses
and may change without notice. Values copied from WatchGPT (checked against openai/codex
on 24.09.2026). Tested here only against mocked responses.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

log = logging.getLogger("littlebot.chatgpt")

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
CODEX_BASE = "https://chatgpt.com/backend-api/codex"
AUTH_CLAIM = "https://api.openai.com/auth"
PROFILE_CLAIM = "https://api.openai.com/profile"

DEVICE_CODE_LIFETIME_S = 15 * 60
EARLY_REFRESH_S = 300
DEFAULT_LIFETIME_S = 3600
PERMANENT_REFRESH_ERRORS = frozenset(
    {"invalid_grant", "refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated"})

# Friendly, parent-facing messages (no provider text is ever shown).
MESSAGES = {
    "not_signed_in": "Not signed in with ChatGPT. Sign in on the AI page.",
    "expired": "The ChatGPT sign-in expired. Sign in again on the AI page.",
    "offline": "Could not reach OpenAI. Check the robot's internet connection.",
    "device_code_disabled": "Code sign-in is off for your ChatGPT account. Turn on \"device code login\" "
                            "in ChatGPT > Settings > Security, then try again.",
    "code_expired": "The sign-in code expired before it was used. Start again for a new code.",
    "exchange_failed": "OpenAI did not accept the sign-in. Try again.",
    "account_missing": "That sign-in did not include a ChatGPT account.",
    "storage": "Could not save the sign-in on the robot (check the data folder).",
    "cancelled": "Sign-in cancelled.",
}


class ChatGPTAuthError(Exception):
    """``kind`` is a key of MESSAGES; ``str(e)`` is the friendly message."""

    def __init__(self, kind: str, status: int | None = None) -> None:
        super().__init__(MESSAGES.get(kind, kind))
        self.kind = kind
        self.status = status


@dataclass(frozen=True)
class Bearer:
    access_token: str
    account_id: str
    fedramp: bool = False

    def __repr__(self) -> str:  # never print the token
        return f"Bearer(account_id={self.account_id[:6]}..., fedramp={self.fedramp})"


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: float
    account_id: str
    plan_type: str | None = None
    email_masked: str | None = None
    fedramp: bool = False
    last_refresh: float = 0.0

    def __repr__(self) -> str:
        return f"TokenSet(account={self.account_id[:6]}..., plan={self.plan_type}, expires_at={self.expires_at:.0f})"


@dataclass
class _DeviceCode:
    device_auth_id: str
    user_code: str
    interval: float
    expires_at: float


def jwt_claims(token: str | None) -> dict[str, Any]:
    """Decode (not verify) a JWT payload. Tokens come straight from OpenAI over TLS."""
    if not token:
        return {}
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return {}
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    auth = payload.get(AUTH_CLAIM) if isinstance(payload.get(AUTH_CLAIM), dict) else {}
    profile = payload.get(PROFILE_CLAIM) if isinstance(payload.get(PROFILE_CLAIM), dict) else {}
    exp = payload.get("exp")
    return {
        "exp": float(exp) if isinstance(exp, (int, float)) else None,
        "account_id": auth.get("chatgpt_account_id") if isinstance(auth.get("chatgpt_account_id"), str) else None,
        "plan_type": auth.get("chatgpt_plan_type") if isinstance(auth.get("chatgpt_plan_type"), str) else None,
        "email": payload.get("email") if isinstance(payload.get("email"), str) else (
            profile.get("email") if isinstance(profile.get("email"), str) else None),
        "fedramp": auth.get("chatgpt_account_is_fedramp") is True,
    }


def mask_email(email: str | None) -> str | None:
    """"a***@example.com": enough for a parent to recognise the account."""
    if not email:
        return None
    at = email.find("@")
    return "***" if at <= 0 else email[0] + "***" + email[at:]


def _error_code(text: str) -> str | None:
    """Handles {"error": "invalid_grant"} and {"error": {"code": "..."}}."""
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    err = obj.get("error")
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        return str(err.get("code") or err.get("type") or "") or None
    return str(obj.get("code") or "") or None


class TokenStore:
    """One JSON file, owner read/write only, replaced atomically."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> TokenSet | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return TokenSet(**{k: data[k] for k in TokenSet.__dataclass_fields__ if k in data})
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError):
            log.warning("ChatGPT sign-in file unreadable; treating as signed out")
            return None

    def write(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(asdict(tokens), f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with _suppress():
                tmp.unlink()
            raise

    def clear(self) -> None:
        with _suppress():
            self.path.unlink()


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return exc[0] is not None and issubclass(exc[0], OSError)


class ChatGPTAuth:
    """Device-code sign-in, token refresh and sign-out. Safe to share between services."""

    def __init__(self, token_path: Path, *, originator: str = "littlebot", user_agent: str = "LittleBot",
                 issuer: str = ISSUER, transport: httpx.AsyncBaseTransport | None = None,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self.store = TokenStore(token_path)
        self.originator = originator
        self.user_agent = user_agent
        self.issuer = issuer.rstrip("/")
        self._transport = transport
        self._now = now
        self._sleep = sleep
        self._lock = threading.Lock()
        self._refresh_locks: dict[int, asyncio.Lock] = {}
        self._cached: TokenSet | None = None
        self._loaded = False
        self._device: _DeviceCode | None = None
        self._poll_task: asyncio.Task[Any] | None = None
        self._sign_in_error: str | None = None
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------- urls
    @property
    def verification_url(self) -> str:
        return f"{self.issuer}/codex/device"

    @property
    def _device_redirect(self) -> str:
        return f"{self.issuer}/deviceauth/callback"

    def _client(self) -> httpx.AsyncClient:
        # No redirects / retries: an authorization code is single-use.
        return httpx.AsyncClient(transport=self._transport, follow_redirects=False,
                                 timeout=httpx.Timeout(15.0, connect=8.0),
                                 headers={"User-Agent": self.user_agent, "originator": self.originator})

    def on_change(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.debug("chatgpt auth listener failed", exc_info=True)

    # ------------------------------------------------------------ state
    def _load(self) -> TokenSet | None:
        with self._lock:
            if not self._loaded:
                self._cached = self.store.read()
                self._loaded = True
            return self._cached

    @property
    def signed_in(self) -> bool:
        return self._load() is not None

    def status(self) -> dict[str, Any]:
        """Safe for the dashboard: no tokens, a masked e-mail only."""
        tokens = self._load()
        dev = self._device
        pending = None
        if dev is not None and self._poll_task is not None and not self._poll_task.done():
            pending = {"user_code": dev.user_code, "verification_url": self.verification_url,
                       "expires_in": max(0, int(dev.expires_at - self._now()))}
        return {
            "signed_in": tokens is not None,
            "email": tokens.email_masked if tokens else None,
            "plan": tokens.plan_type if tokens else None,
            "pending": pending,
            "error": self._sign_in_error,
        }

    # ---------------------------------------------------------- sign-in
    async def start_sign_in(self) -> dict[str, Any]:
        """Ask OpenAI for a code to type at ``verification_url``; poll for approval in the background."""
        if self._poll_task is not None and not self._poll_task.done() and self._device is not None \
                and self._device.expires_at - self._now() > 60:
            return self.status()
        self.cancel_sign_in(quiet=True)
        self._sign_in_error = None
        async with self._client() as client:
            try:
                resp = await client.post(f"{self.issuer}/api/accounts/deviceauth/usercode", json={"client_id": CLIENT_ID})
            except httpx.HTTPError:
                raise ChatGPTAuthError("offline") from None
        if resp.status_code == 404:
            raise ChatGPTAuthError("device_code_disabled", 404)
        if resp.status_code >= 400:
            raise ChatGPTAuthError("exchange_failed", resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            raise ChatGPTAuthError("exchange_failed", resp.status_code) from None
        dev_id = data.get("device_auth_id") if isinstance(data, dict) else None
        code = (data.get("user_code") or data.get("usercode")) if isinstance(data, dict) else None
        if not isinstance(dev_id, str) or not isinstance(code, str) or not dev_id or not code:
            raise ChatGPTAuthError("exchange_failed", resp.status_code)
        try:
            interval = min(60.0, max(1.0, float(data.get("interval") or 5)))
        except (TypeError, ValueError):
            interval = 5.0
        self._device = _DeviceCode(dev_id, code, interval, self._now() + DEVICE_CODE_LIFETIME_S)
        self._poll_task = asyncio.get_running_loop().create_task(self._poll(self._device), name="chatgpt-sign-in")
        log.info("ChatGPT sign-in started: waiting for the code to be entered at %s", self.verification_url)
        self._changed()
        return self.status()

    def cancel_sign_in(self, *, quiet: bool = False) -> None:
        task, self._poll_task = self._poll_task, None
        self._device = None
        if task is not None and not task.done():
            task.cancel()
            if not quiet:
                self._sign_in_error = None
                self._changed()

    async def _poll(self, dev: _DeviceCode) -> None:
        interval = dev.interval
        try:
            async with self._client() as client:
                while self._now() < dev.expires_at:
                    await self._sleep(interval)
                    try:
                        resp = await client.post(f"{self.issuer}/api/accounts/deviceauth/token",
                                                 json={"device_auth_id": dev.device_auth_id, "user_code": dev.user_code})
                    except httpx.HTTPError:
                        continue  # offline for a moment: keep waiting
                    if resp.status_code in (403, 404):
                        continue  # not approved yet
                    code = _error_code(resp.text)
                    if code == "slow_down":
                        interval = min(60.0, interval + 5.0)
                        continue
                    if code in ("deviceauth_authorization_pending", "authorization_pending"):
                        continue
                    if code in ("expired_token", "deviceauth_expired"):
                        raise ChatGPTAuthError("code_expired")
                    if resp.status_code >= 400:
                        raise ChatGPTAuthError("exchange_failed", resp.status_code)
                    try:
                        data = resp.json()
                    except ValueError:
                        raise ChatGPTAuthError("exchange_failed", resp.status_code) from None
                    auth_code = data.get("authorization_code") if isinstance(data, dict) else None
                    verifier = data.get("code_verifier") if isinstance(data, dict) else None
                    if not isinstance(auth_code, str) or not isinstance(verifier, str):
                        raise ChatGPTAuthError("exchange_failed", resp.status_code)
                    tokens = await self._exchange(client, auth_code, verifier)
                    self._complete(tokens)
                    log.info("ChatGPT sign-in complete (%s plan)", self._cached.plan_type if self._cached else "?")
                    return
                raise ChatGPTAuthError("code_expired")
        except asyncio.CancelledError:
            raise
        except ChatGPTAuthError as e:
            self._sign_in_error = str(e)
            log.warning("ChatGPT sign-in failed: %s", e.kind)
        except Exception:  # noqa: BLE001
            self._sign_in_error = MESSAGES["exchange_failed"]
            log.exception("ChatGPT sign-in failed")
        finally:
            if self._device is dev:
                self._device = None
            self._changed()

    async def _exchange(self, client: httpx.AsyncClient, code: str, verifier: str) -> dict[str, Any]:
        try:
            resp = await client.post(f"{self.issuer}/oauth/token", data={
                "grant_type": "authorization_code", "client_id": CLIENT_ID, "code": code,
                "redirect_uri": self._device_redirect, "code_verifier": verifier})
        except httpx.HTTPError:
            raise ChatGPTAuthError("offline") from None
        if resp.status_code >= 400:
            raise ChatGPTAuthError("exchange_failed", resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            raise ChatGPTAuthError("exchange_failed", resp.status_code) from None
        if not isinstance(data, dict):
            raise ChatGPTAuthError("exchange_failed", resp.status_code)
        return data

    def _expiry(self, jwt_exp: float | None, expires_in: Any) -> float:
        options = [v for v in (jwt_exp, (self._now() + float(expires_in))
                               if isinstance(expires_in, (int, float)) else None) if v is not None]
        return min(options) if options else self._now() + DEFAULT_LIFETIME_S

    def _complete(self, data: dict[str, Any]) -> TokenSet:
        access, refresh = data.get("access_token"), data.get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise ChatGPTAuthError("exchange_failed")
        ac, ic = jwt_claims(access), jwt_claims(data.get("id_token"))
        account = ac.get("account_id") or ic.get("account_id")
        if not account or (ac.get("account_id") and ic.get("account_id") and ac["account_id"] != ic["account_id"]):
            raise ChatGPTAuthError("account_missing")
        tokens = TokenSet(
            access_token=access, refresh_token=refresh, expires_at=self._expiry(ac.get("exp"), data.get("expires_in")),
            account_id=account, plan_type=ac.get("plan_type") or ic.get("plan_type"),
            email_masked=mask_email(ic.get("email") or ac.get("email")),
            fedramp=bool(ac.get("fedramp") or ic.get("fedramp")), last_refresh=self._now())
        try:
            self.store.write(tokens)
        except OSError:
            raise ChatGPTAuthError("storage") from None
        with self._lock:
            self._cached, self._loaded = tokens, True
        self._sign_in_error = None
        return tokens

    # ---------------------------------------------------------- tokens
    def _refresh_lock(self) -> asyncio.Lock:
        key = id(asyncio.get_running_loop())
        lock = self._refresh_locks.get(key)
        if lock is None:
            lock = self._refresh_locks[key] = asyncio.Lock()
        return lock

    async def bearer(self) -> Bearer:
        """A valid access token (refreshed when it expires within 5 minutes)."""
        async with self._refresh_lock():
            tokens = self._load()
            if tokens is None:
                raise ChatGPTAuthError("not_signed_in")
            if tokens.expires_at <= self._now() + EARLY_REFRESH_S:
                tokens = await self._refresh(tokens)
            return Bearer(tokens.access_token, tokens.account_id, tokens.fedramp)

    async def on_unauthorized(self, rejected: Bearer) -> Bearer:
        """After a 401: refresh once, unless another caller already did."""
        async with self._refresh_lock():
            tokens = self._load()
            if tokens is None:
                raise ChatGPTAuthError("not_signed_in")
            if tokens.access_token == rejected.access_token:
                tokens = await self._refresh(tokens)
            return Bearer(tokens.access_token, tokens.account_id, tokens.fedramp)

    async def _refresh(self, current: TokenSet) -> TokenSet:
        async with self._client() as client:
            try:
                resp = await client.post(f"{self.issuer}/oauth/token", json={
                    "client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": current.refresh_token})
            except httpx.HTTPError:
                raise ChatGPTAuthError("offline") from None
        if resp.status_code >= 400:
            code = _error_code(resp.text)
            if resp.status_code == 401 or code in PERMANENT_REFRESH_ERRORS:
                log.warning("ChatGPT sign-in no longer valid (%s); signed out", code or resp.status_code)
                self._wipe()
                raise ChatGPTAuthError("expired", resp.status_code)
            raise ChatGPTAuthError("offline" if resp.status_code >= 500 else "exchange_failed", resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            raise ChatGPTAuthError("exchange_failed", resp.status_code) from None
        access = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(access, str) or not access:
            raise ChatGPTAuthError("exchange_failed", resp.status_code)
        claims = jwt_claims(access)
        if claims.get("account_id") and claims["account_id"] != current.account_id:
            self._wipe()
            raise ChatGPTAuthError("expired")
        refresh = data.get("refresh_token")
        nxt = TokenSet(
            access_token=access,
            refresh_token=refresh if isinstance(refresh, str) and refresh else current.refresh_token,
            expires_at=self._expiry(claims.get("exp"), data.get("expires_in")), account_id=current.account_id,
            plan_type=claims.get("plan_type") or current.plan_type, email_masked=current.email_masked,
            fedramp=current.fedramp, last_refresh=self._now())
        try:
            self.store.write(nxt)
        except OSError:
            log.warning("could not save refreshed ChatGPT tokens")
        with self._lock:
            self._cached = nxt
        return nxt

    def _wipe(self) -> None:
        self.store.clear()
        with self._lock:
            self._cached, self._loaded = None, True
        self._changed()

    async def sign_out(self) -> bool:
        """Revoke (best effort) and forget the tokens. Returns whether OpenAI confirmed the revoke."""
        self.cancel_sign_in(quiet=True)
        tokens = self._load()
        revoked = False
        if tokens is not None:
            try:
                async with self._client() as client:
                    resp = await client.post(f"{self.issuer}/oauth/revoke", json={
                        "token": tokens.refresh_token, "token_type_hint": "refresh_token", "client_id": CLIENT_ID})
                revoked = resp.status_code < 400
            except httpx.HTTPError:
                revoked = False
        self._sign_in_error = None
        self._wipe()
        log.info("signed out of ChatGPT (revoked: %s)", revoked)
        return revoked

    def headers(self, bearer: Bearer) -> dict[str, str]:
        """Headers every chatgpt.com request carries."""
        h = {"Authorization": f"Bearer {bearer.access_token}", "ChatGPT-Account-ID": bearer.account_id,
             "originator": self.originator, "User-Agent": self.user_agent}
        if bearer.fedramp:
            h["X-OpenAI-Fedramp"] = "true"
        return h

    async def aclose(self) -> None:
        self.cancel_sign_in(quiet=True)
