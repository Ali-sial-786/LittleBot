"""Natural times for reminders and alarms (littlebot/skills/when.py): phrases, ISO times, repeats, time zones."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from littlebot.skills.when import (
    EVERY_DAY,
    WEEKDAY_SET,
    WEEKEND_SET,
    WhenError,
    clock_words,
    day_words,
    days_from_text,
    days_text,
    next_time,
    now_local,
    parse_repeat,
    parse_when,
    repeat_words,
    say_when,
    until_words,
)

TZ = ZoneInfo("America/Toronto")
NOW = dt.datetime(2026, 10, 30, 14, 5, tzinfo=TZ)  # Friday 2:05 pm (daylight saving time ends Sunday 1 November)


def at(text: str, now: dt.datetime = NOW, **kw) -> str:
    w = parse_when(text, now, **kw)
    out = w.at.strftime("%a %d %b %H:%M")
    if w.at.second:
        out += f":{w.at.second:02d}"
    return out + (f" [{repeat_words(w.days)}]" if w.days else "")


@pytest.mark.parametrize("text, expected", [
    # relative
    ("in 20 minutes", "Fri 30 Oct 14:25"),
    ("in 20 mins", "Fri 30 Oct 14:25"),
    ("20 minutes from now", "Fri 30 Oct 14:25"),
    ("in twenty five minutes", "Fri 30 Oct 14:30"),
    ("in half an hour", "Fri 30 Oct 14:35"),
    ("in an hour and a half", "Fri 30 Oct 15:35"),
    ("in 1.5 hours", "Fri 30 Oct 15:35"),
    ("in 1h30m", "Fri 30 Oct 15:35"),
    ("in 2 hours and 15 minutes", "Fri 30 Oct 16:20"),
    ("in a minute", "Fri 30 Oct 14:06"),
    ("in 90 seconds", "Fri 30 Oct 14:06:30"),
    ("in a couple of hours", "Fri 30 Oct 16:05"),
    ("in 3 days", "Mon 02 Nov 14:05"),
    ("in 2 days at 5pm", "Sun 01 Nov 17:00"),
    # a clock time: the next time it comes round
    ("at 6pm", "Fri 30 Oct 18:00"),
    ("6:30 p.m.", "Fri 30 Oct 18:30"),
    ("at 6", "Fri 30 Oct 18:00"),  # 6 am has passed: 6 pm
    ("at 1", "Sat 31 Oct 01:00"),  # 1 pm has passed: 1 am
    ("18:30", "Fri 30 Oct 18:30"),
    ("at 730", "Fri 30 Oct 19:30"),
    ("seven thirty pm", "Fri 30 Oct 19:30"),
    ("half past six", "Fri 30 Oct 18:30"),
    ("quarter to seven", "Fri 30 Oct 18:45"),
    ("six o'clock", "Fri 30 Oct 18:00"),
    ("noon", "Sat 31 Oct 12:00"),
    ("midnight", "Sat 31 Oct 00:00"),
    ("at 6 in the evening", "Fri 30 Oct 18:00"),
    ("at 10 in the morning", "Sat 31 Oct 10:00"),
    ("remind me at 6pm", "Fri 30 Oct 18:00"),
    # days
    ("tomorrow at 7:30", "Sat 31 Oct 07:30"),
    ("tomorrow at 7", "Sat 31 Oct 07:00"),
    ("tomorrow at 3", "Sat 31 Oct 15:00"),  # a day and no am/pm: the usual half of the day
    ("tomorrow morning", "Sat 31 Oct 08:00"),
    ("tonight", "Fri 30 Oct 20:00"),
    ("tonight at 9", "Fri 30 Oct 21:00"),
    ("at 1 tonight", "Sat 31 Oct 01:00"),
    ("friday at 5pm", "Fri 30 Oct 17:00"),  # today, still to come
    ("friday at 1pm", "Fri 06 Nov 13:00"),  # today's has passed: next week
    ("monday 9am", "Mon 02 Nov 09:00"),
    ("next monday at 9", "Mon 02 Nov 09:00"),
    ("Tues at 4", "Tue 03 Nov 16:00"),
    ("on october 31 at 5pm", "Sat 31 Oct 17:00"),
    ("december 25th at 9am", "Fri 25 Dec 09:00"),
    ("31 december at 23:59", "Thu 31 Dec 23:59"),
    ("the 15th at 10", "Sun 15 Nov 10:00"),
    ("next week", "Fri 06 Nov 09:00"),
    ("this weekend", "Sat 31 Oct 09:00"),
    # ISO-8601 (what the model usually sends)
    ("2026-10-30T18:00", "Fri 30 Oct 18:00"),
    ("2026-10-30 18:00:00", "Fri 30 Oct 18:00"),
    ("2026-10-31T07:00:00-04:00", "Sat 31 Oct 07:00"),
    ("2026-10-31T11:00:00Z", "Sat 31 Oct 07:00"),
    ("2026-11-02", "Mon 02 Nov 09:00"),
    # repeats (daylight saving time ends on the 1st: still 7 am on the wall clock)
    ("every weekday at 7", "Mon 02 Nov 07:00 [every weekday]"),
    ("weekdays at 6:30am", "Mon 02 Nov 06:30 [every weekday]"),
    ("every day at 8", "Sat 31 Oct 08:00 [every day]"),
    ("daily at 7:15am", "Sat 31 Oct 07:15 [every day]"),
    ("every morning at 7", "Sat 31 Oct 07:00 [every day]"),
    ("every monday and wednesday at 6pm", "Mon 02 Nov 18:00 [every Monday and Wednesday]"),
    ("mondays at 6", "Mon 02 Nov 18:00 [every Monday]"),
    ("every sat and sun at 9", "Sat 31 Oct 09:00 [every weekend]"),
    ("on weekends at 10", "Sat 31 Oct 10:00 [every weekend]"),
    ("weekly at 9am", "Sat 31 Oct 09:00 [every Saturday]"),
    ("every friday", "Fri 06 Nov 09:00 [every Friday]"),
    ("2026-10-31T07:00 every weekday", "Mon 02 Nov 07:00 [every weekday]"),
])
def test_phrases(text: str, expected: str) -> None:
    assert at(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("7", "Sat 31 Oct 07:00"),
    ("at 6", "Sat 31 Oct 06:00"),
    ("tomorrow at 7", "Sat 31 Oct 07:00"),
    ("friday at 6", "Fri 06 Nov 06:00"),  # an alarm on a day: the morning
    ("every weekday at 6:30", "Mon 02 Nov 06:30 [every weekday]"),
])
def test_alarm_times(text: str, expected: str) -> None:
    assert at(text, dt.datetime(2026, 10, 30, 22, 0, tzinfo=TZ), alarm=True) == expected


@pytest.mark.parametrize("text, words", [
    ("", "no time"),
    ("banana", "couldn't understand"),
    ("in 20", "couldn't understand"),
    ("now", "couldn't understand"),
    ("every hour", "daily, on weekdays"),
    ("every other day", "daily, on weekdays"),
    ("2026-10-29T10:00", "has passed"),
    ("today at 9am", "has passed"),
    ("13:75", "isn't a time"),
    ("at 25", "isn't a time"),
    ("in 2 years", "couldn't understand"),
    ("in 400 days", "more than a year"),
    ("2026-02-30T10:00", "isn't a date"),
    ("in 20 minutes every day", "needs a time of day"),
])
def test_errors_say_what_to_do(text: str, words: str) -> None:
    with pytest.raises(WhenError) as e:
        parse_when(text, NOW)
    assert words in str(e.value)


def test_an_alarm_needs_a_time() -> None:
    with pytest.raises(WhenError, match="needs a time"):
        parse_when("tomorrow", NOW, alarm=True)


def test_separate_repeat_values() -> None:
    assert parse_repeat(None) is None and parse_repeat("") is None and parse_repeat("once") is None
    assert parse_repeat("daily") == EVERY_DAY and parse_repeat("Every Day") == EVERY_DAY
    assert parse_repeat("weekdays") == WEEKDAY_SET and parse_repeat("mon-fri") == WEEKDAY_SET
    assert parse_repeat("weekends") == WEEKEND_SET
    assert parse_repeat("weekly") == "weekly"
    assert parse_repeat("monday,wednesday") == frozenset({0, 2})
    assert parse_repeat(["Tue", "thursdays"]) == frozenset({1, 3})
    for bad in ("hourly", "monthly", "every 2 days", "sometimes"):
        with pytest.raises(WhenError):
            parse_repeat(bad)
    w = parse_when("2026-10-31T07:00", NOW, repeat=parse_repeat("weekdays"))  # Saturday: first on Monday
    assert w.at.strftime("%a %H:%M") == "Mon 07:00" and w.days == WEEKDAY_SET
    w = parse_when("at 9am", NOW, repeat="weekly")
    assert w.days == frozenset({5}) and w.at.strftime("%a %H:%M") == "Sat 09:00"
    w = parse_when("every day at 8", NOW, repeat=WEEKDAY_SET)  # the words win
    assert w.days == EVERY_DAY


def test_next_time_keeps_the_wall_clock_across_daylight_saving() -> None:
    sat = dt.datetime(2026, 10, 31, 7, 0, tzinfo=TZ)
    sun = next_time(sat, 7, 0, EVERY_DAY)
    assert sun.strftime("%a %H:%M %z") == "Sun 07:00 -0500"
    assert sun.timestamp() - sat.timestamp() == 25 * 3600  # the clocks went back an hour in the night
    assert next_time(sat, 7, 0, WEEKDAY_SET).strftime("%a %d") == "Mon 02"
    assert next_time(sat - dt.timedelta(minutes=1), 7, 0).strftime("%a %d %H:%M") == "Sat 31 07:00"


def test_the_robots_own_zone_when_none_is_named() -> None:
    now = now_local(NOW.timestamp(), None)  # the system's zone, whatever it is
    w = parse_when("in 20 minutes", now)
    assert w.at.timestamp() == pytest.approx(NOW.timestamp() + 1200)
    w = parse_when("tomorrow at 7am", now)
    assert (w.at.hour, w.at.minute) == (7, 0) and (w.at.date() - now.date()).days == 1
    assert now_local(NOW.timestamp(), TZ) == NOW


def test_saying_it_back() -> None:
    assert clock_words(NOW) == "2:05 pm" and clock_words(NOW.replace(hour=18, minute=0)) == "6 pm"
    assert clock_words(NOW.replace(hour=0, minute=30)) == "12:30 am"
    assert clock_words(NOW, twelve=False) == "14:05"
    assert day_words(NOW, NOW) == "today"
    assert day_words(NOW + dt.timedelta(days=1), NOW) == "tomorrow"
    assert day_words(NOW + dt.timedelta(days=3), NOW) == "on Monday"
    assert day_words(NOW + dt.timedelta(days=30), NOW) == "on Sunday 29 November"
    assert day_words(NOW + dt.timedelta(days=70), NOW) == "on Friday 8 January 2027"
    assert say_when(NOW.replace(hour=18, minute=0), NOW) == "6 pm today (Friday 30 October)"
    assert say_when(NOW + dt.timedelta(days=3), NOW) == "2:05 pm on Monday (Monday 2 November)"
    assert say_when(NOW + dt.timedelta(days=30), NOW) == "2:05 pm on Sunday 29 November"
    assert until_words(45) == "in 45 seconds" and until_words(90) == "in 1 minute 30 seconds"
    assert until_words(1200) == "in 20 minutes" and until_words(2 * 3600 + 600) == "in 2 hours 10 minutes"
    assert until_words(3600) == "in 1 hour" and until_words(5 * 86400) == "in 5 days"
    assert repeat_words(frozenset()) == "" and repeat_words(EVERY_DAY) == "every day"
    assert repeat_words({0, 2, 4}) == "every Monday, Wednesday and Friday"
    assert days_from_text(days_text({4, 0})) == frozenset({0, 4}) and days_from_text("x,9,") == frozenset()
