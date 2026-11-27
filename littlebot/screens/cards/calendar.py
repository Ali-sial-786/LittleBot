"""The calendar card: the family calendar's events for a day or a few days.

A plain snapshot: the calendar skill puts the rows in the spec and shows the card itself, so nothing here reads
the calendar. Each event is a row: its time ("3:00 pm", "All day") in the event's own colour, a dot in that colour,
the event (on two lines when it is long) and where it is, right-aligned with a pin (on a line of its own when the
event leaves no room for it). When the events span more than one day, each day starts with a heading band
("Tomorrow"), shown again at the top of a page that goes on with that day; a heading never ends a page. As many
rows as fit, at most MAX_LINES lines a page (about eight events), paged with the round arrows, a tap or "next".
The one just added or changed glows for a moment (``spec["highlight"]``: its id).

The agenda layout (rows, day headings, paging, drawing) is shared with the reminders card (reminders.py).

Spec::

    {"card": "calendar", "title": "Today", "highlight": "e1", "linger": 12, "empty": "Nothing on today",
     "items": [{"id": "e1", "day": "Today", "when": "3:00 pm", "text": "Dentist", "where": "Main St Clinic",
                "color": "#7986CB"}, ...]}
"""

from __future__ import annotations

import functools
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, MAX_LINES, SMALL_PX, TITLE_PX, Card, CardHost, CardType, register
from littlebot.screens.cards import _kit as kit
from littlebot.screens.cards import candy

HIGHLIGHT_S = 4.0
MAX_ITEMS = 60
DEFAULT_TITLE = "Calendar"
DEFAULT_EMPTY = "Nothing on the calendar"
DOT_COLOR: art.Color = art.SKY
DAY_COLOR: art.Color = art.inkier(art.GRAPE, 0.3)
GLOW_COLOR: art.Color = art.LEMON
ICON_COLOR: art.Color = art.STRAWBERRY
EMPTY_ICON = 132

_HEX = re.compile(r"^#([0-9a-fA-F]{6})$")

# ---------------------------------------------------------------- the agenda layout (also reminders.py)
INNER =kit.PANEL.inflate(-2 * kit.PAD, -24)  # where the rows go: 940 x 430
BAR_H = 70  # the page arrows and dots at the bottom, when there is more than one page
ARROW_R = 30
ARROW_HIT = 46  # a tap this close to an arrow's centre is on it (a 92 px target)
ARROW_Y = kit.PANEL.bottom - 42
BACK_X = kit.PANEL.x + 52
NEXT_X = kit.PANEL.right - 52
SIDE_GAP = 28  # between the words and a place beside them
MAX_AIR = 34  # a short page spreads its rows out by up to this much each
ROW_COLOR: art.Color = art.mix(kit.PANEL_BORDER, art.CREAM, 0.6)


@dataclass(frozen=True)
class Tier:
    """The agenda's sizes: :data:`BIG` when everything fits on one page at it (a short day reads from across the
    room), else :data:`NORMAL` (about eight events a page)."""

    time_px: int  # the time, in the event's colour
    time_w: int  # the time column ("10:30 am", "All day")
    icon_w: int  # the dot (or an alarm's bell) between the time and the words
    item_sizes: tuple[int, int]  # the words: one line at the first, else two at the second
    side_px: int  # where it is / how it repeats, soft, beside or under the words
    day_px: int  # a day's heading
    day_h: int  # its band and the air under it
    row_pad: int  # above and below an event's words
    dot_r: int
    icon_px: int  # an alarm's bell

    @property
    def text_x(self) -> int:
        return INNER.x + self.time_w + self.icon_w

    @property
    def side_line(self) -> int:  # a place (or repeat) on a line of its own
        return kit.line_h(self.side_px) + 2

    @property
    def side_icon(self) -> int:
        return self.side_px - 4


NORMAL = Tier(time_px=40, time_w=164, icon_w=46, item_sizes=(40, BODY_PX), side_px=32, day_px=BODY_PX, day_h=54,
              row_pad=12, dot_r=11, icon_px=40)
BIG = Tier(time_px=TITLE_PX, time_w=210, icon_w=58, item_sizes=(TITLE_PX, 44), side_px=BODY_PX, day_px=44, day_h=66,
           row_pad=17, dot_r=14, icon_px=52)


def _block_h(size: int, n: int) -> int:
    """How tall ``n`` lines at ``size`` are (kit.text_block's sprite)."""
    return kit.line_h(size) * max(0, n - 1) + art.font(size).get_height()


def side_w(side: str, tier: Tier = NORMAL) -> int:
    return tier.side_icon + 10 + kit.text_w(side, tier.side_px) if side else 0


@functools.lru_cache(maxsize=512)
def row_layout(text: str, side: str, tier: Tier = NORMAL) -> tuple[int, tuple[str, ...], bool]:
    """How an event's words sit beside its time: (size, lines, whether ``side`` goes on a line of its own). The
    words get one line at the tier's bigger size, else two at its smaller one (the second ends in "..." when it
    goes on)."""
    width = INNER.right - tier.text_x
    size, lines = kit.fit(text, tier.item_sizes, width, max_lines=1)
    if len(lines) > 1:
        size = tier.item_sizes[-1]
        lines = kit.wrap(text, size, width)
        if len(lines) > 2:
            lines = (lines[0], kit.ellipsize(" ".join(lines[1:]), size, width))
    below = bool(side) and (len(lines) > 1 or kit.text_w(lines[0], size) + SIDE_GAP + side_w(side, tier) > width)
    return size, tuple(lines), below


def event_row(row: dict[str, Any], *, tier: Tier, clock: str, text: str, side: str = "", side_kind: str = "pin",
              color: art.Color = art.SKY, icon: Callable[[int], pygame.Surface] | None = None) -> dict[str, Any]:
    """``row`` (an event's own fields) laid out as an agenda row: its height (``h``), its lines (``k``) and what is
    drawn (``lay``). ``icon(px)``: the sprite between the time and the words (the event's dot when None)."""
    size, lines, below = row_layout(text, side, tier)
    h = 2 * tier.row_pad + _block_h(size, len(lines)) + (tier.side_line if below else 0)
    room = INNER.right - tier.text_x - tier.side_icon - 10
    lay = {"tier": tier, "clock": clock, "size": size, "lines": lines, "side": side, "below": below, "color": color,
           "side_text": kit.ellipsize(side, tier.side_px, room) if side else "",
           "icon": icon(tier.icon_px) if icon is not None else dot(color, tier.dot_r),
           "side_icon": side_icon(side_kind, tier.side_icon)}
    return {**row, "type": "event", "h": h, "k": len(lines) + int(below), "lay": lay}


def day_row(text: str, tier: Tier = NORMAL) -> dict[str, Any]:
    return {"type": "day", "text": text, "h": tier.day_h, "k": 1, "tier": tier}


def layout(build: Callable[[Tier], list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """The agenda rows ``build(tier)`` makes, and their pages: BIG when they all fit on one page, else NORMAL."""
    rows = build(BIG)
    if len(_fill(rows, INNER.h, MAX_LINES)) <= 1:
        return rows, [rows]
    rows = build(NORMAL)
    return rows, paginate(rows)


def paginate(rows: Sequence[dict[str, Any]], *, room: int = INNER.h, paged_room: int = INNER.h - BAR_H,
             max_lines: int = MAX_LINES) -> list[list[dict[str, Any]]]:
    """Rows into as few pages as fit (``room`` px, or ``paged_room`` beside the page arrows; at most ``max_lines``
    lines), evened out (12 events: 6 + 6, not 8 + 4). A day's heading never ends a page, and a page that goes on
    with a day starts with its heading again."""
    if not rows:
        return [[]]
    pages = _fill(rows, room, max_lines)
    if len(pages) <= 1:
        return pages
    pages = _fill(rows, paged_room, max_lines)
    total = sum(r["h"] for r in rows)
    for limit in range(max(max(r["h"] for r in rows), total // len(pages)), paged_room + 1, 4):
        even = _fill(rows, limit, max_lines)
        if len(even) == len(pages):
            return even
    return pages


def _fill(rows: Sequence[dict[str, Any]], room: int, max_lines: int) -> list[list[dict[str, Any]]]:
    pages: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    px = n = 0
    day: dict[str, Any] | None = None
    for i, row in enumerate(rows):
        h, k = row["h"], row["k"]
        if row["type"] == "day":
            day = row
            nxt = rows[i + 1] if i + 1 < len(rows) and rows[i + 1]["type"] != "day" else None
            need_h, need_k = h + (nxt["h"] if nxt else 0), k + (nxt["k"] if nxt else 0)
        else:
            need_h, need_k = h, k
        if any(r["type"] != "day" for r in cur) and (px + need_h > room or n + need_k > max_lines):
            pages.append(cur)
            cur, px, n = [], 0, 0
            if row["type"] != "day" and day is not None:  # the day goes on: its heading again
                cur, px, n = [day], day["h"], day["k"]
        cur.append(row)
        px += h
        n += k
    if cur:
        pages.append(cur)
    return pages


def page_frames(page: Sequence[dict[str, Any]], paged: bool) -> list[pygame.Rect]:
    """Where each row of ``page`` goes: the page centred in the panel, a short one spread out a little."""
    room = INNER.h - (BAR_H if paged else 0)
    total = sum(r["h"] for r in page)
    air = min(MAX_AIR, max(0, room - total) // max(1, len(page)))
    y = INNER.y + max(0, (room - total - air * len(page)) // 2)
    out = []
    for r in page:
        out.append(pygame.Rect(INNER.x, y, INNER.w, r["h"] + air))
        y += r["h"] + air
    return out


def side_icon(kind: str, size: int = 28, color: art.Color = kit.TEXT_SOFT) -> pygame.Surface:
    """The little icon before a place ("pin"), a repeat ("repeat") or a snooze ("snooze")."""
    key = ("agenda_side", kind, size, color)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            c = n / 2
            if kind == "pin":
                pygame.draw.circle(s, color, (c, n * 0.38), n * 0.3)
                pygame.draw.polygon(s, color, [(c - n * 0.26, n * 0.5), (c + n * 0.26, n * 0.5), (c, n * 0.96)])
                pygame.draw.circle(s, art.CREAM, (c, n * 0.38), n * 0.12)
            elif kind == "repeat":
                rect = pygame.Rect(n * 0.12, n * 0.12, n * 0.76, n * 0.76)
                pygame.draw.arc(s, color, rect, math.radians(40), math.radians(330), max(2, int(n * 0.13)))
                tip = (c + n * 0.38 * math.cos(math.radians(40)), c - n * 0.38 * math.sin(math.radians(40)))
                pygame.draw.polygon(s, color, [(tip[0] - n * 0.2, tip[1] - n * 0.06), (tip[0] + n * 0.14,
                                                                                     tip[1] - n * 0.12),
                                               (tip[0] + n * 0.02, tip[1] + n * 0.22)])
            else:  # a sleepy moon
                pygame.draw.circle(s, color, (c, c), n * 0.42)
                pygame.draw.circle(s, (0, 0, 0, 0), (c + n * 0.2, c - n * 0.14), n * 0.36)

        return art.supersampled((size, size), draw)

    return art.cached(key, make)


def dot(color: art.Color, r: int = NORMAL.dot_r) -> pygame.Surface:
    key = ("calendar_dot", color, r)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            rr = r * k
            pygame.draw.circle(s, art.inkier(color, 0.35), (rr + k, rr + k), rr + k)
            pygame.draw.circle(s, color, (rr + k, rr + k), rr)

        side = 2 * r + 2
        return art.supersampled((side, side), draw)

    return art.cached(key, make)


def _day_band(text: str, w: int, tier: Tier) -> pygame.Surface:
    key = ("agenda_day", text, w, tier.day_px, tier.day_h)

    def make() -> pygame.Surface:
        h = tier.day_h - 12
        out = art.rounded_rect(w, h, h // 2, art.mix(art.GRAPE, art.WHITE, 0.84)).copy()
        pygame.draw.rect(out, art.mix(art.GRAPE, art.WHITE, 0.25), pygame.Rect(0, 0, 10, h),
                         border_top_left_radius=h // 2, border_bottom_left_radius=h // 2)
        label = art.text(kit.ellipsize(text, tier.day_px, w - 48), tier.day_px, DAY_COLOR)
        out.blit(label, label.get_rect(midleft=(26, h // 2 + 1)))
        return out

    return art.cached(key, make)


def draw_page(surf: pygame.Surface, page: Sequence[dict[str, Any]], *, paged: bool, enter: float, t: float,
              highlight: Any = None) -> None:
    """The rows of one page: day headings and events (``highlight``: the id that glows for a moment)."""
    lift = int((1 - art.ease_out_cubic(enter)) * 16)
    frames = page_frames(page, paged)
    for i, (row, rect) in enumerate(zip(page, frames)):
        rect = rect.move(0, lift)
        if row["type"] == "day":
            band = _day_band(row["text"], rect.w, row.get("tier", NORMAL))
            surf.blit(band, band.get_rect(midleft=(rect.x, rect.centery - 2)))
            continue
        lay = row["lay"]
        tier: Tier = lay["tier"]
        if highlight is not None and row.get("id") == highlight and t < HIGHLIGHT_S:
            glow = 0.5 + 0.5 * math.sin(t * 5.0)
            surf.blit(art.rounded_rect(rect.w + 12, rect.h - 4, 18, art.mix(art.CREAM, GLOW_COLOR, 0.25 + 0.2 * glow)),
                      (rect.x - 6, rect.y + 2))
        elif i and page[i - 1]["type"] == "event":
            pygame.draw.line(surf, ROW_COLOR, (rect.x + 4, rect.y), (rect.right - 4, rect.y), 2)
        words_h = _block_h(lay["size"], len(lay["lines"]))
        top = rect.y + (rect.h - words_h - (tier.side_line if lay["below"] else 0)) // 2
        mid = top + art.font(lay["size"]).get_height() // 2  # the first line's middle
        if lay["clock"]:
            when = candy.fit(lay["clock"], tier.time_px, tier.time_w - 16, art.inkier(lay["color"], 0.25),
                             min_size=SMALL_PX)
            surf.blit(when, when.get_rect(midleft=(rect.x, mid + 1)))
        icon = lay["icon"]
        surf.blit(icon, icon.get_rect(center=(rect.x + tier.time_w + tier.icon_w // 2 - 8, mid)))
        surf.blit(kit.text_block(lay["lines"], lay["size"], kit.TEXT), (tier.text_x, top))
        if lay["side"]:
            _draw_side(surf, lay, top + words_h + 4 if lay["below"] else None, mid)


def _draw_side(surf: pygame.Surface, lay: dict[str, Any], below_y: int | None, mid: int) -> None:
    tier: Tier = lay["tier"]
    img = art.text(lay["side_text"], tier.side_px, kit.TEXT_SOFT)
    if below_y is None:  # right-aligned on the event's line
        x, cy = INNER.right - img.get_width(), mid + 1
    else:
        x, cy = tier.text_x + tier.side_icon + 10, below_y + kit.line_h(tier.side_px) // 2
    surf.blit(img, img.get_rect(midleft=(x, cy)))
    surf.blit(lay["side_icon"], lay["side_icon"].get_rect(midright=(x - 10, cy)))


def draw_paging(surf: pygame.Surface, pages: int, page: int) -> None:
    if pages <= 1:
        return
    if page > 0:
        art.blit_center(surf, kit.arrow(-1, r=ARROW_R), (BACK_X, ARROW_Y))
    if page < pages - 1:
        art.blit_center(surf, kit.arrow(1, r=ARROW_R), (NEXT_X, ARROW_Y))
    kit.page_dots(surf, pages, page, (kit.PANEL.centerx, ARROW_Y))


def on_back_arrow(x: float, y: float) -> bool:
    return math.hypot(x - BACK_X, y - ARROW_Y) <= ARROW_HIT


def draw_empty(surf: pygame.Surface, icon: pygame.Surface, message: str, hint: str) -> None:
    """An empty agenda: a big icon, what's (not) there, and what to say."""
    box = kit.PANEL
    msg = art.text(kit.ellipsize(message, TITLE_PX, box.w - 60), TITLE_PX, kit.TEXT)
    tip = art.text(kit.ellipsize(hint, BODY_PX, box.w - 60), BODY_PX, kit.TEXT_SOFT)
    total = icon.get_height() + 24 + msg.get_height() + 18 + tip.get_height()
    y = box.y + (box.h - total) // 2
    surf.blit(icon, icon.get_rect(midtop=(box.centerx, y)))
    y += icon.get_height() + 24
    surf.blit(msg, msg.get_rect(midtop=(box.centerx, y)))
    y += msg.get_height() + 18
    surf.blit(tip, tip.get_rect(midtop=(box.centerx, y)))


# ---------------------------------------------------------------- the calendar card
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
        self.rows, self._pages = layout(self._rows)  # the events and their days' headings, as agenda rows
        self.page = 0
        self.page_t = 0.0

    def _rows(self, tier: Tier) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        day = ""
        for e in self.events:
            if self.multi_day and e["day"] and e["day"] != day:
                day = e["day"]
                rows.append(day_row(day, tier))
            rows.append(event_row(e, tier=tier, clock=e["when"], text=e["text"], side=e["where"], color=e["color"]))
        return rows

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
            if on_back_arrow(x, y):
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
            draw_empty(surf, calendar_icon(EMPTY_ICON), self.empty, "Say “add the dentist on Tuesday at 3”")
            return
        draw_page(surf, self.page_rows(), paged=self.pages > 1, enter=kit.appear(self.t - self.page_t, 0.0, 0.3),
                  t=self.t, highlight=self.highlight)
        draw_paging(surf, self.pages, self.page)


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
        {"id": "d", "day": "Wed 11 Nov", "when": "10:00 am", "text": "Parent-teacher meeting about the school "
                                                                     "trip", "where": "Oakwood Primary School",
         "color": "#F6BF26"},
        {"id": "e", "day": "Wed 11 Nov", "when": "6:30 pm", "text": "Swimming", "where": "Leisure centre",
         "color": "#33B679"}]},
    {"card": "calendar", "title": "Saturday", "items": [], "empty": "Nothing on Saturday"},
]

__all__ = ["BIG", "NORMAL", "SAMPLES", "CalendarCard", "Tier", "calendar_icon", "day_row", "dot", "draw_empty",
           "draw_page", "draw_paging", "event_row", "hex_color", "layout", "on_back_arrow", "paginate", "side_icon"]
