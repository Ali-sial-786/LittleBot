"""The briefing card: "good morning" at a glance (littlebot/skills/briefing.py puts it up with the spoken briefing).

The greeting beside the presenter's eyes with the date under it. In the panel: the weather as a block across the
top (its drawn icon, the temperature now big, the conditions, the high / low and the chance of rain), then the
headlines, each up to two lines with its source in a candy chip, and the next reminder in a strip at the bottom.
More headlines than fit (the news kind) are paged: tap, swipe or "next".

Readability (littlebot/screens/base.py): headlines at BODY_PX or a little bigger when the page has room, at most
MAX_LINES lines of text at a time (the weather block and the reminder count as one each), the gist only (the voice
says the rest). The layout is worked out once, when the card is made; a frame is blits of cached sprites.
"""

from __future__ import annotations

import math
import re
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, MAX_LINES, SMALL_PX, TITLE_PX, Card, CardHost, CardType, H, W, register
from littlebot.screens.cards import _kit, candy

SWIPE_PX = 60
SOURCE_COLORS = (art.STRAWBERRY, art.SKY, art.MINT, art.GRAPE, art.TANGERINE, art.PERIWINKLE)
MAX_HEADLINES = 15
# How the headlines are set, tried in turn: the first that puts them all on one page wins, else the second, paged.
# (size, most lines a headline wraps to; the last line ends in "..." when it goes on)
HEAD_STYLES = ((40, 2), (BODY_PX, 2), (BODY_PX, 1))
ROW_GAP = 16  # between two headlines (a soft rule sits in it)
SOURCE_MAX_W = 210  # the source chips' column at most (a longer source is cut short)
COLUMN_GAP = 18  # between the source chips and the headlines
BADGE_R = 22  # a headline without a source gets its number instead
# The weather block across the top of the panel.
WEATHER_H = 120
WEATHER_GAP = 14
# What it counts for against MAX_LINES: one glance, like a row (a big number, the conditions and two or three
# figures), so three two-line headlines and the reminder still fit beside it.
WEATHER_LINES = 1
ICON_PX = 108
TEMP_PX = 96
COND_PX = 48
WEATHER_FILL: art.Color = art.mix(art.SKY, art.WHITE, 0.84)
WEATHER_EDGE: art.Color = art.mix(art.SKY, art.WHITE, 0.45)
LOW_INK: art.Color = art.mix(art.INK_SOFT, art.INK, 0.35)
# The reminder strip along the bottom.
REMINDER_H = 56
REMINDER_GAP = 12
REMINDER_PX = 40
BELL_PX = 46
REMINDER_FILL: art.Color = art.mix(art.LEMON, art.WHITE, 0.72)
REMINDER_EDGE: art.Color = art.mix(art.LEMON, art.INK, 0.18)
DOTS_H = 28  # room for the page dots under the headlines (only when there is more than one page)

_WHEN = re.compile(r"^([A-Za-z][A-Za-z ]{1,18}):\s*")
_HILO = re.compile(r"^(-?\d+(?:\.\d+)?)\s*°?\s*[CF]?\s*/\s*(-?\d+(?:\.\d+)?)\s*°?\s*[CF]?$")
_CHANCE = re.compile(r"^(rain|snow|showers?|precipitation)\s*:?\s*(\d{1,3})\s*%$", re.I)
_SPLIT = re.compile(r"\s*[·|;]\s*")


def parse_weather(line: str, conditions: str = "") -> dict[str, Any]:
    """The skill's weather line ("Tomorrow: 11° / 4° · light rain · rain 85%") taken apart for the weather block:
    ``when`` ("Tomorrow"), ``high`` / ``low`` ("11", "4"), ``conditions``, ``chance`` ("Rain 85%") and any ``other``
    parts as they came. Free text the AI wrote comes back as ``other``."""
    out: dict[str, Any] = {"when": "", "high": None, "low": None, "conditions": "", "chance": "", "other": []}
    s = str(line or "").strip()
    m = _WHEN.match(s)
    if m:
        out["when"] = m.group(1).strip().capitalize()
        s = s[m.end():]
    cond = str(conditions or "").strip()
    for part in [p for p in _SPLIT.split(s) if p]:
        hilo = _HILO.match(part)
        chance = _CHANCE.match(part)
        if hilo and out["high"] is None:
            out["high"], out["low"] = hilo.group(1), hilo.group(2)
        elif chance and not out["chance"]:
            out["chance"] = f"{chance.group(1).capitalize()} {chance.group(2)}%"
        elif not out["conditions"] and (part.lower() == cond.lower() or (not cond and not re.search(r"\d", part))):
            out["conditions"] = part
        else:
            out["other"].append(part)
    if not out["conditions"] and cond and not out["other"]:
        out["conditions"] = cond
    return out


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _bell(size: int) -> pygame.Surface:
    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            body = art.LEMON
            edge = art.inkier(body, 0.4)
            top = (n * 0.5, n * 0.18)
            pts = [(n * 0.22, n * 0.72), (n * 0.3, n * 0.36), (n * 0.5, n * 0.2), (n * 0.7, n * 0.36),
                   (n * 0.78, n * 0.72)]
            pygame.draw.polygon(s, edge, [(x, y + n * 0.03) for x, y in pts])
            pygame.draw.circle(s, edge, top, n * 0.08)
            pygame.draw.polygon(s, body, pts)
            pygame.draw.circle(s, body, (n * 0.5, n * 0.38), n * 0.2)
            art.capsule(s, edge, (n * 0.16, n * 0.74), (n * 0.84, n * 0.74), n * 0.1)
            pygame.draw.circle(s, edge, (n * 0.5, n * 0.84), n * 0.08)
            pygame.draw.ellipse(s, art.lighten(body, 0.6), pygame.Rect(n * 0.36, n * 0.3, n * 0.1, n * 0.22))

        return art.supersampled((size, size), draw)

    return art.cached(("brief_bell", size), make)


def _pairs(pairs: tuple[tuple[str, str], ...], size: int, width: int) -> pygame.Surface:
    """ "High 12°   Low 4°   Rain 70%": soft labels, ink values, as many pairs as fit ``width`` (cached)."""
    key = ("brief_pairs", pairs, size, width)

    def make() -> pygame.Surface:
        imgs: list[tuple[pygame.Surface | None, pygame.Surface]] = []
        w = 0
        gap = size  # between two pairs
        for label, value in pairs:
            lab = art.text(f"{label} ", size, LOW_INK) if label else None
            val = art.text(value, size, art.INK)
            add = (gap if imgs else 0) + (lab.get_width() if lab else 0) + val.get_width()
            if w + add > width:
                if not imgs:  # one pair too wide on its own: the value cut short
                    val = art.text(_kit.ellipsize(value, size, max(10, width - (lab.get_width() if lab else 0))),
                                   size, art.INK)
                    imgs.append((lab, val))
                    w = (lab.get_width() if lab else 0) + val.get_width()
                break
            imgs.append((lab, val))
            w += add
        h = art.font(size).get_height()
        out = pygame.Surface((max(1, w), h), pygame.SRCALPHA)
        x = 0
        for lab, val in imgs:
            if lab is not None:
                out.blit(lab, (x, 0))
                x += lab.get_width()
            out.blit(val, (x, 0))
            x += val.get_width() + gap
        return out

    return art.cached(key, make)


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
        self.headlines = _kit.str_list(spec.get("headlines"), max_items=MAX_HEADLINES, limit=160)
        sources = spec.get("sources") if isinstance(spec.get("sources"), list) else []
        self.sources = [_kit.clean(s, 24) for s in sources][:len(self.headlines)]
        self.reminder = _kit.clean(spec.get("reminder"), 80)
        self.page = 0
        self.mood = "happy"
        self._swipe = 0.0
        self._page_at = 0.0  # when the page on show came up (its headlines pop in)
        self._wx = parse_weather(self.weather, self.conditions) if self.weather else None
        self._layout()

    # ------------------------------------------------------------ layout (worked out once)
    @staticmethod
    def inner() -> pygame.Rect:
        return candy.PANEL.inflate(-2 * _kit.PAD, -2 * _kit.PAD)

    def weather_rect(self) -> pygame.Rect | None:
        """The weather block across the top; without headlines it fills the panel above the reminder."""
        if not self.weather:
            return None
        r = self.inner()
        if not self.headlines:
            return pygame.Rect(r.x, r.y, r.w, r.h - (REMINDER_H + REMINDER_GAP if self.reminder else 0))
        return pygame.Rect(r.x, r.y, r.w, WEATHER_H)

    def reminder_rect(self) -> pygame.Rect | None:
        """The reminder strip along the bottom (in the middle of the panel when it is all there is)."""
        if not self.reminder:
            return None
        r = self.inner()
        if not self.weather and not self.headlines:
            return pygame.Rect(r.x, r.centery - REMINDER_H // 2, r.w, REMINDER_H)
        return pygame.Rect(r.x, r.bottom - REMINDER_H, r.w, REMINDER_H)

    def news_rect(self, *, dots: bool = False) -> pygame.Rect:
        """Where the headlines go: between the weather block and the reminder strip (less the page dots' row)."""
        r = self.inner()
        top = r.y + (WEATHER_H + WEATHER_GAP if self.weather else 0)
        bottom = r.bottom - (REMINDER_H + REMINDER_GAP if self.reminder else 0) - (DOTS_H if dots else 0)
        return pygame.Rect(r.x, top, r.w, max(0, bottom - top))

    def _chip(self, i: int) -> pygame.Surface:
        """The ``i``-th headline's source chip (or its number), made once in _layout."""
        return self._chips[i]

    def _make_chip(self, i: int) -> pygame.Surface:
        source = self.sources[i] if i < len(self.sources) else ""
        color = SOURCE_COLORS[(sum(map(ord, source)) if source else i) % len(SOURCE_COLORS)]
        if source:
            return _kit.chip(_kit.ellipsize(source, SMALL_PX, SOURCE_MAX_W - 36), color)
        return _kit.number_badge(i + 1, color, r=BADGE_R)

    def _layout(self) -> None:
        """Sets the headlines' size, their lines, and which go on which page."""
        lines_left = MAX_LINES - (WEATHER_LINES if self.weather else 0) - (1 if self.reminder else 0)
        self.max_lines = max(2, lines_left)
        self._chips = [self._make_chip(i) for i in range(len(self.headlines))]
        self._col = max((c.get_width() for c in self._chips), default=-COLUMN_GAP) + COLUMN_GAP
        chip_h = max((c.get_height() for c in self._chips), default=0)
        width = self.inner().w - self._col
        best: tuple[int, list[tuple[str, ...]], list[list[int]]] | None = None
        for size, most in HEAD_STYLES:
            wrapped = [self._wrap(h, size, width, most) for h in self.headlines]
            heights = [max(chip_h, self._text_h(len(ls), size)) for ls in wrapped]
            lines = [len(ls) for ls in wrapped]
            pages = self._paged(heights, lines)
            if len(pages) <= 1:
                best = (size, wrapped, pages)
                break
            if best is None or (most > 1 and len(best[2]) > len(pages)):
                best = (size, wrapped, pages)
        assert best is not None
        self.head_px, self._wrapped, self._pages = best[0], best[1], best[2] or [[]]

    def _paged(self, heights: list[int], lines: list[int]) -> list[list[int]]:
        if not heights:
            return [[]]
        room = self.news_rect().h
        pages = _kit.pages_by_height(heights, lines, gap=ROW_GAP, room=room, max_lines=self.max_lines)
        if len(pages) > 1:  # the page dots need a row of their own
            pages = _kit.pages_by_height(heights, lines, gap=ROW_GAP, room=room - DOTS_H, max_lines=self.max_lines)
        return pages

    @staticmethod
    def _wrap(text: str, size: int, width: int, most: int) -> tuple[str, ...]:
        lines = list(_kit.wrap(text, size, width))
        if len(lines) > most:
            lines = lines[:most - 1] + [_kit.ellipsize(" ".join(lines[most - 1:]), size, width)]
        return tuple(lines)

    @staticmethod
    def _text_h(n: int, size: int) -> int:
        return _kit.line_h(size) * (n - 1) + art.font(size).get_height()

    @property
    def per_page(self) -> int:
        """How many headlines the page on show holds."""
        return len(self._pages[self.page]) if self._pages else 0

    @property
    def pages(self) -> int:
        return max(1, len(self._pages))

    def shown(self) -> list[tuple[int, str]]:
        return [(i, self.headlines[i]) for i in self._pages[self.page]] if self._pages else []

    # ------------------------------------------------------------ steering
    def turn(self, step: int) -> bool:
        new = max(0, min(self.pages - 1, self.page + step))
        if new == self.page:
            return False
        self.page = new
        self._page_at = self.t
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
        surf.blit(art.background((W, H), seed=23), (0, 0))
        dy = candy.slide_in(self.t)
        self._header(surf, dy * 0.5)
        candy.panel(surf, dy=dy)
        wr, rr = self.weather_rect(), self.reminder_rect()
        if wr is not None:
            self._weather_block(surf, wr.move(0, int(dy)))
        self._headlines(surf, int(dy))
        if rr is not None:
            self._reminder(surf, rr.move(0, int(dy)))
        if self.pages > 1:
            news = self.news_rect(dots=True)
            _kit.page_dots(surf, self.pages, self.page, (news.centerx, news.bottom + DOTS_H // 2 + int(dy)))

    def _header(self, surf: pygame.Surface, dy: float) -> None:
        x = candy.HEADER_X
        if not self.date:
            candy.header(surf, self.title, dy=dy)
            return
        width = W - x - 24
        title = candy.fit(self.title, TITLE_PX, width, min_size=TITLE_PX)
        date = candy.fit(self.date, BODY_PX, width, _kit.TEXT_SOFT, min_size=BODY_PX)
        gap = 6
        top = _kit.HEADER.centery - (title.get_height() + gap + date.get_height()) // 2 + int(dy)
        surf.blit(title, (x, top))
        surf.blit(date, (x + 2, top + title.get_height() + gap))

    # ------------------------------------------------------------ the weather block
    def _weather_block(self, surf: pygame.Surface, r: pygame.Rect) -> None:
        from littlebot.screens.cards.weather import icon_kind, weather_icon  # noqa: PLC0415

        wx = self._wx or {}
        surf.blit(art.rounded_rect(r.w, r.h, 24, WEATHER_FILL, border=WEATHER_EDGE, border_w=3), r.topleft)
        grow = 1.5 if r.h >= 2 * WEATHER_H else 1.0  # no headlines: the weather is the card, half as big again
        icon_px, temp_px = int(ICON_PX * grow), int(TEMP_PX * grow)
        bob = round(math.sin(self.t * 1.8) * 4)
        icon = weather_icon(icon_kind(self.conditions or wx.get("conditions") or self.weather), icon_px)
        x = r.x + int(18 * grow)
        surf.blit(icon, icon.get_rect(midleft=(x, r.centery + bob)))
        x += icon.get_width() + 16
        hi, lo = wx.get("high"), wx.get("low")
        big_low = False
        if self.temperature is not None:
            deg = candy.degrees(self.temperature, temp_px)
            surf.blit(deg, deg.get_rect(midleft=(x, r.centery + 4)))
            x += deg.get_width() + 30
        elif hi is not None:  # a day ahead: its high big, the low beside it
            deg = candy.degrees(hi, temp_px)
            dr = surf.blit(deg, deg.get_rect(midleft=(x, r.centery + 4)))
            low = candy.degrees(f"/{lo}", int(COND_PX * grow), LOW_INK)
            surf.blit(low, low.get_rect(bottomleft=(dr.right + 6, dr.bottom - int(temp_px * 0.14))))
            x = dr.right + 6 + low.get_width() + 30
            big_low = True
        width = r.right - 22 - x
        rows = self._weather_rows(wx, width, show_range=not big_low)
        if not rows:
            return
        gap = 10
        total = sum(img.get_height() for img in rows) + gap * (len(rows) - 1)
        y = r.centery - total // 2 + 2
        for img in rows:
            surf.blit(img, (x, y))
            y += img.get_height() + gap

    def _weather_rows(self, wx: dict[str, Any], width: int, *, show_range: bool) -> list[pygame.Surface]:
        """The block's words, right of the temperature: the conditions big (after "Tomorrow" when it is about
        tomorrow), then the high / low and the chance of rain; free text wrapped to two lines."""
        rows: list[pygame.Surface] = []
        cond = _cap(str(wx.get("conditions") or ""))
        when = str(wx.get("when") or "")
        if cond or when:
            rows.append(self._conditions_row(when, cond, width))
        pairs: list[tuple[str, str]] = []
        if show_range and wx.get("high") is not None:
            pairs += [("High", f"{wx['high']}°"), ("Low", f"{wx['low']}°")]
        if wx.get("chance"):
            label, value = str(wx["chance"]).split(" ", 1)
            pairs.append((label, value))
        if pairs:
            rows.append(_pairs(tuple(pairs), BODY_PX, width))
        other = " · ".join(wx.get("other") or [])
        if other:  # what the AI wrote in its own words: two lines, or one under the conditions and the figures
            most = 1 if rows and pairs else 2
            rows.append(_kit.text_block(list(self._wrap(other, BODY_PX, width, most)), BODY_PX, art.INK))
        return rows

    @staticmethod
    def _conditions_row(when: str, cond: str, width: int) -> pygame.Surface:
        key = ("brief_cond", when, cond, width)

        def make() -> pygame.Surface:
            chip = _kit.chip(when, art.GRAPE) if when else None
            cw = chip.get_width() + 14 if chip else 0
            text = candy.fit(cond, COND_PX, max(10, width - cw), min_size=BODY_PX) if cond else None
            h = max(chip.get_height() if chip else 0, text.get_height() if text else 0)
            out = pygame.Surface((max(1, cw + (text.get_width() if text else 0)), max(1, h)), pygame.SRCALPHA)
            if chip is not None:
                out.blit(chip, chip.get_rect(midleft=(0, h // 2)))
            if text is not None:
                out.blit(text, text.get_rect(midleft=(cw, h // 2 + 1)))
            return out

        return art.cached(key, make)

    # ------------------------------------------------------------ the headlines
    def _headlines(self, surf: pygame.Surface, dy: int) -> None:
        area = self.news_rect(dots=self.pages > 1).move(0, dy)
        size = self.head_px
        rows = self._pages[self.page] if self._pages else []
        heights = []
        for i in rows:
            chip = self._chip(i)
            heights.append(max(chip.get_height(), self._text_h(len(self._wrapped[i]), size)))
        total = sum(heights) + ROW_GAP * max(0, len(rows) - 1)
        # under the weather a page starts at the top of its room; the news alone sits in the middle of the panel
        y = area.y + max(0, (area.h - total) // 2 if not self.weather else min((area.h - total) // 3, 16))
        since = self.t - self._page_at
        for n, (i, h) in enumerate(zip(rows, heights)):
            k = _kit.appear(since, 0.1 * n, 0.3)
            if k > 0:
                self._headline(surf, area.x, y, h, i, k)
            if n:
                rule_y = y - ROW_GAP // 2
                pygame.draw.line(surf, candy.PANEL_EDGE, (area.x + self._col, rule_y), (area.right, rule_y), 2)
            y += h + ROW_GAP

    def _headline(self, surf: pygame.Surface, x: int, y: int, h: int, i: int, k: float) -> None:
        size = self.head_px
        off = int((1 - k) * 30)
        chip = self._chip(i)
        first_mid = y + art.font(size).get_height() // 2
        _kit.blit_alpha(surf, chip, chip.get_rect(midleft=(x + off, max(y + chip.get_height() // 2, first_mid)))
                        .topleft, k)
        text = _kit.text_block(list(self._wrapped[i]), size, _kit.TEXT)
        top = y + max(0, (chip.get_height() - art.font(size).get_height()) // 2) if len(self._wrapped[i]) == 1 else y
        _kit.blit_alpha(surf, text, (x + self._col + off, top + 1), k)

    # ------------------------------------------------------------ the reminder
    def _reminder(self, surf: pygame.Surface, r: pygame.Rect) -> None:
        surf.blit(art.rounded_rect(r.w, r.h, r.h // 2, REMINDER_FILL, border=REMINDER_EDGE, border_w=3), r.topleft)
        bell = _bell(BELL_PX)
        tilt = math.sin(self.t * 6) * 8 if self.t < 1.6 else 0.0  # a little ring as it appears
        art.blit_center(surf, art.rotated(bell, tilt, key=("brief_bell", BELL_PX)), (r.x + 22 + BELL_PX // 2,
                                                                                     r.centery - 2))
        x = r.x + 22 + BELL_PX + 14
        label = art.text("Next:", REMINDER_PX, LOW_INK)
        surf.blit(label, label.get_rect(midleft=(x, r.centery + 1)))
        x += label.get_width() + 12
        text = candy.fit(self.reminder, REMINDER_PX, r.right - 24 - x, art.INK)
        surf.blit(text, text.get_rect(midleft=(x, r.centery + 1)))


async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> BriefingCard | None:
    card = BriefingCard(spec, host)
    if not (card.weather or card.headlines or card.reminder):
        return None
    return card


register(CardType(
    kind="briefing",
    description="A morning or evening briefing at a glance: the greeting and date, the weather, the top "
                "headlines with their sources and the next reminder (get_briefing puts it up by itself).",
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
     "sources": ["CBC", "CBC Hamilton", "BBC World"], "reminder": "9 am dentist"},
    {"card": "briefing", "title": "Top stories", "date": "Friday 30 October",
     "headlines": ["Hamilton council approves new LRT station designs after a long debate",
                   "Leafs edge Canadiens 3-2 in overtime thriller",
                   "Scientists spot a new comet heading our way; it should be visible next month",
                   "Snow expected across Ontario this weekend",
                   "New library opens downtown with a whole floor for children",
                   "Prices at the grocery store level off for the first time in a year",
                   "Local bakery wins national bread award",
                   "School board adds more French immersion classes",
                   "Bridge repairs close the Skyway for two nights"],
     "sources": ["CBC", "CBC Sports", "BBC World", "CBC", "CBC Hamilton", "BBC", "CBC Hamilton", "CBC", "CBC"]},
    {"card": "briefing", "title": "Good night", "date": "Friday 30 October",
     "weather": "Tomorrow: 11° / 4° · light rain · rain 85%", "conditions": "light rain",
     "headlines": ["Hamilton council approves new LRT station designs after a long debate that went on late into "
                   "the night", "Leafs edge Canadiens 3-2 in overtime thriller after a late goal from the captain",
                   "Scientists spot a new comet heading our way, and it may be bright enough to see without "
                   "a telescope"],
     "sources": ["CBC", "CBC Sports", "BBC World"], "reminder": "8 am school run, then the bake sale at 10"},
]

__all__ = ["SAMPLES", "BriefingCard", "parse_weather"]
