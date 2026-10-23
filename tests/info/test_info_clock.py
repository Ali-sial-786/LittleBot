"""The time (littlebot/info/clock.py): local time, date and day of the week."""

from __future__ import annotations

import datetime as _dt

import pytest

from littlebot.info.clock import now_info, part_of_day, zone

UTC_EVENING = _dt.datetime(2026, 10, 24, 1, 30, tzinfo=_dt.timezone.utc)  # Friday 21:30 in Toronto


def test_the_time_in_a_time_zone():
    assert now_info("America/Toronto", now=UTC_EVENING) == {
        "time_24h": "21:30", "time_12h": "9:30 pm", "day": "Friday", "date": "Friday 23 October 2026",
        "part_of_day": "evening", "timezone": "America/Toronto", "utc_offset": "-04:00"}
    tokyo = now_info("Asia/Tokyo", now=UTC_EVENING)
    assert tokyo["day"] == "Saturday" and tokyo["time_12h"] == "10:30 am" and tokyo["part_of_day"] == "morning"
    assert now_info("UTC", now=_dt.datetime(2026, 1, 1, 0, 5, tzinfo=_dt.timezone.utc))["time_12h"] == "12:05 am"


def test_the_robots_own_time_zone(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Paris")
    import time

    time.tzset()
    try:
        out = now_info(None, now=UTC_EVENING)
        assert out["timezone"] == "Europe/Paris" and out["time_24h"] == "03:30" and out["day"] == "Saturday"
        naive = now_info(None, now=_dt.datetime(2026, 10, 23, 14, 0))  # a naive time is local time
        assert naive["time_24h"] == "14:00" and naive["part_of_day"] == "afternoon"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_now_without_arguments_has_every_field():
    out = now_info()
    assert set(out) == {"time_24h", "time_12h", "day", "date", "part_of_day", "timezone", "utc_offset"}
    assert out["day"] in out["date"]


def test_unknown_time_zones_are_refused():
    with pytest.raises(ValueError):
        zone("Mars/Olympus_Mons")
    with pytest.raises(ValueError):
        now_info("../../etc/passwd")
    assert zone("") is None


def test_parts_of_the_day():
    assert [part_of_day(h) for h in (4, 5, 11, 12, 16, 17, 20, 21, 23)] == [
        "night", "morning", "morning", "afternoon", "afternoon", "evening", "evening", "evening", "night"]
