"""The briefing card: "good morning" at a glance (littlebot/skills/briefing.py puts it up with the spoken briefing).

The greeting beside the presenter's eyes with the date under it; in the panel the weather line with its drawn icon
(the weather card's) and the temperature now, the top headlines (one line each, their source in a candy chip) and
the next reminder at the bottom. More headlines than fit (the news kind) are paged: tap, swipe or "next".

Readability (littlebot/screens/base.py): body text at BODY_PX, at most MAX_LINES rows at a time, the gist only
(the voice says the rest).
"""

from __future__ import annotations

import math
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, MAX_LINES, SMALL_PX, TITLE_PX, Card, CardHost, CardType, W, register
from littlebot.screens.cards import _kit, candy

ROW_H = 62
SWIPE_PX = 60
SOURCE_COLORS = (art.STRAWBERRY, art.SKY, art.MINT, art.GRAPE, art.TANGERINE, art.PERIWINKLE)


def _bell(size: int) -> pygame.Surface:
    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            body = art.LEMON
            edge = art.inkier(body, 0.4)
            top = (n * 0.5, n * 0.18)
            pts = [(n * 0.22, n * 0.72), (n * 0.3, n * 0.36), (n * 0.5, n * 0.2), (n * 0.7, n * 0.36), (n * 0.78, n * 0.72)]
            pygame.draw.polygon(s, edge, [(x, y + n * 0.03) for x, y in pts])
            pygame.draw.circle(s, edge, top, n * 0.08)
            pygame.draw.polygon(s, body, pts)
            pygame.draw.circle(s, body, (n * 0.5, n * 0.38), n * 0.2)
            art.capsule(s, edge, (n * 0.16, n * 0.74), (n * 0.84, n * 0.74), n * 0.1)
            pygame.draw.circle(s, edge, (n * 0.5, n * 0.84), n * 0.08)
            pygame.draw.ellipse(s, art.lighten(body, 0.6), pygame.Rect(n * 0.36, n * 0.3, n * 0.1, n * 0.22))

        return art.supersampled((size, size), draw)

    return art.cached(("brief_bell", size), make)


class BriefingCard(Card):
    kind = "briefing"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None) -> None:
        super().__init__(spec, host)
        self.title = _kit.clean(spec.get("title"), 40) or "Your briefing"
        self.date = _kit.clean(spec.get("date"), 40)
        self.weather = _kit.clean(spec.get("weather"), 90)
        self.conditions = _kit.clean(spec.get("conditions"), 40)
        t = spec.get("temperature")
        self.temperature = int(round(t)) if isinstance(t, (int, float)) and not isinstance(t, bool) else None
        self.headlines = _kit.str_list(spec.get("headlines"), max_items=15, limit=160)
        sources = spec.get("sources") if isinstance(spec.get("sources"), list) else []
        self.sources = [_kit.clean(s, 24) for s in sources][:len(self.headlines)]
        self.reminder = _kit.clean(spec.get("reminder"), 80)
        self.page = 0
        self.mood = "happy"
        self._swipe = 0.0

    # ------------------------------------------------------------ layout
    @property
    def per_page(self) -> int:
        rows = MAX_LINES - (1 if self.weather else 0) - (1 if self.reminder else 0)
        return max(3, rows)

    @property
    def pages(self) -> int:
        return max(1, math.ceil(len(self.headlines) / self.per_page))

    def shown(self) -> list[tuple[int, str]]:
        start = self.page * self.per_page
        return list(enumerate(self.headlines))[start:start + self.per_page]

    # ------------------------------------------------------------ steering
    def turn(self, step: int) -> bool:
        new = max(0, min(self.pages - 1, self.page + step))
        if new == self.page:
            return False
        self.page = new
        return True

    def command(self, action: str, args: dict[str, Any]) -> bool:
        if action == "next":
            return self.turn(1)
        if action == "prev":
            return self.turn(-1)
        return False

    def tap(self, x: float, y: float) -> bool:
        if self.pages <= 1:
            return False
        if x < W / 3 and self.page > 0:
            return self.turn(-1)
        return self.turn(1) or self.turn(-self.page)  # past the last page: back to the first

    def key(self, name: str) -> bool:
        if name in ("right", "down", "space"):
            return self.turn(1)
        if name in ("left", "up"):
            return self.turn(-1)
        return False

    def drag(self, dx: float, dy: float) -> None:
        self._swipe += dx

    def drag_end(self) -> None:
        swipe, self._swipe = self._swipe, 0.0
        if abs(swipe) >= SWIPE_PX:
            self.turn(1 if swipe < 0 else -1)

    # ------------------------------------------------------------ the voice
    def describe(self) -> str:
        parts = [self.title + (f", {self.date}" if self.date else "")]
        if self.weather:
            parts.append(self.weather)
        if self.headlines:
            first = self.shown()[0][1] if self.shown() else self.headlines[0]
            more = f" (page {self.page + 1} of {self.pages})" if self.pages > 1 else ""
            parts.append(f"{len(self.headlines)} headlines{more}, first: {first}")
        if self.reminder:
            parts.append(f"next reminder: {self.reminder}")
        return "; ".join(parts)[:200]

    def status(self) -> dict[str, Any]:
        return {**super().status(), "page": self.page, "pages": self.pages, "headlines": len(self.headlines)}

    # ------------------------------------------------------------ frames
    def draw(self, surf: pygame.Surface) -> None:
        surf.blit(art.background((W, 480), seed=23), (0, 0))
        dy = candy.slide_in(self.t)
        self._header(surf, dy * 0.5)
        box = candy.panel(surf, dy=dy)
        y = box.top + 18
        if self.weather:
            self._weather_row(surf, box, y)
            y += ROW_H + 14
            pygame.draw.line(surf, candy.PANEL_EDGE, (box.left + 24, y - 7), (box.right - 24, y - 7), 3)
        for n, (i, title) in enumerate(self.shown()):
            age = self.t - 0.12 * n
            if age <= 0:
                continue
            self._headline(surf, box, y + n * ROW_H, i, title, _kit.appear(self.t, 0.12 * n, 0.3))
        if self.reminder:
            self._reminder(surf, box, box.bottom - ROW_H - 2)
        if self.pages > 1:
            at = (box.centerx, box.bottom - 18) if not self.reminder else (box.right - 30 - 13 * (self.pages - 1),
                                                                           box.top - 8)
            _kit.page_dots(surf, self.pages, self.page, at)

    def _header(self, surf: pygame.Surface, dy: float) -> None:
        x = candy.HEADER_X
        if not self.date:
            candy.header(surf, self.title, dy=dy)
            return
        title = candy.fit(self.title, TITLE_PX, W - x - 20)
        surf.blit(title, title.get_rect(midleft=(x, int(36 + dy))))
        date = candy.fit(self.date, SMALL_PX, W - x - 20, art.INK_SOFT, min_size=SMALL_PX)
        surf.blit(date, date.get_rect(midleft=(x + 2, int(80 + dy))))

    def _weather_row(self, surf: pygame.Surface, box: pygame.Rect, y: int) -> None:
        from littlebot.screens.cards.weather import icon_kind, weather_icon  # noqa: PLC0415

        x = box.left + 22
        bob = round(math.sin(self.t * 1.8) * 3)
        icon = weather_icon(icon_kind(self.conditions or self.weather), 76)
        surf.blit(icon, icon.get_rect(midleft=(x, y + ROW_H // 2 + bob)))
        x += icon.get_width() + 12
        if self.temperature is not None:
            deg = candy.degrees(self.temperature, 58)
            surf.blit(deg, deg.get_rect(midleft=(x, y + ROW_H // 2)))
            x += deg.get_width() + 18
        line = candy.fit(self.weather, BODY_PX, box.right - 24 - x, art.INK)
        surf.blit(line, line.get_rect(midleft=(x, y + ROW_H // 2 + 2)))

    def _headline(self, surf: pygame.Surface, box: pygame.Rect, y: int, i: int, title: str, k: float) -> None:
        x = box.left + 22
        source = self.sources[i] if i < len(self.sources) else ""
        color = SOURCE_COLORS[(sum(map(ord, source)) if source else i) % len(SOURCE_COLORS)]
        if source:
            pill = _kit.chip(_kit.ellipsize(source, SMALL_PX, 120), color)
        else:
            pill = _kit.number_badge(i + 1, color, r=20)
        off = int((1 - k) * 30)
        _kit.blit_alpha(surf, pill, pill.get_rect(midleft=(x + off, y + ROW_H // 2)).topleft, k)
        x += pill.get_width() + 14
        text = _kit.text_block([_kit.ellipsize(title, BODY_PX, box.right - 24 - x)], BODY_PX, _kit.TEXT)
        _kit.blit_alpha(surf, text, text.get_rect(midleft=(x + off, y + ROW_H // 2 + 2)).topleft, k)

    def _reminder(self, surf: pygame.Surface, box: pygame.Rect, y: int) -> None:
        x = box.left + 22
        bell = _bell(52)
        tilt = math.sin(self.t * 6) * 8 if self.t < 1.6 else 0.0  # a little ring as it appears
        art.blit_center(surf, art.rotated(bell, tilt, key=("brief_bell", 52)), (x + 26, y + ROW_H // 2 - 4))
        x += 66
        text = candy.fit(f"Next: {self.reminder}", BODY_PX, box.right - 24 - x, art.INK)
        surf.blit(text, text.get_rect(midleft=(x, y + ROW_H // 2 - 2)))


async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> BriefingCard | None:
    card = BriefingCard(spec, host)
    if not (card.weather or card.headlines or card.reminder):
        return None
    return card


register(CardType(
    kind="briefing",
    description="A morning or evening briefing at a glance: the greeting and date, the weather line, the top "
                "headlines and the next reminder (get_briefing puts it up by itself).",
    fields={
        "title": {"type": "string", "description": "the greeting, e.g. 'Good morning'"},
        "date": {"type": "string", "description": "e.g. 'Friday 30 October'"},
        "weather": {"type": "string", "description": "one short line, e.g. '12° / 4° · light rain · rain 70%'"},
        "conditions": {"type": "string", "description": "for the icon, e.g. 'light rain'"},
        "headlines": {"type": "array", "items": {"type": "string"}, "description": "headline titles"},
        "reminder": {"type": "string", "description": "the next reminder, e.g. '6 pm call mom'"},
    },
    build=build,
    actions={"next": "the next headlines", "prev": "the headlines before"},
))

SAMPLES: list[dict[str, Any]] = [
    {"card": "briefing", "title": "Good morning", "date": "Friday 30 October", "temperature": 8,
     "weather": "12° / 4° · light rain · rain 70%", "conditions": "light rain",
     "headlines": ["Hamilton council approves new LRT station designs after a long debate",
                   "Leafs edge Canadiens 3-2 in overtime thriller", "Scientists spot a new comet heading our way"],
     "sources": ["CBC", "CBC", "BBC"], "reminder": "9 am dentist"},
    {"card": "briefing", "title": "Top stories", "date": "Friday 30 October",
     "headlines": [f"Headline number {i} about something that happened today" for i in range(1, 8)],
     "sources": ["CBC", "BBC"] * 4},
]

__all__ = ["SAMPLES", "BriefingCard"]
