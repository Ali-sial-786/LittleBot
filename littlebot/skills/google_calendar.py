"""Google Calendar for LittleBot: the owner's sign-in and the Calendar API requests the calendar skill makes.

Sign-in: OAuth 2.0 for installed apps with PKCE and a loopback redirect
(https://developers.google.com/identity/protocols/oauth2/native-app). Google has no ready-made app for LittleBot,
so the owner makes one in their own Google Cloud project (docs/CALENDAR.md): a "Desktop app" OAuth client gives a
Client ID and a client secret (for desktop apps Google says the secret can't really be kept secret; it is kept like a
key anyway). Then, run from ``scripts/setup_calendar.py``:

1. :meth:`GoogleAuth.begin` makes a PKCE code verifier and a random ``state`` (in memory only) and returns Google's
   sign-in URL.
2. The owner signs in and allows LittleBot. Google sends the browser to ``http://127.0.0.1:8889/?state=...&code=...``:
   nothing listens there (127.0.0.1 is the phone or PC itself), so the page fails to load, which is expected. The
   owner copies that address from the address bar and pastes it into the script.
3. :meth:`GoogleAuth.finish` checks the ``state``, exchanges the code and keeps the tokens with the client in
   ``data/google_calendar.json`` (mode 600, replaced atomically). The access token lasts an hour and is refreshed a
   minute before; a refresh token Google no longer accepts ends the sign-in.

Scopes: ``calendar.events`` (see and change events on the calendars) and ``calendar.readonly`` (the list of
calendars and their colours). Tokens and the secret are never logged, never in ``status()`` and never in a
``repr``. Errors are short kinds (:data:`ERRORS`), never what Google said.

:class:`GoogleCalendar` makes the requests (``https://www.googleapis.com/calendar/v3``): the calendars, the events
in a time range (recurring ones as single instances, so "cancel Tuesday's soccer" touches only that one), and
adding, changing and deleting an event. Answers are turned into plain dicts (:func:`parse_event`).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
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
from urllib.parse import quote, urlencode

import httpx

from littlebot.skills.spotify_api import parse_redirect, pkce_challenge, pkce_verifier

log = logging.getLogger("littlebot.skills.calendar")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://www.googleapis.com/calendar/v3"
# Desktop-app clients may use any loopback port without registering it.
REDIRECT_URI = "http://127.0.0.1:8889/"
SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/calendar.events",
                           "https://www.googleapis.com/auth/calendar.readonly")
PENDING_S = 15 * 60
REFRESH_MARGIN_S = 60.0
AUTH_TIMEOUT_S = 10.0
CALENDARS_TTL_S = 3600.0  # the list of calendars changes rarely
MAX_EVENTS = 250  # per calendar per request
_CLIENT_ID = re.compile(r"^[0-9]+-[0-9a-z]+\.apps\.googleusercontent\.com$")

ERRORS: dict[str, str] = {
    "not_connected": "Google Calendar isn't connected yet (scripts/setup_calendar.py, see docs/CALENDAR.md)",
    "bad_client_id": "that isn't a Google OAuth Client ID (it ends in .apps.googleusercontent.com)",
    "bad_secret": "that doesn't look like the client secret (it usually starts with GOCSPX-)",
    "no_pending": "start the sign-in first, then paste the address you land on",
    "expired": "that sign-in took too long; start again",
    "state_mismatch": "that address is from a different sign-in; start again and paste the new one",
    "no_code": "paste the whole address from the address bar (it has code= and state= in it)",
    "denied": "the sign-in was cancelled on Google's page",
    "bad_code": "Google refused the sign-in code (it works once and only for a few minutes); start again",
    "bad_client": "Google doesn't accept that Client ID and secret (check them in the Google Cloud console)",
    "revoked": "Google ended LittleBot's sign-in to the calendar; connect it again (scripts/setup_calendar.py)",
    "auth": "Google refused LittleBot's calendar sign-in; connect it again (scripts/setup_calendar.py)",
    "forbidden": "Google didn't allow that on this calendar (it may be read-only)",
    "not_found": "that event or calendar isn't there any more",
    "rate_limited": "Google asked LittleBot to slow down; try again in a minute",
    "timeout": "Google Calendar didn't answer in time",
    "network": "Google Calendar couldn't be reached (is the robot online?)",
    "server": "Google Calendar had a problem; try again in a minute",
    "bad_request": "Google Calendar couldn't do that",
    "bad_response": "Google Calendar gave an answer LittleBot couldn't read",
}


class CalendarError(Exception):
    """``kind`` is a key of :data:`ERRORS`; ``str(e)`` the plain sentence."""

    def __init__(self, kind: str) -> None:
        self.kind = kind if kind in ERRORS else "bad_response"
        super().__init__(ERRORS[self.kind])


def clean_client_id(value: Any) -> str | None:
    s = "".join(str(value or "").split())
    return s if _CLIENT_ID.match(s) else None


def clean_secret(value: Any) -> str | None:
    s = "".join(str(value or "").split())
    return s if 20 <= len(s) <= 80 and re.fullmatch(r"[A-Za-z0-9_\-]+", s) else None


# ---------------------------------------------------------------- the token file
@dataclass
class Tokens:
    client_id: str
    client_secret: str
    access_token: str
    refresh_token: str
    expires_at: float  # wall-clock seconds
    scope: str = ""
    connected_at: float = 0.0

    def __repr__(self) -> str:  # never print a token or the secret
        return f"Tokens(client_id={self.client_id[:8]}..., expires_at={self.expires_at:.0f})"


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
            log.warning("calendar: %s is unreadable; treating Google Calendar as not connected", self.path.name)
            return None
        if not (tokens.refresh_token and tokens.client_id and tokens.client_secret):
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
    client_secret: str
    verifier: str
    state: str
    created: float

    def __repr__(self) -> str:
        return f"_Pending(client_id={self.client_id[:8]}...)"


# ---------------------------------------------------------------- the sign-in
class GoogleAuth:
    """Sign-in, token refresh and sign-out (the calendar skill owns one)."""

    def __init__(self, token_path: Path, *, transport: httpx.AsyncBaseTransport | None = None,
                 now: Callable[[], float] = time.time) -> None:
        self.store = TokenStore(token_path)
        self._transport = transport
        self._now = now
        self._tokens: Tokens | None = None
        self._loaded = False
        self._pending: _Pending | None = None
        self._locks: dict[int, asyncio.Lock] = {}

    def tokens(self) -> Tokens | None:
        if not self._loaded:
            self._tokens = self.store.read()
            self._loaded = True
        return self._tokens

    @property
    def connected(self) -> bool:
        return self.tokens() is not None

    def reload(self) -> None:
        """Read the token file again next time (the setup script may have just written it)."""
        self._loaded = False

    def status(self) -> dict[str, Any]:
        t = self.tokens()
        return {"connected": t is not None, "redirect_uri": REDIRECT_URI,
                "connected_at": t.connected_at if t is not None else None,
                "missing_scopes": [s for s in SCOPES if t is not None and t.scope and s not in t.scope.split()]}

    def begin(self, client_id: str, client_secret: str) -> str:
        """Start a sign-in: Google's sign-in URL to open. Raises CalendarError (bad_client_id, bad_secret)."""
        cid = clean_client_id(client_id)
        if cid is None:
            raise CalendarError("bad_client_id")
        secret = clean_secret(client_secret)
        if secret is None:
            raise CalendarError("bad_secret")
        verifier = pkce_verifier()
        self._pending = _Pending(cid, secret, verifier, secrets.token_urlsafe(18), self._now())
        query = {"client_id": cid, "response_type": "code", "redirect_uri": REDIRECT_URI, "scope": " ".join(SCOPES),
                 "code_challenge_method": "S256", "code_challenge": pkce_challenge(verifier),
                 "state": self._pending.state, "access_type": "offline", "prompt": "consent"}
        log.info("calendar: sign-in started")
        return f"{AUTH_URL}?{urlencode(query)}"

    async def finish(self, pasted: str) -> Tokens:
        """Finish the sign-in from the address the browser landed on. Raises CalendarError."""
        got = parse_redirect(pasted)
        p = self._pending
        if got.get("error"):
            self._pending = None
            raise CalendarError("denied")
        if p is None:
            raise CalendarError("no_pending")
        if self._now() - p.created >= PENDING_S:
            self._pending = None
            raise CalendarError("expired")
        if not got.get("code") or not got.get("state"):
            raise CalendarError("no_code")
        if not hmac.compare_digest(got["state"].encode("utf-8"), p.state.encode("utf-8")):
            raise CalendarError("state_mismatch")
        self._pending = None
        data = await self._token_request({"grant_type": "authorization_code", "code": got["code"],
                                          "redirect_uri": REDIRECT_URI, "client_id": p.client_id,
                                          "client_secret": p.client_secret, "code_verifier": p.verifier})
        tokens = self._tokens_from(data, p.client_id, p.client_secret, previous=None)
        self.store.write(tokens)
        self._tokens, self._loaded = tokens, True
        log.info("calendar: Google Calendar connected")
        return tokens

    def disconnect(self) -> bool:
        had = self.connected
        self.store.clear()
        self._tokens, self._loaded, self._pending = None, True, None
        if had:
            log.info("calendar: Google Calendar disconnected")
        return had

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(id(loop))
        if lock is None:
            self._locks = {id(loop): asyncio.Lock()}
            lock = self._locks[id(loop)]
        return lock

    async def access_token(self, *, force_refresh: bool = False) -> str:
        """A valid access token (refreshed when it runs out soon). Raises CalendarError."""
        t = self.tokens()
        if t is None:
            raise CalendarError("not_connected")
        if not force_refresh and t.access_token and t.expires_at - REFRESH_MARGIN_S > self._now():
            return t.access_token
        async with self._lock():
            t = self.tokens()
            if t is None:
                raise CalendarError("not_connected")
            if not force_refresh and t.access_token and t.expires_at - REFRESH_MARGIN_S > self._now():
                return t.access_token
            try:
                data = await self._token_request({"grant_type": "refresh_token", "refresh_token": t.refresh_token,
                                                  "client_id": t.client_id, "client_secret": t.client_secret})
            except CalendarError as e:
                if e.kind in ("bad_code", "bad_client"):  # the refresh token (or the client) is no good any more
                    self.store.clear()
                    self._tokens = None
                    log.warning("calendar: Google refused the sign-in when refreshing; connect it again")
                    raise CalendarError("revoked") from None
                raise
            fresh = self._tokens_from(data, t.client_id, t.client_secret, previous=t)
            self.store.write(fresh)
            self._tokens = fresh
            log.debug("calendar: access token refreshed")
            return fresh.access_token

    def _tokens_from(self, data: Any, client_id: str, client_secret: str, *, previous: Tokens | None) -> Tokens:
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
            raise CalendarError("bad_response")
        refresh = data.get("refresh_token") if isinstance(data.get("refresh_token"), str) else None
        refresh = refresh or (previous.refresh_token if previous is not None else None)
        if not refresh:
            raise CalendarError("bad_response")
        try:
            lasts = max(60.0, float(data.get("expires_in") or 3600))
        except (TypeError, ValueError):
            lasts = 3600.0
        scope = data.get("scope") if isinstance(data.get("scope"), str) else (previous.scope if previous else "")
        return Tokens(client_id=client_id, client_secret=client_secret, access_token=data["access_token"],
                      refresh_token=refresh, expires_at=self._now() + lasts, scope=scope or "",
                      connected_at=previous.connected_at if previous is not None else self._now())

    async def _token_request(self, form: dict[str, str]) -> Any:
        try:
            async with httpx.AsyncClient(transport=self._transport, follow_redirects=False,
                                         timeout=httpx.Timeout(AUTH_TIMEOUT_S, connect=5.0)) as client:
                resp = await client.post(TOKEN_URL, data=form, headers={"Accept": "application/json"})
        except httpx.TimeoutException:
            raise CalendarError("timeout") from None
        except httpx.HTTPError:
            raise CalendarError("network") from None
        if resp.status_code >= 400:
            kind = _token_error(resp)
            log.info("calendar: the token request (%s) answered HTTP %d (%s)", form.get("grant_type"),
                     resp.status_code, kind)
            raise CalendarError(kind)
        try:
            return resp.json()
        except ValueError:
            raise CalendarError("bad_response") from None


def _token_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    error = str(body.get("error") or "") if isinstance(body, dict) else ""
    if error in ("invalid_client", "unauthorized_client"):
        return "bad_client"
    if error == "invalid_grant" or resp.status_code == 400:
        return "bad_code"
    if resp.status_code == 429:
        return "rate_limited"
    return "server" if resp.status_code >= 500 else "bad_response"


# ---------------------------------------------------------------- events as plain dicts
def _when(value: Any, tz: _dt.tzinfo) -> tuple[_dt.datetime | None, bool]:
    """Google's {dateTime} or {date} as an aware datetime, and whether it is a whole day."""
    if not isinstance(value, dict):
        return None, False
    try:
        if isinstance(value.get("dateTime"), str):
            at = _dt.datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
            return (at if at.tzinfo else at.replace(tzinfo=tz)), False
        if isinstance(value.get("date"), str):
            d = _dt.date.fromisoformat(value["date"])
            return _dt.datetime(d.year, d.month, d.day, tzinfo=tz), True
    except ValueError:
        return None, False
    return None, False


def parse_event(item: Any, calendar: dict[str, Any], tz: _dt.tzinfo) -> dict[str, Any] | None:
    """One event from the API as {id, calendar_id, calendar, title, start, end, all_day, location, color,
    can_edit, recurring}; None for a cancelled one, one they declined, or one that can't be read."""
    if not isinstance(item, dict) or item.get("status") == "cancelled" or not isinstance(item.get("id"), str):
        return None
    for who in item.get("attendees") or []:
        if isinstance(who, dict) and who.get("self") and who.get("responseStatus") == "declined":
            return None
    start, all_day = _when(item.get("start"), tz)
    end, _ = _when(item.get("end"), tz)
    if start is None:
        return None
    if end is None or end < start:
        end = start + (_dt.timedelta(days=1) if all_day else _dt.timedelta(0))
    title = " ".join(str(item.get("summary") or "").split())[:120] or "(no title)"
    return {"id": item["id"], "calendar_id": str(calendar.get("id") or "primary"),
            "calendar": str(calendar.get("name") or ""), "title": title, "start": start, "end": end,
            "all_day": all_day, "location": " ".join(str(item.get("location") or "").split())[:120],
            "color": str(calendar.get("color") or ""), "can_edit": bool(calendar.get("can_edit", True)),
            "recurring": bool(item.get("recurringEventId"))}


def event_body(title: str | None, start: _dt.datetime | None, end: _dt.datetime | None, *, all_day: bool = False,
               zone_name: str = "", location: str | None = None) -> dict[str, Any]:
    """The JSON for a new event, or the fields to change (None: left as it is): whole days as dates (the end day not
    included), else times with their UTC offset (and the home's zone name when known, so a repeat keeps its time)."""
    body: dict[str, Any] = {}
    if title is not None:
        body["summary"] = title
    if start is not None and end is not None:
        if all_day:
            last = end.date() if end.date() > start.date() else start.date() + _dt.timedelta(days=1)
            body["start"] = {"date": start.date().isoformat()}
            body["end"] = {"date": last.isoformat()}
        else:
            for key, at in (("start", start), ("end", end)):
                body[key] = {"dateTime": at.isoformat(timespec="seconds")}
                if zone_name:
                    body[key]["timeZone"] = zone_name
    if location is not None:
        body["location"] = location
    return body


# ---------------------------------------------------------------- the API
class GoogleCalendar:
    """Calendar API requests as the signed-in owner. Raises CalendarError."""

    def __init__(self, auth: GoogleAuth, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: Callable[[], float] | float = 6.0, base_url: str = API_URL,
                 now: Callable[[], float] = time.time) -> None:
        self.auth = auth
        self._transport = transport
        self._timeout = timeout
        self.base_url = base_url
        self._now = now
        self._client: httpx.AsyncClient | None = None
        self._client_key: tuple[Any, ...] | None = None
        self._calendars: list[dict[str, Any]] | None = None
        self._calendars_t = -1e9

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
        """One request (retried once with a fresh token on HTTP 401). The JSON body, None when empty."""
        timeout = self.timeout()
        resp: httpx.Response | None = None
        for attempt in (0, 1):
            token = await self.auth.access_token(force_refresh=attempt == 1)
            try:
                resp = await asyncio.wait_for(self._http().request(
                    method, path, params=params, json=body, headers={"Authorization": f"Bearer {token}"}), timeout)
            except (httpx.TimeoutException, asyncio.TimeoutError):
                raise CalendarError("timeout") from None
            except httpx.HTTPError:
                raise CalendarError("network") from None
            if resp.status_code != 401:
                break
        assert resp is not None
        if resp.status_code >= 400:
            kind = {401: "auth", 403: "forbidden", 404: "not_found", 410: "not_found", 429: "rate_limited"}.get(
                resp.status_code, "server" if resp.status_code >= 500 else "bad_request")
            if resp.status_code == 403 and _rate_limited(resp):
                kind = "rate_limited"
            log.info("calendar: %s %s answered HTTP %d (%s)", method, path.split("?")[0][:40], resp.status_code, kind)
            raise CalendarError(kind)
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise CalendarError("bad_response") from None

    async def calendars(self, *, fresh: bool = False) -> list[dict[str, Any]]:
        """The calendars they see in Google Calendar (ticked in its list): {id, name, primary, color, can_edit},
        the main one first. Kept an hour."""
        if not fresh and self._calendars is not None and self._now() - self._calendars_t < CALENDARS_TTL_S:
            return self._calendars
        data = await self.call("GET", "/users/me/calendarList", params={"minAccessRole": "reader",
                                                                         "maxResults": 100})
        out = []
        for item in (data or {}).get("items") or []:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            if item.get("hidden") or item.get("selected") is False:
                continue
            out.append({"id": item["id"], "name": str(item.get("summaryOverride") or item.get("summary") or "")[:60],
                        "primary": bool(item.get("primary")), "color": str(item.get("backgroundColor") or ""),
                        "can_edit": item.get("accessRole") in ("owner", "writer")})
        out.sort(key=lambda c: (not c["primary"], c["name"].lower()))
        self._calendars, self._calendars_t = out, self._now()
        return out

    async def events(self, start: _dt.datetime, end: _dt.datetime, tz: _dt.tzinfo, *,
                     query: str | None = None) -> list[dict[str, Any]]:
        """Every calendar's events overlapping [start, end) (single instances, soonest first)."""
        cals = await self.calendars()
        params: dict[str, Any] = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                                  "orderBy": "startTime", "maxResults": MAX_EVENTS}
        if query:
            params["q"] = query[:80]

        async def one(cal: dict[str, Any]) -> list[dict[str, Any]]:
            data = await self.call("GET", f"/calendars/{quote(cal['id'], safe='')}/events", params=params)
            return [e for e in (parse_event(i, cal, tz) for i in (data or {}).get("items") or []) if e]

        found = await asyncio.gather(*(one(c) for c in cals), return_exceptions=True)
        out: list[dict[str, Any]] = []
        failed: list[CalendarError] = []
        for res in found:
            if isinstance(res, CalendarError):
                failed.append(res)
            elif isinstance(res, list):
                out += res
        if failed and not out and len(failed) == len(found):
            raise failed[0]
        out.sort(key=lambda e: (e["start"], not e["all_day"], e["title"].lower()))
        return out

    async def add(self, calendar: dict[str, Any], body: dict[str, Any], tz: _dt.tzinfo) -> dict[str, Any]:
        data = await self.call("POST", f"/calendars/{quote(calendar['id'], safe='')}/events", body=body)
        got = parse_event(data, calendar, tz)
        if got is None:
            raise CalendarError("bad_response")
        return got

    async def change(self, calendar: dict[str, Any], event_id: str, body: dict[str, Any],
                     tz: _dt.tzinfo) -> dict[str, Any]:
        data = await self.call("PATCH", f"/calendars/{quote(calendar['id'], safe='')}/events/"
                                        f"{quote(event_id, safe='')}", body=body)
        got = parse_event(data, calendar, tz)
        if got is None:
            raise CalendarError("bad_response")
        return got

    async def delete(self, calendar: dict[str, Any], event_id: str) -> None:
        await self.call("DELETE", f"/calendars/{quote(calendar['id'], safe='')}/events/{quote(event_id, safe='')}")


def _rate_limited(resp: httpx.Response) -> bool:
    try:
        body = resp.json()
    except ValueError:
        return False
    errors = ((body or {}).get("error") or {}).get("errors") if isinstance(body, dict) else None
    return any(isinstance(e, dict) and "RateLimit" in str(e.get("reason") or "") for e in errors or [])


__all__ = ["API_URL", "AUTH_URL", "CalendarError", "ERRORS", "GoogleAuth", "GoogleCalendar", "REDIRECT_URI",
           "SCOPES", "TOKEN_URL", "Tokens", "clean_client_id", "clean_secret", "event_body", "parse_event"]
