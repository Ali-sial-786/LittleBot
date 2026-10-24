"""The clock card: the time and date, big, here or anywhere ("what time is it in Tokyo?").

A friendly analogue face (big numbers, candy dots, a sun or moon for day and night) for the little
one, and the digital time, the weekday and the date for the grown-ups. Another place's time comes
with how far ahead or behind home it is.

Places: an IANA zone ("Asia/Tokyo") is used as it is; common cities and countries are mapped to
their zone here (no network); anything else is looked up with the weather service's place search
(littlebot/info/weather.py), which knows each place's zone. The face is redrawn once a minute (a
cached sprite), so a frame is a few blits.
"""

from __future__ import annotations

import datetime as _dt
import functools
import logging
import math
import time
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import pygame

from littlebot.games import art
from littlebot.screens.base import (
    BODY_PX,
    TITLE_PX,
    Card,
    CardHost,
    CardType,
    W,
    register,
)
from littlebot.screens.cards import candy
from littlebot.screens.cards.weather import weather_icon

log = logging.getLogger("littlebot.screens.clock")

FACE_PX = 316
FACE_CENTER = (candy.PANEL.left + 176, candy.PANEL.centery)
TEXT_X = candy.PANEL.left + 520  # the centre of the text column
TEXT_W = 320

# Common places -> IANA zones (a country is its capital's or biggest city's zone).
ZONES: dict[str, str] = {
    # cities
    "london": "Europe/London", "paris": "Europe/Paris", "berlin": "Europe/Berlin", "madrid": "Europe/Madrid",
    "barcelona": "Europe/Madrid", "rome": "Europe/Rome", "milan": "Europe/Rome", "amsterdam": "Europe/Amsterdam",
    "brussels": "Europe/Brussels", "vienna": "Europe/Vienna", "zurich": "Europe/Zurich", "geneva": "Europe/Zurich",
    "dublin": "Europe/Dublin", "lisbon": "Europe/Lisbon", "athens": "Europe/Athens", "istanbul": "Europe/Istanbul",
    "moscow": "Europe/Moscow", "stockholm": "Europe/Stockholm", "oslo": "Europe/Oslo",
    "copenhagen": "Europe/Copenhagen", "helsinki": "Europe/Helsinki", "warsaw": "Europe/Warsaw",
    "prague": "Europe/Prague", "budapest": "Europe/Budapest", "kyiv": "Europe/Kyiv", "kiev": "Europe/Kyiv",
    "edinburgh": "Europe/London", "manchester": "Europe/London", "new york": "America/New_York",
    "nyc": "America/New_York", "boston": "America/New_York", "washington": "America/New_York",
    "miami": "America/New_York", "atlanta": "America/New_York", "philadelphia": "America/New_York",
    "detroit": "America/Detroit", "chicago": "America/Chicago", "houston": "America/Chicago",
    "dallas": "America/Chicago", "austin": "America/Chicago", "new orleans": "America/Chicago",
    "denver": "America/Denver", "phoenix": "America/Phoenix", "salt lake city": "America/Denver",
    "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles", "san francisco": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "las vegas": "America/Los_Angeles", "san diego": "America/Los_Angeles",
    "portland": "America/Los_Angeles", "anchorage": "America/Anchorage", "honolulu": "Pacific/Honolulu",
    "hawaii": "Pacific/Honolulu", "alaska": "America/Anchorage", "toronto": "America/Toronto",
    "montreal": "America/Toronto", "ottawa": "America/Toronto", "vancouver": "America/Vancouver",
    "calgary": "America/Edmonton", "edmonton": "America/Edmonton", "winnipeg": "America/Winnipeg",
    "halifax": "America/Halifax", "mexico city": "America/Mexico_City", "sao paulo": "America/Sao_Paulo",
    "rio de janeiro": "America/Sao_Paulo", "rio": "America/Sao_Paulo", "buenos aires": "America/Argentina/Buenos_Aires",
    "santiago": "America/Santiago", "lima": "America/Lima", "bogota": "America/Bogota", "tokyo": "Asia/Tokyo",
    "osaka": "Asia/Tokyo", "kyoto": "Asia/Tokyo", "seoul": "Asia/Seoul", "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai", "hong kong": "Asia/Hong_Kong", "taipei": "Asia/Taipei", "singapore": "Asia/Singapore",
    "bangkok": "Asia/Bangkok", "hanoi": "Asia/Bangkok", "jakarta": "Asia/Jakarta", "manila": "Asia/Manila",
    "kuala lumpur": "Asia/Kuala_Lumpur", "delhi": "Asia/Kolkata", "new delhi": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata", "bangalore": "Asia/Kolkata", "kolkata": "Asia/Kolkata", "karachi": "Asia/Karachi",
    "lahore": "Asia/Karachi", "islamabad": "Asia/Karachi", "dhaka": "Asia/Dhaka", "kathmandu": "Asia/Kathmandu",
    "dubai": "Asia/Dubai", "abu dhabi": "Asia/Dubai", "doha": "Asia/Qatar", "riyadh": "Asia/Riyadh",
    "tehran": "Asia/Tehran", "jerusalem": "Asia/Jerusalem", "tel aviv": "Asia/Jerusalem", "cairo": "Africa/Cairo",
    "lagos": "Africa/Lagos", "nairobi": "Africa/Nairobi", "johannesburg": "Africa/Johannesburg",
    "cape town": "Africa/Johannesburg", "casablanca": "Africa/Casablanca", "accra": "Africa/Accra",
    "addis ababa": "Africa/Addis_Ababa", "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "brisbane": "Australia/Brisbane", "perth": "Australia/Perth", "adelaide": "Australia/Adelaide",
    "auckland": "Pacific/Auckland", "wellington": "Pacific/Auckland", "reykjavik": "Atlantic/Reykjavik",
    # countries and regions
    "uk": "Europe/London", "united kingdom": "Europe/London", "britain": "Europe/London",
    "great britain": "Europe/London", "england": "Europe/London", "scotland": "Europe/London",
    "wales": "Europe/London", "ireland": "Europe/Dublin", "france": "Europe/Paris", "germany": "Europe/Berlin",
    "spain": "Europe/Madrid", "italy": "Europe/Rome", "netherlands": "Europe/Amsterdam",
    "holland": "Europe/Amsterdam", "belgium": "Europe/Brussels", "switzerland": "Europe/Zurich",
    "austria": "Europe/Vienna", "portugal": "Europe/Lisbon", "greece": "Europe/Athens", "turkey": "Europe/Istanbul",
    "russia": "Europe/Moscow", "sweden": "Europe/Stockholm", "norway": "Europe/Oslo",
    "denmark": "Europe/Copenhagen", "finland": "Europe/Helsinki", "poland": "Europe/Warsaw",
    "czechia": "Europe/Prague", "czech republic": "Europe/Prague", "hungary": "Europe/Budapest",
    "ukraine": "Europe/Kyiv", "iceland": "Atlantic/Reykjavik", "usa": "America/New_York",
    "us": "America/New_York", "united states": "America/New_York", "america": "America/New_York",
    "canada": "America/Toronto", "mexico": "America/Mexico_City", "brazil": "America/Sao_Paulo",
    "argentina": "America/Argentina/Buenos_Aires", "chile": "America/Santiago", "peru": "America/Lima",
    "colombia": "America/Bogota", "japan": "Asia/Tokyo", "korea": "Asia/Seoul", "south korea": "Asia/Seoul",
    "china": "Asia/Shanghai", "taiwan": "Asia/Taipei", "thailand": "Asia/Bangkok", "vietnam": "Asia/Bangkok",
    "indonesia": "Asia/Jakarta", "philippines": "Asia/Manila", "malaysia": "Asia/Kuala_Lumpur",
    "india": "Asia/Kolkata", "pakistan": "Asia/Karachi", "bangladesh": "Asia/Dhaka", "nepal": "Asia/Kathmandu",
    "uae": "Asia/Dubai", "united arab emirates": "Asia/Dubai", "qatar": "Asia/Qatar",
    "saudi arabia": "Asia/Riyadh", "iran": "Asia/Tehran", "israel": "Asia/Jerusalem", "egypt": "Africa/Cairo",
    "nigeria": "Africa/Lagos", "kenya": "Africa/Nairobi", "south africa": "Africa/Johannesburg",
    "morocco": "Africa/Casablanca", "ghana": "Africa/Accra", "ethiopia": "Africa/Addis_Ababa",
    "australia": "Australia/Sydney", "new zealand": "Pacific/Auckland",
    "utc": "UTC", "gmt": "UTC", "greenwich": "Europe/London",
}
HOME_WORDS = ("", "here", "home", "local", "this house", "where we are")


@functools.lru_cache(maxsize=1)
def _zone_cities() -> dict[str, str]:
    """ "los angeles" -> "America/Los_Angeles": the city part of every zone the system knows."""
    out: dict[str, str] = {}
    try:
        names = available_timezones()
    except Exception:  # noqa: BLE001 - no tz database: only the table above
        return out
    for name in sorted(names):
        if "/" in name and not name.startswith(("Etc/", "SystemV/", "US/")):
            out.setdefault(name.rsplit("/", 1)[1].replace("_", " ").lower(), name)
    return out


def _zone(name: str) -> _dt.tzinfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def find_zone(place: str) -> tuple[str, str] | None:
    """(IANA zone, place to show) for "Tokyo", "japan", "Asia/Tokyo" or "Tokyo, Japan"; None if unknown here."""
    text = " ".join(str(place or "").split())[:100]
    if not text:
        return None
    if "/" in text or text.upper() in ("UTC", "GMT"):
        z = text.replace(" ", "_")
        if _zone(z) is not None:
            return z, z.rsplit("/", 1)[-1].replace("_", " ")
    low = text.lower().removeprefix("the ").strip(" .?!")
    for key in (low, low.split(",")[0].strip()):
        if key in ZONES:
            show = text.split(",")[0].strip()
            return ZONES[key], show[:1].upper() + show[1:] if show.islower() else show
        if key in _zone_cities():
            return _zone_cities()[key], text.split(",")[0].strip().title()
    rest = [p.strip() for p in low.split(",")[1:] if p.strip()]
    for key in rest:  # "Springfield, Illinois" is unknown, but a country after the comma is enough
        if key in ZONES:
            return ZONES[key], text.split(",")[0].strip().title()
    return None


def _offset_words(there: _dt.datetime, here: _dt.datetime) -> str:
    """ "13 hours ahead", "5½ hours behind", "the same time as here"."""
    a, b = there.utcoffset(), here.utcoffset()
    if a is None or b is None:
        return ""
    minutes = int((a - b).total_seconds() // 60)
    if minutes == 0:
        return "Same time as here"
    hours, rest = divmod(abs(minutes), 60)
    frac = {0: "", 30: "½", 15: "¼", 45: "¾"}.get(rest)
    amount = f"{hours}{frac}" if frac is not None else f"{hours}:{rest:02d}"
    unit = "hour" if amount == "1" else "hours"
    return f"{amount} {unit} {'ahead' if minutes > 0 else 'behind'}"


def _face(size: int, hour: int, minute: int) -> pygame.Surface:
    """The analogue clock face showing hour:minute (cached: redrawn once a minute)."""
    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            c = (n / 2, n / 2)
            rim = art.BUBBLEGUM
            pygame.draw.circle(s, art.inkier(rim, 0.4), c, n * 0.5)
            pygame.draw.circle(s, rim, c, n * 0.475)
            pygame.draw.circle(s, art.lighten(rim, 0.35), c, n * 0.455)
            pygame.draw.circle(s, art.CREAM, c, n * 0.42)
            numbers = {0: "12", 3: "3", 6: "6", 9: "9"}
            f = art.font(int(n * 0.15))
            for i in range(12):
                a = math.radians(i * 30 - 90)
                if i in numbers:
                    img = f.render(numbers[i], True, art.INK)
                    p = (c[0] + math.cos(a) * n * 0.335, c[1] + math.sin(a) * n * 0.335 + n * 0.006)
                    s.blit(img, img.get_rect(center=(int(p[0]), int(p[1]))))
                else:
                    color = art.CANDY[i % len(art.CANDY)]
                    p = (c[0] + math.cos(a) * n * 0.345, c[1] + math.sin(a) * n * 0.345)
                    pygame.draw.circle(s, art.inkier(color, 0.3), p, n * 0.03)
                    pygame.draw.circle(s, color, p, n * 0.022)
            for angle, length, width, color in (
                    ((hour % 12 + minute / 60) * 30, 0.175, 0.064, art.STRAWBERRY),
                    (minute * 6, 0.245, 0.044, art.SKY)):
                a = math.radians(angle - 90)
                tip = (c[0] + math.cos(a) * n * length, c[1] + math.sin(a) * n * length)
                art.capsule(s, art.inkier(color, 0.4), c, tip, n * (width + 0.018))
                art.capsule(s, color, c, tip, n * width)
            pygame.draw.circle(s, art.inkier(art.LEMON, 0.4), c, n * 0.05)
            pygame.draw.circle(s, art.LEMON, c, n * 0.036)

        return art.supersampled((size, size), draw)

    return art.cached(("clock_face", size, hour % 12, minute), make)


class ClockCard(Card):
    """The time and date here (``zone`` None: home) or at another place."""

    kind = "clock"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None, *, zone: str | None = None,
                 place: str = "", home_zone: str | None = None, twelve: bool = True,
                 wall: Callable[[], float] | None = None) -> None:
        super().__init__(spec, host)
        self.twelve = twelve
        self.home_zone = home_zone
        self.wall = wall or time.time  # wall-clock seconds (tests and previews replace it)
        self._key: tuple[Any, ...] = ()
        self.set_place(zone, place)

    def set_place(self, zone: str | None, place: str) -> None:
        self.zone, self.place = zone, place
        self.title = f"Time in {place}" if zone else "The time now"
        self._key = ()
        self._refresh()

    def _tz(self, name: str | None) -> _dt.tzinfo | None:
        return _zone(name) if name else None

    def _times(self) -> tuple[_dt.datetime, _dt.datetime]:
        now = _dt.datetime.fromtimestamp(self.wall(), _dt.UTC)
        home_tz = self._tz(self.home_zone)
        here = now.astimezone(home_tz) if home_tz else now.astimezone()
        tz = self._tz(self.zone)
        return (now.astimezone(tz) if tz else here), here

    def _refresh(self) -> None:
        there, here = self._times()
        key = (there.year, there.month, there.day, there.hour, there.minute)
        if key == self._key:
            return
        self._key = key
        self.local = there
        if self.twelve:
            self.time_text = f"{there.hour % 12 or 12}:{there.minute:02d}"
            self.ampm = "am" if there.hour < 12 else "pm"
        else:
            self.time_text, self.ampm = f"{there.hour:02d}:{there.minute:02d}", ""
        self.weekday = f"{there:%A}"
        self.date_text = f"{there.day} {there:%B}"
        self.offset = _offset_words(there, here) if self.zone else ""
        self.daytime = 6 <= there.hour < 18
        self._lines = self._compose()

    def _compose(self) -> list[pygame.Surface]:
        """The text column, made once a minute (not every frame)."""
        lines: list[pygame.Surface] = []
        big = candy.fit(self.time_text, 150, TEXT_W - (70 if self.ampm else 0), min_size=96)
        if self.ampm:
            ampm = art.text(self.ampm, TITLE_PX, art.INK)
            row = pygame.Surface((big.get_width() + 8 + ampm.get_width(), big.get_height()), pygame.SRCALPHA)
            row.blit(big, (0, 0))
            row.blit(ampm, ampm.get_rect(bottomleft=(big.get_width() + 8, big.get_height() - big.get_height() // 7)))
            big = row
        lines.append(big)
        icon = weather_icon("sun" if self.daytime else "moon", 58)
        day = art.text(self.weekday, TITLE_PX, art.INK)
        row = pygame.Surface((icon.get_width() + 8 + day.get_width(), max(icon.get_height(), day.get_height())),
                             pygame.SRCALPHA)
        row.blit(icon, icon.get_rect(midleft=(0, row.get_height() // 2)))
        row.blit(day, day.get_rect(midleft=(icon.get_width() + 8, row.get_height() // 2 + 2)))
        lines.append(row)
        lines.append(art.text(self.date_text, 38, art.INK))
        if self.offset:
            lines.append(candy.chip(self.offset, BODY_PX, art.INK, art.lighten(art.SKY, 0.7)))
        return lines

    def update(self, dt: float) -> None:
        super().update(dt)
        self._refresh()

    def command(self, action: str, args: dict[str, Any]) -> bool:
        if action != "place":
            return False
        name = " ".join(str((args or {}).get("name") or (args or {}).get("place") or "").split())
        if name.lower() in HOME_WORDS:
            self.set_place(None, "")
            return True
        found = find_zone(name)
        if found is None:
            return False
        self.set_place(*found)
        return True

    def describe(self) -> str:
        t = f"{self.time_text} {self.ampm}".strip()
        when = f"{t} on {self.weekday} {self.date_text}"
        if not self.zone:
            return f"It's {when}"
        return f"In {self.place} it's {when}" + (f", {self.offset.lower()}" if self.offset else "")

    def status(self) -> dict[str, Any]:
        return {**super().status(), "zone": self.zone or self.home_zone or "local"}

    def draw(self, surf: pygame.Surface) -> None:
        surf.blit(art.background((W, 480), seed=5), (0, 0))
        dy = candy.slide_in(self.t)
        candy.header(surf, self.title, dy=dy * 0.5)
        box = candy.panel(surf, dy=dy)
        candy.blit_mid(surf, _face(FACE_PX, self.local.hour, self.local.minute),
                       (FACE_CENTER[0], FACE_CENTER[1] + dy))
        lines = self._lines
        gaps = 14
        total = sum(img.get_height() for img in lines) + gaps * (len(lines) - 1)
        y = box.centery - total / 2
        for img in lines:
            surf.blit(img, img.get_rect(midtop=(TEXT_X, int(y))))
            y += img.get_height() + gaps


def _home_zone(ctx: Any) -> str | None:
    info = getattr(getattr(ctx, "config", None), "info", None)
    name = str(getattr(info, "timezone", "") or "").strip()
    return name if name and _zone(name) is not None else None


async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> ClockCard | None:
    """The clock here, or at ``spec["place"]`` / ``spec["timezone"]``; None when the place's zone isn't known."""
    zone_name, place = None, ""
    asked = " ".join(str(spec.get("timezone") or spec.get("place") or "").split())[:100]
    if asked.lower() not in HOME_WORDS:
        found = find_zone(asked)
        if found is None and spec.get("timezone") and spec.get("place"):
            found = find_zone(str(spec["place"]))
        if found is None and ctx is not None:  # the weather service's place search knows every place's zone
            from littlebot.info.weather import (
                WeatherError,
                get_weather,
            )

            try:
                p = await get_weather(ctx).find_bounded(str(spec.get("place") or asked))
            except WeatherError as e:
                log.info("screens: no clock for that place (%s)", e.kind)
                return None
            if p.timezone and _zone(p.timezone) is not None:
                found = (p.timezone, p.name)
        if found is None:
            return None
        zone_name, place = found
        if spec.get("place") and "/" in asked:  # the AI gave the zone and the place: show the place's name
            place = " ".join(str(spec["place"]).split())[:40]
    wall = None
    if spec.get("at"):  # a fixed moment (previews, SAMPLES)
        try:
            at = _dt.datetime.fromisoformat(str(spec["at"]))
            fixed = (at if at.tzinfo else at.replace(tzinfo=_dt.UTC)).timestamp()
            wall = lambda: fixed
        except ValueError:
            wall = None
    home = _home_zone(ctx) if ctx is not None else str(spec.get("home_timezone") or "") or None
    return ClockCard(spec, host, zone=zone_name, place=place, home_zone=home,
                     twelve=candy.uses_12h(ctx) if ctx is not None else bool(spec.get("twelve_hour", True)),
                     wall=wall)


register(CardType(
    kind="clock",
    description="The time and date, big, with a friendly clock face: here, or in another city or country "
                "('what time is it in Tokyo?', 'what day is it?').",
    fields={
        "place": {"type": "string", "description": "A city or country, only for somewhere else: 'Tokyo', "
                                                   "'Australia'. Leave out for here."},
        "timezone": {"type": "string", "description": "The IANA time zone if you know it ('Asia/Tokyo'); "
                                                      "optional."},
    },
    build=build,
    required=(),
    actions={"place": "the time somewhere else: {\"name\": \"Tokyo\"} (or an IANA zone); {\"name\": \"here\"} for "
                      "home"},
))

# Example specs for scripts/screen_preview.py ("at" fixes the moment shown).
SAMPLES: list[dict[str, Any]] = [
    {"card": "clock", "at": "2026-10-24T13:30:00+00:00", "home_timezone": "America/Toronto"},
    {"card": "clock", "place": "Tokyo", "at": "2026-10-24T13:30:00+00:00", "home_timezone": "America/Toronto"},
    {"card": "clock", "place": "Delhi", "at": "2026-10-24T13:05:00+00:00", "home_timezone": "Europe/London",
     "twelve_hour": False},
]

__all__ = ["SAMPLES", "ZONES", "ClockCard", "find_zone"]
