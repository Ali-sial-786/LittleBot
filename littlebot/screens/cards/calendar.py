"""The calendar card: the family calendar's events for a day or a few days.

A plain snapshot: the calendar skill puts the rows in the spec and shows the card itself, so nothing here reads
the calendar. Five rows a page, each with its time ("3:00 pm", "All day") on the left, a dot in the event's own
colour, the event and, if there's room, where it is. When the events span more than one day, each day starts with
a small day label ("Tomorrow"); the one just added or changed glows for a moment (``spec["highlight"]``: its id).

Spec::

    {"card": "calendar", "title": "Today", "highlight": "e1", "linger": 12, "empty": "Nothing on today",
     "items": [{"id": "e1", "day": "Today", "when": "3:00 pm", "text": "Dentist", "where": "Main St Clinic",
                "color": "#7986CB"}, ...]}
"""

from __future__ import annotations

import math
import re
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, MAX_LINES, Card, CardHost, CardType, register
from littlebot.screens.cards import _kit as kit
from littlebot.screens.cards import candy

ROWS = MAX_LINES
ROW_H = 58
ROW_TOP = kit.PANEL.y + 12
TIME_W = 232  # the time column
ITEM_PX = 36
DAY_PX = 30
DOT_R = 9
ARROW_Y = kit.PANEL.bottom - 30
ARROW_HIT = 44
HIGHLIGHT_S = 4.0
MAX_ITEMS = 60
DEFAULT_TITLE = "Calendar"
DEFAULT_EMPTY = "Nothing on the calendar"
DOT_COLOR: art.Color = art.SKY
DAY_COLOR: art.Color = art.inkier(art.GRAPE, 0.3)
GLOW_COLOR: art.Color = art.LEMON
ICON_COLOR: art.Color = art.STRAWBERRY

_HEX = re.compile(r"^#([0-9a-fA-F]{6})$")


def _linger(spec: dict[str, Any]) -> float | None:
    try:
        v = float(spec.get("linger"))
    except (TypeError, ValueError):
        return None
    return v if 0 < v <= 600 else None


def hex_color(value: Any, default: art.Color = DOT_COLOR) -> art.Color:
    """ "#7986CB" as an (r, g, b); anything else is ``default``."""
    m = _HEX.match(value.strip()) if isinstance(value, str) else None
    if m is None:
        return default
    n = int(m.group(1), 16)
    return (n >> 16) & 255, (n >> 8) & 255, n & 255


def calendar_icon(size: int, color: art.Color = ICON_COLOR) -> pygame.Surface:
    """A friendly page-a-day calendar: a cream page with a coloured top band, two rings and a grid of days."""
    key = ("calendar_icon", size, color)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            edge = art.inkier(color, 0.4)
            page = pygame.Rect(int(n * 0.08), int(n * 0.14), int(n * 0.84), int(n * 0.78))
            pygame.draw.rect(s, edge, page.inflate(int(n * 0.06), int(n * 0.06)), border_radius=int(n * 0.14))
            pygame.draw.rect(s, art.CREAM, page, border_radius=int(n * 0.12))
            band = pygame.Rect(page.x, page.y, page.w, int(n * 0.22))
            pygame.draw.rect(s, color, band, border_top_left_radius=int(n * 0.12),
                             border_top_right_radius=int(n * 0.12))
            for fx in (0.3, 0.7):  # the rings
                ring = pygame.Rect(0, 0, int(n * 0.08), int(n * 0.2))
                ring.center = (int(n * fx), int(n * 0.15))
                pygame.draw.rect(s, edge, ring, border_radius=int(n * 0.04))
            for row in range(3):  # the days
                for col in range(4):
                    cx = page.x + page.w * (0.17 + col * 0.22)
                    cy = band.bottom + (page.bottom - band.bottom) * (0.22 + row * 0.28)
                    tint = color if (row, col) == (1, 2) else art.mix(art.INK_SOFT, art.CREAM, 0.35)
                    pygame.draw.circle(s, tint, (cx, cy), n * 0.045)
            pygame.draw.circle(s, art.lighten(color, 0.55), (page.x + n * 0.12, page.y + n * 0.08), n * 0.04)

        return art.supersampled((size, size), draw)

    return art.cached(key, make)


def _dot(color: art.Color) -> pygame.Surface:
    key = ("calendar_dot", color)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            r = DOT_R * k
            pygame.draw.circle(s, art.inkier(color, 0.35), (r + k, r + k), r + k)
            pygame.draw.circle(s, color, (r + k, r + k), r)

        side = 2 * DOT_R + 2
        return art.supersampled((side, side), draw)

    return art.cached(key, make)


def _clean_items(raw: Any) -> list[dict[str, Any]]:
    """The events from the spec, tidied; anything malformed is left out."""
    out: list[dict[str, Any]] = []
    for i, it in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(it, dict):
            continue
        text = kit.clean(it.get("text"), 80)
        if not text:
            continue
        out.append({"id": kit.clean(it.get("id"), 60) or f"#{i + 1}", "day": kit.clean(it.get("day"), 30),
                    "when": kit.clean(it.get("when"), 24), "text": text[:1].upper() + text[1:],
                    "where": kit.clean(it.get("where"), 60), "color": hex_color(it.get("color"))})
        if len(out) >= MAX_ITEMS:
            break
    return out


def _paginate(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """ROWS rows a page; a day label never sits alone at the bottom of a page (it moves to the next one)."""
    pages: list[list[dict[str, Any]]] = []
    page: list[dict[str, Any]] = []
    for row in rows:
        if len(page) == ROWS or (row["type"] == "day" and len(page) == ROWS - 1):
            pages.append(page)
            page = []
        page.append(row)
    if page or not pages:
        pages.append(page)
    return pages


class CalendarCard(Card):
    """The calendar's events, a page at a time."""

    kind = "calendar"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None) -> None:
        super().__init__(spec, host)
        self.title = kit.clean(spec.get("title"), 40) or DEFAULT_TITLE
        self.empty = kit.clean(spec.get("empty"), 60) or DEFAULT_EMPTY
        self.highlight = kit.clean(spec.get("highlight"), 60) or None
        self.linger_seconds = _linger(spec)
        self.events = _clean_items(spec.get("items"))
        self.multi_day = len({e["day"] for e in self.events if e["day"]}) > 1
        rows: list[dict[str, Any]] = []
        day = ""
        for e in self.events:
            if self.multi_day and e["day"] and e["day"] != day:
                day = e["day"]
                rows.append({"type": "day", "text": day})
            rows.append({"type": "event", **e})
        self.rows = rows
        self._pages = _paginate(rows)
        self.page = 0
        self.page_t = 0.0

    # ------------------------------------------------------------ what it shows
    @property
    def pages(self) -> int:
        return len(self._pages)

    def page_rows(self) -> list[dict[str, Any]]:
        return self._pages[self.page]

    def _turn(self, page: int) -> bool:
        page = max(0, min(self.pages - 1, page))
        if page != self.page:
            self.page, self.page_t = page, self.t
        return True

    def tap(self, x: float, y: float) -> bool:
        if self.pages > 1:
            if math.hypot(x - (kit.PANEL.x + 44), y - ARROW_Y) <= ARROW_HIT:
                return self.command("prev", {})
            return self.command("next" if self.page < self.pages - 1 else "page", {"n": 1})
        return False

    def key(self, name: str) -> bool:
        if name in ("right", "space", "down", "return"):
            return self.command("next", {})
        if name in ("left", "up", "backspace"):
            return self.command("prev", {})
        return False

    def command(self, action: str, args: dict[str, Any]) -> bool:
        args = args if isinstance(args, dict) else {}
        if action == "next":
            return self.pages > 1 and self._turn(self.page + 1)
        if action == "prev":
            return self.pages > 1 and self._turn(self.page - 1)
        if action == "page":
            n = kit.as_int(args.get("n"), 0)
            return 1 <= n <= self.pages and self._turn(n - 1)
        return False

    def describe(self) -> str:
        name = DEFAULT_TITLE if self.title == DEFAULT_TITLE else f"{DEFAULT_TITLE} ({self.title})"
        if not self.events:
            return f"{name}: {self.empty}"
        n = len(self.events)
        shown = []
        for r in self.page_rows():
            if r["type"] != "event":
                continue
            words = " ".join(w for w in ((r["day"] if self.multi_day else ""), r["when"], r["text"]) if w)
            shown.append(words + (f" at {r['where']}" if r["where"] else ""))
        paging = f" (page {self.page + 1} of {self.pages})" if self.pages > 1 else ""
        return f"{name}, {n} event{'' if n == 1 else 's'}{paging}: {'; '.join(shown)}"

    def status(self) -> dict[str, Any]:
        return {**super().status(), "events": len(self.events), "page": self.page + 1, "pages": self.pages}

    # ------------------------------------------------------------ drawing
    def draw(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=41)
        kit.header_title(surf, self.title)
        kit.panel(surf)
        if not self.events:
            box = kit.PANEL
            art.blit_center(surf, calendar_icon(96), (box.centerx, box.y + 78))
            msg = art.text(kit.ellipsize(self.empty, 48, box.w - 40), 48, kit.TEXT)
            surf.blit(msg, msg.get_rect(center=(box.centerx, box.centery + 22)))
            hint = art.text("Say “add the dentist on Tuesday at 3”", BODY_PX, kit.TEXT_SOFT)
            surf.blit(hint, hint.get_rect(center=(box.centerx, box.centery + 82)))
            return
        enter = kit.appear(self.t - self.page_t, 0.0, 0.3)
        for i, row in enumerate(self.page_rows()):
            if row["type"] == "day":
                self._draw_day(surf, i, row, enter)
            else:
                self._draw_row(surf, i, row, enter)
        if self.pages > 1:
            if self.page > 0:
                art.blit_center(surf, kit.arrow(-1), (kit.PANEL.x + 44, ARROW_Y))
            if self.page < self.pages - 1:
                art.blit_center(surf, kit.arrow(1), (kit.PANEL.right - 44, ARROW_Y))
            kit.page_dots(surf, self.pages, self.page, (kit.PANEL.centerx, ARROW_Y))

    def _row_rect(self, i: int, enter: float) -> tuple[pygame.Rect, int]:
        rect = pygame.Rect(kit.PANEL.x + 14, ROW_TOP + i * ROW_H, kit.PANEL.w - 28, ROW_H - 4)
        return rect, int(rect.centery + (1 - art.ease_out_cubic(enter)) * 14)

    def _draw_day(self, surf: pygame.Surface, i: int, row: dict[str, Any], enter: float) -> None:
        rect, cy = self._row_rect(i, enter)
        label = art.text(kit.ellipsize(row["text"], DAY_PX, rect.w - 16), DAY_PX, DAY_COLOR)
        lr = label.get_rect(midleft=(rect.x + 8, cy + 8))
        surf.blit(label, lr)
        pygame.draw.line(surf, art.mix(DAY_COLOR, art.CREAM, 0.6), (lr.right + 14, lr.centery),
                         (rect.right - 8, lr.centery), 2)

    def _draw_row(self, surf: pygame.Surface, i: int, row: dict[str, Any], enter: float) -> None:
        rect, cy = self._row_rect(i, enter)
        if row["id"] == self.highlight and self.t < HIGHLIGHT_S:
            glow = 0.5 + 0.5 * math.sin(self.t * 5.0)
            hl = art.rounded_rect(rect.w, rect.h, 18, art.mix(art.CREAM, GLOW_COLOR, 0.25 + 0.2 * glow))
            surf.blit(hl, (rect.x, cy - rect.h // 2))
        elif i and self.page_rows()[i - 1]["type"] == "event":
            pygame.draw.line(surf, art.mix(kit.PANEL_BORDER, art.CREAM, 0.6), (rect.x + 8, rect.y - 2),
                             (rect.right - 8, rect.y - 2), 2)
        color = row["color"]
        if row["when"]:
            when = candy.fit(row["when"], BODY_PX, TIME_W - 12, art.inkier(color, 0.25), min_size=26)
            surf.blit(when, when.get_rect(midleft=(rect.x + 8, cy + 2)))
        x = rect.x + TIME_W
        dot = _dot(color)
        surf.blit(dot, dot.get_rect(midleft=(x, cy)))
        x += dot.get_width() + 12
        room = rect.right - x - 8
        img = art.text(kit.ellipsize(row["text"], ITEM_PX, room), ITEM_PX, kit.TEXT)
        surf.blit(img, img.get_rect(midleft=(x, cy + 2)))
        left = room - img.get_width() - 14
        if row["where"] and left > 90:
            tag = art.text(kit.ellipsize(row["where"], 26, left), 26, kit.TEXT_SOFT)
            surf.blit(tag, tag.get_rect(midleft=(x + img.get_width() + 14, cy + 4)))


async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> CalendarCard | None:
    """The events in the spec (the calendar skill wrote them)."""
    return CalendarCard(spec, host)


register(CardType(
    kind="calendar",
    description="The family calendar's events for a day or a few days, with their times. Shown by the calendar "
                "tools themselves (don't build it by hand).",
    fields={},
    build=build,
    required=(),
    actions={"next": "the next page", "prev": "the page before", "page": "a page: {\"n\": 2}"},
))

# Example specs for scripts/screen_preview.py.
SAMPLES: list[dict[str, Any]] = [
    {"card": "calendar", "title": "Today", "highlight": "e2", "items": [
        {"id": "e1", "day": "Today", "when": "8:30 am", "text": "School drop-off", "color": "#33B679"},
        {"id": "e2", "day": "Today", "when": "3:00 pm", "text": "Dentist", "where": "Main St Clinic",
         "color": "#7986CB"},
        {"id": "e3", "day": "Today", "when": "6:00 pm", "text": "Soccer practice", "where": "Riverside Park",
         "color": "#F4511E"}]},
    {"card": "calendar", "title": "This week", "items": [
        {"id": "a", "day": "Today", "when": "3:00 pm", "text": "Dentist", "color": "#7986CB"},
        {"id": "b", "day": "Tomorrow", "when": "All day", "text": "Zayn's birthday", "color": "#E67C73"},
        {"id": "c", "day": "Tomorrow", "when": "5:00 pm", "text": "Birthday dinner", "where": "Nana's house"},
        {"id": "d", "day": "Wednesday", "when": "10:00 am", "text": "Parent-teacher meeting", "color": "#F6BF26"}]},
    {"card": "calendar", "title": "Saturday", "items": [], "empty": "Nothing on Saturday"},
]

__all__ = ["SAMPLES", "CalendarCard", "calendar_icon", "hex_color"]
