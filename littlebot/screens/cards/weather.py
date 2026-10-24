"""The weather card: now, a day, the week or hour by hour, at home or anywhere (littlebot/info/weather.py).

"What's the weather?" shows now: a big drawn icon, the temperature, feels-like, today's high and low
and the chance of rain. "And tomorrow?" / "on Saturday?" turn it to that day (its icon, high and low,
rain, a few hours across it); "the week" shows seven small day tiles; "hour by hour" the hours of the
day shown. The voice steers it (``command``: ``day``, ``view``, ``place``); a tap on the right half
shows the next day, on the left the day before (the arrow keys too).

The forecast comes from ``WeatherService.outlook()`` (one request, cached 10 minutes, nothing sent in
offline mode or with cloud communication off): units follow home and the parent's setting. Icons are
drawn with pygame shapes and cached, so a frame is a handful of blits.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import re
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import (
    BODY_PX,
    SMALL_PX,
    TITLE_PX,
    Card,
    CardHost,
    CardType,
    W,
    register,
)
from littlebot.screens.cards import candy

log = logging.getLogger("littlebot.screens.weather")

VIEWS = ("now", "day", "week", "hours")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WINDY = {"km/h": 40, "mph": 25}  # from this wind speed a dry day shows the wind icon
SLIDE_PX = 120  # how far the content slides when the day changes

# ---------------------------------------------------------------- icons
CLOUD: art.Color = (250, 250, 255)
CLOUD_DARK: art.Color = (176, 176, 212)  # a storm cloud
FOG: art.Color = (196, 186, 222)
DROP: art.Color = art.SKY
SUN: art.Color = art.LEMON
RAYS: art.Color = (255, 186, 70)
MOON: art.Color = (255, 228, 140)
WIND: art.Color = (110, 200, 236)
LOW_INK: art.Color = art.mix(art.INK_SOFT, art.INK, 0.35)  # a low temperature: softer, still readable


def icon_kind(conditions: str, *, windy: bool = False) -> str:
    """Which icon for Open-Meteo's words ("light rain" -> "rain"; "clear" at night -> "moon")."""
    c = str(conditions or "").lower()
    if "thunder" in c:
        return "storm"
    if "snow" in c:
        return "snow"
    if any(w in c for w in ("rain", "drizzle", "shower")):
        return "rain"
    if "fog" in c:
        return "fog"
    if windy:
        return "wind"
    if c in ("clear", "mainly clear"):
        return "moon"
    if "partly" in c:
        return "partly"
    if "cloud" in c:
        return "cloud"
    return "sun"


def _stroke(s: pygame.Surface, color: art.Color, pts: list[tuple[float, float]], width: float) -> None:
    """A thick rounded line through ``pts``."""
    for a, b in itertools.pairwise(pts):
        art.capsule(s, color, a, b, width)


def _blob(s: pygame.Surface, color: art.Color, circles: list[tuple[float, float, float]],
          base: tuple[float, float, float, float], grow: float = 0.0) -> None:
    for x, y, r in circles:
        pygame.draw.circle(s, color, (x, y), r + grow)
    bx, by, bw, bh = base
    pygame.draw.rect(s, color, pygame.Rect(bx - grow, by - grow, bw + 2 * grow, bh + 2 * grow),
                     border_radius=int(bh / 2 + grow))


def _cloud(s: pygame.Surface, cx: float, cy: float, w: float, fill: art.Color = CLOUD) -> None:
    """A puffy cloud ``w`` wide, centred at (cx, cy)."""
    h = w * 0.36
    circles = [(cx - w * 0.2, cy - h * 0.1, w * 0.2), (cx + w * 0.06, cy - h * 0.42, w * 0.26),
               (cx + w * 0.3, cy - h * 0.02, w * 0.16)]
    base = (cx - w * 0.42, cy - h * 0.15, w * 0.84, h * 0.75)
    lw = max(2.0, w * 0.045)
    _blob(s, art.inkier(fill, 0.32), circles, base, lw)
    _blob(s, fill, circles, base)
    shade = art.mix(fill, art.INK, 0.07)  # a soft shade along the bottom
    pygame.draw.rect(s, shade, pygame.Rect(cx - w * 0.36, cy + h * 0.36, w * 0.72, h * 0.18),
                     border_radius=int(h * 0.09))
    pygame.draw.ellipse(s, art.WHITE, pygame.Rect(cx - w * 0.04, cy - h * 0.95, w * 0.2, h * 0.24))


def _sun(s: pygame.Surface, cx: float, cy: float, r: float, *, face: bool) -> None:
    for i in range(8):
        a = i * math.pi / 4
        p0 = (cx + math.cos(a) * r * 1.3, cy + math.sin(a) * r * 1.3)
        p1 = (cx + math.cos(a) * r * 1.62, cy + math.sin(a) * r * 1.62)
        art.capsule(s, art.inkier(RAYS, 0.25), p0, p1, r * 0.3)
        art.capsule(s, RAYS, p0, p1, r * 0.19)
    pygame.draw.circle(s, art.inkier(SUN, 0.35), (cx, cy), r)
    pygame.draw.circle(s, SUN, (cx, cy), r * 0.88)
    pygame.draw.ellipse(s, art.lighten(SUN, 0.62), pygame.Rect(cx - r * 0.55, cy - r * 0.6, r * 0.42, r * 0.26))
    if face:
        art.cute_face(s, cx, cy + r * 0.08, r * 1.5)


def _moon(s: pygame.Surface, cx: float, cy: float, r: float) -> None:
    layer = pygame.Surface(s.get_size(), pygame.SRCALPHA)
    pygame.draw.circle(layer, art.inkier(MOON, 0.35), (cx, cy), r)
    pygame.draw.circle(layer, MOON, (cx, cy), r * 0.88)
    pygame.draw.circle(layer, (0, 0, 0, 0), (cx + r * 0.55, cy - r * 0.35), r * 0.8)  # the bite
    s.blit(layer, (0, 0))
    for (dx, dy, k) in ((0.9, -0.75, 0.22), (1.15, 0.2, 0.15), (0.35, 0.95, 0.12)):
        pts = art.star_points(cx + dx * r, cy + dy * r, r * k * 1.4, r * k * 0.6)
        pygame.draw.polygon(s, art.LEMON, pts)


def _drop(s: pygame.Surface, x: float, y: float, r: float) -> None:
    for color, k in ((art.inkier(DROP, 0.35), 1.0), (DROP, 0.78)):
        rr = r * k
        pygame.draw.circle(s, color, (x, y), rr)
        pygame.draw.polygon(s, color, [(x - rr * 0.92, y - rr * 0.35), (x, y - rr * 2.3 + (1 - k) * r * 0.5),
                                       (x + rr * 0.92, y - rr * 0.35)])
    pygame.draw.circle(s, art.lighten(DROP, 0.6), (x - r * 0.3, y - r * 0.1), r * 0.2)


def _flake(s: pygame.Surface, x: float, y: float, r: float) -> None:
    for color, w in ((art.inkier(art.SKY, 0.3), r * 0.42), (art.WHITE, r * 0.22)):
        for i in range(3):
            a = i * math.pi / 3 + math.pi / 2
            art.capsule(s, color, (x - math.cos(a) * r, y - math.sin(a) * r), (x + math.cos(a) * r, y + math.sin(a) * r), w)


def _bolt(s: pygame.Surface, x: float, y: float, h: float) -> None:
    pts = [(x + h * 0.1, y), (x - h * 0.28, y + h * 0.55), (x + h * 0.02, y + h * 0.55), (x - h * 0.12, y + h),
           (x + h * 0.34, y + h * 0.4), (x + h * 0.04, y + h * 0.4), (x + h * 0.24, y)]
    pygame.draw.polygon(s, art.inkier(art.LEMON, 0.4), pts)
    pygame.draw.lines(s, art.inkier(art.LEMON, 0.4), True, pts, max(2, int(h * 0.1)))
    inner = [(px * 0.8 + x * 0.2 + h * 0.01, py * 0.84 + (y + h * 0.5) * 0.16) for px, py in pts]
    pygame.draw.polygon(s, art.LEMON, inner)


def _gust(s: pygame.Surface, x0: float, x1: float, y: float, curl: float, w: float) -> None:
    """A wind line from x0 to x1 that curls up at the end (radius ``curl``)."""
    pts = [(x0, y), (x1, y)]
    for i in range(1, 19):
        a = i * 1.4 * math.pi / 18  # round from the bottom of the curl, up and back over the line
        pts.append((x1 + curl * math.sin(a), y - curl + curl * math.cos(a)))
    _stroke(s, art.inkier(WIND, 0.3), pts, w * 1.6)
    _stroke(s, WIND, pts, w)


def weather_icon(kind: str, size: int) -> pygame.Surface:
    """A drawn weather icon (sun, moon, partly, cloud, rain, snow, storm, fog, wind), ``size`` px square."""
    size = max(16, int(size))

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            face = size >= 120
            if kind == "sun":
                _sun(s, n * 0.5, n * 0.5, n * 0.28, face=face)
            elif kind == "moon":
                _moon(s, n * 0.45, n * 0.52, n * 0.3)
            elif kind == "partly":
                _sun(s, n * 0.38, n * 0.38, n * 0.2, face=False)
                _cloud(s, n * 0.6, n * 0.7, n * 0.66)
            elif kind == "cloud":
                _cloud(s, n * 0.64, n * 0.38, n * 0.5, art.mix(CLOUD, CLOUD_DARK, 0.45))
                _cloud(s, n * 0.47, n * 0.6, n * 0.84)
            elif kind == "rain":
                _cloud(s, n * 0.5, n * 0.42, n * 0.84)
                for i, dx in enumerate((-0.22, 0.0, 0.22)):
                    _drop(s, n * (0.5 + dx), n * (0.84 + (0.05 if i == 1 else 0)), n * 0.075)
            elif kind == "snow":
                _cloud(s, n * 0.5, n * 0.38, n * 0.84)
                for i, dx in enumerate((-0.26, 0.0, 0.26)):
                    _flake(s, n * (0.5 + dx), n * (0.8 + (0.07 if i == 1 else 0)), n * 0.1)
            elif kind == "storm":
                _cloud(s, n * 0.5, n * 0.4, n * 0.86, CLOUD_DARK)
                _bolt(s, n * 0.44, n * 0.54, n * 0.42)
            elif kind == "fog":
                _cloud(s, n * 0.5, n * 0.36, n * 0.72)
                for i, (a, b) in enumerate(((0.16, 0.74), (0.28, 0.86), (0.12, 0.66))):
                    y = n * (0.66 + i * 0.12)
                    art.capsule(s, art.inkier(FOG, 0.2), (n * a, y), (n * b, y), n * 0.075)
                    art.capsule(s, FOG, (n * a, y), (n * b, y), n * 0.045)
            else:  # wind
                _gust(s, n * 0.1, n * 0.6, n * 0.42, n * 0.13, n * 0.055)
                _gust(s, n * 0.2, n * 0.74, n * 0.62, n * 0.11, n * 0.055)
                _gust(s, n * 0.06, n * 0.42, n * 0.8, n * 0.08, n * 0.05)

        return art.supersampled((size, size), draw)

    return art.cached(("wx_icon", kind, size), make)


def _arrow(size: int, right: bool) -> pygame.Surface:
    """A soft chevron hinting that a tap there turns the day."""
    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            pts = [(n * 0.3, n * 0.15), (n * 0.7, n * 0.5), (n * 0.3, n * 0.85)]
            if not right:
                pts = [(n - x, y) for x, y in pts]
            _stroke(s, art.INK_SOFT, pts, n * 0.2)

        return art.supersampled((size, size), draw)

    return art.cached(("wx_arrow", size, right), make)


def _drop_icon(size: int) -> pygame.Surface:
    """A small rain drop beside a chance of rain."""
    def make() -> pygame.Surface:
        return art.supersampled((size, size), lambda s, k: _drop(s, size * k * 0.5, size * k * 0.66, size * k * 0.3))

    return art.cached(("wx_drop", size), make)


# ---------------------------------------------------------------- the card
def _num(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _short_place(label: str) -> str:
    return str(label or "").split(",")[0].strip() or "here"


def _rain_kind(conditions: str) -> str:
    c = str(conditions or "").lower()
    return "storms" if "thunder" in c else "snow" if "snow" in c else "rain"


def rain_words(chance: int | None, conditions: str) -> str:
    """ "rain likely" / "snow possible" / "a small chance of rain" / "" (dry or unknown)."""
    if chance is None or chance < 20:
        return ""
    kind = _rain_kind(conditions)
    if chance >= 70:
        return f"{kind} likely"
    if chance >= 40:
        return f"{kind} possible"
    return f"a small chance of {kind}"


def _wet(conditions: str) -> bool:
    return icon_kind(conditions) in ("rain", "snow", "storm")


def parse_day(value: Any, forecast: list[dict[str, Any]]) -> int | None:
    """A day index from 0 / "2" / "today" / "tomorrow" / "Saturday" / "the day after tomorrow"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        i = int(value)
    else:
        s = " ".join(str(value or "").lower().split())
        if not s:
            return None
        if re.fullmatch(r"[+-]?\d+", s):
            i = int(s)
        elif "after tomorrow" in s:
            i = 2
        elif "tomorrow" in s:
            i = 1
        elif s in ("today", "now", "tonight", "this evening", "this afternoon", "this morning"):
            i = 0
        else:
            day = next((d for d in WEEKDAYS if d in s or (len(s) >= 3 and d.startswith(s))), None)
            if day is None:
                return None
            matches = [n for n, f in enumerate(forecast) if str(f.get("date", "")).lower().startswith(day)]
            if not matches:
                return None
            i = matches[0]
            if i == 0 and ("next" in s) and len(matches) > 1:
                i = matches[1]
    return max(0, min(len(forecast) - 1, i)) if forecast else None


class WeatherCard(Card):
    """The forecast for one place, on one day, in one of four views."""

    kind = "weather"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None, *, report: dict[str, Any],
                 fetch: Any = None, loop: asyncio.AbstractEventLoop | None = None, twelve: bool = True) -> None:
        super().__init__(spec, host)
        self._fetch = fetch  # async (place) -> report, for "place"
        self._loop = loop
        self._pending: Any = None  # a concurrent Future while another place is fetched
        self.twelve = twelve
        self.view = "now"
        self.day = 0
        self._slide = 0.0
        self.set_report(report)
        day = parse_day(spec.get("day"), self.forecast)
        view = str(spec.get("view") or "").lower()
        if day is not None:
            self.day = day
        self.view = view if view in VIEWS else ("day" if day else "now")
        self.title = self._title()
        self.mood = "happy" if self.view == "now" and icon_kind(self.now.get("conditions", "")) == "sun" else "neutral"

    # ------------------------------------------------------------ data
    def set_report(self, report: dict[str, Any]) -> None:
        self.report = report
        self.place = _short_place(report.get("place", ""))
        units = report.get("units") or {}
        self.deg = str(units.get("temperature") or "°")
        self.wind_unit = str(units.get("wind") or "km/h")
        self.now = report.get("now") or {}
        self.forecast = [d for d in report.get("forecast") or [] if isinstance(d, dict)][:7]
        self.day = min(self.day, max(0, len(self.forecast) - 1))
        self.title = self._title()

    def _day(self, i: int | None = None) -> dict[str, Any]:
        i = self.day if i is None else i
        return self.forecast[i] if 0 <= i < len(self.forecast) else {}

    def day_name(self, i: int | None = None, *, cap: bool = True) -> str:
        i = self.day if i is None else i
        name = "today" if i == 0 else "tomorrow" if i == 1 else str(self._day(i).get("date", "")).split(" ")[0]
        name = name or f"in {i} days"
        return name[:1].upper() + name[1:] if cap else name

    def _windy(self, speed: Any) -> bool:
        n = _num(speed)
        return n is not None and n >= WINDY.get(self.wind_unit, 40)

    def _icon_for_day(self, d: dict[str, Any]) -> str:
        return icon_kind(d.get("conditions", ""), windy=self._windy(d.get("wind_max")))

    def _title(self) -> str:
        if self.view == "now":
            return f"Now in {self.place}"
        if self.view == "week":
            return f"The week in {self.place}"
        if self.view == "hours":
            return "The next hours" if self.day == 0 else f"{self.day_name()}, hour by hour"
        return f"{self.day_name()} in {self.place}"

    # ------------------------------------------------------------ steering
    def set_day(self, i: int, *, view: str | None = None) -> bool:
        if not self.forecast:
            return False
        i = max(0, min(len(self.forecast) - 1, i))
        new_view = view or ("day" if self.view in ("now", "week") else self.view)
        if i == self.day and new_view == self.view:
            return True
        if new_view == self.view and i != self.day:
            self._slide = SLIDE_PX * (1 if i > self.day else -1)
        self.day, self.view = i, new_view
        self.title = self._title()
        return True

    def set_view(self, view: str) -> bool:
        view = str(view or "").lower().strip()
        view = {"today": "day", "forecast": "day", "hourly": "hours", "hour": "hours", "current": "now",
                "days": "week", "7 days": "week"}.get(view, view)
        if view not in VIEWS:
            return False
        if view == "now":
            self.day = 0
        self.view = view
        self.title = self._title()
        self._slide = 0.0
        return True

    def command(self, action: str, args: dict[str, Any]) -> bool:
        args = args if isinstance(args, dict) else {}
        if action == "day":
            for key in ("offset", "weekday", "day"):
                if key in args:
                    i = parse_day(args[key], self.forecast)
                    if i is not None:
                        return self.set_day(i, view=args.get("view") if args.get("view") in VIEWS else None)
            return False
        if action == "view":
            ok = self.set_view(str(args.get("view") or args.get("name") or ""))
            if ok and "day" in args:
                i = parse_day(args["day"], self.forecast)
                if i is not None:
                    self.day = i
                    self.title = self._title()
            return ok
        if action == "place":
            return self._change_place(str(args.get("name") or args.get("place") or ""))
        return False

    def _change_place(self, name: str) -> bool:
        name = " ".join(name.split())[:100]
        if self._fetch is None or self._loop is None or not self._loop.is_running():
            return False
        self._pending = asyncio.run_coroutine_threadsafe(self._fetch(name or None), self._loop)
        return True

    def tap(self, x: float, y: float) -> bool:
        if self.view == "week" and self.forecast:
            i = self._tile_at(x, y)
            if i is not None:
                return self.set_day(i, view="day")
        if self.view == "now":
            return self.set_day(1, view="day") if x >= W / 2 else False
        return self.set_day(self.day + (1 if x >= W / 2 else -1))

    def key(self, name: str) -> bool:
        if name in ("right", "left"):
            return self.tap(W if name == "right" else 0, 300)
        if name in ("up", "down"):
            i = VIEWS.index(self.view)
            return self.set_view(VIEWS[(i + (1 if name == "down" else -1)) % len(VIEWS)])
        return False

    # ------------------------------------------------------------ the voice
    def _range(self, d: dict[str, Any]) -> str:
        lo, hi = _num(d.get("low")), _num(d.get("high"))
        if lo is None or hi is None:
            return ""
        return f"{lo} to {hi} {self.deg}"

    def describe(self) -> str:
        if self.view == "now":
            parts = [f"Now in {self.place}:"]
            t = _num(self.now.get("temperature"))
            if t is not None:
                parts.append(f" {t} {self.deg} and {self.now.get('conditions', 'unknown')}")
                fl = _num(self.now.get("feels_like"))
                if fl is not None and fl != t:
                    parts.append(f", feels like {fl} {self.deg}")
            else:
                parts.append(f" {self.now.get('conditions', 'unknown')}")
            return "".join(parts)
        if self.view == "week":
            lows = [x for x in (_num(d.get("low")) for d in self.forecast) if x is not None]
            highs = [x for x in (_num(d.get("high")) for d in self.forecast) if x is not None]
            out = f"The next {len(self.forecast)} days in {self.place}"
            if lows and highs:
                out += f": {min(lows)} to {max(highs)} {self.deg}"
            wet = [self.day_name(i, cap=False) for i, d in enumerate(self.forecast)
                   if (_num(d.get("chance_of_rain_or_snow_percent")) or 0) >= 50]
            if wet:
                out += f", wet {', '.join(wet[:-1]) + ' and ' + wet[-1] if len(wet) > 1 else wet[0]}"
            return out
        d = self._day()
        if self.view == "hours":
            hours = self._hours()
            temps = [h["temperature"] for h in hours if _num(h.get("temperature")) is not None]
            out = f"{self.day_name()} in {self.place} hour by hour"
            if temps:
                out += f": {min(temps)} to {max(temps)} {self.deg}"
            wet = [h for h in hours if (_num(h.get("chance_of_rain_or_snow_percent")) or 0) >= 50]
            if wet:
                out += f", {_rain_kind(wet[0].get('conditions', ''))} likely around " \
                       f"{candy.hour_label(wet[0].get('time', ''), self.twelve)}"
            return out
        parts = [self._range(d)]
        words = rain_words(_num(d.get("chance_of_rain_or_snow_percent")), d.get("conditions", ""))
        if not (words and _wet(d.get("conditions", "")) and not words.startswith("a small")):
            parts.append(str(d.get("conditions") or ""))
        parts.append(words)
        return f"{self.day_name()} in {self.place}: " + ", ".join(p for p in parts if p)

    def status(self) -> dict[str, Any]:
        return {**super().status(), "view": self.view, "day": self.day, "place": self.place}

    # ------------------------------------------------------------ frames
    def update(self, dt: float) -> None:
        super().update(dt)
        self._slide = art.approach(self._slide, 0.0, 12.0, dt)
        if abs(self._slide) < 0.5:
            self._slide = 0.0
        if self._pending is not None and self._pending.done():
            fut, self._pending = self._pending, None
            try:
                report = fut.result()
            except Exception as e:  # noqa: BLE001 - WeatherError (or worse): the voice says so
                self.host.tell(f"The weather card couldn't change place: {e}")
                return
            if report:
                self.set_report(report)
                self.title = self._title()

    def _hours(self) -> list[dict[str, Any]]:
        return [h for h in self._day().get("hours") or [] if isinstance(h, dict)][:6]

    def draw(self, surf: pygame.Surface) -> None:
        surf.blit(art.background((W, 480), seed=11), (0, 0))
        dy = candy.slide_in(self.t)
        candy.header(surf, self.title, dy=dy * 0.5)
        box = candy.panel(surf, dy=dy)
        clip = surf.get_clip()
        surf.set_clip(box.inflate(-8, -8))
        ox = int(self._slide)
        if self.view == "now":
            self._draw_now(surf, box, ox)
        elif self.view == "week":
            self._draw_week(surf, box)
        elif self.view == "hours":
            self._draw_hours(surf, box, ox)
        else:
            self._draw_day(surf, box, ox)
        surf.set_clip(clip)
        if self.view in ("day", "hours"):
            if self.day > 0:
                candy.blit_mid(surf, _arrow(40, False), (box.left + 24, box.top + 110))
            if self.day < len(self.forecast) - 1:
                candy.blit_mid(surf, _arrow(40, True), (box.right - 24, box.top + 110))

    def _bob(self) -> int:
        return round(math.sin(self.t * 1.8) * 4)

    def _draw_now(self, surf: pygame.Surface, box: pygame.Rect, ox: int) -> None:
        now, today = self.now, self._day(0)
        kind = icon_kind(now.get("conditions", ""), windy=self._windy(now.get("wind")))
        candy.blit_mid(surf, weather_icon(kind, 200), (box.left + 150 + ox, box.top + 130 + self._bob()))
        cx = box.left + 470 + ox
        candy.blit_mid(surf, candy.degrees(_num(now.get("temperature")), 150), (cx, box.top + 92))
        cond = str(now.get("conditions") or "")
        candy.blit_mid(surf, candy.fit(cond[:1].upper() + cond[1:], TITLE_PX, 380), (cx, box.top + 180))
        fl = _num(now.get("feels_like"))
        if fl is not None:
            candy.blit_mid(surf, art.text(f"Feels like {fl}°", BODY_PX, art.INK), (cx, box.top + 228))
        chips = []
        if _num(today.get("high")) is not None:
            chips.append(candy.chip(f"High {today['high']}°", BODY_PX, art.INK, art.lighten(art.STRAWBERRY, 0.72)))
        if _num(today.get("low")) is not None:
            chips.append(candy.chip(f"Low {today['low']}°", BODY_PX, art.INK, art.lighten(art.SKY, 0.7)))
        chance = _num(today.get("chance_of_rain_or_snow_percent"))
        if chance is not None:
            chips.append(candy.chip(f"Rain {chance}%", BODY_PX, art.INK, art.lighten(art.PERIWINKLE, 0.72)))
        self._row(surf, chips, box.centerx + ox, box.bottom - 52)

    def _row(self, surf: pygame.Surface, items: list[pygame.Surface], cx: float, cy: float, gap: int = 16) -> None:
        total = sum(i.get_width() for i in items) + gap * max(0, len(items) - 1)
        x = cx - total / 2
        for img in items:
            surf.blit(img, img.get_rect(midleft=(int(x), int(cy))))
            x += img.get_width() + gap

    def _draw_day(self, surf: pygame.Surface, box: pygame.Rect, ox: int) -> None:
        d = self._day()
        candy.blit_mid(surf, weather_icon(self._icon_for_day(d), 150), (box.left + 140 + ox, box.top + 104 + self._bob()))
        cx = box.left + 440 + ox
        hi, lo = _num(d.get("high")), _num(d.get("low"))
        big = candy.degrees(hi, 112)
        small = candy.degrees(lo, 64, LOW_INK) if lo is not None else None
        slash = art.text("/", 64, LOW_INK)
        w = big.get_width() + (18 + slash.get_width() + 12 + small.get_width() if small else 0)
        x0 = int(cx - w / 2)
        r = surf.blit(big, big.get_rect(midleft=(x0, box.top + 78)))
        if small is not None:
            base = r.bottom - int(112 * 0.16)  # sit the small numbers on the big one's baseline
            rs = surf.blit(slash, slash.get_rect(bottomleft=(r.right + 18, base + 4)))
            surf.blit(small, small.get_rect(bottomleft=(rs.right + 12, base + 6)))
        cond = str(d.get("conditions") or "")
        items = [candy.fit(cond[:1].upper() + cond[1:], 40, 290)]
        chance = _num(d.get("chance_of_rain_or_snow_percent"))
        if chance is not None:
            items.append(candy.chip(f"Rain {chance}%", BODY_PX, art.INK, art.lighten(art.PERIWINKLE, 0.72)))
        self._row(surf, items, cx, box.top + 168)
        hours = self._hours()
        if self.day > 0 and len(hours) > 4:
            hours = hours[1:5]  # morning to evening
        self._hour_tiles(surf, hours[:4], box, box.top + 278, tile_w=150, icon=56, ox=ox, rain=False)

    def _hour_tiles(self, surf: pygame.Surface, hours: list[dict[str, Any]], box: pygame.Rect, cy: int, *,
                    tile_w: int, icon: int, ox: int, rain: bool) -> None:
        if not hours:
            return
        gap = 10
        tile_h = 150 if not rain else 290
        total = len(hours) * tile_w + (len(hours) - 1) * gap
        x = box.centerx - total / 2 + ox
        tile = art.rounded_rect(tile_w, tile_h, 22, art.WHITE, shadow=4, border=(250, 222, 236), border_w=3)
        for h in hours:
            r = pygame.Rect(int(x), int(cy - tile_h / 2), tile_w, tile_h)
            surf.blit(tile, r.topleft)
            label = candy.hour_label(h.get("time", ""), self.twelve)
            if rain:
                candy.blit_mid(surf, art.text(label, BODY_PX, art.INK), (r.centerx, r.top + 34))
                candy.blit_mid(surf, weather_icon(icon_kind(h.get("conditions", "")), icon), (r.centerx, r.top + 108))
                candy.blit_mid(surf, candy.degrees(_num(h.get("temperature")), 48), (r.centerx, r.top + 186))
                chance = _num(h.get("chance_of_rain_or_snow_percent"))
                if chance is not None:
                    pct = art.text(f"{chance}%", BODY_PX, art.inkier(art.SKY, 0.55) if chance >= 40 else LOW_INK)
                    drop = _drop_icon(22)
                    x1 = r.centerx - (drop.get_width() + 4 + pct.get_width()) // 2
                    surf.blit(drop, drop.get_rect(midleft=(x1, r.top + 244)))
                    surf.blit(pct, pct.get_rect(midleft=(x1 + drop.get_width() + 4, r.top + 246)))
            else:
                candy.blit_mid(surf, art.text(label, SMALL_PX, art.INK), (r.centerx, r.top + 22))
                candy.blit_mid(surf, weather_icon(icon_kind(h.get("conditions", "")), icon), (r.centerx, r.top + 72))
                candy.blit_mid(surf, candy.degrees(_num(h.get("temperature")), 38), (r.centerx, r.top + 124))
            x += tile_w + gap

    def _draw_hours(self, surf: pygame.Surface, box: pygame.Rect, ox: int) -> None:
        hours = self._hours()
        if not hours:
            candy.blit_mid(surf, art.text("No hours to show", BODY_PX, art.INK), box.center)
            return
        tile_w = min(150, (box.w - 90 - 10 * (len(hours) - 1)) // len(hours))
        self._hour_tiles(surf, hours, box, box.centery, tile_w=tile_w, icon=min(76, tile_w - 20), ox=ox, rain=True)

    def _tiles(self) -> tuple[int, int, int]:
        n = max(1, len(self.forecast))
        gap = 8
        tile_w = min(110, (candy.PANEL.w - 24 - gap * (n - 1)) // n)
        x0 = candy.PANEL.centerx - (n * tile_w + (n - 1) * gap) // 2
        return x0, tile_w, gap

    def _tile_at(self, x: float, y: float) -> int | None:
        x0, tw, gap = self._tiles()
        if not (candy.PANEL.top <= y <= candy.PANEL.bottom):
            return None
        i = int((x - x0) // (tw + gap))
        return i if 0 <= i < len(self.forecast) else None

    def _draw_week(self, surf: pygame.Surface, box: pygame.Rect) -> None:
        x0, tw, gap = self._tiles()
        th = box.h - 36
        for i, d in enumerate(self.forecast):
            age = self.t - 0.06 * i  # the tiles pop in one after another
            k = art.ease_out_back(age / 0.3, 1.6) if age < 0.3 else 1.0
            if age <= 0:
                continue
            chosen = i == self.day
            fill = art.lighten(art.LEMON, 0.7) if chosen else art.WHITE
            edge = art.inkier(art.LEMON, 0.2) if chosen else (250, 222, 236)
            tile = art.rounded_rect(tw, th, 22, fill, shadow=4, border=edge, border_w=4 if chosen else 3)
            r = pygame.Rect(x0 + i * (tw + gap), box.top + 16, tw, th)
            if k < 0.99:
                art.blit_center(surf, art.scaled(tile, k), r.center)
                continue
            surf.blit(tile, r.topleft)
            name = "Today" if i == 0 else str(d.get("date", ""))[:3]
            candy.blit_mid(surf, candy.fit(name, BODY_PX, tw - 8), (r.centerx, r.top + 34))
            candy.blit_mid(surf, weather_icon(self._icon_for_day(d), min(84, tw - 12)), (r.centerx, r.top + 108))
            candy.blit_mid(surf, candy.degrees(_num(d.get("high")), 44), (r.centerx, r.top + 180))
            candy.blit_mid(surf, candy.degrees(_num(d.get("low")), BODY_PX, LOW_INK), (r.centerx, r.top + 222))
            chance = _num(d.get("chance_of_rain_or_snow_percent"))
            if chance is not None and chance >= 30:
                candy.blit_mid(surf, art.pill(f"{chance}%", SMALL_PX, art.WHITE, art.inkier(art.SKY, 0.25),
                                              pad=(8, 4)), (r.centerx, r.top + 276))


# ---------------------------------------------------------------- building
async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> WeatherCard | None:
    """Fetch the forecast (home, or ``spec["place"]``) and make the card; None without one (offline, no place)."""
    report = spec.get("report")  # a ready forecast (previews, SAMPLES): nothing fetched
    fetch = None
    if not isinstance(report, dict):
        if ctx is None:
            return None
        from littlebot.info.weather import WeatherError, get_weather

        svc = get_weather(ctx)

        async def fetch(place: str | None) -> dict[str, Any]:
            return await svc.outlook(place)

        try:
            report = await fetch(" ".join(str(spec.get("place") or "").split())[:100] or None)
        except WeatherError as e:
            log.info("screens: no weather card (%s)", e.kind)
            return None
    if not report.get("forecast") and not report.get("now"):
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    return WeatherCard(spec, host, report=report, fetch=fetch, loop=loop,
                       twelve=candy.uses_12h(ctx) if ctx is not None else True)


register(CardType(
    kind="weather",
    description="The weather on screen while you talk about it: now (big icon, temperature, feels-like, today's "
                "high and low, chance of rain), one day's forecast (today, tomorrow or a weekday up to 6 days ahead, "
                "with a few hours across it), the next 7 days, or hour by hour. Use it for EVERY weather question. "
                "Leave out place for the weather at home.",
    fields={
        "place": {"type": "string", "description": "A city or place, only if not home: 'Paris', 'Springfield, "
                                                   "Illinois'."},
        "view": {"type": "string", "enum": list(VIEWS),
                 "description": "now: the weather right now; day: one day's forecast (give day); week: the next 7 "
                                "days; hours: hour by hour (today, or the day given)."},
        "day": {"type": "string", "description": "For view day or hours: 'today', 'tomorrow', a weekday such as "
                                                 "'Saturday', or the number of days ahead ('2')."},
    },
    build=build,
    required=(),
    actions={
        "day": "show another day: {\"offset\": 1} (days ahead, 0 today, up to 6) or {\"weekday\": \"Saturday\"}",
        "view": "switch view: {\"view\": \"now\" | \"day\" | \"week\" | \"hours\"}",
        "place": "the weather somewhere else: {\"name\": \"Paris\"} (empty name: home again)",
    },
))


def _sample_report() -> dict[str, Any]:
    days = [("today", "Saturday 24 October", "partly cloudy", 13, 5, 10), ("tomorrow", "Sunday 25 October",
            "light rain", 11, 4, 85), ("Monday", "Monday 26 October", "sunny", 10, 2, 0),
            ("Tuesday", "Tuesday 27 October", "light snow", 1, -3, 60),
            ("Wednesday", "Wednesday 28 October", "thunderstorms", 20, 12, 90),
            ("Thursday", "Thursday 29 October", "fog", 9, 4, 20), ("Friday", "Friday 30 October", "cloudy", 15, 8, 10)]
    hours_today = [("15:00", 12, "partly cloudy", 0), ("18:00", 11, "cloudy", 10), ("21:00", 9, "light rain", 60),
                   ("00:00", 8, "clear", 0), ("03:00", 7, "fog", 10), ("06:00", 6, "cloudy", 20)]
    hours_rainy = [("06:00", 4, "cloudy", 20), ("09:00", 6, "light rain", 70), ("12:00", 9, "rain", 90),
                   ("15:00", 11, "light rain", 80), ("18:00", 8, "cloudy", 30), ("21:00", 6, "mainly clear", 10)]
    def hours(rows: list[tuple[str, int, str, int]], cond: str | None = None) -> list[dict[str, Any]]:
        return [{"time": t, "temperature": tt, "conditions": cond or c, "chance_of_rain_or_snow_percent": p}
                for t, tt, c, p in rows]

    forecast = []
    for i, (day, date, cond, hi, lo, chance) in enumerate(days):
        rows = hours(hours_today) if i == 0 else hours(hours_rainy) if i == 1 else hours(hours_rainy, cond)
        forecast.append({"day": day, "date": date, "conditions": cond, "high": hi, "low": lo,
                         "chance_of_rain_or_snow_percent": chance, "wind_max": 18, "hours": rows})
    return {"place": "Toronto, Ontario, Canada", "local_time": "14:30", "units": {"temperature": "°C", "wind": "km/h"},
            "now": {"temperature": 12, "feels_like": 9, "conditions": "partly cloudy", "wind": 12},
            "forecast": forecast, "source": "Open-Meteo"}


SAMPLE_REPORT = _sample_report()
# Example specs for scripts/screen_preview.py ("report": a ready forecast, so previews need no network).
SAMPLES: list[dict[str, Any]] = [
    {"card": "weather", "view": "now", "report": SAMPLE_REPORT},
    {"card": "weather", "view": "day", "day": "tomorrow", "report": SAMPLE_REPORT},
    {"card": "weather", "view": "week", "report": SAMPLE_REPORT},
    {"card": "weather", "view": "hours", "day": "tomorrow", "report": SAMPLE_REPORT},
]

__all__ = ["SAMPLES", "WeatherCard", "icon_kind", "parse_day", "rain_words", "weather_icon"]
