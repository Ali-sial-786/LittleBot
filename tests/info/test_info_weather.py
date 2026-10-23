"""The weather (littlebot/info/weather.py) against stand-ins for Open-Meteo and the IP services (no network)."""

from __future__ import annotations

import asyncio
import time

import pytest
from weather_fakes import IP_ANSWERS, FakeWeb, weather_service

from littlebot.info.weather import (
    IP_KEY, IP_KEEP_S, Place, WeatherError, conditions, parse_coordinates, summarize, summary_text, temperature_units,
)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def online(ctx):
    ctx.privacy.set(cloud_enabled=True)
    return ctx


async def test_home_weather_from_the_internet_connection(online):
    web = FakeWeb()
    report = await weather_service(online, web).report()
    # home came from the IP service (Toronto, Canada): Celsius and km/h
    assert report["place"] == "Toronto, Ontario" and report["location_from"] == \
        "home, from your internet connection, approximate"
    assert report["units"] == {"temperature": "°C", "wind": "km/h"}
    assert report["now"] == {"temperature": 12, "feels_like": 9, "conditions": "sunny", "humidity_percent": 59,
                             "wind": 11, "gusts": 17}
    today, tomorrow = report["forecast"]
    assert today == {"day": "today", "date": "Friday 23 October", "conditions": "cloudy", "high": 13, "low": 5,
                     "chance_of_rain_or_snow_percent": 0, "wind_max": 17, "gusts_max": 37, "uv_index_max": 3.5,
                     "sunrise": "07:35", "sunset": "18:20"}
    assert tomorrow["day"] == "tomorrow" and tomorrow["conditions"] == "light rain" and tomorrow["rain_mm"] == 6.2
    assert tomorrow["chance_of_rain_or_snow_percent"] == 85
    # the next hours in three-hour blocks: the worst conditions and the highest chance in each
    assert report["next_hours"][0] == {"hours": "18:00-21:00", "conditions": "partly cloudy", "temperature": 11,
                                       "chance_of_rain_or_snow_percent": 20}
    later = report["next_hours"][1]
    assert later["conditions"] == "rain" and later["chance_of_rain_or_snow_percent"] == 80
    assert web.hosts() == ["ipinfo.io", "api.open-meteo.com"]
    params = web.requests[-1].url.params
    assert params["latitude"] == "43.7001" and params["longitude"] == "-79.4163" and params["timezone"] == "auto"
    assert params["temperature_unit"] == "celsius" and params["wind_speed_unit"] == "kmh"
    assert "apparent_temperature" in params["current"] and "precipitation_probability_max" in params["daily"]
    assert params["forecast_days"] == "7"


async def test_the_ip_location_falls_back_to_the_next_service_and_is_kept(online):
    web = FakeWeb(ip_ok=("ipwho.is",))  # ipinfo.io is rate limited
    report = await weather_service(online, web).report()
    assert report["place"] == "Columbus, Ohio, United States" and report["units"]["temperature"] == "°F"
    assert web.hosts()[:2] == ["ipinfo.io", "ipwho.is"]
    saved = online.db.kv_get(IP_KEY)
    assert saved["place"]["country_code"] == "US" and saved["from"] == "ipwho.is"
    # after a restart (a new service), the kept location is used: no IP request
    web2 = FakeWeb(ip_ok=())
    report = await weather_service(online, web2).report()
    assert report["place"] == "Columbus, Ohio, United States" and web2.hosts() == ["api.open-meteo.com"]


async def test_an_old_ip_location_is_looked_up_again_and_kept_if_that_fails(online):
    old = Place("Paris", 48.85, 2.35, country="France", country_code="FR").to_dict()
    online.db.kv_set(IP_KEY, {"place": old, "at": time.time() - IP_KEEP_S - 60})
    web = FakeWeb()
    assert (await weather_service(online, web).report())["place"] == "Toronto, Ontario"  # looked up again
    online.db.kv_set(IP_KEY, {"place": old, "at": time.time() - IP_KEEP_S - 60})
    web = FakeWeb(ip_ok=())
    assert (await weather_service(online, web).report())["place"] == "Paris, France"  # old, better than nothing


async def test_no_ip_location_is_a_plain_error_and_not_asked_again_at_once(online):
    web = FakeWeb(ip_ok=())
    svc = weather_service(online, web)
    with pytest.raises(WeatherError) as e:
        await svc.report()
    assert e.value.kind == "no_location" and "Settings, Weather & time" in str(e.value)
    assert web.hosts() == ["ipinfo.io", "ipwho.is", "ipapi.co"]
    with pytest.raises(WeatherError):
        await svc.report()
    assert len(web.requests) == 3  # failed a moment ago: answered at once, nothing sent


async def test_a_forecast_is_reused_for_ten_minutes(online):
    clock, web = Clock(), FakeWeb()
    svc = weather_service(online, web, now=clock)
    await svc.report()
    t0 = time.monotonic()
    await svc.report(days=3)
    assert time.monotonic() - t0 < 0.05 and web.count("api.open-meteo.com") == 1  # the same forecast
    clock.t += 9 * 60
    await svc.report()
    assert web.count("api.open-meteo.com") == 1
    clock.t += 2 * 60
    await svc.report()
    assert web.count("api.open-meteo.com") == 2 and web.count("ipinfo.io") == 1  # home is still known


async def test_concurrent_questions_share_one_request(online):
    web = FakeWeb(delay=0.05)
    svc = weather_service(online, web)
    a, b = await asyncio.gather(svc.report(), svc.report())
    assert a == b and web.count("api.open-meteo.com") == 1 and web.count("ipinfo.io") == 1


async def test_another_place_is_searched_and_keeps_home_units(online):
    web = FakeWeb(ip_ok=("ipwho.is",))  # home: Ohio -> Fahrenheit
    report = await weather_service(online, web).report("Paris", days=3)
    assert report["place"] == "Paris, Île-de-France, France" and report["location_from"] == "the place asked for"
    assert report["units"] == {"temperature": "°F", "wind": "mph"} and report["now"]["temperature"] == 53
    assert [d["day"] for d in report["forecast"]] == ["today", "tomorrow", "Sunday"]
    geo = next(r for r in web.requests if r.url.host == "geocoding-api.open-meteo.com")
    assert geo.url.params["name"] == "Paris" and geo.url.params["language"] == "en"
    forecast = next(r for r in web.requests if r.url.host == "api.open-meteo.com")
    assert forecast.url.params["latitude"] == "48.8534"


async def test_a_city_with_its_region(online):
    web = FakeWeb(places={"springfield": FakeWeb().places["springfield"]})  # "Springfield, Illinois" finds nothing
    svc = weather_service(online, web)
    place = await svc.find("Springfield, Illinois")
    assert place.region == "Illinois" and place.timezone == "America/Chicago"
    assert [r.url.params["name"] for r in web.requests] == ["Springfield, Illinois", "Springfield"]
    assert (await svc.find("springfield,  illinois")) == place and len(web.requests) == 2  # remembered


async def test_an_unknown_place(online):
    svc = weather_service(online, FakeWeb())
    with pytest.raises(WeatherError) as e:
        await svc.report("Narnia")
    assert e.value.kind == "not_found" and "Narnia" in str(e.value)


async def test_coordinates_need_no_search():
    assert parse_coordinates("43.65, -79.38") == Place("43.65, -79.38", 43.65, -79.38)
    assert parse_coordinates("43.65 -79.38") is not None and parse_coordinates("Paris") is None
    assert parse_coordinates("95, 10") is None and parse_coordinates("10, 200") is None


async def test_the_parents_choice_wins_then_the_config_then_the_internet(online):
    web = FakeWeb()
    svc = weather_service(online, web)
    online.config.info.location = "Paris"
    home, source = await svc.home()
    assert home.name == "Paris" and source == "config" and "ipinfo.io" not in web.hosts()
    await svc.set_home("Tokyo")
    home, source = await svc.home()
    assert home.name == "Tokyo" and source == "dashboard"
    assert online.settings.get("info.location")["query"] == "Tokyo"
    assert svc.status()["location"] == "Tokyo, Japan" and svc.status()["source_detail"] == "set on the dashboard"
    svc.clear_home()
    online.config.info.location = ""
    home, source = await svc.home()
    assert home.name == "Toronto" and source == "internet"
    online.config.info.locate_by_ip = False
    svc._ip.clear()
    assert await svc.home() == (None, "none")


async def test_setting_coordinates_works_with_cloud_off_but_a_name_needs_it(ctx):
    web = FakeWeb()
    svc = weather_service(ctx, web)
    place = await svc.set_home("43.65, -79.38")
    assert place.name == "43.65, -79.38" and svc.status()["source"] == "dashboard" and not web.requests
    with pytest.raises(WeatherError) as e:
        await svc.set_home("Paris")
    assert e.value.kind == "cloud_off"
    with pytest.raises(WeatherError) as e:
        await svc.report()
    assert e.value.kind == "cloud_off" and not web.requests  # nothing is sent while cloud is off


async def test_offline_mode_sends_nothing():
    from littlebot.testing import make_test_context

    ctx = make_test_context(mode="offline")
    web = FakeWeb()
    with pytest.raises(WeatherError) as e:
        await weather_service(ctx, web).report()
    assert e.value.kind == "offline" and not web.requests
    ctx.db.close()


async def test_units(online, monkeypatch):
    web = FakeWeb()
    svc = weather_service(online, web)
    online.settings.set("info.units", "fahrenheit")
    report = await svc.report()
    assert report["units"] == {"temperature": "°F", "wind": "mph"} and report["now"]["temperature"] == 53
    assert web.requests[-1].url.params["temperature_unit"] == "fahrenheit"
    with pytest.raises(ValueError):
        svc.set_units("kelvin")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    assert temperature_units("auto", None) == "fahrenheit" and temperature_units("auto", "CA") == "celsius"
    monkeypatch.setenv("LC_ALL", "en_GB.UTF-8")
    assert temperature_units("auto", None) == "celsius" and temperature_units("celsius", "US") == "celsius"


async def test_a_slow_service_is_cut_off_and_its_answer_kept_for_next_time(online):
    online.config.info.timeout_seconds = 0.15
    web = FakeWeb(delay=0.1)  # IP lookup + forecast: 0.2 s
    svc = weather_service(online, web)
    t0 = time.monotonic()
    with pytest.raises(WeatherError) as e:
        await svc.report()
    assert e.value.kind == "timeout" and time.monotonic() - t0 < 0.3
    await asyncio.sleep(0.25)  # the requests carried on in the background...
    web.delay = 0
    n = len(web.requests)
    report = await svc.report()  # ...so the next question is answered from what they found
    assert report["place"] == "Toronto, Ontario" and len(web.requests) == n


async def test_service_errors(online):
    svc = weather_service(online, FakeWeb(forecast_status=500))
    with pytest.raises(WeatherError) as e:
        await svc.report()
    assert e.value.kind == "bad_response"
    # Open-Meteo's free limit ("Daily API request limit exceeded", HTTP 429) is said as such
    svc = weather_service(online, FakeWeb(forecast_status=429))
    with pytest.raises(WeatherError) as e:
        await svc.report()
    assert e.value.kind == "limited" and "try again later" in str(e.value)

    import httpx

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    from littlebot.info.weather import WeatherService

    svc = WeatherService(online, transport=httpx.MockTransport(broken))
    with pytest.raises(WeatherError) as e:
        await svc.report("Paris")
    assert e.value.kind == "network"


def test_a_partial_answer_still_reads():
    report = summarize({"current": {"temperature_2m": 3.4, "weather_code": 71, "is_day": 0}, "daily": {}},
                       Place("Oslo", 59.9, 10.7, country="Norway"), units="celsius", wind="kmh")
    assert report == {"place": "Oslo, Norway", "units": {"temperature": "°C", "wind": "km/h"},
                      "now": {"temperature": 3, "conditions": "light snow"}, "source": "Open-Meteo"}
    assert conditions(0, day=False) == "clear" and conditions(1) == "mostly sunny" and conditions("x") == "unknown"
    assert conditions(95) == "thunderstorms"


async def test_the_parents_one_line_summary(online):
    report = await weather_service(online, FakeWeb()).report()
    assert summary_text(report) == ("Toronto, Ontario: now 12°C (feels like 9°C), sunny. Today 5 to 13°C, cloudy, "
                                    "0% chance of rain or snow.")


def test_ip_answer_shapes_are_read():
    from littlebot.info.weather import IP_SERVICES

    found = {url: parse(IP_ANSWERS[httpx_host(url)]) for url, parse in IP_SERVICES}
    assert [p.label for p in found.values()] == ["Toronto, Ontario", "Columbus, Ohio, United States",
                                                  "Paris, Île-de-France, France"]
    assert [p.timezone for p in found.values()] == ["America/Toronto", "America/New_York", "Europe/Paris"]
    assert all(parse({"success": False, "error": True}) is None for _url, parse in IP_SERVICES)


def httpx_host(url: str) -> str:
    import httpx

    return httpx.URL(url).host
