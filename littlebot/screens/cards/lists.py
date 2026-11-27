"""The lists card: a shopping, to-do or any named list, with a tick box per item ("what's on the shopping list?").

Items come from the Lists skill (``ctx.skills.get("lists")``, littlebot/skills/lists.py) and stay current: a change
by voice while the card is up shows straight away (the skill's ``version``). A tap on an item (its whole row, a
90 px target) ticks it: the box fills, the words are struck through, and a moment later it is taken off the list
(``Lists.tick_off``); the voice is told.

Four rows a column: a list of more than four short items goes in two columns (eight a page: "Milk", "Eggs", ...),
else one column with the words as big as fit (on two lines when long). The round arrows (or "next" / "prev") turn
pages.

Without the skill (previews) the card shows ``spec["items"]``. After a change by voice the skill shows the card
for a short look (``spec["linger"]`` seconds once the chat is over); "read the list" shows it for the usual time.
"""

from __future__ import annotations

import functools
import logging
import math
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, TITLE_PX, Card, CardHost, CardType, register
from littlebot.screens.cards import _kit as kit
from littlebot.skills.lists import SHOPPING, list_key, title

log = logging.getLogger("littlebot.screens.lists")

INNER = kit.PANEL.inflate(-2 * kit.PAD, -24)  # where the rows go: 940 x 430
ROWS = 4  # rows a column (each a 90 px tick target): eight items a page in two columns, four in one
ROW_H = 90
COLUMNS_GAP = 44
ONE_COLUMN_SIZES = (TITLE_PX, 44, 40, BODY_PX)  # an item's words in one column: as big as all fit
TWO_COLUMN_SIZES = (44, 40, BODY_PX)  # ... in two (each on one line)
CHECK = 58  # the tick box
TEXT_GAP = 22  # between the box and the words
TICK_S = 0.6  # a ticked item stays (struck through) this long, then goes
BAR_H = 70  # the page arrows and dots, under the rows
ARROW_R = 30
ARROW_HIT = 46  # a 92 px target
ARROW_Y = kit.PANEL.bottom - 42
MAX_PREVIEW_ITEMS = 60
EMPTY_CHECK = 112
ROW_COLOR: art.Color = art.mix(kit.PANEL_BORDER, art.CREAM, 0.6)


@functools.lru_cache(maxsize=64)
def list_layout(items: tuple[str, ...]) -> tuple[int, int, tuple[tuple[str, ...], ...]]:
    """How a list's items sit: (columns, size, each item's lines). Two columns when there are more than ROWS items
    and every one fits on a line in half the panel; else one column, every item on one line at the biggest size
    they all fit, else on two lines (the second ends in "..." when it goes on)."""
    half = (INNER.w - COLUMNS_GAP) // 2 - CHECK - TEXT_GAP
    if len(items) > ROWS:
        for size in TWO_COLUMN_SIZES:
            if all(kit.text_w(i, size) <= half for i in items):
                return 2, size, tuple((i,) for i in items)
    width = INNER.w - CHECK - TEXT_GAP
    for most in (1, 2):
        for size in ONE_COLUMN_SIZES:
            wrapped = [kit.wrap(i, size, width) for i in items]
            if all(len(w) <= most for w in wrapped):
                return 1, size, tuple(tuple(w) for w in wrapped)
    size = ONE_COLUMN_SIZES[-1]
    out = []
    for i in items:
        w = kit.wrap(i, size, width)
        out.append(tuple(w) if len(w) <= 2 else (w[0], kit.ellipsize(" ".join(w[1:]), size, width)))
    return 1, size, tuple(out)


def _linger(spec: dict[str, Any]) -> float | None:
    try:
        v = float(spec.get("linger"))
    except (TypeError, ValueError):
        return None
    return v if 0 < v <= 600 else None


def dashes(h: int, color: art.Color) -> pygame.Surface:
    """A soft dashed vertical line, ``h`` px tall (a sprite: a frame only blits it)."""
    key = ("lists_dashes", h, color)

    def make() -> pygame.Surface:
        out = pygame.Surface((2, h), pygame.SRCALPHA)
        for y in range(0, h, 18):
            pygame.draw.line(out, color, (0, y), (0, min(h, y + 9)), 2)
        return out

    return art.cached(key, make)


def _check_box(on: bool, color: art.Color, size: int = CHECK) -> pygame.Surface:
    """A rounded tick box, empty or filled with a tick."""
    key = ("lists_check", on, color, size)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            rect = pygame.Rect(0, 0, int(n), int(n))
            pygame.draw.rect(s, art.inkier(color, 0.35), rect, border_radius=int(n * 0.26))
            inner = rect.inflate(-int(n * 0.16), -int(n * 0.16))
            pygame.draw.rect(s, color if on else art.WHITE, inner, border_radius=int(n * 0.2))
            if on:
                pts = [(n * 0.24, n * 0.52), (n * 0.43, n * 0.71), (n * 0.77, n * 0.31)]
                pygame.draw.lines(s, art.WHITE, False, pts, max(2, int(n * 0.13)))
                for p in (pts[0], pts[-1]):
                    pygame.draw.circle(s, art.WHITE, p, n * 0.065)

        return art.supersampled((size, size), draw)

    return art.cached(key, make)


class ListCard(Card):
    """One list, paged (four rows a column, two columns of short items), tick boxes to tick items off."""

    kind = "lists"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None, *, skill: Any = None,
                 key: str = SHOPPING, items: list[str] | None = None) -> None:
        super().__init__(spec, host)
        self.skill = skill
        self.list_name = key or SHOPPING
        self.title = title(self.list_name)
        self.linger_seconds = _linger(spec)
        self.page = 0
        self.page_t = 0.0
        self.ticking: dict[int, float] = {}  # item id -> when it was tapped (self.t)
        self._local = list(items or [])
        self._local_ids = list(range(len(self._local)))
        self._version = -1
        self.rows: list[dict[str, Any]] = []
        self.columns = 1
        self.size = ONE_COLUMN_SIZES[0]
        self._load()

    # ------------------------------------------------------------ the list
    def _load(self) -> None:
        if self.skill is not None:
            try:
                got = self.skill.items(self.list_name)
                self._version = int(getattr(self.skill, "version", 0))
            except Exception:  # noqa: BLE001 - the card keeps what it had
                log.debug("lists card: couldn't read the list", exc_info=True)
                return
            rows = [{"id": int(r["id"]), "item": str(r["item"])} for r in got]
        else:
            rows = [{"id": i, "item": s} for i, s in zip(self._local_ids, self._local, strict=True)]
        words = tuple(kit.clean(r["item"], 120) or "-" for r in rows)
        words = tuple(w[:1].upper() + w[1:] for w in words)
        self.columns, self.size, lines = list_layout(words)
        self.rows = [{**r, "lines": ls} for r, ls in zip(rows, lines, strict=True)]
        self.page = max(0, min(self.page, self.pages - 1))

    @property
    def per_page(self) -> int:
        """Items a page: up to ROWS a column, evened out over the pages (9 items: 5 + 4, not 8 + 1)."""
        most = ROWS * self.columns
        pages = max(1, math.ceil(len(self.rows) / most))
        return max(1, math.ceil(len(self.rows) / pages))

    @property
    def pages(self) -> int:
        return max(1, math.ceil(len(self.rows) / self.per_page))

    def page_rows(self) -> list[dict[str, Any]]:
        return self.rows[self.page * self.per_page:(self.page + 1) * self.per_page]

    def _take_off(self, rid: int) -> None:
        if self.skill is not None:
            try:
                self.skill.tick_off(self.list_name, rid)
            except Exception:  # noqa: BLE001
                log.warning("lists card: couldn't tick an item off", exc_info=True)
        else:
            if rid in self._local_ids:
                i = self._local_ids.index(rid)
                del self._local_ids[i]
                del self._local[i]
        self._load()

    def update(self, dt: float) -> None:
        super().update(dt)
        for rid, t0 in list(self.ticking.items()):
            if self.t - t0 >= TICK_S:
                del self.ticking[rid]
                self._take_off(rid)
        if self.skill is not None and int(getattr(self.skill, "version", 0)) != self._version:
            self._load()
        self.mood = "happy" if self.ticking else "neutral"

    # ------------------------------------------------------------ where things are
    def _rows_down(self) -> int:
        """How many rows this page's first column has."""
        n = len(self.page_rows())
        return max(1, min(ROWS, math.ceil(n / self.columns)))

    def _top(self) -> int:
        room = INNER.h - (BAR_H if self.pages > 1 else 0)
        return INNER.y + max(0, (room - self._rows_down() * ROW_H) // 2)

    def _row_rect(self, i: int) -> pygame.Rect:
        """The i-th item of this page: its whole row (the tick target). Two columns: down the first, then the
        second."""
        col_w = (INNER.w - COLUMNS_GAP) // 2 if self.columns == 2 else INNER.w
        down = self._rows_down()
        col, row = (i // down, i % down) if self.columns == 2 else (0, i)
        return pygame.Rect(INNER.x + col * (col_w + COLUMNS_GAP), self._top() + row * ROW_H, col_w, ROW_H)

    def _arrows(self) -> tuple[tuple[int, int], tuple[int, int]]:
        return (kit.PANEL.x + 52, ARROW_Y), (kit.PANEL.right - 52, ARROW_Y)

    # ------------------------------------------------------------ touch and voice
    def _turn(self, page: int) -> bool:
        page = max(0, min(self.pages - 1, page))
        if page != self.page:
            self.page, self.page_t = page, self.t
        return True

    def tap(self, x: float, y: float) -> bool:
        if self.pages > 1:
            back, fwd = self._arrows()
            if math.hypot(x - back[0], y - back[1]) <= ARROW_HIT:
                return self.command("prev", {})
            if math.hypot(x - fwd[0], y - fwd[1]) <= ARROW_HIT:
                return self.command("next", {})
        for i, row in enumerate(self.page_rows()):
            if self._row_rect(i).collidepoint(x, y):
                if row["id"] in self.ticking:
                    return True
                self.ticking[row["id"]] = self.t
                self.host.sound("pop")
                self.host.tell(f"{row['item']} was just ticked off the {self.title} on the screen.")
                return True
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
        if action == "list" and self.skill is not None:
            key = list_key(args.get("name") or args.get("list"))
            if not key:
                return False
            self.list_name, self.title, self.page, self.ticking = key, title(key), 0, {}
            self._load()
            return True
        return False

    def describe(self) -> str:
        n = len(self.rows)
        if not n:
            return f"{self.title}: empty"
        shown = ", ".join(r["item"] for r in self.page_rows())
        paging = f" (page {self.page + 1} of {self.pages})" if self.pages > 1 else ""
        return f"{self.title}: {n} item{'s' if n != 1 else ''}{paging}: {shown}"

    def status(self) -> dict[str, Any]:
        return {**super().status(), "list": self.list_name, "items": len(self.rows), "page": self.page + 1,
                "pages": self.pages, "columns": self.columns}

    # ------------------------------------------------------------ drawing
    def draw(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=29)
        n = len(self.rows)
        badge = kit.chip(f"{n} item{'s' if n != 1 else ''}", art.MINT, size=BODY_PX) if n else None
        head = kit.HEADER.copy()
        if badge is not None:
            head.w -= badge.get_width() + 14
            surf.blit(badge, badge.get_rect(midright=(kit.HEADER.right, kit.HEADER.centery)))
        kit.header_title(surf, self.title, rect=head)
        kit.panel(surf)
        if not n:
            self._draw_empty(surf)
            return
        enter = art.ease_out_cubic(kit.appear(self.t - self.page_t, 0.0, 0.3))
        lift = int((1 - enter) * 16)
        rows = self.page_rows()
        if self.columns == 2 and len(rows) > self._rows_down():  # a dashed line between the columns
            surf.blit(dashes(self._rows_down() * ROW_H - 20, ROW_COLOR), (INNER.centerx - 1, self._top() + 10 + lift))
        for i, row in enumerate(rows):
            self._draw_row(surf, i, row, lift)
        if self.pages > 1:
            back, fwd = self._arrows()
            if self.page > 0:
                art.blit_center(surf, kit.arrow(-1, r=ARROW_R), back)
            if self.page < self.pages - 1:
                art.blit_center(surf, kit.arrow(1, r=ARROW_R), fwd)
            kit.page_dots(surf, self.pages, self.page, (kit.PANEL.centerx, ARROW_Y))

    def _draw_row(self, surf: pygame.Surface, i: int, row: dict[str, Any], lift: int) -> None:
        rect = self._row_rect(i).move(0, lift)
        tick_t = self.ticking.get(row["id"])
        on = tick_t is not None
        color = kit.ACCENTS[row["id"] % len(kit.ACCENTS)]
        if i % self._rows_down():
            pygame.draw.line(surf, ROW_COLOR, (rect.x + 4, rect.y), (rect.right - 4, rect.y), 2)
        box = _check_box(on, art.MINT if on else color)
        surf.blit(box, box.get_rect(midleft=(rect.x + 2, rect.centery)))
        lines = row["lines"]
        img = kit.text_block(lines, self.size, kit.TEXT_SOFT if on else kit.TEXT)
        r = img.get_rect(midleft=(rect.x + CHECK + TEXT_GAP, rect.centery + 2))
        if not on:
            surf.blit(img, r)
            return
        fade = art.clamp((self.t - (tick_t or 0.0)) / TICK_S, 0.0, 1.0)
        kit.blit_alpha(surf, img, r.topleft, 1.0 - 0.6 * fade)
        pitch = kit.line_h(self.size)
        half = art.font(self.size).get_height() // 2
        for n, line in enumerate(lines):  # struck through, line by line
            w = kit.text_w(line, self.size) + 8
            y = r.y + n * pitch + half
            pygame.draw.line(surf, kit.TEXT_SOFT, (r.x - 4, y), (r.x - 4 + int(w * min(1.0, fade * 2.5)), y), 4)

    def _draw_empty(self, surf: pygame.Surface) -> None:
        box = kit.PANEL
        empty = _check_box(False, art.SKY, EMPTY_CHECK)
        msg = art.text("Nothing on it yet", TITLE_PX, kit.TEXT)
        hint = "Say “add milk”" if self.list_name == SHOPPING else "Say “add ... to the list”"
        tip = art.text(hint, BODY_PX, kit.TEXT_SOFT)
        total = EMPTY_CHECK + 28 + msg.get_height() + 18 + tip.get_height()
        y = box.y + (box.h - total) // 2
        surf.blit(empty, empty.get_rect(midtop=(box.centerx, y)))
        y += EMPTY_CHECK + 28
        surf.blit(msg, msg.get_rect(midtop=(box.centerx, y)))
        surf.blit(tip, tip.get_rect(midtop=(box.centerx, y + msg.get_height() + 18)))


def _skill(ctx: Any) -> Any:
    skills = getattr(ctx, "skills", None) if ctx is not None else None
    try:
        return skills.get("lists") if skills is not None and hasattr(skills, "get") else None
    except Exception:  # noqa: BLE001
        return None


async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> ListCard | None:
    """The list ``spec["list"]`` names (the shopping list when none), live from the Lists skill."""
    skill = _skill(ctx)
    key = list_key(spec.get("list"))
    if skill is not None:
        if not key:
            try:
                have = [k for k, _ in skill.names()]
            except Exception:  # noqa: BLE001
                have = []
            key = SHOPPING if SHOPPING in have or not have else have[0]
        return ListCard(spec, host, skill=skill, key=key)
    items = kit.str_list(spec.get("items"), max_items=MAX_PREVIEW_ITEMS, limit=80)
    return ListCard(spec, host, key=key or SHOPPING, items=items)


register(CardType(
    kind="lists",
    description="One of the family's lists (shopping, to-do, packing ...) with tick boxes: 'show me the shopping "
                "list'. The items come from the robot's own lists.",
    fields={"list": {"type": "string", "description": "Which list: 'shopping', 'to-do' or its name. Optional "
                                                      "(the shopping list)."}},
    build=build,
    required=(),
    actions={"next": "the next page", "prev": "the page before", "page": "a page: {\"n\": 2}",
             "list": "another list: {\"name\": \"to-do\"}"},
))

# Example specs for scripts/screen_preview.py (no skill there: the items are given).
SAMPLES: list[dict[str, Any]] = [
    {"card": "lists", "list": "shopping", "items": ["Milk", "Eggs", "2% milk", "Bananas", "Diapers", "Coffee",
                                                    "Bread"]},
    {"card": "lists", "list": "to-do", "items": ["Call the bank about the new card", "Book Amara's check-up",
                                                 "Fix the wobbly kitchen chair before the weekend"]},
    {"card": "lists", "list": "packing", "items": []},
]

__all__ = ["SAMPLES", "ListCard"]
