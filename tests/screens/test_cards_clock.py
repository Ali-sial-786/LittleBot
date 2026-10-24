"""The clock card (littlebot/screens/cards/clock.py): here and elsewhere, zones from names, no network."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "info"))
from weather_fakes import FakeWeb, weather_service

from littlebot.screens import base, preview
from littlebot.screens.cards import clock
from littlebot.screens.cards.clock import ClockCard, find_zone

NOON_UTC = dt.datetime(2026, 10, 24, 13, 30, tzinfo=dt.UTC).timestamp()


def make(zone=None, place="", home="America/Toronto", twelve=True, at=NOON_UTC) -> ClockCard:
    return ClockCard({"card": "clock"}, base.RecordingHost(), zone=zone, place=place, home_zone=home, twelve=twelve,
                     wall=lambda: at)


def test_registered_with_fields_actions_and_samples():
    ct = base.card_types()["clock"]
    assert set(ct.fields) == {"place", "timezone"} and ct.required == () and set(ct.actions) == {"place"}
    assert len(clock.SAMPLES) == 3


async def test_every_sample_builds_and_draws():
    for spec in clock.SAMPLES:
        c = await base.build(spec, None, base.RecordingHost())
        assert isinstance(c, ClockCard)
        assert preview.surface(c, seconds=0.5).get_size() == (base.W, base.H)
    c = await base.build(clock.SAMPLES[1], None, base.RecordingHost())
    assert c.describe() == "In Tokyo it's 10:30 pm on Saturday 24 October, 13 hours ahead"


def test_find_zone_by_name_country_or_iana():
    assert find_zone("Tokyo") == ("Asia/Tokyo", "Tokyo")
    assert find_zone("japan") == ("Asia/Tokyo", "Japan")
    assert find_zone("Asia/Tokyo") == ("Asia/Tokyo", "Tokyo")
    assert find_zone("America/New York") == ("America/New_York", "New York")
    assert find_zone("Tokyo, Japan") == ("Asia/Tokyo", "Tokyo")
    assert find_zone("the UK") == ("Europe/London", "the UK")
    assert find_zone("Vladivostok")[0] == "Asia/Vladivostok"  # from the system's zone list
    assert find_zone("Springfield, Australia")[0] == "Australia/Sydney"  # a country after the comma
    assert find_zone("UTC") == ("UTC", "UTC")
    assert find_zone("Atlantis") is None and find_zone("") is None


def test_here_and_elsewhere():
    here = make()
    assert here.title == "The time now" and here.describe() == "It's 9:30 am on Saturday 24 October"
    tokyo = make("Asia/Tokyo", "Tokyo")
    assert tokyo.title == "Time in Tokyo" and tokyo.offset == "13 hours ahead"
    delhi = make("Asia/Kolkata", "Delhi", home="Europe/London", twelve=False, at=NOON_UTC - 25 * 60)
    assert delhi.describe() == "In Delhi it's 18:35 on Saturday 24 October, 4½ hours ahead"
    la = make("America/Los_Angeles", "Los Angeles")
    assert la.offset == "3 hours behind" and la.ampm == "am" and la.time_text == "6:30"
    assert make("America/Toronto", "Toronto").offset == "Same time as here"
    assert make("Asia/Kathmandu", "Kathmandu", home="UTC").offset == "5¾ hours ahead"
    assert make("Australia/Sydney", "Sydney").weekday == "Sunday"  # already tomorrow there


def test_it_follows_the_clock():
    now = [NOON_UTC]
    c = ClockCard({}, base.RecordingHost(), home_zone="UTC", twelve=False, wall=lambda: now[0])
    assert c.time_text == "13:30" and c.daytime
    now[0] += 61
    c.update(1.0)
    assert c.time_text == "13:31"
    now[0] += 6 * 3600
    c.update(1.0)
    assert c.time_text == "19:31" and not c.daytime


def test_command_place():
    c = make()
    assert c.command("place", {"name": "Paris"}) and c.zone == "Europe/Paris" and c.title == "Time in Paris"
    assert c.describe().startswith("In Paris it's 3:30 pm")
    assert c.command("place", {"name": "here"}) and c.zone is None and c.title == "The time now"
    assert not c.command("place", {"name": "Atlantis"}) and not c.command("jump", {})


async def test_build_from_a_zone_or_a_place(ctx):
    host = base.RecordingHost()
    c = await base.build({"card": "clock", "timezone": "Asia/Tokyo", "place": "Tokyo"}, ctx, host)
    assert c.zone == "Asia/Tokyo" and c.place == "Tokyo"
    c = await base.build({"card": "clock", "place": "home"}, ctx, host)
    assert c.zone is None
    ctx.config.info.timezone = "Europe/Paris"
    c = await base.build({"card": "clock", "place": "London"}, ctx, host)
    assert c.home_zone == "Europe/Paris" and c.offset == "1 hour behind"


async def test_unknown_places_are_found_with_the_weather_service(ctx):
    web = FakeWeb()
    weather_service(ctx, web)
    host = base.RecordingHost()
    assert await base.build({"card": "clock", "place": "Springfield"}, ctx, host) is None  # cloud off: not asked
    assert web.requests == []
    ctx.privacy.set(cloud_enabled=True)
    c = await base.build({"card": "clock", "place": "Springfield"}, ctx, host)
    assert c.zone == "America/Chicago" and c.place == "Springfield"
    assert await base.build({"card": "clock", "place": "Atlantis"}, ctx, host) is None
