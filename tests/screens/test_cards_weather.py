"""The weather card (littlebot/screens/cards/weather.py): views, steering, the voice's line, no network."""

from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "info"))
from weather_fakes import FakeWeb, weather_service

from littlebot.info.weather import WeatherError
from littlebot.screens import base, preview
from littlebot.screens.cards import weather
from littlebot.screens.cards.weather import (
    SAMPLE_REPORT,
    WeatherCard,
    icon_kind,
    parse_day,
    rain_words,
)


class StubWeather:
    """Stands in for WeatherService: canned forecasts, nothing fetched."""

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.asked: list[str | None] = []

    async def outlook(self, place: str | None = None) -> dict:
        self.asked.append(place)
        if self.error:
            raise WeatherError(self.error)
        report = copy.deepcopy(SAMPLE_REPORT)
        if place:
            report["place"] = f"{place}, Somewhere"
            report["now"]["temperature"] = 20
        return report


def card(**spec) -> WeatherCard:
    c = WeatherCard({"card": "weather", **spec}, base.RecordingHost(), report=copy.deepcopy(SAMPLE_REPORT))
    return c


def test_registered_with_fields_actions_and_samples():
    ct = base.card_types()["weather"]
    assert set(ct.fields) == {"place", "view", "day"} and ct.required == ()
    assert set(ct.actions) == {"day", "view", "place"}
    assert ct.fields["view"]["enum"] == ["now", "day", "week", "hours"]
    assert {s["view"] for s in weather.SAMPLES} == {"now", "day", "week", "hours"}


async def test_every_sample_builds_and_draws():
    for spec in weather.SAMPLES:
        c = await base.build(spec, None, base.RecordingHost())
        assert isinstance(c, WeatherCard) and c.view == spec["view"]
        surf = preview.surface(c, seconds=0.5)
        assert surf.get_size() == (base.W, base.H)


def test_describe_says_what_is_shown():
    assert card().describe() == "Now in Toronto: 12 °C and partly cloudy, feels like 9 °C"
    assert card(day="tomorrow").describe() == "Tomorrow in Toronto: 4 to 11 °C, rain likely"
    assert card(view="day", day="Monday").describe() == "Monday in Toronto: 2 to 10 °C, sunny"
    assert card(view="day", day="Tuesday").describe() == "Tuesday in Toronto: -3 to 1 °C, snow possible"
    assert card(view="week").describe() == \
        "The next 7 days in Toronto: -3 to 20 °C, wet tomorrow, Tuesday and Wednesday"
    hours = card(view="hours", day=1)
    hours.twelve = False
    assert hours.describe() == "Tomorrow in Toronto hour by hour: 4 to 11 °C, rain likely around 09:00"
    assert card(view="hours").title == "The next hours"


def test_spec_day_and_view():
    assert (card().view, card().day) == ("now", 0)
    c = card(day="Saturday")  # Saturday is today in the sample
    assert (c.view, c.day) == ("now", 0)
    c = card(day="2")
    assert (c.view, c.day, c.title) == ("day", 2, "Monday in Toronto")
    assert card(view="hours", day="tomorrow").title == "Tomorrow, hour by hour"
    assert card(view="nonsense").view == "now"


def test_parse_day():
    f = SAMPLE_REPORT["forecast"]
    assert parse_day(0, f) == 0 and parse_day("tomorrow", f) == 1 and parse_day("the day after tomorrow", f) == 2
    assert parse_day("Wednesday", f) == 4 and parse_day("wed", f) == 4 and parse_day("next Saturday", f) == 0
    assert parse_day(12, f) == 6 and parse_day("-3", f) == 0
    assert parse_day("someday", f) is None and parse_day("", f) is None and parse_day(True, f) is None


def test_commands_change_the_day_and_the_view():
    c = card()
    assert c.command("day", {"offset": 1}) and (c.view, c.day) == ("day", 1)
    assert c.command("day", {"weekday": "Thursday"}) and c.day == 5 and c.title == "Thursday in Toronto"
    assert c.describe().startswith("Thursday in Toronto: 4 to 9 °C")
    assert c.command("view", {"view": "hours"}) and c.view == "hours" and c.day == 5
    assert c.command("day", {"offset": 2}) and c.view == "hours" and c.day == 2  # stays hour by hour
    assert c.command("view", {"view": "week"}) and c.view == "week"
    assert c.command("view", {"view": "now"}) and (c.view, c.day) == ("now", 0)
    assert not c.command("view", {"view": "radar"}) and not c.command("day", {"weekday": "Funday"})
    assert not c.command("dance", {})
    assert c.command("view", {"view": "day", "day": "tomorrow"}) and (c.view, c.day) == ("day", 1)


def test_taps_and_keys_turn_the_day():
    c = card()
    assert not c.tap(100, 300)  # now: nothing before today
    assert c.tap(600, 300) and (c.view, c.day) == ("day", 1)
    assert c.tap(600, 300) and c.day == 2
    assert c.tap(100, 300) and c.day == 1
    assert c.key("right") and c.day == 2 and c.key("left") and c.day == 1
    for _ in range(10):
        c.tap(700, 300)
    assert c.day == 6  # the last day
    c.set_view("week")
    x0, tw, gap = c._tiles()
    assert c.tap(x0 + 3 * (tw + gap) + tw / 2, 300) and (c.view, c.day) == ("day", 3)
    assert c.key("down") and c.view == "week"


def test_a_change_of_day_slides():
    c = card(day=1)
    c.command("day", {"offset": 2})
    assert c._slide > 0
    for _ in range(40):
        c.update(1 / 30)
    assert c._slide == 0


def test_icons_and_rain_words():
    assert icon_kind("light rain") == "rain" and icon_kind("heavy snow showers") == "snow"
    assert icon_kind("thunderstorms with hail") == "storm" and icon_kind("freezing fog") == "fog"
    assert icon_kind("clear") == "moon" and icon_kind("sunny") == "sun" and icon_kind("partly cloudy") == "partly"
    assert icon_kind("cloudy") == "cloud" and icon_kind("sunny", windy=True) == "wind"
    assert icon_kind("rain", windy=True) == "rain"
    for kind in ("sun", "moon", "partly", "cloud", "rain", "snow", "storm", "fog", "wind"):
        assert weather.weather_icon(kind, 64).get_size() == (64, 64)
    assert rain_words(85, "rain") == "rain likely" and rain_words(45, "snow") == "snow possible"
    assert rain_words(25, "cloudy") == "a small chance of rain" and rain_words(5, "rain") == ""
    assert rain_words(None, "rain") == "" and rain_words(90, "thunderstorms") == "storms likely"


def test_wind_shows_the_wind_icon_on_a_dry_windy_day():
    c = card()
    assert c._icon_for_day({"conditions": "sunny", "wind_max": 45}) == "wind"
    c.wind_unit = "mph"
    assert c._icon_for_day({"conditions": "sunny", "wind_max": 26}) == "wind"
    assert c._icon_for_day({"conditions": "sunny", "wind_max": 20}) == "sun"


async def test_build_fetches_through_the_weather_service(ctx):
    stub = StubWeather()
    ctx.extras["weather"] = stub
    c = await base.build({"card": "weather", "day": "tomorrow"}, ctx, base.RecordingHost())
    assert isinstance(c, WeatherCard) and c.view == "day" and stub.asked == [None]
    c = await base.build({"card": "weather", "place": "  Paris "}, ctx, base.RecordingHost())
    assert c.place == "Paris" and stub.asked[-1] == "Paris"


async def test_no_forecast_no_card(ctx):
    ctx.extras["weather"] = StubWeather(error="network")
    assert await base.build({"card": "weather"}, ctx, base.RecordingHost()) is None
    assert await base.build({"card": "weather"}, None, base.RecordingHost()) is None


async def test_another_place_is_fetched_in_the_background(ctx):
    stub = StubWeather()
    ctx.extras["weather"] = stub
    host = base.RecordingHost()
    c = await base.build({"card": "weather"}, ctx, host)
    assert c.command("place", {"name": "Lisbon"})
    for _ in range(50):
        await asyncio.sleep(0.01)
        c.update(0.01)
        if c.place == "Lisbon":
            break
    assert c.place == "Lisbon" and c.title == "Now in Lisbon" and c.describe().startswith("Now in Lisbon: 20 °C")
    stub.error = "not_found"
    assert c.command("place", {"name": "Atlantis"})
    for _ in range(50):
        await asyncio.sleep(0.01)
        c.update(0.01)
        if host.told:
            break
    assert c.place == "Lisbon" and "couldn't change place" in host.told[-1]


def test_place_needs_a_running_loop():
    assert not card().command("place", {"name": "Paris"})


@pytest.fixture
def online(ctx):
    ctx.privacy.set(cloud_enabled=True)
    return ctx


async def test_outlook_from_the_real_service_against_a_fake_open_meteo(online):
    web = FakeWeb()
    svc = weather_service(online, web)
    report = await svc.outlook()
    today, tomorrow = report["forecast"][:2]
    assert [h["time"] for h in today["hours"]][:3] == ["18:00", "21:00", "00:00"]  # the next hours
    assert [h["time"] for h in tomorrow["hours"]] == ["06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]
    assert today["hours"][1] == {"time": "21:00", "temperature": 10, "conditions": "cloudy",
                                 "chance_of_rain_or_snow_percent": 40}
    assert len(report["forecast"]) == 7 and "hours" not in (await svc.report())["forecast"][0]
    assert web.requests[-1].url.params["forecast_hours"] == "168"
    c = await base.build({"card": "weather", "view": "week"}, online, base.RecordingHost())
    assert c.describe().startswith("The next 7 days in Toronto:")
    preview.surface(c, seconds=0.2)


async def test_offline_or_cloud_off_no_card(ctx):
    web = FakeWeb()
    weather_service(ctx, web)  # cloud communication is off by default
    assert await base.build({"card": "weather"}, ctx, base.RecordingHost()) is None
    assert web.requests == []
