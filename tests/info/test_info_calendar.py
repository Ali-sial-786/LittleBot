"""The family's calendars (littlebot/info/calendar.py) against stand-ins for Google Calendar's iCal feeds."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from calendar_fakes import (
    ALI_HOME, ALI_WORK, FEEDS, NOW, PUBLIC, SAM_HOME, FakeGoogle, Monotonic, add_family, calendar_service, event, ics,
)

from littlebot.info.calendar import (
    CalendarError, link_kind, masked, normalize_link, parse_day, read_events, summary_text, trim_feed,
)

TODAY = dt.date(2026, 11, 3)  # a Tuesday


@pytest.fixture
def online(ctx):
    ctx.privacy.set(cloud_enabled=True)
    ctx.config.info.timezone = "America/Toronto"
    return ctx


def titles(agenda):
    return [e["title"] for e in agenda.get("events", [])]


# ---------------------------------------------------------------- links
def test_links_a_parent_may_paste():
    assert normalize_link(ALI_WORK) == ALI_WORK
    assert normalize_link("  " + ALI_WORK + "\n") == ALI_WORK
    assert normalize_link(ALI_WORK.replace("https://", "webcal://")) == ALI_WORK
    assert normalize_link(ALI_WORK.replace("https://", "")) == ALI_WORK
    # a public calendar by its ID, its embed link or its subscribe link: its public iCal address
    public = "https://calendar.google.com/calendar/ical/abc123%40group.calendar.google.com/public/basic.ics"
    assert normalize_link("abc123@group.calendar.google.com") == public
    assert normalize_link("https://calendar.google.com/calendar/embed?src=abc123%40group.calendar.google.com"
                          "&ctz=America%2FToronto") == public
    assert normalize_link("https://calendar.google.com/calendar/u/0?cid=YWJjMTIzQGdyb3VwLmNhbGVuZGFyLmdvb2dsZS5jb20")\
        == public
    # another calendar app's feed is fine too
    assert normalize_link("webcal://p52-caldav.icloud.com/published/2/abc") == \
        "https://p52-caldav.icloud.com/published/2/abc"
    for bad in ("", "hello", "http://calendar.google.com/calendar/ical/x/basic.ics", "https://calendar.google.com/",
                "ftp://example.com/x.ics", "https://user:pw@example.com/x.ics", "x" * 3000):
        with pytest.raises(CalendarError) as e:
            normalize_link(bad)
        assert e.value.kind == "bad_link" and "Secret address in iCal format" in e.value.help


def test_the_dashboard_sees_what_kind_of_link_never_the_link():
    assert link_kind(ALI_WORK) == "google_secret" and masked(ALI_WORK) == "calendar.google.com, secret address"
    assert link_kind(PUBLIC) == "google_public" and masked(PUBLIC) == "calendar.google.com, public address"
    other = "https://outlook.office365.com/owa/calendar/abc/def/calendar.ics"
    assert link_kind(other) == "other" and masked(other) == "outlook.office365.com, calendar link"
    assert "private" not in masked(ALI_WORK) and "0a1b2c" not in masked(ALI_WORK)


# ---------------------------------------------------------------- days
@pytest.mark.parametrize("words, first, days", [
    (None, TODAY, None), ("today", TODAY, None), ("this afternoon", TODAY, None),
    ("tomorrow", dt.date(2026, 11, 4), None), ("tomorrow morning", dt.date(2026, 11, 4), None),
    ("the day after tomorrow", dt.date(2026, 11, 5), None), ("yesterday", dt.date(2026, 11, 2), None),
    ("Friday", dt.date(2026, 11, 6), None), ("on friday", dt.date(2026, 11, 6), None),
    ("tuesday", TODAY, None), ("next Tuesday", dt.date(2026, 11, 10), None), ("this monday", dt.date(2026, 11, 9), None),
    ("this week", TODAY, 6), ("next week", dt.date(2026, 11, 9), 7), ("this weekend", dt.date(2026, 11, 7), 2),
    ("next weekend", dt.date(2026, 11, 14), 2), ("this month", TODAY, 28), ("next month", dt.date(2026, 12, 1), 31),
    ("2026-11-20", dt.date(2026, 11, 20), None), ("20 November", dt.date(2026, 11, 20), None),
    ("November 20th", dt.date(2026, 11, 20), None), ("the 20th", dt.date(2026, 11, 20), None),
    ("the 1st", dt.date(2026, 12, 1), None), ("5 January", dt.date(2027, 1, 5), None),
    ("Dec 25 2027", dt.date(2027, 12, 25), None),
])
def test_the_days_people_ask_about(words, first, days):
    assert parse_day(words, TODAY) == (first, days)


@pytest.mark.parametrize("words", ["someday", "31 February", "the 45th", "Smarch 3", "2026-13-01"])
def test_days_it_cannot_tell(words):
    with pytest.raises(CalendarError) as e:
        parse_day(words, TODAY)
    assert e.value.kind == "bad_date"


def test_sunday_this_weekend_is_what_is_left_of_it():
    assert parse_day("this weekend", dt.date(2026, 11, 8)) == (dt.date(2026, 11, 8), 1)
    assert parse_day("this weekend", dt.date(2026, 11, 7)) == (dt.date(2026, 11, 7), 2)


# ---------------------------------------------------------------- reading a feed
def test_old_one_off_events_are_dropped_before_reading_and_nothing_else_changes():
    tz = __import__("zoneinfo").ZoneInfo("America/Toronto")
    data = FEEDS[ALI_WORK]
    small = trim_feed(data, dt.date(2026, 11, 1))
    assert b"Kick-off from last year" not in small and b"Standup" in small and b"RECURRENCE-ID" in small
    import recurring_ical_events  # noqa: PLC0415
    import icalendar  # noqa: PLC0415

    start, end = dt.datetime(2026, 11, 2, tzinfo=tz), dt.datetime(2027, 2, 1, tzinfo=tz)
    full = recurring_ical_events.of(icalendar.Calendar.from_ical(data)).between(start, end)
    name, events = read_events(data, dt.date(2026, 11, 2), dt.date(2027, 1, 31), tz)
    assert name == "Ali Work" and len(events) == len(full)
    # a folded line (RFC 5545: long lines go on over several) is unfolded first
    folded = data.replace(b"SUMMARY:Dentist - Dr Lee", b"SUMMARY:Dentist -\r\n  Dr Lee")
    assert [e.title for e in read_events(folded, TODAY, TODAY + dt.timedelta(days=3), tz)[1]
            if e.title.startswith("Dentist")] == ["Dentist - Dr Lee"]


# ---------------------------------------------------------------- answers
async def test_whats_on_today_for_everyone(online):
    web = FakeGoogle()
    svc = calendar_service(online, web)
    add_family(svc)
    agenda = await svc.agenda()
    assert agenda["from"] == "today, Tuesday 3 November" and agenda["now"] == "Tuesday 08:00"
    assert agenda["calendars_read"] == ["Ali's Work", "Ali's Personal", "Sam's Personal"]
    standup, call = agenda["events"]
    # the repeating standup, changed for today only (longer), and a call given in UTC, in Toronto time
    assert standup == {"day": "today", "date": "Tuesday 3 November", "time": "09:30-10:30", "title": "Standup (long)",
                       "whose": "Ali", "calendar": "Work"}
    assert call["time"] == "14:00-14:30" and "place" not in call  # a video-call link isn't a place
    assert "1234" not in str(agenda)  # descriptions (dial-in PINs, notes) never go to the AI
    assert web.count() == 3


async def test_one_person_one_calendar_and_the_next_days(online):
    svc = calendar_service(online, FakeGoogle())
    add_family(svc)
    week = await svc.agenda(person="Ali", day="this week")
    assert week["to"] == "Sunday 8 November"
    assert titles(week) == ["Standup (long)", "Planning call", "Conference", "Dentist - Dr Lee", "Family dinner"]
    conference = week["events"][2]
    assert conference["time"] == "all day" and conference["until"] == "Thursday 5 November" and \
        conference["place"] == "Montreal, QC"
    assert "Lunch with Jo" not in titles(week)  # cancelled
    work = await svc.agenda(person="ali's", calendar="work calendar", day="thursday")
    assert titles(work) == ["Dentist - Dr Lee"] and work["events"][0]["place"] == "12 King St, Toronto"
    # next week's Monday standup is there, Tuesday's was cancelled for that day (EXDATE)
    nxt = await svc.agenda(person="Ali", calendar="Work", day="next week")
    assert [(e["date"], e["title"]) for e in nxt["events"]] == [("Monday 9 November", "Standup")]


async def test_a_family_event_on_two_calendars_is_said_once(online):
    svc = calendar_service(online, FakeGoogle())
    add_family(svc)
    friday = await svc.agenda(day="friday")
    assert friday["events"] == [{"day": "Friday", "date": "Friday 6 November", "time": "18:00-20:00",
                                 "title": "Family dinner", "whose": "Ali and Sam", "calendar": "Personal"}]


async def test_whose_calendar(online):
    svc = calendar_service(online, FakeGoogle())
    add_family(svc)
    with pytest.raises(CalendarError) as e:
        await svc.agenda(person="Bob")
    assert e.value.kind == "unknown_person" and "Ali (Work, Personal) and Sam (Personal)" in str(e.value)
    with pytest.raises(CalendarError) as e:
        await svc.agenda(person="my")  # two people have calendars: the model must say whose
    assert e.value.kind == "whose" and "Ali (Work, Personal)" in str(e.value)
    with pytest.raises(CalendarError) as e:
        await svc.agenda(person="Sam", calendar="work")
    assert e.value.kind == "unknown_calendar" and "Sam (Personal)" in str(e.value)
    assert titles(await svc.agenda(person="everyone", day="saturday")) == ["Swimming lessons"]
    svc.remove(3)  # only Ali's calendars left: "my calendar" is Ali's
    assert titles(await svc.agenda(person="me", calendar="work")) == ["Standup (long)", "Planning call"]


async def test_when_is_the_dentist(online):
    svc = calendar_service(online, FakeGoogle())
    add_family(svc)
    found = await svc.agenda(search="the dentist appointment")
    assert found["looked_for"] == "dentist" and titles(found) == ["Dentist - Dr Lee"]
    assert found["to"] == "Thursday 3 December"  # a search looks a month ahead
    swims = await svc.agenda(person="Sam", search="swim")
    assert len(swims["events"]) == 4 and swims["events"][0]["date"] == "Saturday 7 November"
    none = await svc.agenda(search="haircut")
    assert none["note"] == "nothing found with those words" and "events" not in none


async def test_a_quiet_day_says_so(online):
    svc = calendar_service(online, FakeGoogle())
    add_family(svc)
    sunday = await svc.agenda(day="sunday")
    assert sunday["note"] == "nothing on the calendar that day" and "events" not in sunday
    assert summary_text(sunday) == "Nothing on the calendar that day."


async def test_days_past_the_kept_window_are_worked_out_from_the_kept_copy(online):
    web = FakeGoogle()
    svc = calendar_service(online, web)
    add_family(svc)
    await svc.agenda()
    later = await svc.agenda(person="Ali", calendar="work", day="2027-03-01")  # beyond three months: no new request
    assert titles(later) == ["Standup"] and web.count() == 3


# ---------------------------------------------------------------- fast answers
async def test_calendars_are_kept_and_an_older_copy_answers_at_once(online):
    web, mono = FakeGoogle(), Monotonic()
    svc = calendar_service(online, web, now=mono)
    add_family(svc)
    await svc.agenda()
    assert web.count() == 3
    mono.t += 5 * 60
    await svc.agenda(day="tomorrow")
    assert web.count() == 3  # read 5 minutes ago: kept
    mono.t += 10 * 60
    web.delay["*"] = 0.3
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    agenda = await svc.agenda()  # 15 minutes old: answers from the kept copy at once, reads again for next time
    assert titles(agenda)[0] == "Standup (long)" and loop.time() - t0 < 0.2
    await asyncio.sleep(0.4)
    assert web.count() == 6
    await svc.agenda()
    assert web.count() == 6  # the fresh copy is kept
    mono.t += 7 * 3600  # too old to answer from: waits for the read
    t0 = loop.time()
    await svc.agenda()
    assert web.count() == 9 and loop.time() - t0 >= 0.3


async def test_a_slow_calendar_is_left_out_and_ready_next_time(online):
    online.config.calendar.timeout_seconds = 0.2
    web = FakeGoogle()
    web.delay[SAM_HOME] = 0.5
    svc = calendar_service(online, web)
    add_family(svc)
    agenda = await svc.agenda(day="friday")
    assert agenda["not_read"] == [{"calendar": "Sam's Personal", "why": "the calendar didn't answer in time"}]
    assert agenda["events"][0]["whose"] == "Ali"
    await asyncio.sleep(0.5)
    agenda = await svc.agenda(day="friday")
    assert "not_read" not in agenda and agenda["events"][0]["whose"] == "Ali and Sam"


async def test_a_calendar_that_fails_is_left_out_and_marked(online):
    web = FakeGoogle()
    web.status[ALI_HOME] = 404  # the secret address was reset in Google
    svc = calendar_service(online, web)
    add_family(svc)
    agenda = await svc.agenda(day="friday")
    assert agenda["not_read"][0]["calendar"] == "Ali's Personal" and agenda["events"][0]["whose"] == "Sam"
    await asyncio.sleep(0)
    row = next(c for c in svc.status()["calendars"] if c["id"] == 2)
    assert row["ok"] is False and row["problem"] == "not_found" and "Secret address" in row["problem_detail"]


async def test_warm_reads_every_calendar(online):
    web = FakeGoogle()
    svc = calendar_service(online, web)
    add_family(svc)
    await svc.warm()
    assert web.count() == 3
    await svc.agenda()
    assert web.count() == 3


# ---------------------------------------------------------------- gates
async def test_nothing_is_read_while_cloud_is_off_or_offline(ctx):
    web = FakeGoogle()
    svc = calendar_service(ctx, web)
    add_family(svc)
    with pytest.raises(CalendarError) as e:
        await svc.agenda()
    assert e.value.kind == "cloud_off"
    await svc.warm()
    assert web.count() == 0 and svc.status()["blocked"] == "cloud_off"


async def test_no_calendars_yet(online):
    svc = calendar_service(online, FakeGoogle())
    with pytest.raises(CalendarError) as e:
        await svc.agenda()
    assert e.value.kind == "no_calendars" and "dashboard" in str(e.value)
    assert not svc.has_calendars()


# ---------------------------------------------------------------- adding calendars (the dashboard)
async def test_adding_a_calendar_reads_it_first(online):
    web = FakeGoogle()
    svc = calendar_service(online, web)
    cal, check = await svc.add("Ali", "Work", ALI_WORK)
    assert cal["person"] == "Ali" and cal["label"] == "Work" and cal["name"] == "Ali Work" and cal["ok"] is True
    assert check["ok"] and check["name"] == "Ali Work" and check["next"] == "Standup (long): today 09:30"
    # the next two weeks: three standups (not the cancelled one on the 10th), the call and the dentist
    assert check["events_soon"] == cal["events_soon"] == 5
    assert svc.has_calendars() and svc.people() == ["Ali"]
    await svc.agenda()
    assert web.count() == 1  # the read when it was added is kept


async def test_a_calendar_with_no_name_given_is_named_after_itself(online):
    svc = calendar_service(online, FakeGoogle())
    cal, _ = await svc.add("Sam", "", SAM_HOME)
    assert cal["label"] == "Sam"
    cal, _ = await svc.add("Sam", None, PUBLIC)
    assert cal["label"] == "Holidays in Canada" and cal["link_kind"] == "google_public"


async def test_wrong_links_are_not_kept(online):
    web = FakeGoogle()
    web.pages.add("https://calendar.google.com/calendar/ical/x/page.ics")
    svc = calendar_service(online, web)
    for link, kind in (("https://calendar.google.com/calendar/ical/nobody%40gmail.com/private-1/basic.ics",
                        "not_found"), ("https://calendar.google.com/calendar/ical/x/page.ics", "not_calendar"),
                       ("my calendar", "bad_link")):
        with pytest.raises(CalendarError) as e:
            await svc.add("Ali", "Work", link)
        assert e.value.kind == kind
    assert svc.status()["calendars"] == []


async def test_a_link_that_cant_be_checked_now_is_kept(online):
    online.privacy.set(cloud_enabled=False)
    web = FakeGoogle()
    svc = calendar_service(online, web)
    cal, check = await svc.add("Ali", "Work", ALI_WORK)
    assert check["ok"] is False and check["problem"] == "cloud_off" and cal["ok"] is None  # not checked yet
    assert web.count() == 0


async def test_names_and_duplicates(online):
    svc = calendar_service(online, FakeGoogle())
    await svc.add("Ali", "Work", ALI_WORK)
    with pytest.raises(ValueError, match="already has a calendar called Work"):
        await svc.add("ali", "work calendar", ALI_HOME)
    with pytest.raises(ValueError, match="already on the list"):
        await svc.add("Sam", "Office", ALI_WORK)
    for bad in ("", "<script>", "x" * 41, "123"):
        with pytest.raises(ValueError):
            await svc.add(bad, "Personal", ALI_HOME)
    cal, _ = await svc.add("Ali", "Personal", ALI_HOME)
    renamed, check = await svc.update(cal["id"], person="Alison", label="Home")
    assert renamed["person"] == "Alison" and renamed["label"] == "Home" and check is None
    moved, check = await svc.update(cal["id"], link=SAM_HOME)
    assert check["ok"] and moved["name"] == "Sam"
    assert svc.remove(cal["id"]) and not svc.remove(cal["id"])
    assert [c["label"] for c in svc.status()["calendars"]] == ["Work"]


async def test_the_status_never_has_a_link(online):
    svc = calendar_service(online, FakeGoogle())
    add_family(svc)
    await svc.agenda()
    status = str(svc.status())
    for link in (ALI_WORK, ALI_HOME, SAM_HOME):
        assert link not in status and link.split("/private-")[1].split("/")[0] not in status
    assert svc.status()["people"] == ["Ali", "Sam"] and svc.status()["timezone"] == "America/Toronto"


async def test_a_huge_or_not_calendar_file_is_refused(online):
    from littlebot.info import calendar as cal_mod

    web = FakeGoogle({ALI_WORK: ics("Big", *[event(f"e{i}@x", "Thing", "20261103T090000") for i in range(3)])})
    svc = calendar_service(online, web)
    old = cal_mod.MAX_BYTES
    cal_mod.MAX_BYTES = 100
    try:
        with pytest.raises(CalendarError) as e:
            await svc.add("Ali", "Work", ALI_WORK)
        assert e.value.kind == "too_big"
    finally:
        cal_mod.MAX_BYTES = old
    assert NOW.tzinfo is not None
