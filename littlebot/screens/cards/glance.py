"""The glance card: the day at a glance, like a smart display's home screen.

The time (big) and the weather now (its icon, the temperature, today's high and low) on the left; the date as the
title; on the right what's next, up to four lines of it: the timers running, the next events, the next reminder or
alarm, the lists with something on them. Shown when they ask "what's my day?" / "show me the time and
weather", from the dashboard, or by a double tap on the face (littlebot/face/display.py). The clock ticks; the rest
is read when the card is made (the weather from the cached forecast: it never waits long).
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, MAX_LINES, TITLE_PX, Card, CardHost, CardType, register
from littlebot.screens.cards import _kit as kit

log = logging.getLogger("littlebot.screens.glance")

TIME_PX = 168  # the clock, big
AMPM_PX = TITLE_PX
TEMP_PX = 72
WEATHER_PX = BODY_PX
ICON = 116
WEATHER_WAIT_S = 1.5
MAX_UPCOMING = 4  # what's next, each on up to two lines...
UPCOMING_LINES = MAX_LINES - 3  # ...and this many lines in all (the weather has two, "Coming up" one)
INNER = kit.PANEL.inflate(-2 * kit.PAD, -2 * kit.PAD)  # 940 x 402
LEFT = pygame.Rect(INNER.x, INNER.y, 420, INNER.h)  # the time and the weather
RIGHT = pygame.Rect(LEFT.right + 2 * kit.GUTTER, INNER.y, INNER.right - LEFT.right - 2 * kit.GUTTER, INNER.h)
DOT_R = 10
ITEM_INDENT = 34


def dashes(h: int, color: art.Color = art.mix(kit.PANEL_BORDER, art.CREAM, 0.6)) -> pygame.Surface:
    """A soft dashed vertical line, ``h`` px tall (a sprite: a frame only blits it)."""
    key = ("glance_dashes", h, color)

    def make() -> pygame.Surface:
        out = pygame.Surface((2, h), pygame.SRCALPHA)
        for y in range(0, h, 18):
            pygame.draw.line(out, color, (0, y), (0, min(h, y + 9)), 2)
        return out

    return art.cached(key, make)


class GlanceCard(Card):
    kind = "glance"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None, *, weather: dict[str, Any] | None = None,
                 upcoming: list[str] | None = None, twelve: bool = True, tz: _dt.tzinfo | None = None) -> None:
        super().__init__(spec, host)
        self.title = "Today"
        self.mood = "happy"
        self.weather = weather or {}
        self.upcoming = [kit.clean(u, 90) for u in (upcoming or []) if kit.clean(u, 90)][:MAX_UPCOMING]
        self.twelve = twelve
        self.tz = tz
        self._items = self._lay_out_upcoming()

    def _now(self) -> _dt.datetime:
        return _dt.datetime.now(self.tz) if self.tz is not None else _dt.datetime.now()

    def _lay_out_upcoming(self) -> list[tuple[str, ...]]:
        """Each line of what's next wrapped to the right column (two lines at most), as many as fit."""
        out: list[tuple[str, ...]] = []
        left = UPCOMING_LINES
        width = RIGHT.w - ITEM_INDENT
        for line in self.upcoming:
            if left <= 0:
                break
            wrapped = kit.wrap(line, BODY_PX, width)
            most = min(2, left)
            if len(wrapped) > most:
                wrapped = (*wrapped[:most - 1], kit.ellipsize(" ".join(wrapped[most - 1:]), BODY_PX, width))
            out.append(tuple(wrapped))
            left -= len(wrapped)
        return out

    def draw(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=41)
        now = self._now()
        kit.header_title(surf, f"{now:%A}, {now:%B} {now.day}")
        kit.panel(surf)
        weather = self._weather_sprite()
        big = art.text(f"{now.hour % 12 or 12 if self.twelve else now.hour}:{now:%M}", TIME_PX, art.INK)
        ampm = art.text("AM" if now.hour < 12 else "PM", AMPM_PX, kit.TEXT_SOFT) if self.twelve else None
        clock_w = big.get_width() + (ampm.get_width() + 12 if ampm is not None else 0)
        gap = 22
        total = big.get_height() + (gap + weather.get_height() if weather is not None else 0)
        top = LEFT.y + (LEFT.h - total) // 2
        x = LEFT.centerx - clock_w // 2
        surf.blit(big, (x, top))
        if ampm is not None:  # beside the time, on its baseline
            base = top + art.font(TIME_PX).get_ascent()
            surf.blit(ampm, (x + big.get_width() + 12, base - art.font(AMPM_PX).get_ascent()))
        if weather is not None:
            surf.blit(weather, weather.get_rect(midtop=(LEFT.centerx, top + big.get_height() + gap)))
        surf.blit(dashes(INNER.h - 16), (LEFT.right + kit.GUTTER - 1, INNER.y + 8))  # between the two halves
        self._draw_upcoming(surf)

    def _draw_upcoming(self, surf: pygame.Surface) -> None:
        head = art.text("Coming up", BODY_PX, kit.TEXT_SOFT)
        pitch = kit.line_h(BODY_PX)
        gap = 18
        blocks = [kit.text_block(list(lines), BODY_PX, kit.TEXT, pitch=pitch) for lines in self._items]
        if not blocks:
            blocks = [kit.text_block(["Nothing coming up."], BODY_PX, kit.TEXT_SOFT)]
        total = head.get_height() + gap + sum(b.get_height() for b in blocks) + gap * (len(blocks) - 1)
        y = RIGHT.y + max(0, (RIGHT.h - total) // 2)
        surf.blit(head, (RIGHT.x, y))
        y += head.get_height() + gap
        for i, block in enumerate(blocks):
            if self._items:
                dot = art.circle(DOT_R, kit.ACCENTS[i % len(kit.ACCENTS)])
                surf.blit(dot, dot.get_rect(center=(RIGHT.x + DOT_R + 2, y + art.font(BODY_PX).get_height() // 2)))
            surf.blit(block, (RIGHT.x + (ITEM_INDENT if self._items else 0), y))
            y += block.get_height() + gap

    def _weather_sprite(self) -> pygame.Surface | None:
        """The weather now as one cached sprite: the icon, the temperature, the conditions with today's high and
        low."""
        now = self.weather.get("now") or {}
        if not now:
            return None
        deg = (self.weather.get("units") or {}).get("temperature", "°")
        temp = now.get("temperature")
        conditions = kit.clean(now.get("conditions"), 40)
        today = (self.weather.get("forecast") or [{}])[0] if self.weather.get("forecast") else {}
        hi, lo = today.get("high"), today.get("low")
        line1 = f"{temp}{deg}" if temp is not None else conditions
        line2 = f"High {hi}°  Low {lo}°" if hi is not None and lo is not None else (conditions if temp is not None
                                                                                    else "")
        key = ("glance_weather", conditions, line1, line2)

        def make() -> pygame.Surface:
            from littlebot.screens.cards.weather import icon_kind, weather_icon  # noqa: PLC0415

            icon = weather_icon(icon_kind(conditions), ICON)
            width = LEFT.w - ICON - 18
            t1 = art.text(kit.ellipsize(line1, TEMP_PX, width), TEMP_PX, art.INK)
            t2 = art.text(kit.ellipsize(line2, WEATHER_PX, width), WEATHER_PX, kit.TEXT_SOFT) if line2 else None
            words_h = t1.get_height() + (6 + t2.get_height() if t2 is not None else 0)
            w = ICON + 18 + max(t1.get_width(), t2.get_width() if t2 is not None else 0)
            h = max(ICON, words_h)
            out = pygame.Surface((w, h), pygame.SRCALPHA)
            out.blit(icon, (0, (h - ICON) // 2))
            y = (h - words_h) // 2
            out.blit(t1, (ICON + 18, y))
            if t2 is not None:
                out.blit(t2, (ICON + 18, y + t1.get_height() + 6))
            return out

        return art.cached(key, make)

    def update(self, dt: float) -> None:
        super().update(dt)

    def tap(self, x: float, y: float) -> bool:
        self.done = True
        return True

    def describe(self) -> str:
        now = self._now()
        parts = [f"the time and date ({now:%A %H:%M})"]
        if self.weather.get("now"):
            parts.append("the weather now")
        if self.upcoming:
            parts.append("what's coming up: " + "; ".join(self.upcoming))
        return "today at a glance: " + ", ".join(parts)


def upcoming_lines(ctx: Any) -> list[str]:
    """What's next, from the skills (timers, the calendar, reminders, lists): short lines, most urgent first."""
    out: list[str] = []
    screens = getattr(ctx, "screens", None)
    try:
        timers = screens.timers_now() if screens is not None else ""
    except Exception:  # noqa: BLE001
        timers = ""
    if timers:
        out.append(timers)
    hub = getattr(ctx, "skills", None)
    for name in ("calendar", "reminders", "lists"):
        skill = hub.get(name) if hub is not None and hasattr(hub, "get") else None
        if skill is None:
            continue
        lines = getattr(skill, "glance_lines", None)
        try:
            got = lines() if callable(lines) else [s for s in str(skill.context_note() or "").split("\n") if s.strip()]
        except Exception:  # noqa: BLE001
            got = []
        out += [str(g) for g in got if str(g).strip()][:2]
    return out


async def _build(spec: dict[str, Any], ctx: Any, host: CardHost) -> Card | None:
    import asyncio  # noqa: PLC0415

    weather: dict[str, Any] = {}
    try:
        from littlebot.info.weather import get_weather  # noqa: PLC0415

        svc = get_weather(ctx)
        if svc.blocked_reason() is None:
            weather = await asyncio.wait_for(svc.report(days=1), WEATHER_WAIT_S)
    except Exception:  # noqa: BLE001 - the card without the weather
        log.debug("glance: no weather", exc_info=True)
    twelve = True
    try:
        from littlebot.screens.cards import candy  # noqa: PLC0415

        twelve = candy.uses_12h(ctx)
    except Exception:  # noqa: BLE001
        pass
    tz = None
    try:
        from littlebot.info.clock import zone  # noqa: PLC0415

        tz = zone(getattr(ctx.config.info, "timezone", "") or None)
    except Exception:  # noqa: BLE001
        tz = None
    return GlanceCard(spec, host, weather=weather, upcoming=upcoming_lines(ctx), twelve=twelve, tz=tz)


register(CardType(
    kind="glance",
    description=("The day at a glance: the time, the date, the weather now and today's high and low, and what's "
                 "coming up (reminders, alarms, timers, lists). For \"what's my day like\", \"show me the time and "
                 "weather\", \"what's coming up\"."),
    fields={},
    build=_build,
))
