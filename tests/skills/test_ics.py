"""iCalendar feed parsing and expansion (littlebot/skills/ics.py): lines, times, zones, repeats, overrides, junk."""

from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo

from littlebot.skills import ics
from littlebot.skills.ics import Occurrence, occurrences, parse

TOR = ZoneInfo("America/Toronto")
UTC = dt.timezone.utc


def cal(*events: str, name: str = "Family", crlf: bool = True) -> str:
    body = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN", f"X-WR-CALNAME:{name}"]
    for ev in events:
        body += ["BEGIN:VEVENT", *[ln for ln in ev.strip("\n").split("\n")], "END:VEVENT"]
    body.append("END:VCALENDAR")
    return ("\r\n" if crlf else "\n").join(body) + "\r\n"


def local(y: int, m: int, d: int, h: int = 0, mi: int = 0, tz: dt.tzinfo = TOR) -> dt.datetime:
    return dt.datetime(y, m, d, h, mi, tzinfo=tz)


def window(feed_text: str, start: dt.datetime, end: dt.datetime, tz: dt.tzinfo = TOR, **kw) -> list[Occurrence]:
    return occurrences(parse(feed_text), start, end, tz, **kw)


def starts(occ: list[Occurrence]) -> list[dt.datetime]:
    return [o.start for o in occ]


# ---------------------------------------------------------------- lines, parameters, text

def test_unfolding_params_quotes_and_unescaping():
    text = cal(
        "UID:a1\n"
        "SUMMARY:Dentist for\n"
        " Sam\\, then\n"
        "\tlunch\\nat noon\n"
        "LOCATION:12 Main St\\; Suite 4\\\\B\n"
        "DESCRIPTION;ALTREP=\"cid:part1\":ignored text: with colons\n"
        "DTSTART;TZID=\"America/Toronto\":20261103T150000\n"
        "DTEND;TZID=America/Toronto:20261103T160000"
    )
    (o,) = window(text, local(2026, 11, 3), local(2026, 11, 4))
    assert o.uid == "a1"
    assert o.title == "Dentist forSam, thenlunch at noon"  # folds join with no space; \n becomes one space
    assert o.location == "12 Main St; Suite 4\\B"
    assert o.start == local(2026, 11, 3, 15) and o.end == local(2026, 11, 3, 16)
    assert not o.all_day and o.calendar == "Family"


def test_quoted_parameter_holding_a_colon():
    text = cal('UID:q\nSUMMARY:Call\nDTSTART;TZID="(UTC-05:00) Eastern Time (US & Canada)":20261110T090000\n'
               "DURATION:PT30M")
    (o,) = window(text, local(2026, 11, 10), local(2026, 11, 11), tz=UTC)
    assert o.start == local(2026, 11, 10, 9)  # read in Toronto time, given back in the caller's zone
    assert o.start.tzinfo is UTC and o.end - o.start == dt.timedelta(minutes=30)


def test_lf_and_crlf_read_the_same():
    ev = "UID:x\nSUMMARY:Swim\nDTSTART:20261105T220000Z\nDTEND:20261105T230000Z"
    a = window(cal(ev, crlf=True), local(2026, 11, 5), local(2026, 11, 6))
    b = window(cal(ev, crlf=False), local(2026, 11, 5), local(2026, 11, 6))
    assert a == b and len(a) == 1


def test_empty_summary_and_calendar_name():
    (o,) = window(cal("UID:n\nDTSTART:20261105T120000Z", name="Kids\\, school"), local(2026, 11, 5),
                  local(2026, 11, 6))
    assert o.title == "(no title)" and o.calendar == "Kids, school"


# ---------------------------------------------------------------- DTSTART / DTEND / DURATION

def test_duration_and_missing_dtend():
    text = cal(
        "UID:d1\nSUMMARY:Practice\nDTSTART;TZID=America/Toronto:20261104T170000\nDURATION:PT1H30M",
        "UID:d2\nSUMMARY:Trip\nDTSTART;VALUE=DATE:20261104\nDURATION:P2D",
        "UID:d3\nSUMMARY:Holiday\nDTSTART;VALUE=DATE:20261105",
        "UID:d4\nSUMMARY:Deadline\nDTSTART;TZID=America/Toronto:20261105T120000",
    )
    occ = {o.uid: o for o in window(text, local(2026, 11, 4), local(2026, 11, 8))}
    assert occ["d1"].end - occ["d1"].start == dt.timedelta(hours=1, minutes=30)
    assert occ["d2"].all_day and occ["d2"].end == local(2026, 11, 6)
    assert occ["d3"].start == local(2026, 11, 5) and occ["d3"].end == local(2026, 11, 6)  # one day
    assert occ["d4"].start == occ["d4"].end == local(2026, 11, 5, 12)  # zero length, still listed


def test_all_day_event_spanning_days_overlaps_window():
    text = cal("UID:camp\nSUMMARY:Camp\nDTSTART;VALUE=DATE:20261030\nDTEND;VALUE=DATE:20261104")
    (o,) = window(text, local(2026, 11, 2), local(2026, 11, 3))
    assert o.all_day and o.start == local(2026, 10, 30) and o.end == local(2026, 11, 4)
    assert window(text, local(2026, 11, 4), local(2026, 11, 5)) == []  # DTEND is exclusive


# ---------------------------------------------------------------- zones

def test_utc_floating_and_unknown_tzid():
    text = cal(
        "UID:u\nSUMMARY:UTC\nDTSTART:20261105T140000Z\nDTEND:20261105T150000Z",
        "UID:f\nSUMMARY:Floating\nDTSTART:20261105T090000\nDTEND:20261105T100000",
        "UID:k\nSUMMARY:Unknown\nDTSTART;TZID=Mars/Olympus:20261105T110000\nDTEND;TZID=Mars/Olympus:20261105T120000",
    )
    occ = {o.uid: o for o in window(text, local(2026, 11, 5), local(2026, 11, 6))}
    assert occ["u"].start == local(2026, 11, 5, 9)  # 14:00 UTC is 9:00 EST
    assert occ["f"].start == local(2026, 11, 5, 9) and occ["f"].start.tzinfo is TOR
    assert occ["k"].start == local(2026, 11, 5, 11)


def test_windows_tzid_from_outlook():
    text = cal("UID:w\nSUMMARY:Standup\nDTSTART;TZID=Pacific Standard Time:20261105T090000\n"
               "DTEND;TZID=Pacific Standard Time:20261105T093000",
               "UID:e\nSUMMARY:Sync\nDTSTART;TZID=Eastern Standard Time:20261105T100000\nDURATION:PT1H")
    occ = {o.uid: o for o in window(text, local(2026, 11, 5), local(2026, 11, 6))}
    assert occ["w"].start == local(2026, 11, 5, 9, tz=ZoneInfo("America/Los_Angeles"))
    assert occ["e"].start == local(2026, 11, 5, 10)


def test_vtimezone_x_lic_location_alias():
    text = cal("UID:v\nSUMMARY:Call\nDTSTART;TZID=My Office Zone:20261105T100000\nDURATION:PT1H")
    text = text.replace("BEGIN:VEVENT", "BEGIN:VTIMEZONE\r\nTZID:My Office Zone\r\nX-LIC-LOCATION:Europe/London\r\n"
                        "END:VTIMEZONE\r\nBEGIN:VEVENT", 1)
    (o,) = window(text, local(2026, 11, 5), local(2026, 11, 6))
    assert o.start == local(2026, 11, 5, 10, tz=ZoneInfo("Europe/London"))


def test_cancelled_event_skipped():
    text = cal("UID:c\nSUMMARY:Off\nSTATUS:CANCELLED\nDTSTART:20261105T140000Z",
               "UID:ok\nSUMMARY:On\nSTATUS:CONFIRMED\nDTSTART:20261105T150000Z")
    assert [o.uid for o in window(text, local(2026, 11, 5), local(2026, 11, 6))] == ["ok"]


# ---------------------------------------------------------------- RRULE

def test_weekly_keeps_wall_clock_time_across_dst():
    # Toronto leaves daylight time on Sunday 1 November 2026.
    text = cal("UID:dst\nSUMMARY:Piano\nDTSTART;TZID=America/Toronto:20261026T090000\n"
               "DTEND;TZID=America/Toronto:20261026T100000\nRRULE:FREQ=WEEKLY",
               "UID:day\nSUMMARY:Walk\nDTSTART;TZID=America/Toronto:20261030T073000\nDURATION:PT1H\n"
               "RRULE:FREQ=DAILY;COUNT=4")
    occ = window(text, local(2026, 10, 26), local(2026, 11, 3))
    piano = [o for o in occ if o.uid == "dst"]
    assert [(o.start.hour, o.end.hour) for o in piano] == [(9, 10), (9, 10)]
    assert [o.start.astimezone(UTC).hour for o in piano] == [13, 14]
    walk = [o.start for o in occ if o.uid == "day"]
    assert walk == [local(2026, 10, d, 7, 30) for d in (30, 31)] + [local(2026, 11, d, 7, 30) for d in (1, 2)]
    assert all(o.end - o.start == dt.timedelta(hours=1) for o in occ)


def test_monthly_second_tuesday_last_friday_and_negative_monthday():
    text = cal("UID:t\nSUMMARY:Council\nDTSTART;TZID=America/Toronto:20260113T190000\nRRULE:FREQ=MONTHLY;BYDAY=2TU",
               "UID:f\nSUMMARY:Payday\nDTSTART;VALUE=DATE:20260130\nRRULE:FREQ=MONTHLY;BYDAY=-1FR",
               "UID:e\nSUMMARY:Rent\nDTSTART;VALUE=DATE:20260131\nRRULE:FREQ=MONTHLY;BYMONTHDAY=-1")
    occ = window(text, local(2026, 11, 1), local(2027, 1, 1))
    by = lambda uid: [o.start.date() for o in occ if o.uid == uid]  # noqa: E731
    assert by("t") == [dt.date(2026, 11, 10), dt.date(2026, 12, 8)]
    assert by("f") == [dt.date(2026, 11, 27), dt.date(2026, 12, 25)]
    assert by("e") == [dt.date(2026, 11, 30), dt.date(2026, 12, 31)]
    feb = window(text, local(2026, 2, 1), local(2026, 3, 1))
    assert [o.start.date() for o in feb if o.uid == "e"] == [dt.date(2026, 2, 28)]


def test_monthly_plain_skips_short_months_and_bymonthday():
    text = cal("UID:m\nSUMMARY:Bill\nDTSTART;VALUE=DATE:20260131\nRRULE:FREQ=MONTHLY",
               "UID:b\nSUMMARY:Bins\nDTSTART;VALUE=DATE:20260101\nRRULE:FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=1,15")
    occ = window(text, local(2026, 1, 1), local(2026, 6, 1))
    assert [o.start.date() for o in occ if o.uid == "m"] == [dt.date(2026, 1, 31), dt.date(2026, 3, 31),
                                                             dt.date(2026, 5, 31)]
    assert [o.start.date() for o in occ if o.uid == "b"] == [dt.date(2026, 1, 1), dt.date(2026, 1, 15),
                                                             dt.date(2026, 3, 1), dt.date(2026, 3, 15),
                                                             dt.date(2026, 5, 1), dt.date(2026, 5, 15)]


def test_yearly_rules():
    text = cal("UID:bday\nSUMMARY:Mom's birthday\nDTSTART;VALUE=DATE:19700412\nRRULE:FREQ=YEARLY",
               "UID:tg\nSUMMARY:Thanksgiving\nDTSTART;VALUE=DATE:20201012\nRRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=2MO",
               "UID:leap\nSUMMARY:Leap\nDTSTART;VALUE=DATE:20240229\nRRULE:FREQ=YEARLY")
    occ = window(text, local(2026, 1, 1), local(2027, 1, 1))
    assert {o.uid: o.start.date() for o in occ} == {"bday": dt.date(2026, 4, 12), "tg": dt.date(2026, 10, 12)}
    assert [o.uid for o in window(text, local(2028, 2, 1), local(2028, 3, 1))] == ["leap"]


def test_weekly_from_2019_jumps_to_the_window(monkeypatch):
    # With only 10 periods allowed, walking week by week from 2019 could never reach 2026.
    monkeypatch.setattr(ics, "MAX_PERIODS", 10)
    text = cal("UID:old\nSUMMARY:Soccer\nDTSTART;TZID=America/Toronto:20190107T180000\nDURATION:PT1H\n"
               "RRULE:FREQ=WEEKLY;BYDAY=MO,WE")
    occ = window(text, local(2026, 11, 2), local(2026, 11, 9))
    assert starts(occ) == [local(2026, 11, 2, 18), local(2026, 11, 4, 18)]


def test_weekly_interval_and_wkst():
    text = cal("UID:i\nSUMMARY:Cleaning\nDTSTART;TZID=America/Toronto:20261102T100000\n"
               "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,FR")
    occ = window(text, local(2026, 11, 1), local(2026, 11, 30))
    assert [o.start.day for o in occ] == [2, 6, 16, 20]


def test_count_counts_from_dtstart():
    text = cal("UID:c3\nSUMMARY:Lessons\nDTSTART;TZID=America/Toronto:20261102T160000\nRRULE:FREQ=WEEKLY;COUNT=3",
               "UID:c400\nSUMMARY:Club\nDTSTART;TZID=America/Toronto:20190107T180000\nRRULE:FREQ=WEEKLY;COUNT=400")
    occ = window(text, local(2026, 11, 1), local(2027, 1, 1))
    assert [o.start.day for o in occ if o.uid == "c3"] == [2, 9, 16]
    last = dt.date(2019, 1, 7) + dt.timedelta(weeks=399)
    club = [o for o in window(text, local(2026, 1, 1), local(2028, 1, 1)) if o.uid == "c400"]
    assert club[-1].start == local(last.year, last.month, last.day, 18)
    assert len(club) == len([w for w in range(400) if dt.date(2019, 1, 7) + dt.timedelta(weeks=w)
                             >= dt.date(2026, 1, 1)])


def test_until_date_and_datetime():
    text = cal("UID:ud\nSUMMARY:Camp\nDTSTART;TZID=America/Toronto:20261101T080000\nRRULE:FREQ=DAILY;UNTIL=20261104",
               "UID:uz\nSUMMARY:Shift\nDTSTART;TZID=America/Toronto:20261101T080000\n"
               "RRULE:FREQ=DAILY;UNTIL=20261103T130000Z",
               "UID:ul\nSUMMARY:Local\nDTSTART:20261101T080000\nRRULE:FREQ=DAILY;UNTIL=20261102T080000")
    occ = window(text, local(2026, 10, 1), local(2026, 12, 1))
    assert [o.start.day for o in occ if o.uid == "ud"] == [1, 2, 3, 4]
    assert [o.start.day for o in occ if o.uid == "uz"] == [1, 2, 3]  # 13:00Z on 3 Nov is 8:00 EST: included
    assert [o.start.day for o in occ if o.uid == "ul"] == [1, 2]


def test_bysetpos_last_weekday_of_month():
    text = cal("UID:p\nSUMMARY:Report\nDTSTART;VALUE=DATE:20260130\n"
               "RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1")
    occ = window(text, local(2026, 10, 1), local(2027, 1, 1))
    assert [o.start.date() for o in occ] == [dt.date(2026, 10, 30), dt.date(2026, 11, 30), dt.date(2026, 12, 31)]


def test_unsupported_freq_lists_first_instance_only():
    text = cal("UID:h\nSUMMARY:Pill\nDTSTART:20261105T120000Z\nRRULE:FREQ=HOURLY;COUNT=5")
    assert len(window(text, local(2026, 11, 5), local(2026, 11, 6))) == 1


# ---------------------------------------------------------------- EXDATE, RDATE, RECURRENCE-ID

def test_exdate_lines_lists_zones_and_dates():
    text = cal("UID:x\nSUMMARY:Gym\nDTSTART;TZID=America/Toronto:20261102T070000\nRRULE:FREQ=DAILY;COUNT=7\n"
               "EXDATE;TZID=America/Toronto:20261103T070000,20261104T070000\n"
               "EXDATE:20261106T120000Z",
               "UID:ad\nSUMMARY:School\nDTSTART;VALUE=DATE:20261102\nRRULE:FREQ=DAILY;COUNT=5\n"
               "EXDATE;VALUE=DATE:20261103\nEXDATE;VALUE=DATE:20261105")
    occ = window(text, local(2026, 11, 1), local(2026, 11, 10))
    assert [o.start.day for o in occ if o.uid == "x"] == [2, 5, 7, 8]
    assert [o.start.day for o in occ if o.uid == "ad"] == [2, 4, 6]


def test_rdate_adds_instances():
    text = cal("UID:r\nSUMMARY:Recital\nDTSTART;TZID=America/Toronto:20261102T180000\nDURATION:PT2H\n"
               "RDATE;TZID=America/Toronto:20261105T180000,20261107T130000\nRDATE:20261108T230000Z")
    occ = window(text, local(2026, 11, 1), local(2026, 11, 10))
    assert starts(occ) == [local(2026, 11, 2, 18), local(2026, 11, 5, 18), local(2026, 11, 7, 13),
                           local(2026, 11, 8, 18)]
    assert all(o.end - o.start == dt.timedelta(hours=2) for o in occ)


def test_moved_and_cancelled_instances():
    master = ("UID:s\nSUMMARY:Swim\nDTSTART;TZID=America/Toronto:20261102T170000\nDURATION:PT1H\n"
              "RRULE:FREQ=WEEKLY;BYDAY=MO,WE")
    moved = ("UID:s\nRECURRENCE-ID;TZID=America/Toronto:20261104T170000\nSUMMARY:Swim (moved)\n"
             "DTSTART;TZID=America/Toronto:20261105T180000\nDTEND;TZID=America/Toronto:20261105T190000")
    gone = "UID:s\nRECURRENCE-ID:20261109T220000Z\nSTATUS:CANCELLED\nDTSTART:20261109T220000Z\nSUMMARY:Swim"
    occ = window(cal(moved, master, gone), local(2026, 11, 1), local(2026, 11, 13))
    assert [(o.start, o.title) for o in occ] == [
        (local(2026, 11, 2, 17), "Swim"),
        (local(2026, 11, 5, 18), "Swim (moved)"),
        (local(2026, 11, 11, 17), "Swim"),
    ]


def test_instance_moved_out_of_the_window_is_gone_from_it():
    master = "UID:m\nSUMMARY:Tea\nDTSTART:20261102T150000Z\nRRULE:FREQ=DAILY"
    moved = "UID:m\nRECURRENCE-ID:20261103T150000Z\nSUMMARY:Tea\nDTSTART:20261120T150000Z"
    occ = window(cal(master, moved), local(2026, 11, 3), local(2026, 11, 4))
    assert occ == []


# ---------------------------------------------------------------- limits, junk, size

def test_sorted_and_limited():
    text = cal("UID:b\nSUMMARY:B\nDTSTART:20261105T150000Z", "UID:a\nSUMMARY:A\nDTSTART:20261105T140000Z",
               "UID:d\nSUMMARY:Daily\nDTSTART:20261101T120000Z\nRRULE:FREQ=DAILY")
    occ = window(text, local(2026, 11, 1), local(2026, 12, 1), limit=5)
    assert len(occ) == 5 and starts(occ) == sorted(starts(occ))
    assert [o.uid for o in window(text, local(2026, 11, 5), local(2026, 11, 6))] == ["d", "a", "b"]


def test_junk_never_raises():
    junk = [
        "",
        "hello world",
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:nonsense\nEND:VEVENT\nEND:VCALENDAR",
        "BEGIN:VEVENT\nSUMMARY:no start\nEND:VEVENT",
        "BEGIN:VEVENT\nDTSTART:20261305T120000Z\nEND:VEVENT",
        "BEGIN:VEVENT\nDTSTART:20261105T120000Z\nRRULE:FREQ=WEEKLY;INTERVAL=x\nDURATION:soon\nEND:VEVENT",
        "BEGIN:VEVENT\nDTSTART:20261105T120000Z\nRRULE:FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30\nEND:VEVENT",
        "END:VEVENT\nEND:VCALENDAR\n:::\n;;;\nBEGIN:\nDTSTART;TZID=\"unterminated:20261105T120000",
        "\x00\x01�" * 100,
    ]
    for text in junk:
        feed = parse(text)
        assert isinstance(occurrences(feed, local(2026, 1, 1), local(2027, 1, 1), TOR), list)
    # a good event next to bad ones still comes through
    text = cal("UID:bad\nDTSTART:2026", "UID:good\nSUMMARY:Fine\nDTSTART:20261105T120000Z",
               "UID:bad2\nDTSTART;VALUE=DATE:20261399")
    assert [o.uid for o in window(text, local(2026, 11, 5), local(2026, 11, 6))] == ["good"]
    assert parse(b"BEGIN:VEVENT\nDTSTART:20261105T120000Z\nEND:VEVENT").events  # bytes are decoded


def test_big_feed_parses_fast_and_caps_events():
    pad = "DESCRIPTION:" + "x" * 70 + "\n " + "y" * 70 + "\n " + "z" * 300
    events = [f"UID:e{i}\nSUMMARY:Event {i}\nDTSTART;TZID=America/Toronto:2026{(i % 12) + 1:02d}{(i % 28) + 1:02d}"
              f"T100000\nDURATION:PT1H\n{pad}\nATTENDEE;CN=\"Someone\":mailto:a@example.com" for i in range(6000)]
    text = cal(*events)
    assert len(text) > 2_000_000
    t0 = time.perf_counter()
    feed = parse(text)
    took = time.perf_counter() - t0
    assert len(feed.events) == ics.MAX_EVENTS
    assert took < 1.0, took
    t0 = time.perf_counter()
    occ = occurrences(feed, local(2026, 1, 1), local(2027, 1, 1), TOR)
    assert len(occ) == 500 and time.perf_counter() - t0 < 1.0


def test_old_one_off_events_are_skipped_so_a_busy_calendar_keeps_what_is_coming(monkeypatch):
    # Years of one-off events, written before this week's repeating ones (calendars keep everything): without
    # "since" the old ones filled MAX_EVENTS and the coming week came back empty.
    monkeypatch.setattr(ics, "MAX_EVENTS", 50)
    old = [f"UID:old{i}\nSUMMARY:Old {i}\nDTSTART:2024{(i % 12) + 1:02d}{(i % 28) + 1:02d}T150000Z\n"
           f"DTEND:2024{(i % 12) + 1:02d}{(i % 28) + 1:02d}T160000Z" for i in range(200)]
    keep = [
        "UID:weekly\nSUMMARY:Standup\nDTSTART;TZID=America/Toronto:20230102T093000\n"
        "DTEND;TZID=America/Toronto:20230102T100000\nRRULE:FREQ=WEEKLY;BYDAY=MO",  # began long ago, still repeats
        "UID:weekly\nSUMMARY:Standup (moved)\nRECURRENCE-ID;TZID=America/Toronto:20261102T093000\n"
        "DTSTART;TZID=America/Toronto:20261102T110000\nDTEND;TZID=America/Toronto:20261102T113000",
        "UID:dent\nSUMMARY:Dentist\nDTSTART;TZID=America/Toronto:20261104T150000\nDURATION:PT1H",
        "UID:trip\nSUMMARY:Trip\nDTSTART;VALUE=DATE:20261025\nDTEND;VALUE=DATE:20261106",  # began before, still on
        "UID:last\nSUMMARY:Last week\nDTSTART;TZID=America/Toronto:20261027T090000\nDURATION:PT1H",
    ]
    text = cal(*old, *keep)
    week = (local(2026, 11, 2), local(2026, 11, 9))
    assert window(text, *week) == []  # the cap: only old events were kept
    feed = parse(text, since=dt.date(2026, 10, 3))
    assert sorted(e.uid for e in feed.events) == ["dent", "last", "trip", "weekly", "weekly"]
    got = occurrences(feed, *week, TOR)
    assert [(o.title, o.start.strftime("%a %H:%M")) for o in got] == [
        ("Trip", "Sun 00:00"), ("Standup (moved)", "Mon 11:00"), ("Dentist", "Wed 15:00")]
