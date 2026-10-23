"""Stand-ins for Open-Meteo (forecast + place search) and the IP-location services (no network)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

TORONTO = {"id": 6167865, "name": "Toronto", "latitude": 43.70011, "longitude": -79.4163, "country_code": "CA",
           "timezone": "America/Toronto", "country": "Canada", "admin1": "Ontario", "population": 2600000}
PARIS = {"id": 2988507, "name": "Paris", "latitude": 48.85341, "longitude": 2.3488, "country_code": "FR",
         "timezone": "Europe/Paris", "country": "France", "admin1": "Île-de-France", "population": 2138551}
TOKYO = {"id": 1850147, "name": "Tokyo", "latitude": 35.6895, "longitude": 139.69171, "country_code": "JP",
         "timezone": "Asia/Tokyo", "country": "Japan", "admin1": "Tokyo"}
SPRINGFIELDS = [
    {"name": "Springfield", "latitude": 37.21533, "longitude": -93.29824, "country_code": "US",
     "timezone": "America/Chicago", "country": "United States", "admin1": "Missouri"},
    {"name": "Springfield", "latitude": 39.80172, "longitude": -89.64371, "country_code": "US",
     "timezone": "America/Chicago", "country": "United States", "admin1": "Illinois"},
]
PLACES = {"toronto": [TORONTO], "paris": [PARIS], "tokyo": [TOKYO], "springfield": SPRINGFIELDS}

# What each IP service says (shapes as measured from the real services, 10.2026).
IP_ANSWERS = {
    "ipinfo.io": {"ip": "203.0.113.5", "city": "Toronto", "region": "Ontario", "country": "CA",
                  "loc": "43.7001,-79.4163", "timezone": "America/Toronto"},
    "ipwho.is": {"ip": "203.0.113.5", "success": True, "city": "Columbus", "region": "Ohio",
                 "country": "United States", "country_code": "US", "latitude": 39.9612, "longitude": -82.9988,
                 "timezone": {"id": "America/New_York", "abbr": "EDT", "utc": "-04:00"}},
    "ipapi.co": {"ip": "203.0.113.5", "city": "Paris", "region": "Île-de-France", "country_name": "France",
                 "country_code": "FR", "latitude": 48.8566, "longitude": 2.3522, "timezone": "Europe/Paris"},
}


def forecast(params: Any) -> dict[str, Any]:
    """An Open-Meteo forecast answer (shape as measured from the real API, 10.2026)."""
    f = params.get("temperature_unit") == "fahrenheit"

    def t(c: float) -> float:
        return round(c * 9 / 5 + 32, 1) if f else c

    mph = params.get("wind_speed_unit") == "mph"

    def w(kmh: float) -> float:
        return round(kmh / 1.609, 1) if mph else kmh

    days = int(params.get("forecast_days") or 7)
    hours = int(params.get("forecast_hours") or 13)
    dates = [f"2026-10-{23 + i:02d}" for i in range(days)]
    return {
        "latitude": float(params["latitude"]), "longitude": float(params["longitude"]), "timezone": "America/Toronto",
        "current_units": {"temperature_2m": "°F" if f else "°C"},
        "current": {"time": "2026-10-23T17:30", "interval": 900, "temperature_2m": t(11.7),
                    "apparent_temperature": t(8.6), "relative_humidity_2m": 59, "precipitation": 0.0, "weather_code": 0,
                    "wind_speed_10m": w(10.9), "wind_gusts_10m": w(17.3), "is_day": 1},
        "hourly": {"time": [f"2026-10-{23 + (17 + i) // 24:02d}T{(17 + i) % 24:02d}:00" for i in range(hours)],
                   "temperature_2m": [t(12.1 - i * 0.5) for i in range(hours)],
                   "precipitation_probability": [0, 0, 10, 20, 40, 70, 80, 60, 30, 10, 0, 0, 0][:hours],
                   "weather_code": [0, 0, 1, 2, 3, 61, 63, 61, 3, 2, 0, 0, 0][:hours],
                   "is_day": [1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0][:hours]},
        "daily": {"time": dates, "weather_code": [3, 61, 0, 71, 95, 2, 45][:days],
                  "temperature_2m_max": [t(12.9), t(14.7), t(10), t(1), t(20), t(15), t(9)][:days],
                  "temperature_2m_min": [t(5.3), t(4.6), t(2), t(-3), t(12), t(8), t(4)][:days],
                  "precipitation_probability_max": [0, 85, 5, 60, 90, 10, 20][:days],
                  "precipitation_sum": [0.0, 6.2, 0.0, 0.4, 12.0, 0.0, 0.1][:days],
                  "snowfall_sum": [0.0, 0.0, 0.0, 3.5, 0.0, 0.0, 0.0][:days],
                  "wind_speed_10m_max": [w(17.1)] * days, "wind_gusts_10m_max": [w(37.4)] * days,
                  "uv_index_max": [3.45] * days,
                  "sunrise": [f"{d}T07:35" for d in dates], "sunset": [f"{d}T18:20" for d in dates]},
    }


class FakeWeb:
    """httpx handler for api.open-meteo.com, geocoding-api.open-meteo.com and the IP services.

    ``ip_ok``: which IP services answer (others give HTTP 429, like a rate limit). ``delay``: seconds
    before every answer (to test the time limit)."""

    def __init__(self, *, ip_ok: tuple[str, ...] = ("ipinfo.io", "ipwho.is", "ipapi.co"),
                 places: dict[str, list[dict[str, Any]]] | None = None, delay: float = 0.0,
                 forecast_status: int = 200) -> None:
        self.ip_ok = ip_ok
        self.places = PLACES if places is None else places
        self.delay = delay
        self.forecast_status = forecast_status
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        host, params = request.url.host, request.url.params
        if host == "api.open-meteo.com":
            if self.forecast_status != 200:
                return httpx.Response(self.forecast_status, json={"error": True, "reason": "nope"})
            return httpx.Response(200, json=forecast(params))
        if host == "geocoding-api.open-meteo.com":
            found = self.places.get(params.get("name", "").strip().lower())
            return httpx.Response(200, json={"results": found, "generationtime_ms": 0.4} if found
                                  else {"generationtime_ms": 0.1})
        if host in IP_ANSWERS:
            if host not in self.ip_ok:
                return httpx.Response(429, json={"error": True, "reason": "RateLimited"})
            return httpx.Response(200, json=IP_ANSWERS[host])
        return httpx.Response(404)

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]

    def count(self, host: str) -> int:
        return self.hosts().count(host)


def weather_service(ctx: Any, web: FakeWeb | None = None, **kw: Any) -> Any:
    """A WeatherService on ``ctx`` (``ctx.extras["weather"]``, as get_weather would make it) using ``web``."""
    from littlebot.info.weather import WeatherService

    svc = WeatherService(ctx, transport=httpx.MockTransport(web or FakeWeb()), **kw)
    ctx.extras["weather"] = svc
    return svc
