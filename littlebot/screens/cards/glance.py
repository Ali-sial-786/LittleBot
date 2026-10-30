"""The glance card: the day at a glance, like a smart display's home screen.

The time (big), the date, the weather now with today's high and low, and what's next: the next reminder or alarm,
the timers running, the lists with something on them. Shown when they ask "what's my day?" / "show me the time and
weather", from the dashboard, or by a double tap on the face (littlebot/face/display.py). The clock ticks; the rest
is read when the card is made (the weather from the cached forecast: it never waits long).
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, SMALL_PX, Card, CardHost, CardType, register
from littlebot.screens.cards import _kit as kit

log = logging.getLogger("littlebot.screens.glance")

TIME_PX = 112
WEATHER_WAIT_S = 1.5


class GlanceCard(Card):
    kind = "glance"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None, *, weather: dict[str, Any] | None = None,
                 upcoming: list[str] | None = None, twelve: bool = True, tz: _dt.tzinfo | None = None) -> None:
        super().__init__(spec, host)
        self.title = "Today"
        self.mood = "happy"
        self.weather = weather or {}
        self.upcoming = [kit.clean(u, 70) for u in (upcoming or []) if kit.clean(u, 70)][:3]
        self.twelve = twelve
        self.tz = tz

    def _now(self) -> _dt.datetime:
        return _dt.datetime.now(self.tz) if self.tz is not None else _dt.datetime.now()

    def draw(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=41)
        now = self._now()
        hour = now.hour % 12 or 12 if self.twelve else now.hour
        clock = f"{hour}:{now:%M}"
        kit.header_title(surf, f"{now:%A}, {now:%B} {now.day}")
        kit.panel(surf)
        box = kit.PANEL.inflate(-2 * kit.PAD, -2 * kit.PAD)
        left = pygame.Rect(box.x, box.y, box.w // 2, box.h)
        big = art.text(clock, TIME_PX, art.INK)
        surf.blit(big, big.get_rect(midtop=(left.centerx, left.y + 4)))
        if self.twelve:
            ampm = art.text("AM" if now.hour < 12 else "PM", SMALL_PX + 4, kit.TEXT_SOFT)
            surf.blit(ampm, ampm.get_rect(midtop=(left.centerx, left.y + big.get_height() + 2)))
        self._draw_weather(surf, pygame.Rect(left.x, left.bottom - 120, left.w, 120))
        right = pygame.Rect(box.centerx + 10, box.y, box.w // 2 - 10, box.h)
        lines = self.upcoming or ["Nothing coming up."]
        y = right.y + 6
        head = art.text("Coming up", BODY_PX, kit.TEXT_SOFT)
        surf.blit(head, (right.x, y))
        y += kit.line_h(BODY_PX) + 4
        for i, line in enumerate(lines):
            wrapped = kit.wrap(line, BODY_PX, right.w - 30)[:2]
            dot = kit.number_badge(i + 1, kit.ACCENTS[i % len(kit.ACCENTS)], r=13) if self.upcoming else None
            if dot is not None:
                surf.blit(dot, (right.x, y + 6))
            block = kit.text_block(list(wrapped), BODY_PX, kit.TEXT)
            surf.blit(block, (right.x + (34 if dot is not None else 0), y))
            y += block.get_height() + 14
            if y > right.bottom - 30:
                break

    def _draw_weather(self, surf: pygame.Surface, r: pygame.Rect) -> None:
        now = self.weather.get("now") or {}
        if not now:
            return
        from littlebot.screens.cards.weather import icon_kind, weather_icon  # noqa: PLC0415

        deg = (self.weather.get("units") or {}).get("temperature", "°")
        icon = weather_icon(icon_kind(str(now.get("conditions") or "")), 84)
        surf.blit(icon, icon.get_rect(midleft=(r.x + 6, r.centery)))
        temp = now.get("temperature")
        line1 = f"{temp}{deg}" if temp is not None else str(now.get("conditions") or "")
        today = (self.weather.get("forecast") or [{}])[0] if self.weather.get("forecast") else {}
        hi, lo = today.get("high"), today.get("low")
        line2 = f"H {hi}°  L {lo}°" if hi is not None and lo is not None else str(now.get("conditions") or "")
        t1 = art.text(line1, 48, art.INK)
        t2 = art.text(kit.ellipsize(line2, SMALL_PX + 2, r.w - 110), SMALL_PX + 2, kit.TEXT_SOFT)
        surf.blit(t1, (r.x + 100, r.centery - t1.get_height() + 6))
        surf.blit(t2, (r.x + 100, r.centery + 8))

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
    """What's next, from the skills (reminders, timers, lists): short lines, most urgent first."""
    out: list[str] = []
    screens = getattr(ctx, "screens", None)
    try:
        timers = screens.timers_now() if screens is not None else ""
    except Exception:  # noqa: BLE001
        timers = ""
    if timers:
        out.append(timers)
    hub = getattr(ctx, "skills", None)
    for name in ("reminders", "lists"):
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
