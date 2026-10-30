"""Spotify's Web API for LittleBot: the owner's sign-in and the requests the Spotify skill makes.

Sign-in: OAuth 2.0 Authorization Code with PKCE (https://developer.spotify.com/documentation/web-api/tutorials/
code-pkce-flow), so there is no client secret anywhere. The owner makes an app at developer.spotify.com, registers
the redirect URI :data:`REDIRECT_URI` there and gives LittleBot the app's Client ID (the dashboard). Then:

1. :meth:`SpotifyAuth.begin` makes a code verifier (random, kept in memory only), its S256 challenge and a random
   ``state``, and returns the authorize URL; the dashboard opens it in a new tab.
2. The owner approves. Spotify sends the browser to ``http://127.0.0.1:8888/callback?code=...&state=...``: nothing
   listens there (127.0.0.1 is the phone or PC itself), so the page fails to load, which is expected. The owner
   copies that address from the address bar and pastes it into the dashboard.
3. :meth:`SpotifyAuth.finish` checks the ``state`` (a pasted address from another sign-in is refused), exchanges the
   code with the verifier and keeps the tokens in ``data/spotify_token.json`` (mode 600, replaced atomically).
   The access token lasts an hour; :meth:`SpotifyAuth.access_token` refreshes it a minute before (Spotify may hand
   out a new refresh token each time: it is kept). A refresh token Spotify no longer accepts ends the sign-in.

Tokens are never logged, never returned by ``status()`` and never shown in a ``repr``. Errors are short kinds
(:data:`ERRORS`), never what Spotify said. :class:`SpotifyAPI` makes the Web API requests (``https://api.spotify.com
/v1``) with the access token, retries once with a fresh one on HTTP 401, and turns failures into a
:class:`SpotifyError`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

log = logging.getLogger("littlebot.skills.spotify")

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
# Spotify only accepts https or a loopback IP address (not "localhost") as a redirect URI.
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES: tuple[str, ...] = ("user-read-playback-state", "user-modify-playback-state", "user-read-currently-playing",
                           "playlist-read-private", "user-library-read", "user-top-read")
PENDING_S = 15 * 60  # a started sign-in can be finished this long
REFRESH_MARGIN_S = 60.0  # refresh the access token this long before it runs out
AUTH_TIMEOUT_S = 10.0
_CLIENT_ID = re.compile(r"^[0-9a-fA-F]{32}$")

ERRORS: dict[str, str] = {
    "off": "Spotify is switched off in LittleBot's settings",
    "no_client_id": "Spotify isn't set up yet (the Client ID, on the dashboard's Assistant page)",
    "bad_client_id": "that isn't a Spotify Client ID (32 letters and numbers, from developer.spotify.com)",
    "not_connected": "Spotify isn't connected yet (Connect Spotify, on the dashboard's Assistant page)",
    "offline": "LittleBot is in offline mode, so it can't reach Spotify",
    "cloud_off": "cloud communication is off in Privacy, so LittleBot can't use Spotify",
    "no_pending": "press Connect Spotify first, then paste the address you land on",
    "expired": "that sign-in took too long; press Connect Spotify again",
    "state_mismatch": "that address is from a different sign-in; press Connect Spotify again and paste the new one",
    "no_code": "paste the whole address from the address bar (it has code= and state= in it)",
    "denied": "the sign-in was cancelled on Spotify's page",
    "bad_code": "Spotify refused the sign-in code (it works once and only for a few minutes); press Connect Spotify "
                "again",
    "redirect": "Spotify refused the redirect address: add http://127.0.0.1:8888/callback to the app on "
                "developer.spotify.com, exactly like that",
    "bad_client": "Spotify doesn't know that Client ID (check it on developer.spotify.com)",
    "revoked": "Spotify ended LittleBot's sign-in; connect it again on the dashboard",
    "auth": "Spotify refused LittleBot's sign-in; connect it again on the dashboard",
    "premium": "Spotify only lets apps play music on an account with Spotify Premium",
    "forbidden": "Spotify didn't allow that",
    "no_device": "LittleBot's Spotify receiver isn't running, so Spotify can't play on the robot "
                 "(scripts/setup_spotify.sh --check)",
    "restricted": "Spotify won't let LittleBot control its receiver right now",
    "no_active": "nothing is playing on Spotify right now",
    "not_found": "Spotify couldn't find that",
    "rate_limited": "Spotify asked LittleBot to slow down; try again in a minute",
    "timeout": "Spotify didn't answer in time",
    "network": "Spotify couldn't be reached (is the robot online?)",
    "server": "Spotify had a problem; try again in a minute",
    "bad_request": "Spotify couldn't do that",
    "bad_response": "Spotify gave an answer LittleBot couldn't read",
    "nothing_found": "Spotify found nothing for that; try other words",
    "explicit": "the songs Spotify found for that are marked explicit, and LittleBot skips those",
    "no_playlist": "there's no playlist with that name in their Spotify library",
    "spotify_made": "Spotify no longer lets apps open its own personal playlists (Discover Weekly, Release Radar, "
                    "Daily Mix); open it in the Spotify app and pick LittleBot as the speaker",
    "empty_library": "there's nothing saved in their Spotify library for that yet",
}


class SpotifyError(Exception):
    """``kind`` is a key of :data:`ERRORS`; ``str(e)`` the plain sentence."""

    def __init__(self, kind: str, *, retry_after: float | None = None) -> None:
        self.kind = kind if kind in ERRORS else "bad_response"
        self.retry_after = retry_after
        super().__init__(ERRORS[self.kind])


# ---------------------------------------------------------------- PKCE
def pkce_verifier() -> str:
    """A code verifier: 64 characters of A-Z a-z 0-9 - _ (RFC 7636 allows 43 to 128 of [A-Za-z0-9-._~])."""
    return secrets.token_urlsafe(48)


def pkce_challenge(verifier: str) -> str:
    """BASE64URL(SHA256(verifier)) without padding (the S256 method)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def clean_client_id(value: Any) -> str | None:
    """The Client ID tidied up (32 hex characters), or None when it isn't one."""
    s = "".join(str(value or "").split())
    return s.lower() if _CLIENT_ID.match(s) else None


# ---------------------------------------------------------------- the token file
@dataclass
class Tokens:
    client_id: str
    access_token: str
    refresh_token: str
    expires_at: float  # wall-clock seconds
    scope: str = ""
    connected_at: float = 0.0

    def __repr__(self) -> str:  # never print a token
        return f"Tokens(client_id={self.client_id[:6]}..., expires_at={self.expires_at:.0f}, scope={self.scope!r})"


class TokenStore:
    """One JSON file, readable and writable only by the robot's user (0600), replaced atomically."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> Tokens | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            tokens = Tokens(**{k: data[k] for k in Tokens.__dataclass_fields__ if k in data})
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError):
            log.warning("spotify: %s is unreadable; treating Spotify as not connected", self.path.name)
            return None
        if not (tokens.access_token and tokens.refresh_token and tokens.client_id):
            return None
        return tokens

    def write(self, tokens: Tokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(asdict(tokens), fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


@dataclass
class _Pending:
    client_id: str
    verifier: str
    state: str
    created: float

    def __repr__(self) -> str:
        return f"_Pending(client_id={self.client_id[:6]}...)"


def parse_redirect(pasted: str) -> dict[str, str]:
    """{code, state, error} from what was pasted: the whole address, or just its ``?code=...&state=...`` part."""
    text = "".join(str(pasted or "").split())[:4000]
    if not text:
        return {}
    parts = urlsplit(text)
    query = parts.query or (parts.fragment if "=" in parts.fragment else "")
    if not query and "=" in text and "://" not in text:
        query = text.lstrip("?")
    found = parse_qs(query, keep_blank_values=False)
    return {k: v[0] for k, v in found.items() if k in ("code", "state", "error") and v and v[0]}


# ---------------------------------------------------------------- the sign-in
class SpotifyAuth:
    """Sign-in, token refresh and sign-out. One per robot (the skill owns it)."""

    def __init__(self, token_path: Path, *, transport: httpx.AsyncBaseTransport | None = None,
                 now: Callable[[], float] = time.time) -> None:
        self.store = TokenStore(token_path)
        self._transport = transport
        self._now = now
        self._tokens: Tokens | None = None
        self._loaded = False
        self._pending: _Pending | None = None
        self._locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------ state
    def tokens(self) -> Tokens | None:
        if not self._loaded:
            self._tokens = self.store.read()
            self._loaded = True
        return self._tokens

    @property
    def connected(self) -> bool:
        return self.tokens() is not None

    @property
    def client_id(self) -> str | None:
        t = self.tokens()
        return t.client_id if t is not None else None

    @property
    def pending(self) -> bool:
        p = self._pending
        return p is not None and self._now() - p.created < PENDING_S

    def status(self) -> dict[str, Any]:
        """For the dashboard: never a token."""
        t = self.tokens()
        return {"connected": t is not None, "pending": self.pending, "redirect_uri": REDIRECT_URI,
                "scopes": list(SCOPES), "connected_at": t.connected_at if t is not None else None,
                "missing_scopes": [s for s in SCOPES if t is not None and s not in t.scope.split()]}

    # ------------------------------------------------------------ connect
    def begin(self, client_id: str) -> str:
        """Start a sign-in for ``client_id``: the authorize URL to open. Raises SpotifyError("bad_client_id")."""
        cid = clean_client_id(client_id)
        if cid is None:
            raise SpotifyError("bad_client_id")
        verifier = pkce_verifier()
        self._pending = _Pending(cid, verifier, secrets.token_urlsafe(18), self._now())
        query = {"client_id": cid, "response_type": "code", "redirect_uri": REDIRECT_URI,
                 "code_challenge_method": "S256", "code_challenge": pkce_challenge(verifier),
                 "state": self._pending.state, "scope": " ".join(SCOPES)}
        log.info("spotify: sign-in started")
        return f"{AUTH_URL}?{urlencode(query)}"

    async def finish(self, pasted: str) -> Tokens:
        """Finish the sign-in from the address the browser landed on. Raises SpotifyError."""
        got = parse_redirect(pasted)
        p = self._pending
        if got.get("error"):
            self._pending = None
            raise SpotifyError("denied")
        if p is None:
            raise SpotifyError("no_pending")
        if self._now() - p.created >= PENDING_S:
            self._pending = None
            raise SpotifyError("expired")
        if not got.get("code") or not got.get("state"):
            raise SpotifyError("no_code")
        if not hmac.compare_digest(got["state"].encode("utf-8"), p.state.encode("utf-8")):
            raise SpotifyError("state_mismatch")
        self._pending = None  # a code works once: a second paste starts again
        data = await self._token_request({"grant_type": "authorization_code", "code": got["code"],
                                          "redirect_uri": REDIRECT_URI, "client_id": p.client_id,
                                          "code_verifier": p.verifier})
        tokens = self._tokens_from(data, p.client_id, previous=None)
        self.store.write(tokens)
        self._tokens, self._loaded = tokens, True
        log.info("spotify: connected")
        return tokens

    def disconnect(self) -> bool:
        """Forget the sign-in (the token file is deleted). True when there was one."""
        had = self.connected
        self.store.clear()
        self._tokens, self._loaded, self._pending = None, True, None
        if had:
            log.info("spotify: disconnected")
        return had

    # ------------------------------------------------------------ tokens
    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(id(loop))
        if lock is None:
            self._locks = {id(loop): asyncio.Lock()}
            lock = self._locks[id(loop)]
        return lock

    async def access_token(self, *, force_refresh: bool = False) -> str:
        """A valid access token (refreshed when it runs out soon). Raises SpotifyError."""
        t = self.tokens()
        if t is None:
            raise SpotifyError("not_connected")
        if not force_refresh and t.expires_at - REFRESH_MARGIN_S > self._now():
            return t.access_token
        async with self._lock():
            t = self.tokens()
            if t is None:
                raise SpotifyError("not_connected")
            if t.expires_at - REFRESH_MARGIN_S > self._now() and not force_refresh:
                return t.access_token  # refreshed while this one waited
            try:
                data = await self._token_request({"grant_type": "refresh_token", "refresh_token": t.refresh_token,
                                                  "client_id": t.client_id})
            except SpotifyError as e:
                if e.kind in ("bad_code", "bad_client", "redirect"):  # the refresh token is no good any more
                    self.store.clear()
                    self._tokens = None
                    log.warning("spotify: the sign-in was refused when refreshing; connect it again")
                    raise SpotifyError("revoked") from None
                raise
            fresh = self._tokens_from(data, t.client_id, previous=t)
            self.store.write(fresh)
            self._tokens = fresh
            log.info("spotify: access token refreshed")
            return fresh.access_token

    def _tokens_from(self, data: Any, client_id: str, *, previous: Tokens | None) -> Tokens:
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
            raise SpotifyError("bad_response")
        refresh = data.get("refresh_token") if isinstance(data.get("refresh_token"), str) else None
        refresh = refresh or (previous.refresh_token if previous is not None else None)
        if not refresh:
            raise SpotifyError("bad_response")
        try:
            lasts = max(60.0, float(data.get("expires_in") or 3600))
        except (TypeError, ValueError):
            lasts = 3600.0
        scope = data.get("scope") if isinstance(data.get("scope"), str) else (previous.scope if previous else "")
        return Tokens(client_id=client_id, access_token=data["access_token"], refresh_token=refresh,
                      expires_at=self._now() + lasts, scope=scope or "",
                      connected_at=previous.connected_at if previous is not None else self._now())

    async def _token_request(self, form: dict[str, str]) -> Any:
        try:
            async with httpx.AsyncClient(transport=self._transport, follow_redirects=False,
                                         timeout=httpx.Timeout(AUTH_TIMEOUT_S, connect=5.0)) as client:
                resp = await client.post(TOKEN_URL, data=form, headers={"Accept": "application/json"})
        except httpx.TimeoutException:
            raise SpotifyError("timeout") from None
        except httpx.HTTPError:
            raise SpotifyError("network") from None
        if resp.status_code >= 400:
            kind = _token_error(resp)
            log.info("spotify: the token request (%s) answered HTTP %d (%s)", form.get("grant_type"),
                     resp.status_code, kind)
            raise SpotifyError(kind)
        try:
            return resp.json()
        except ValueError:
            raise SpotifyError("bad_response") from None


def _token_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    error = str(body.get("error") or "") if isinstance(body, dict) else ""
    desc = str(body.get("error_description") or "").lower() if isinstance(body, dict) else ""
    if "redirect" in desc:
        return "redirect"
    if error == "invalid_client":
        return "bad_client"
    if error == "invalid_grant" or resp.status_code == 400:
        return "bad_code"
    if resp.status_code == 429:
        return "rate_limited"
    return "server" if resp.status_code >= 500 else "bad_response"


# ---------------------------------------------------------------- the Web API
class SpotifyAPI:
    """Web API requests as the signed-in owner. ``call`` returns the JSON body (None for an empty answer)."""

    def __init__(self, auth: SpotifyAuth, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: Callable[[], float] | float = 6.0, base_url: str = API_URL) -> None:
        self.auth = auth
        self._transport = transport
        self._timeout = timeout
        self.base_url = base_url
        self._client: httpx.AsyncClient | None = None
        self._client_key: tuple[Any, ...] | None = None

    def timeout(self) -> float:
        t = self._timeout() if callable(self._timeout) else self._timeout
        try:
            return max(0.5, float(t))
        except (TypeError, ValueError):
            return 6.0

    def _http(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        timeout = self.timeout()
        key = (loop, timeout)
        if self._client is None or self._client.is_closed or self._client_key != key:
            from littlebot import __version__  # noqa: PLC0415

            self._client = httpx.AsyncClient(
                base_url=self.base_url, transport=self._transport, follow_redirects=False,
                headers={"User-Agent": f"LittleBot/{__version__}", "Accept": "application/json"},
                timeout=httpx.Timeout(timeout, connect=min(4.0, timeout)))
            self._client_key = key
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._client = None

    async def call(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                   body: dict[str, Any] | None = None) -> Any:
        """One request. Raises SpotifyError (kinds: auth, premium, no_device, no_active, timeout ...)."""
        timeout = self.timeout()
        resp: httpx.Response | None = None
        for attempt in (0, 1):
            token = await self.auth.access_token(force_refresh=attempt == 1)
            try:
                resp = await asyncio.wait_for(self._http().request(
                    method, path, params=params, json=body, headers={"Authorization": f"Bearer {token}"}), timeout)
            except (httpx.TimeoutException, asyncio.TimeoutError):
                raise SpotifyError("timeout") from None
            except httpx.HTTPError:
                raise SpotifyError("network") from None
            if resp.status_code != 401:
                break
        assert resp is not None
        code = resp.status_code
        if code in (202, 204) or (code < 300 and not resp.content.strip()):
            return None
        if code < 300:
            try:
                return resp.json()
            except ValueError:
                raise SpotifyError("bad_response") from None
        kind = _api_error(resp, path)
        log.info("spotify: %s %s answered HTTP %d (%s)", method, path, code, kind)
        retry = None
        if code == 429:
            try:
                retry = float(resp.headers.get("Retry-After") or 0) or None
            except ValueError:
                retry = None
        raise SpotifyError(kind, retry_after=retry)


def _api_error(resp: httpx.Response, path: str) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    reason = ""
    if isinstance(err, dict):
        reason = f"{err.get('reason') or ''} {err.get('message') or ''}".lower()
    elif isinstance(err, str):
        reason = err.lower()
    code = resp.status_code
    if code == 401:
        return "auth"
    if code == 403:
        if "premium" in reason:
            return "premium"
        return "restricted" if "restrict" in reason else "forbidden"
    if code == 404:
        if path.startswith("/me/player"):
            return "no_active" if "active" in reason else "no_device"
        return "not_found"
    if code == 429:
        return "rate_limited"
    if code >= 500:
        return "server"
    if code in (400, 405, 409, 422):
        return "bad_request"
    return "bad_response"


__all__ = ["API_URL", "AUTH_URL", "ERRORS", "REDIRECT_URI", "SCOPES", "TOKEN_URL", "SpotifyAPI", "SpotifyAuth",
           "SpotifyError", "TokenStore", "Tokens", "clean_client_id", "parse_redirect", "pkce_challenge",
           "pkce_verifier"]
