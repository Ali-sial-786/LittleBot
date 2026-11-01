"""Google Calendar's sign-in and API wrapper (littlebot/skills/google_calendar.py) against a fake Google."""

from __future__ import annotations

import datetime as _dt
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from littlebot.skills.google_calendar import (
    REDIRECT_URI, SCOPES, CalendarError, GoogleAuth, GoogleCalendar, Tokens, clean_client_id, clean_secret,
    event_body, parse_event)
from tests.skills.calendar_fakes import CLIENT_ID, SECRET, TZ, FakeGoogle, timed, whole_day


class Clock:
    t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


def test_client_id_and_secret_are_checked():
    assert clean_client_id(f" {CLIENT_ID} ") == CLIENT_ID
    assert clean_client_id("abc") is None and clean_client_id("123-abc.apps.example.com") is None
    assert clean_secret(SECRET) == SECRET and clean_secret("short") is None and clean_secret("bad/chars!" * 3) is None
    assert clean_secret(f" {SECRET}\n") == SECRET  # a paste with spaces around it


async def test_sign_in_flow(tmp_path):
    google = FakeGoogle()
    clock = Clock()
    auth = GoogleAuth(tmp_path / "g.json", transport=httpx.MockTransport(google), now=clock)
    with pytest.raises(CalendarError, match="Client ID"):
        auth.begin("nope", SECRET)
    with pytest.raises(CalendarError, match="secret"):
        auth.begin(CLIENT_ID, "x")
    url = auth.begin(CLIENT_ID, SECRET)
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert q["client_id"] == CLIENT_ID and q["redirect_uri"] == REDIRECT_URI and q["access_type"] == "offline"
    assert q["scope"].split() == list(SCOPES) and q["code_challenge_method"] == "S256" and SECRET not in url
    with pytest.raises(CalendarError, match="different sign-in"):
        await auth.finish(f"{REDIRECT_URI}?state=wrong&code=abc")
    with pytest.raises(CalendarError, match="whole address"):
        await auth.finish("hello")
    tokens = await auth.finish(f"{REDIRECT_URI}?state={q['state']}&code=4/abc&scope=x")
    form = google.token_forms[-1]
    assert form["grant_type"] == "authorization_code" and form["client_secret"] == SECRET and form["code"] == "4/abc"
    assert len(form["code_verifier"]) >= 43
    assert tokens.refresh_token == "RT-secret" and auth.connected
    assert GoogleAuth(tmp_path / "g.json").tokens() is not None  # kept on disk
    with pytest.raises(CalendarError, match="start the sign-in first"):
        await auth.finish(f"{REDIRECT_URI}?state={q['state']}&code=4/abc")  # a code works once
    status = str(auth.status()) + repr(tokens)
    assert SECRET not in status and "RT-secret" not in status and "AT1" not in status


async def test_denied_and_expired(tmp_path):
    clock = Clock()
    auth = GoogleAuth(tmp_path / "g.json", transport=httpx.MockTransport(FakeGoogle()), now=clock)
    auth.begin(CLIENT_ID, SECRET)
    with pytest.raises(CalendarError, match="cancelled"):
        await auth.finish(f"{REDIRECT_URI}?error=access_denied&state=x")
    url = auth.begin(CLIENT_ID, SECRET)
    clock.t += 16 * 60
    state = parse_qs(urlsplit(url).query)["state"][0]
    with pytest.raises(CalendarError, match="too long"):
        await auth.finish(f"{REDIRECT_URI}?state={state}&code=x")


async def test_refresh_and_revoked(tmp_path):
    google = FakeGoogle()
    clock = Clock()
    path = tmp_path / "g.json"
    auth = GoogleAuth(path, transport=httpx.MockTransport(google), now=clock)
    auth.store.write(Tokens(CLIENT_ID, SECRET, "OLD", "RT-secret", expires_at=clock.t + 30))
    token = await auth.access_token()  # runs out within the margin: refreshed
    assert token == "AT1" and google.token_forms[-1]["grant_type"] == "refresh_token"
    assert auth.tokens().refresh_token == "RT-secret"  # Google didn't send a new one: the old one is kept
    assert await auth.access_token() == "AT1"
    google.fail = 400
    with pytest.raises(CalendarError, match="ended LittleBot's sign-in"):
        await auth.access_token(force_refresh=True)
    assert not auth.connected and not path.exists()


async def test_api_retries_once_on_401_and_names_errors(tmp_path):
    google = FakeGoogle()
    clock = Clock()
    auth = GoogleAuth(tmp_path / "g.json", transport=httpx.MockTransport(google), now=clock)
    auth.store.write(Tokens(CLIENT_ID, SECRET, "AT0", "RT-secret", expires_at=clock.t + 3000))
    api = GoogleCalendar(auth, transport=httpx.MockTransport(google), now=clock)
    google.fail = 401
    with pytest.raises(CalendarError) as e:
        await api.calendars()
    assert e.value.kind == "auth" and google.auth_headers == ["Bearer AT0", "Bearer AT1"]
    for status, kind in ((403, "forbidden"), (404, "not_found"), (429, "rate_limited"), (500, "server"),
                         (400, "bad_request")):
        google.fail = status
        with pytest.raises(CalendarError) as e:
            await api.call("GET", "/users/me/calendarList")
        assert e.value.kind == kind
    google.fail = None
    cals = await api.calendars()
    assert [c["name"] for c in cals] == ["me@example.com", "Family", "Holidays in Canada"]
    assert [c["can_edit"] for c in cals] == [True, True, False]
    n = len(google.requests)
    await api.calendars()
    assert len(google.requests) == n  # kept an hour
    await api.close()


def test_parse_event_shapes():
    cal = {"id": "c", "name": "Me", "color": "#fff", "can_edit": True}
    e = parse_event(timed("a", "  Dentist \n visit ", 2, 15, location="Clinic"), cal, TZ)
    assert e["title"] == "Dentist visit" and e["start"] == _dt.datetime(2026, 11, 2, 15, tzinfo=TZ)
    assert e["end"] - e["start"] == _dt.timedelta(hours=1) and not e["all_day"] and e["location"] == "Clinic"
    d = parse_event(whole_day("b", "Trip", 3, days=2), cal, TZ)
    assert d["all_day"] and d["end"] - d["start"] == _dt.timedelta(days=2)
    assert parse_event({"id": "x", "status": "cancelled", "start": {"date": "2026-11-02"}}, cal, TZ) is None
    assert parse_event({"id": "x", "start": {}}, cal, TZ) is None
    assert parse_event("junk", cal, TZ) is None
    assert parse_event({"id": "x", "start": {"dateTime": "2026-11-02T10:00:00Z"}}, cal, TZ)["title"] == "(no title)"
    assert parse_event(timed("r", "Soccer", 3, 18, recurringEventId="s"), cal, TZ)["recurring"]


def test_event_body():
    start = _dt.datetime(2026, 11, 4, 15, tzinfo=TZ)
    body = event_body("Haircut", start, start + _dt.timedelta(minutes=45), zone_name="America/Toronto",
                      location="Salon")
    assert body == {"summary": "Haircut", "location": "Salon",
                    "start": {"dateTime": "2026-11-04T15:00:00-05:00", "timeZone": "America/Toronto"},
                    "end": {"dateTime": "2026-11-04T15:45:00-05:00", "timeZone": "America/Toronto"}}
    day = event_body("Trip", start, start, all_day=True)
    assert day["start"] == {"date": "2026-11-04"} and day["end"] == {"date": "2026-11-05"}
    assert event_body(None, None, None, location="Park") == {"location": "Park"}
