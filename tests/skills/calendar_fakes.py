"""A fake Google Calendar (token endpoint + Calendar API) for the calendar and heads-up tests, and a robot set up
with it."""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx

from littlebot.skills.agenda import Calendar
from littlebot.skills.google_calendar import GoogleAuth, Tokens
from littlebot.testing import make_test_context

TZ = ZoneInfo("America/Toronto")
# Monday 2 November 2026, 10:00 in Toronto (standard time since Sunday)
MONDAY_10 = _dt.datetime(2026, 11, 2, 10, 0, tzinfo=TZ)
SECRET = "GOCSPX-not-a-real-secret-123"
CLIENT_ID = "1234567890-abcdefghij.apps.googleusercontent.com"


def at(day: int, hour: int, minute: int = 0) -> str:
    return _dt.datetime(2026, 11, day, hour, minute, tzinfo=TZ).isoformat()


def timed(eid: str, title: str, day: int, hour: int, minutes: int = 60, **extra: Any) -> dict[str, Any]:
    start = _dt.datetime(2026, 11, day, hour, 0, tzinfo=TZ)
    return {"id": eid, "status": "confirmed", "summary": title, "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": (start + _dt.timedelta(minutes=minutes)).isoformat()}, **extra}


def whole_day(eid: str, title: str, day: int, days: int = 1) -> dict[str, Any]:
    d = _dt.date(2026, 11, day)
    return {"id": eid, "status": "confirmed", "summary": title, "start": {"date": d.isoformat()},
            "end": {"date": (d + _dt.timedelta(days=days)).isoformat()}}


class FakeGoogle:
    """Enough of oauth2.googleapis.com and the Calendar API v3, keeping what is asked."""

    def __init__(self) -> None:
        self.calendars: list[dict[str, Any]] = [
            {"id": "me@example.com", "summary": "me@example.com", "primary": True, "accessRole": "owner",
             "backgroundColor": "#7986cb", "selected": True},
            {"id": "family123@group.calendar.google.com", "summary": "Family", "accessRole": "writer",
             "backgroundColor": "#e67c73", "selected": True},
            {"id": "holidays@group.v.calendar.google.com", "summary": "Holidays in Canada", "accessRole": "reader",
             "backgroundColor": "#0b8043"},
            {"id": "hidden@example.com", "summary": "Old stuff", "accessRole": "owner", "hidden": True},
        ]
        self.events: dict[str, list[dict[str, Any]]] = {
            "me@example.com": [timed("dent", "Dentist", 2, 15, location="Main St Clinic"),
                               timed("soc_20261103", "Soccer", 3, 18, recurringEventId="soc"),
                               timed("gone", "Cancelled thing", 2, 12, status="cancelled"),
                               timed("nope", "Declined meeting", 2, 13,
                                     attendees=[{"email": "me@example.com", "self": True,
                                                 "responseStatus": "declined"}])],
            "family123@group.calendar.google.com": [whole_day("bday", "Zayn's birthday", 3),
                                                    timed("early", "Breakfast", 2, 8)],
            "holidays@group.v.calendar.google.com": [whole_day("hol", "Remembrance Day", 11)],
        }
        self.requests: list[tuple[str, str, Any]] = []
        self.auth_headers: list[str] = []
        self.token_forms: list[dict[str, str]] = []
        self.fail: int | None = None
        self.next_id = 1
        self.ics_text = ""  # served at https://ics.example.com/...
        self.ics_gets = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "ics.example.com":
            self.ics_gets += 1
            if not self.ics_text:
                return httpx.Response(404, text="gone")
            return httpx.Response(200, text=self.ics_text, headers={"Content-Type": "text/calendar"})
        if url.host == "oauth2.googleapis.com":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            self.token_forms.append({k: unquote(v) for k, v in form.items()})
            if self.fail == 400:
                return httpx.Response(400, json={"error": "invalid_grant"})
            body = {"access_token": f"AT{len(self.token_forms)}", "expires_in": 3600, "scope": "x"}
            if form.get("grant_type") == "authorization_code":
                body["refresh_token"] = "RT-secret"
            return httpx.Response(200, json=body)
        self.auth_headers.append(request.headers.get("Authorization", ""))
        body = json.loads(request.content) if request.content else None
        path = url.raw_path.decode().split("?")[0].removeprefix("/calendar/v3")
        self.requests.append((request.method, path, body))
        if self.fail:
            return httpx.Response(self.fail, json={"error": {"message": "nope"}})
        if path == "/users/me/calendarList":
            return httpx.Response(200, json={"items": self.calendars})
        parts = [unquote(p) for p in path.split("/")]
        # ["", "calendars", cid, "events", eid?]
        if len(parts) >= 4 and parts[1] == "calendars" and parts[3] == "events":
            cid = parts[2]
            items = self.events.setdefault(cid, [])
            if request.method == "GET":
                lo = _dt.datetime.fromisoformat(url.params["timeMin"])
                hi = _dt.datetime.fromisoformat(url.params["timeMax"])
                q = url.params.get("q", "").lower()
                out = [e for e in items if _start(e) < hi and _end(e) > lo and q in e.get("summary", "").lower()]
                return httpx.Response(200, json={"items": sorted(out, key=_start)})
            if request.method == "POST":
                new = {"id": f"new{self.next_id}", "status": "confirmed", **(body or {})}
                self.next_id += 1
                items.append(new)
                return httpx.Response(200, json=new)
            eid = parts[4]
            found = next((e for e in items if e["id"] == eid), None)
            if found is None:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            if request.method == "PATCH":
                found.update(body or {})
                return httpx.Response(200, json=found)
            if request.method == "DELETE":
                items.remove(found)
                return httpx.Response(204)
        return httpx.Response(404, json={})

    def posts(self) -> list[Any]:
        return [b for m, _p, b in self.requests if m == "POST"]


def _start(e: dict[str, Any]) -> _dt.datetime:
    s = e["start"]
    return _dt.datetime.fromisoformat(s["dateTime"]) if "dateTime" in s else \
        _dt.datetime.fromisoformat(s["date"]).replace(tzinfo=TZ)


def _end(e: dict[str, Any]) -> _dt.datetime:
    s = e["end"]
    return _dt.datetime.fromisoformat(s["dateTime"]) if "dateTime" in s else \
        _dt.datetime.fromisoformat(s["date"]).replace(tzinfo=TZ)


class Clock:
    def __init__(self, at: _dt.datetime = MONDAY_10) -> None:
        self.t = at.timestamp()

    def __call__(self) -> float:
        return self.t

    def set(self, at: _dt.datetime) -> None:
        self.t = at.timestamp()


def make(*, connected: bool = True, google: FakeGoogle | None = None, clock: Clock | None = None,
         cloud: bool = True, ics: str = "", **config: Any) -> tuple[Any, Calendar, FakeGoogle, Clock]:
    ctx = make_test_context(info={"timezone": "America/Toronto"}, **config)
    if cloud:
        ctx.privacy.set(cloud_enabled=True)
    google = google or FakeGoogle()
    clock = clock or Clock()
    if connected:
        GoogleAuth(ctx.extras["google_calendar_token_file"]).store.write(Tokens(
            client_id=CLIENT_ID, client_secret=SECRET, access_token="AT0", refresh_token="RT-secret",
            expires_at=clock() + 3000, connected_at=clock()))
    if ics:
        from pydantic import SecretStr  # noqa: PLC0415

        ctx.config.calendar_ics_url = SecretStr(ics)
    cal = Calendar(ctx, now=clock, transport=httpx.MockTransport(google))
    shown: list[dict[str, Any]] = []

    async def show(spec: dict[str, Any]) -> bool:
        shown.append(spec)
        return True

    cal.show = show  # type: ignore[method-assign]
    cal.shown = shown  # type: ignore[attr-defined]
    return ctx, cal, google, clock
