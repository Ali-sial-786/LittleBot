"""The weather for LittleBot's tools and the dashboard: Open-Meteo (https://open-meteo.com), free, no key.

    forecast  GET https://api.open-meteo.com/v1/forecast?latitude=..&longitude=..&current=..&hourly=..
                  &daily=..&forecast_days=7&forecast_hours=168&timezone=auto&temperature_unit=..&wind_speed_unit=..
    places    GET https://geocoding-api.open-meteo.com/v1/search?name=Paris&count=10&language=en&format=json

Home is, in order: the place the parent chose on the dashboard (Settings, "Weather & time", stored
in the setting ``info.location``), ``info.location`` in the config, else approximately where the
robot's public IP address is (``info.locate_by_ip``: ipinfo.io, then ipwho.is, then ipapi.co, free and
without a key). The IP answer is kept in memory and in the database (``info:ip_location``) and asked
again after a week, so it costs one request once in a while. The dashboard says where home came from.

Units: ``info.units`` (or the parent's choice), "auto" = Fahrenheit and mph for the US and the few
other countries that use them (home's country, else the robot's locale), else Celsius and km/h
(mph in the UK).

Fast on purpose (the live voice waits at most 5 s for an answer): every answer has
``info.timeout_seconds`` (3 s) for all its requests together; a forecast is reused for
``info.cache_minutes`` (10), a place search for a day. A request that runs out of time carries on in
the background, so the next question finds it cached. Nothing is sent in offline mode or while cloud
communication is off in Privacy.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import functools
import locale
import logging
import math
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger("littlebot.info.weather")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
SOURCE = "Open-Meteo"
PLACE_TTL_S = 24 * 3600.0
IP_KEEP_S = 7 * 24 * 3600.0  # the IP location is looked up again after this long
IP_MEMORY_S = 3600.0  # ...and re-read from the database this often (an old one kept after a failure is retried)
IP_RETRY_S = 120.0  # after every IP service failed, don't ask again for this long (answers stay fast)
IP_KEY = "info:ip_location"  # database key/value store
MAX_DAYS = 7
NEXT_HOURS = 4  # three-hour blocks after the current hour in the report
HOURS_AHEAD = MAX_DAYS * 24  # hourly data from the current hour on (the report's next hours, a screen card's days)
DAY_HOURS = (6, 9, 12, 15, 18, 21)  # the hours across a day in ``outlook()`` (the weather card)

CURRENT_VARS = ("temperature_2m", "apparent_temperature", "relative_humidity_2m", "precipitation", "weather_code",
                "wind_speed_10m", "wind_gusts_10m", "is_day")
HOURLY_VARS = ("temperature_2m", "precipitation_probability", "weather_code", "is_day")
DAILY_VARS = ("weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_probability_max",
              "precipitation_sum", "snowfall_sum", "wind_speed_10m_max", "wind_gusts_10m_max", "uv_index_max",
              "sunrise", "sunset")

# Countries (ISO 3166 alpha-2) where people use Fahrenheit, and those that give wind in mph.
FAHRENHEIT_COUNTRIES = frozenset({"US", "PR", "GU", "VI", "AS", "MP", "UM", "LR", "MM", "BS", "BZ", "KY", "PW",
                                  "FM", "MH"})
MPH_COUNTRIES = FAHRENHEIT_COUNTRIES | {"GB", "IM", "JE", "GG"}

# WMO weather interpretation codes (Open-Meteo's ``weather_code``) in plain words.
CONDITIONS = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "cloudy",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "rain showers", 82: "heavy rain showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with heavy hail",
}

ERRORS = {
    "offline": "LittleBot is in offline mode, so it can't get the weather",
    "cloud_off": "cloud communication is off in Privacy, so LittleBot can't get the weather",
    "not_found": "no place called {place} was found; try a nearby city, or the city and its country",
    "no_location": "LittleBot doesn't know where home is yet; a grown-up can set it on the dashboard "
                   "(Settings, Weather & time)",
    "timeout": "the weather service didn't answer in time",
    "network": "the weather service couldn't be reached (is the robot online?)",
    "bad_response": "the weather service gave an answer LittleBot couldn't read",
    "limited": "the weather service is busy (too many requests) and asked LittleBot to try again later",
    "bad_place": "say which place",
}
SOURCE_DETAIL = {
    "dashboard": "set on the dashboard",
    "config": "from LittleBot's config file (info.location)",
    "internet": "from your internet connection, approximate",
    "none": "not set",
}


class WeatherError(Exception):
    """Why there is no weather answer: ``kind`` is a key of ERRORS; str() is a plain sentence."""

    def __init__(self, kind: str, place: str | None = None) -> None:
        self.kind = kind if kind in ERRORS else "bad_response"
        self.place = place
        super().__init__(ERRORS[self.kind].format(place=(place or "that")[:60]))


@dataclass(frozen=True)
class Place:
    name: str  # "Toronto" (or "43.65, -79.38" for coordinates)
    latitude: float
    longitude: float
    region: str = ""  # "Ontario"
    country: str = ""  # "Canada"
    country_code: str = ""  # "CA"
    timezone: str = ""  # "America/Toronto" ("" = not known)

    @property
    def label(self) -> str:
        """ "Toronto, Ontario, Canada" """
        parts = [self.name, self.region, self.country]
        return ", ".join(dict.fromkeys(p for p in parts if p))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "Place | None":
        """A stored place, or None when ``data`` isn't one (empty, or not valid)."""
        if not isinstance(data, dict):
            return None
        try:
            lat, lon = float(data["latitude"]), float(data["longitude"])
        except (KeyError, TypeError, ValueError):
            return None
        if not _valid_coords(lat, lon):
            return None
        text = {k: str(data.get(k) or "").strip()[:80] for k in ("name", "region", "country", "timezone")}
        code = str(data.get("country_code") or "").strip().upper()[:2]
        return cls(text["name"] or _coords_name(lat, lon), lat, lon, text["region"], text["country"], code,
                   text["timezone"])


def _valid_coords(lat: float, lon: float) -> bool:
    return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180


def _coords_name(lat: float, lon: float) -> str:
    return f"{lat:.2f}, {lon:.2f}"


_COORDS = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def parse_coordinates(text: str) -> Place | None:
    """ "43.65, -79.38" -> a Place at those coordinates; anything else -> None."""
    m = _COORDS.match(str(text or ""))
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    return Place(_coords_name(lat, lon), lat, lon) if _valid_coords(lat, lon) else None


def locale_country() -> str | None:
    """The robot's locale territory ("en_US.UTF-8" -> "US"), if it says one."""
    candidates = [os.environ.get(k, "") for k in ("LC_ALL", "LC_MEASUREMENT", "LANG")]
    try:
        candidates.append(locale.getlocale()[0] or "")
    except (ValueError, TypeError):
        pass
    for value in candidates:
        m = re.match(r"^[a-z]{2,3}[_-]([A-Z]{2})\b", value or "")
        if m:
            return m.group(1)
    return None


def temperature_units(choice: str | None, country_code: str | None) -> str:
    """ "celsius" | "fahrenheit": the choice, or (auto) the country's custom, else the locale's."""
    if choice in ("celsius", "fahrenheit"):
        return choice
    country = (country_code or locale_country() or "").upper()
    return "fahrenheit" if country in FAHRENHEIT_COUNTRIES else "celsius"


def wind_units(temperature: str, country_code: str | None) -> str:
    country = (country_code or locale_country() or "").upper()
    return "mph" if temperature == "fahrenheit" or country in MPH_COUNTRIES else "kmh"


def conditions(code: Any, *, day: bool = True) -> str:
    try:
        n = int(code)
    except (TypeError, ValueError):
        return "unknown"
    if n == 0:
        return "sunny" if day else "clear"
    if n == 1 and day:
        return "mostly sunny"
    return CONDITIONS.get(n, "unknown")


# ---------------------------------------------------------------- cache
class _Cache:
    """Values kept for ``ttl()`` seconds. One fetch per key at a time (callers share it); a fetch whose
    caller stopped waiting (timeout) carries on, and its value is kept for the next caller."""

    def __init__(self, ttl: Callable[[], float], now: Callable[[], float], max_items: int = 64) -> None:
        self._ttl, self._now, self._max = ttl, now, max_items
        self._items: dict[Any, tuple[float, Any]] = {}
        self._pending: dict[Any, asyncio.Task[Any]] = {}

    def peek(self, key: Any) -> Any:
        """The kept value (fresh or not), without fetching; None if there is none."""
        hit = self._items.get(key)
        return hit[1] if hit is not None else None

    def fresh(self, key: Any) -> bool:
        hit = self._items.get(key)
        return hit is not None and self._now() - hit[0] < self._ttl()

    async def get(self, key: Any, fetch: Callable[[], Awaitable[Any]]) -> Any:
        if self.fresh(key):
            return self._items[key][1]
        loop = asyncio.get_running_loop()
        task = self._pending.get(key)
        if task is None or task.done() or task.get_loop() is not loop:
            task = self._pending[key] = loop.create_task(fetch())
            task.add_done_callback(functools.partial(self._settle, key))
        return await asyncio.shield(task)

    def put(self, key: Any, value: Any) -> None:
        self._items[key] = (self._now(), value)
        while len(self._items) > self._max:
            self._items.pop(next(iter(self._items)))

    def clear(self) -> None:
        self._items.clear()

    def _settle(self, key: Any, task: asyncio.Task[Any]) -> None:
        if self._pending.get(key) is task:
            del self._pending[key]
        if task.cancelled() or task.exception() is not None:  # (retrieves the exception: no warning)
            return
        self.put(key, task.result())


# ---------------------------------------------------------------- the service
class WeatherService:
    """``get_weather(ctx)``: one per robot, shared by the tools and the dashboard."""

    def __init__(self, ctx: Any, *, transport: httpx.AsyncBaseTransport | None = None,
                 now: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self.ctx = ctx
        self._transport = transport
        self._now = now
        self._wall = wall
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._forecasts = _Cache(lambda: max(0.0, float(self.cfg.cache_minutes)) * 60, now)
        self._places = _Cache(lambda: PLACE_TTL_S, now, max_items=50)
        self._ip = _Cache(lambda: IP_MEMORY_S, now, max_items=1)
        self._ip_failed_at: float | None = None

    @property
    def cfg(self) -> Any:
        return self.ctx.config.info

    # ------------------------------------------------------------ gates
    def blocked_reason(self) -> str | None:
        if self.ctx.config.is_offline:
            return "offline"
        if not self.ctx.privacy.cloud_enabled:
            return "cloud_off"
        return None

    # ------------------------------------------------------------ for the tools
    async def report(self, place: str | None = None, *, days: int = 2) -> dict[str, Any]:
        """The weather now and the forecast (``days``: 1 = today, 2 = and tomorrow, up to 7), at home or
        at ``place``. Raises WeatherError; never takes longer than ``info.timeout_seconds``."""
        days = max(1, min(MAX_DAYS, int(days)))
        return await self._bounded(self._report(str(place or "").strip()[:100] or None, days))

    async def outlook(self, place: str | None = None) -> dict[str, Any]:
        """``report(place, days=7)`` for the screen's weather card (littlebot/screens/cards/weather.py): each
        forecast day also has ``"hours"``, a few hours across it (today: the next ones), each with its
        ``time``, ``temperature``, ``conditions`` and ``chance_of_rain_or_snow_percent``. Raises WeatherError."""
        return await self._bounded(self._report(str(place or "").strip()[:100] or None, MAX_DAYS, hours=True))

    async def find(self, query: str) -> Place:
        """The best match for a place name ("Paris", "Springfield, Illinois") or "lat, lon"."""
        query = " ".join(str(query or "").split())[:100]
        if not query:
            raise WeatherError("bad_place")
        coords = parse_coordinates(query)
        if coords is not None:
            return coords
        reason = self.blocked_reason()
        if reason:
            raise WeatherError(reason)
        found = await self._places.get(query.lower(), functools.partial(self._geocode, query))
        if not found:
            raise WeatherError("not_found", place=query)
        return found[0]

    async def find_bounded(self, query: str) -> Place:
        return await self._bounded(self.find(query))

    async def _bounded(self, coro: Awaitable[Any]) -> Any:
        reason = self.blocked_reason()
        if reason:
            close = getattr(coro, "close", None)
            if close is not None:
                close()  # never started
            raise WeatherError(reason)
        try:
            return await asyncio.wait_for(coro, float(self.cfg.timeout_seconds))
        except asyncio.TimeoutError:
            log.info("weather: no answer within %.1f s (it carries on for next time)", self.cfg.timeout_seconds)
            raise WeatherError("timeout") from None

    async def _report(self, place: str | None, days: int, *, hours: bool = False) -> dict[str, Any]:
        t0 = self._now()
        if place:
            source = "asked"
            if self._units_choice() == "auto":  # units follow home: "weather in Paris" for a family in Ohio is in °F
                where, (home, _) = await asyncio.gather(self.find(place), self._home_or_none())
            else:
                where, home = await self.find(place), None
        else:
            where, source = await self.home()
            if where is None:
                raise WeatherError("no_location")
            home = where
        units = temperature_units(self._units_choice(), home.country_code if home else None)
        wind = wind_units(units, home.country_code if home else None)
        data = await self._forecast(where, units, wind)
        out = summarize(data, where, units=units, wind=wind, days=days, day_hours=hours)
        out["location_from"] = ("the place asked for" if source == "asked" else
                                f"home, {SOURCE_DETAIL.get(source, source)}")
        log.info("weather: answered in %d ms (%s)", int((self._now() - t0) * 1000), source)
        return out

    async def _home_or_none(self) -> tuple[Place | None, str]:
        try:
            return await self.home()
        except WeatherError:
            return None, "none"

    # ------------------------------------------------------------ home
    def _stored_home(self) -> Place | None:
        return Place.from_dict(self.ctx.settings.get("info.location") or {})

    def _units_choice(self) -> str:
        choice = str(self.ctx.settings.get("info.units", self.cfg.units) or "auto")
        return choice if choice in ("auto", "celsius", "fahrenheit") else "auto"

    def home_set(self) -> bool:
        """Home was chosen on the dashboard (no lookup needed to know where it is)."""
        return self._stored_home() is not None

    async def home(self) -> tuple[Place | None, str]:
        """(home, where it came from: dashboard | config | internet | none). May raise WeatherError."""
        stored = self._stored_home()
        if stored is not None:
            return stored, "dashboard"
        if self.cfg.location.strip():
            return await self.find(self.cfg.location), "config"
        if self.cfg.locate_by_ip:
            return await self._ip.get("ip", self._locate_by_ip), "internet"
        return None, "none"

    async def set_home(self, text: str) -> Place:
        """The parent's choice (dashboard): a place name (searched now) or "lat, lon". Raises WeatherError."""
        text = " ".join(str(text or "").split())[:100]
        place = parse_coordinates(text)
        if place is None:
            place = await self.find_bounded(text)
        self.ctx.settings.set("info.location", {**place.to_dict(), "query": text}, source="dashboard")
        log.info("weather: home set on the dashboard")
        return place

    def clear_home(self) -> None:
        """Back to automatic: the config's place, else the internet connection."""
        self.ctx.settings.set("info.location", {}, source="dashboard")

    def set_units(self, units: str) -> str:
        if units not in ("auto", "celsius", "fahrenheit"):
            raise ValueError("units must be auto, celsius or fahrenheit")
        return self.ctx.settings.set("info.units", units, source="dashboard")

    def status(self) -> dict[str, Any]:
        """What the dashboard shows (nothing is fetched: only what is already known)."""
        stored = self._stored_home()
        if stored is not None:
            home, source, query = stored, "dashboard", str((self.ctx.settings.get("info.location") or {})
                                                           .get("query") or "")
        elif self.cfg.location.strip():
            found = self._places.peek(" ".join(self.cfg.location.split())[:100].lower())
            home = parse_coordinates(self.cfg.location) or (found[0] if found else None)
            source, query = "config", self.cfg.location.strip()
        elif self.cfg.locate_by_ip:
            home, source, query = self._ip.peek("ip") or self._saved_ip_place()[0], "internet", ""
        else:
            home, source, query = None, "none", ""
        choice = self._units_choice()
        units = temperature_units(choice, home.country_code if home else None)
        reason = self.blocked_reason()
        return {
            "location": home.label if home else (query or None),
            "found": home is not None,
            "source": source,
            "source_detail": SOURCE_DETAIL[source],
            "query": query,
            "latitude": round(home.latitude, 4) if home else None,
            "longitude": round(home.longitude, 4) if home else None,
            "timezone": (home.timezone or None) if home else None,
            "units": choice,
            "units_in_use": units,
            "wind_units": wind_units(units, home.country_code if home else None),
            "blocked": reason,
            "blocked_detail": ERRORS[reason] if reason else None,
            "locate_by_ip": bool(self.cfg.locate_by_ip),
        }

    async def lookup_status(self) -> dict[str, Any]:
        """``status()`` after finding home if it isn't known yet (bounded; failures are left to status)."""
        if self.blocked_reason() is None:
            try:
                await self._bounded(self.home())
            except WeatherError as e:
                log.info("weather: home not found for the dashboard (%s)", e.kind)
        return self.status()

    # ------------------------------------------------------------ network
    def _http(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._client.is_closed or self._client_loop is not loop:
            from littlebot import __version__  # noqa: PLC0415

            self._client = httpx.AsyncClient(
                transport=self._transport, follow_redirects=False,
                headers={"User-Agent": f"LittleBot/{__version__}", "Accept": "application/json"},
                timeout=httpx.Timeout(float(self.cfg.timeout_seconds),
                                      connect=min(2.5, float(self.cfg.timeout_seconds))))
            self._client_loop = loop
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            resp = await self._http().get(url, params=params)
        except httpx.TimeoutException:
            raise WeatherError("timeout") from None
        except httpx.HTTPError:
            raise WeatherError("network") from None
        if resp.status_code >= 400:
            log.info("weather: %s answered HTTP %d", httpx.URL(url).host, resp.status_code)
            raise WeatherError("limited" if resp.status_code in (429, 503) else "bad_response")
        try:
            data = resp.json()
        except ValueError:
            raise WeatherError("bad_response") from None
        if not isinstance(data, dict):
            raise WeatherError("bad_response")
        return data

    async def _forecast(self, place: Place, units: str, wind: str) -> dict[str, Any]:
        key = (round(place.latitude, 3), round(place.longitude, 3), units, wind)
        return await self._forecasts.get(key, functools.partial(self._fetch_forecast, place, units, wind))

    async def _fetch_forecast(self, place: Place, units: str, wind: str) -> dict[str, Any]:
        t0 = self._now()
        data = await self._get_json(FORECAST_URL, {
            "latitude": f"{place.latitude:.4f}", "longitude": f"{place.longitude:.4f}",
            "current": ",".join(CURRENT_VARS), "hourly": ",".join(HOURLY_VARS), "daily": ",".join(DAILY_VARS),
            "forecast_days": MAX_DAYS, "forecast_hours": HOURS_AHEAD, "timezone": "auto",
            "temperature_unit": units, "wind_speed_unit": wind, "precipitation_unit": "mm"})
        if not isinstance(data.get("current"), dict) or not isinstance(data.get("daily"), dict):
            raise WeatherError("bad_response")
        log.info("weather: forecast fetched in %d ms", int((self._now() - t0) * 1000))
        return data

    async def _geocode(self, query: str) -> list[Place]:
        places = await self._search(query)
        if not places and "," in query:  # "Springfield, Illinois": the name, then the region or country
            name, _, rest = query.partition(",")
            want = [w.strip().lower() for w in rest.split(",") if w.strip()]
            places = [p for p in await self._search(name.strip())
                      if all(any(w == x.lower() or w in x.lower() for x in (p.region, p.country, p.country_code)
                                 if x) for w in want)]
        return places

    async def _search(self, name: str) -> list[Place]:
        if not name:
            return []
        data = await self._get_json(GEOCODING_URL, {"name": name, "count": 10, "language": "en", "format": "json"})
        out: list[Place] = []
        for r in data.get("results") or []:
            p = Place.from_dict({**r, "region": r.get("admin1") or ""}) if isinstance(r, dict) else None
            if p is not None:
                out.append(p)
        return out

    def _saved_ip_place(self) -> tuple[Place | None, float]:
        try:
            saved = self.ctx.db.kv_get(IP_KEY)
        except Exception:  # noqa: BLE001 - a broken database must not stop the weather
            return None, 0.0
        if not isinstance(saved, dict):
            return None, 0.0
        at = saved.get("at")
        return Place.from_dict(saved.get("place")), float(at) if isinstance(at, (int, float)) else 0.0

    async def _locate_by_ip(self) -> Place:
        saved, at = self._saved_ip_place()
        if saved is not None and self._wall() - at < IP_KEEP_S:
            return saved
        if self._ip_failed_at is not None and self._now() - self._ip_failed_at < IP_RETRY_S:
            if saved is not None:
                return saved
            raise WeatherError("no_location")
        for url, parse in IP_SERVICES:
            try:
                place = parse(await self._get_json(url))
            except WeatherError:
                place = None
            if place is not None:
                self._ip_failed_at = None
                try:
                    self.ctx.db.kv_set(IP_KEY, {"place": place.to_dict(), "at": self._wall(),
                                                "from": httpx.URL(url).host})
                except Exception:  # noqa: BLE001
                    log.debug("weather: couldn't keep the IP location", exc_info=True)
                log.info("weather: home located from the internet connection (%s, country %s)",
                         httpx.URL(url).host, place.country_code or "?")
                return place
        self._ip_failed_at = self._now()
        log.warning("weather: couldn't find where the robot is from its internet connection")
        if saved is not None:
            return saved  # old, but better than nothing
        raise WeatherError("no_location")


# ---------------------------------------------------------------- IP location services (free, no key)
def _ip_place(lat: Any, lon: Any, city: Any, region: Any, country: Any, code: Any, tz: Any) -> Place | None:
    return Place.from_dict({"latitude": lat, "longitude": lon, "name": city, "region": region, "country": country,
                            "country_code": code, "timezone": tz})


def _parse_ipinfo(d: dict[str, Any]) -> Place | None:
    lat, _, lon = str(d.get("loc") or "").partition(",")
    return _ip_place(lat, lon, d.get("city"), d.get("region"), "", d.get("country"), d.get("timezone"))


def _parse_ipwhois(d: dict[str, Any]) -> Place | None:
    if d.get("success") is False:
        return None
    tz = d.get("timezone")
    return _ip_place(d.get("latitude"), d.get("longitude"), d.get("city"), d.get("region"), d.get("country"),
                     d.get("country_code"), tz.get("id") if isinstance(tz, dict) else tz)


def _parse_ipapi(d: dict[str, Any]) -> Place | None:
    if d.get("error"):
        return None
    return _ip_place(d.get("latitude"), d.get("longitude"), d.get("city"), d.get("region"), d.get("country_name"),
                     d.get("country_code"), d.get("timezone"))


IP_SERVICES: tuple[tuple[str, Callable[[dict[str, Any]], Place | None]], ...] = (
    ("https://ipinfo.io/json", _parse_ipinfo),
    ("https://ipwho.is/", _parse_ipwhois),
    ("https://ipapi.co/json/", _parse_ipapi),
)


# ---------------------------------------------------------------- the report
def summarize(data: dict[str, Any], place: Place, *, units: str, wind: str, days: int = 2,
              day_hours: bool = False) -> dict[str, Any]:
    """Open-Meteo's answer as a short report for the model (rounded numbers, plain words); ``day_hours``
    adds a few hours across each forecast day (``outlook()``)."""
    cur = data.get("current") if isinstance(data.get("current"), dict) else {}
    daytime = cur.get("is_day") != 0
    now: dict[str, Any] = {
        "temperature": _round(cur.get("temperature_2m")),
        "feels_like": _round(cur.get("apparent_temperature")),
        "conditions": conditions(cur.get("weather_code"), day=daytime),
        "humidity_percent": _round(cur.get("relative_humidity_2m")),
        "wind": _round(cur.get("wind_speed_10m")),
        "gusts": _round(cur.get("wind_gusts_10m")),
    }
    rain_now = _num(cur.get("precipitation"))
    if rain_now:
        now["precipitation_mm"] = round(rain_now, 1)
    out: dict[str, Any] = {
        "place": place.label,
        "local_time": _clock(cur.get("time")),
        "units": {"temperature": "°F" if units == "fahrenheit" else "°C", "wind": "mph" if wind == "mph" else "km/h"},
        "now": {k: v for k, v in now.items() if v is not None},
        "forecast": _days(data.get("daily"), days, data.get("hourly") if day_hours else None),
        "next_hours": _hours(data.get("hourly")),
        "source": SOURCE,
    }
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def summary_text(report: dict[str, Any]) -> str:
    """One line for the parent: "Toronto, Ontario, Canada: now 12°C (feels like 9°C), sunny. Today 5 to 13°C,
    cloudy, 10% chance of rain or snow." """
    deg = (report.get("units") or {}).get("temperature", "°")
    now = report.get("now") or {}
    parts = [f"{report.get('place') or 'Here'}: now"]
    if now.get("temperature") is not None:
        parts.append(f" {now['temperature']}{deg}")
        if now.get("feels_like") is not None and now["feels_like"] != now["temperature"]:
            parts.append(f" (feels like {now['feels_like']}{deg})")
        parts.append(",")
    parts.append(f" {now.get('conditions') or 'unknown'}.")
    today = (report.get("forecast") or [{}])[0]
    if today.get("low") is not None and today.get("high") is not None:
        parts.append(f" Today {today['low']} to {today['high']}{deg}, {today.get('conditions') or 'unknown'}")
        if today.get("chance_of_rain_or_snow_percent") is not None:
            parts.append(f", {today['chance_of_rain_or_snow_percent']}% chance of rain or snow")
        parts.append(".")
    return "".join(parts)


def _days(daily: Any, days: int, hourly: Any = None) -> list[dict[str, Any]]:
    if not isinstance(daily, dict):
        return []
    dates = daily.get("time") if isinstance(daily.get("time"), list) else []
    out = []
    for i, date in enumerate(dates[:days]):
        def at(key: str) -> Any:
            values = daily.get(key)
            return values[i] if isinstance(values, list) and i < len(values) else None

        try:
            d = _dt.date.fromisoformat(str(date))
        except ValueError:
            continue
        label = "today" if i == 0 else "tomorrow" if i == 1 else f"{d:%A}"
        rain, snow, uv = _num(at("precipitation_sum")), _num(at("snowfall_sum")), _num(at("uv_index_max"))
        day = {
            "day": label, "date": f"{d:%A} {d.day} {d:%B}", "conditions": conditions(at("weather_code")),
            "high": _round(at("temperature_2m_max")), "low": _round(at("temperature_2m_min")),
            "chance_of_rain_or_snow_percent": _round(at("precipitation_probability_max")),
            "rain_mm": round(rain, 1) if rain else None, "snow_cm": round(snow, 1) if snow else None,
            "wind_max": _round(at("wind_speed_10m_max")), "gusts_max": _round(at("wind_gusts_10m_max")),
            "uv_index_max": round(uv, 1) if uv is not None else None,
            "sunrise": _clock(at("sunrise")), "sunset": _clock(at("sunset")),
        }
        if hourly is not None:
            day["hours"] = _day_hours(hourly, d, first=i == 0)
        out.append({k: v for k, v in day.items() if v is not None})
    return out


def _day_hours(hourly: Any, date: _dt.date, *, first: bool) -> list[dict[str, Any]]:
    """A few hours across ``date`` (DAY_HOURS); the first day: every third hour from the next one on."""
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        return []

    def at(key: str, i: int) -> Any:
        values = hourly.get(key)
        return values[i] if isinstance(values, list) and i < len(values) else None

    picked: list[tuple[int, str]] = []
    for i, stamp in enumerate(hourly["time"]):
        try:
            t = _dt.datetime.fromisoformat(str(stamp))
        except ValueError:
            continue
        if first:
            if i >= 1 and (i - 1) % 3 == 0:
                picked.append((i, f"{t:%H:%M}"))
        elif t.date() == date and t.minute == 0 and t.hour in DAY_HOURS:
            picked.append((i, f"{t:%H:%M}"))
        if len(picked) >= len(DAY_HOURS):
            break
    out = []
    for i, clock in picked:
        code = at("weather_code", i)
        hour = {
            "time": clock, "temperature": _round(at("temperature_2m", i)),
            "conditions": conditions(code, day=at("is_day", i) != 0) if code is not None else None,
            "chance_of_rain_or_snow_percent": _round(at("precipitation_probability", i)),
        }
        if hour["temperature"] is not None:
            out.append({k: v for k, v in hour.items() if v is not None})
    return out


def _hours(hourly: Any) -> list[dict[str, Any]]:
    """The next few three-hour blocks after the current hour: worst conditions, highest chance of rain."""
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        return []
    times = hourly["time"]

    def col(key: str) -> list[Any]:
        v = hourly.get(key)
        return v if isinstance(v, list) else []

    temps, chances, codes, light = (col("temperature_2m"), col("precipitation_probability"), col("weather_code"),
                                    col("is_day"))
    out = []
    for b in range(NEXT_HOURS):
        idx = [i for i in range(1 + 3 * b, 4 + 3 * b) if i < len(times)]
        if not idx:
            break
        block_codes = [int(c) for c in (codes[i] for i in idx if i < len(codes)) if isinstance(c, (int, float))]
        block_chances = [c for c in (chances[i] for i in idx if i < len(chances)) if isinstance(c, (int, float))]
        mid = idx[len(idx) // 2]
        start, end = _clock(times[idx[0]]), _clock(_plus_hour(times[idx[-1]]))
        day = any(light[i] != 0 for i in idx if i < len(light)) if light else True
        block = {
            "hours": f"{start}-{end}" if start and end else start,
            "conditions": conditions(max(block_codes), day=day) if block_codes else None,
            "temperature": _round(temps[mid]) if mid < len(temps) else None,
            "chance_of_rain_or_snow_percent": _round(max(block_chances)) if block_chances else None,
        }
        out.append({k: v for k, v in block.items() if v is not None})
    return out


def _plus_hour(stamp: Any) -> str | None:
    try:
        return (_dt.datetime.fromisoformat(str(stamp)) + _dt.timedelta(hours=1)).isoformat(timespec="minutes")
    except ValueError:
        return None


def _clock(stamp: Any) -> str | None:
    """ "2026-10-23T17:30" -> "17:30" """
    m = re.search(r"T(\d{2}:\d{2})", str(stamp or ""))
    return m.group(1) if m else None


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _round(v: Any) -> int | None:
    n = _num(v)
    return int(round(n)) if n is not None else None


_KEY = "weather"


def get_weather(ctx: Any) -> WeatherService:
    """The one WeatherService shared by the tools and the dashboard (``ctx.extras["weather"]``)."""
    svc = ctx.extras.get(_KEY)
    if svc is None:
        svc = ctx.extras[_KEY] = WeatherService(ctx)
    return svc


__all__ = ["ERRORS", "Place", "WeatherError", "WeatherService", "get_weather", "parse_coordinates", "summarize",
           "temperature_units"]
