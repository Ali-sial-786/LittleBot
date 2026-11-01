"""The calendar skill (littlebot/skills/agenda.py) against a fake Google Calendar and a fake iCal feed: when it is
offered, listing (days, words, search), the turn's context, the briefing and glance lines, adding, moving and
cancelling events, and that the iCal address and the tokens never show."""

from __future__ import annotations

import datetime as _dt
import json
import logging

import pytest

from littlebot.skills.hub import SkillHub
from tests.skills.calendar_fakes import MONDAY_10, SECRET, TZ, Clock, FakeGoogle, make

ICS_URL = "https://ics.example.com/private-abc123/basic.ics"
ICS = """BEGIN:VCALENDAR\r
X-WR-CALNAME:School\r
BEGIN:VEVENT\r
UID:pd@school\r
DTSTART;TZID=America/Toronto:20261102T160000\r
DTEND;TZID=America/Toronto:20261102T170000\r
SUMMARY:Parent-teacher night\r
LOCATION:Room 4\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:swim@school\r
DTSTART;TZID=America/Toronto:20261104T170000\r
DTEND;TZID=America/Toronto:20261104T180000\r
RRULE:FREQ=WEEKLY;BYDAY=WE\r
SUMMARY:Swimming\r
END:VEVENT\r
END:VCALENDAR\r
"""


@pytest.fixture
def env():
    ctx, cal, google, clock = make()
    yield ctx, cal, google, clock
    ctx.db.close()


async def events(cal, **args):
    return json.loads(await cal.run("calendar_events", args))


# ------------------------------------------------------------------ when it is offered
def test_offered_once_google_or_an_ical_address_is_there():
    ctx, cal, _, _ = make(connected=False)
    assert not cal.available() and cal.tools()[0]["name"] == "calendar_events"
    ctx.db.close()
    ctx, cal, _, _ = make()
    assert cal.available() and [t["name"] for t in cal.tools()] == [
        "calendar_events", "add_calendar_event", "change_calendar_event", "delete_calendar_event"]
    assert "add_calendar_event" in cal.prompt_note()
    ctx.db.close()
    ctx, cal, _, _ = make(connected=False, ics=ICS_URL)
    assert cal.available() and [t["name"] for t in cal.tools()] == ["calendar_events"]  # read only
    assert "read-only" in cal.prompt_note()
    ctx.db.close()
    ctx, cal, _, _ = make(skills={"calendar": False})
    assert not cal.available()
    ctx.db.close()


async def test_offline_and_cloud_off_say_why():
    ctx, cal, google, _ = make(mode="offline", cloud=False)
    assert (await cal.run("calendar_events", {})).startswith("error: LittleBot is in offline mode")
    ctx.db.close()
    ctx, cal, google, _ = make(cloud=False)
    ctx.privacy.set(cloud_enabled=False)
    assert "cloud communication is off" in await cal.run("calendar_events", {})
    assert google.requests == []
    ctx.db.close()


async def test_the_hub_takes_it_in_and_out():
    ctx, cal, _, _ = make(connected=False)
    hub = SkillHub(ctx, [cal])
    assert hub.specs() == []
    ctx.config.calendar_ics_url = __import__("pydantic").SecretStr(ICS_URL)
    assert [s["name"] for s in hub.specs()] == ["calendar_events"]
    ctx.db.close()


# ------------------------------------------------------------------ listing
async def test_today_lists_every_calendar_without_cancelled_or_declined(env):
    ctx, cal, google, _ = env
    out = await events(cal)
    titles = [e["title"] for e in out["events"]]
    assert titles == ["Breakfast", "Dentist"]  # not the cancelled one, not the declined one, not hidden calendars
    dentist = out["events"][1]
    assert dentist["time"] == "3 pm to 4 pm" and dentist["where"] == "Main St Clinic" and dentist["day"] == "today"
    assert out["events"][0].get("over") is True  # breakfast at 8 is over at 10
    assert out["range"].startswith("today (Monday 2 November") and out["shown_on_screen"]
    assert all(h == "Bearer AT0" for h in google.auth_headers)
    card = cal.shown[-1]
    assert card["card"] == "calendar" and card["title"] == "Today"
    assert [i["text"] for i in card["items"]] == ["Breakfast", "Dentist"] and card["items"][1]["when"] == "3 pm"
    assert card["items"][1]["color"] == "#7986cb"
    hidden = [p for _m, p, _b in google.requests if "hidden" in p]
    assert hidden == []


async def test_days_in_words(env):
    ctx, cal, google, _ = env
    monday = MONDAY_10.date()
    assert cal.day_range("tomorrow") == (monday + _dt.timedelta(days=1), 1)
    assert cal.day_range("friday") == (_dt.date(2026, 11, 6), 1)
    assert cal.day_range("monday") == (monday, 1)
    assert cal.day_range("next monday") == (_dt.date(2026, 11, 9), 1)
    assert cal.day_range("this week") == (monday, 7)
    assert cal.day_range("next week") == (_dt.date(2026, 11, 9), 7)
    assert cal.day_range("this weekend") == (_dt.date(2026, 11, 7), 2)
    assert cal.day_range("2026-11-11") == (_dt.date(2026, 11, 11), 1)
    assert cal.day_range("2026-11-11", 3) == (_dt.date(2026, 11, 11), 3)
    assert cal.day_range("the 11th") == (_dt.date(2026, 11, 11), 1)
    out = await events(cal, **{"from": "tomorrow"})
    assert [(e["title"], e["time"]) for e in out["events"]] == [("Zayn's birthday", "all day"), ("Soccer",
                                                                                                 "6 pm to 7 pm")]
    assert out["events"][1]["repeats"] is True and cal.shown[-1]["title"] == "Tomorrow"
    week = await events(cal, **{"from": "this week"})
    assert [e["title"] for e in week["events"]] == ["Breakfast", "Dentist", "Zayn's birthday", "Soccer"]
    assert cal.shown[-1]["title"] == "This week"  # Monday to Sunday: Remembrance Day (the 11th) is next week
    assert [e["title"] for e in (await events(cal, **{"from": "next week"}))["events"]] == ["Remembrance Day"]
    assert "error" in await cal.run("calendar_events", {"from": "the day after the moon landing"})


async def test_search_looks_ahead(env):
    ctx, cal, google, _ = env
    out = await events(cal, search="dentist")
    assert [e["title"] for e in out["events"]] == ["Dentist"] and "180 days" in out["range"]
    gets = [r for r in google.requests if r[0] == "GET" and "events" in r[1]]
    assert gets
    none = await cal.run("calendar_events", {"search": "volcano"})
    assert none.startswith("ok: nothing matching 'volcano'")


async def test_one_calendar_failing_still_answers(env):
    ctx, cal, google, _ = env
    google.calendars[1]["id"] = "broken@x"  # its events 404
    out = await events(cal)
    assert [e["title"] for e in out["events"]] == ["Dentist"]
    google.fail = 500
    cal.google._calendars = None
    assert (await cal.run("calendar_events", {"from": "friday"})).startswith("error: Google Calendar had a problem")


# ------------------------------------------------------------------ the background copy
async def test_context_briefing_and_glance_from_the_copy(env):
    ctx, cal, google, clock = env
    assert cal.context_note() == ""  # nothing fetched yet: nothing claimed
    assert await cal.refresh()
    note = cal.context_note()
    assert note.startswith("Calendar: today 3 pm to 4 pm Dentist (e")
    assert "at Main St Clinic" in note and "tomorrow all day Zayn's birthday" in note and "Breakfast" not in note
    assert cal.briefing_note("morning") == ("Calendar today: 8 am to 9 am: Breakfast; 3 pm to 4 pm: Dentist at Main "
                                            "St Clinic.")
    assert cal.briefing_note("evening").startswith("Calendar tomorrow: all day: Zayn's birthday; 6 pm to 7 pm: Soccer")
    assert cal.glance_lines() == ["3 pm Dentist"]
    clock.set(MONDAY_10.replace(hour=15, minute=30))
    await cal.refresh()
    assert "happening now" in cal.context_note()
    clock.set(MONDAY_10.replace(hour=17))
    await cal.refresh()
    assert cal.glance_lines() == ["Tomorrow 6 pm Soccer"]
    clock.t += 20 * 60  # 20 minutes old: still used
    assert cal.context_note()
    clock.t += 20 * 60  # too old: not claimed any more
    assert cal.context_note() == "" and cal.briefing_note() == "" and cal.glance_lines() == []


async def test_soon_is_for_heads_ups(env):
    ctx, cal, google, clock = env
    clock.set(MONDAY_10.replace(hour=14, minute=31))
    await cal.refresh()
    assert [e.title for e in cal.soon(30 * 60)] == ["Dentist"]
    assert cal.soon(10 * 60) == []
    clock.set(MONDAY_10.replace(hour=15, minute=1))
    assert cal.soon(30 * 60) == []  # started


async def test_listing_uses_a_fresh_copy(env):
    ctx, cal, google, clock = env
    await cal.refresh()
    n = len(google.requests)
    await events(cal)
    assert len(google.requests) == n  # from the copy fetched a moment ago
    clock.t += 120
    await events(cal)
    assert len(google.requests) > n
    google.fail = 503
    out = await events(cal)  # Google down: the copy answers
    assert [e["title"] for e in out["events"]] == ["Breakfast", "Dentist"]


# ------------------------------------------------------------------ adding
async def test_add_with_an_iso_time(env):
    ctx, cal, google, _ = env
    res = await cal.run("add_calendar_event", {"title": "Haircut", "start": "2026-11-04T15:00", "where": "Salon"})
    assert res.startswith("ok: added 'Haircut' on Wednesday 4 November, 3 pm to 4 pm at Salon to the "
                          "me@example.com calendar (id e")
    body = google.posts()[-1]
    assert body["summary"] == "Haircut" and body["location"] == "Salon"
    assert body["start"] == {"dateTime": "2026-11-04T15:00:00-05:00", "timeZone": "America/Toronto"}
    assert body["end"]["dateTime"] == "2026-11-04T16:00:00-05:00"
    card = cal.shown[-1]
    assert card["highlight"] and card["title"] == "Wednesday 4 november".capitalize() or card["items"]


async def test_add_in_words_all_day_and_to_a_named_calendar(env):
    ctx, cal, google, _ = env
    res = await cal.run("add_calendar_event", {"title": "Dentist", "start": "Tuesday at 3", "minutes": 30})
    assert "tomorrow, 3 pm to 3:30 pm" in res
    assert google.posts()[-1]["start"]["dateTime"] == "2026-11-03T15:00:00-05:00"
    res = await cal.run("add_calendar_event", {"title": "Pizza day", "start": "2026-11-13", "all_day": True,
                                               "calendar": "family"})
    assert "all day" in res and "to the Family calendar" in res
    path = [p for m, p, _b in google.requests if m == "POST"][-1]
    assert "family123" in path
    assert google.posts()[-1]["start"] == {"date": "2026-11-13"} and google.posts()[-1]["end"] == {"date": "2026-11-14"}
    res = await cal.run("add_calendar_event", {"title": "Trip", "start": "2026-11-20", "end": "2026-11-22"})
    assert google.posts()[-1]["end"] == {"date": "2026-11-23"} and "until Sunday 22 November" in res
    bad = await cal.run("add_calendar_event", {"title": "X", "start": "2026-11-05T10:00", "calendar": "Holidays"})
    assert bad.startswith("error: there's no calendar called") and "Family" in bad  # read-only ones aren't offered


async def test_add_refuses_what_makes_no_sense(env):
    ctx, cal, google, _ = env
    assert (await cal.run("add_calendar_event", {"start": "2026-11-04T15:00"})).startswith("error: say what")
    assert "passed" in await cal.run("add_calendar_event", {"title": "Old", "start": "2026-11-01T09:00"})
    assert "after the start" in await cal.run("add_calendar_event", {"title": "X", "start": "2026-11-04T15:00",
                                                                     "end": "2026-11-04T14:00"})
    await cal.refresh()
    again = await cal.run("add_calendar_event", {"title": "dentist", "start": "2026-11-02T15:00"})
    assert again.startswith("ok: that's already on the calendar")


async def test_read_only_calendar_cannot_add():
    ctx, cal, google, _ = make(connected=False, ics=ICS_URL)
    google.ics_text = ICS
    assert (await cal.run("add_calendar_event", {"title": "X", "start": "2026-11-04T15:00"})).startswith(
        "error: the calendar is read-only")
    ctx.db.close()


# ------------------------------------------------------------------ changing and cancelling
async def test_move_keeps_the_day_and_the_length(env):
    ctx, cal, google, _ = env
    out = await events(cal)
    dentist = next(e for e in out["events"] if e["title"] == "Dentist")
    res = await cal.run("change_calendar_event", {"id": dentist["id"], "start": "4:30 pm"})
    assert res.startswith(f"ok: changed: now 'Dentist' today, 4:30 pm to 5:30 pm at Main St Clinic (id {dentist['id']})")
    patch = [b for m, _p, b in google.requests if m == "PATCH"][-1]
    assert patch == {"start": {"dateTime": "2026-11-02T16:30:00-05:00", "timeZone": "America/Toronto"},
                     "end": {"dateTime": "2026-11-02T17:30:00-05:00", "timeZone": "America/Toronto"}}
    res = await cal.run("change_calendar_event", {"id": dentist["id"], "title": "Dentist (Amara)"})
    assert "'Dentist (Amara)'" in res
    assert [b for m, _p, b in google.requests if m == "PATCH"][-1] == {"summary": "Dentist (Amara)"}
    soccer = (await events(cal, **{"from": "tomorrow"}))["events"][1]
    assert "only this time" in await cal.run("change_calendar_event", {"id": soccer["id"],
                                                                        "start": "2026-11-03T19:00"})
    assert "say what to change" in await cal.run("change_calendar_event", {"id": dentist["id"]})
    assert "no event" in await cal.run("change_calendar_event", {"id": "e999", "title": "x"})


async def test_cancel_asks_first(env):
    ctx, cal, google, _ = env
    await cal.refresh()
    key = next(e.key for e in cal._cache if e.title == "Dentist")
    ask = await cal.run("delete_calendar_event", {"id": key})
    assert ask.startswith("ok: not deleted yet. Ask them: cancel 'Dentist' today, 3 pm to 4 pm")
    assert not [r for r in google.requests if r[0] == "DELETE"]
    done = await cal.run("delete_calendar_event", {"id": key, "confirmed": True})
    assert done.startswith("ok: cancelled 'Dentist'")
    assert [r for r in google.requests if r[0] == "DELETE"]
    assert "Dentist" not in cal.context_note()
    holiday = (await events(cal, **{"from": "2026-11-11"}))["events"][0]
    assert "can only read" in await cal.run("delete_calendar_event", {"id": holiday["id"], "confirmed": True})


# ------------------------------------------------------------------ iCal addresses
async def test_ical_feed_is_read_and_its_address_never_shows(caplog):
    caplog.set_level(logging.DEBUG)
    ctx, cal, google, _ = make(connected=False, ics=ICS_URL)
    google.ics_text = ICS
    out = await events(cal, **{"from": "this week"})
    assert [(e["title"], e["calendar"]) for e in out["events"]] == [("Parent-teacher night", "School"),
                                                                     ("Swimming", "School")]
    assert out["read_only"] is True
    await events(cal, **{"from": "this week"})
    assert google.ics_gets == 1  # kept a while
    google.ics_text = ""
    cal._ics.clear()
    assert "address didn't work" in await cal.run("calendar_events", {"from": "friday"})
    # httpx logs every address it asks, but the robot keeps httpx's log at WARNING (core/logging_setup.py)
    ours = " ".join(r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore")))
    blob = ours + json.dumps(cal.status()) + cal.context_note()
    assert "private-abc123" not in blob and "ics.example.com" not in blob
    ctx.db.close()


async def test_google_and_its_own_ical_copy_are_not_listed_twice():
    google = FakeGoogle()
    google.ics_text = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:dent\r\nDTSTART:20261102T200000Z\r\n"
                       "DTEND:20261102T210000Z\r\nSUMMARY:Dentist\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    ctx, cal, _, _ = make(google=google, ics=ICS_URL)
    out = await events(cal)
    assert [e["title"] for e in out["events"]] == ["Breakfast", "Dentist"]
    assert "calendar" in out["events"][1] and out["events"][1]["calendar"] == "me@example.com"
    ctx.db.close()


def test_secrets_never_in_status_or_repr(env):
    ctx, cal, google, _ = env
    tokens = cal.auth.tokens()
    blob = json.dumps(cal.status()) + repr(tokens)
    assert SECRET not in blob and "RT-secret" not in blob and "AT0" not in blob


async def test_refresh_bumps_the_version_and_reloads_a_new_sign_in():
    ctx, cal, google, clock = make(connected=False)
    assert not cal.auth.connected
    from littlebot.skills.google_calendar import GoogleAuth, Tokens  # noqa: PLC0415
    from tests.skills.calendar_fakes import CLIENT_ID  # noqa: PLC0415

    GoogleAuth(ctx.extras["google_calendar_token_file"]).store.write(Tokens(
        client_id=CLIENT_ID, client_secret=SECRET, access_token="AT0", refresh_token="RT-secret",
        expires_at=clock() + 3000))
    assert not cal.auth.connected  # read once
    cal.auth.reload()
    assert cal.auth.connected and cal.available()
    v = cal.version
    assert await cal.refresh() and cal.version == v + 1
    ctx.db.close()


def test_zone_is_the_homes():
    ctx, cal, _, _ = make(clock=Clock())
    assert cal.zone_name() == "America/Toronto" and cal.local().tzinfo == TZ
    ctx.db.close()
