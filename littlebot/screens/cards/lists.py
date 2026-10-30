"""The lists card: a shopping, to-do or any named list, with a tick box per item ("what's on the shopping list?").

Items come from the Lists skill (``ctx.skills.get("lists")``, littlebot/skills/lists.py) and stay current: a change
by voice while the card is up shows straight away (the skill's ``version``). A tap on an item ticks it: the box
fills, the words are struck through, and a moment later it is taken off the list (``Lists.tick_off``); the voice
is told. Five items a page; the round arrows (or "next" / "prev") turn pages.

Without the skill (previews) the card shows ``spec["items"]``. After a change by voice the skill shows the card
for a short look (``spec["linger"]`` seconds once the chat is over); "read the list" shows it for the usual time.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, MAX_LINES, Card, CardHost, CardType, register
from littlebot.screens.cards import _kit as kit
from littlebot.skills.lists import SHOPPING, list_key, title

log = logging.getLogger("littlebot.screens.lists")

ITEM_PX = 38
ROWS = MAX_LINES  # items a page
ROW_H = 58
CHECK = 36  # the tick box
TICK_S = 0.6  # a ticked item stays (struck through) this long, then goes
ARROW_R = 24
ARROW_HIT = 44
MAX_PREVIEW_ITEMS = 60
ROW_TOP = kit.PANEL.y + 12
ARROW_Y = kit.PANEL.bottom - 30


def _linger(spec: dict[str, Any]) -> float | None:
    try:
        v = float(spec.get("linger"))
    except (TypeError, ValueError):
        return None
    return v if 0 < v <= 600 else None


def _check_box(on: bool, color: art.Color) -> pygame.Surface:
    """A rounded tick box, empty or filled with a tick."""
    key = ("lists_check", on, color)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = CHECK * k
            rect = pygame.Rect(0, 0, int(n), int(n))
            pygame.draw.rect(s, art.inkier(color, 0.35), rect, border_radius=int(10 * k))
            inner = rect.inflate(-int(6 * k), -int(6 * k))
            pygame.draw.rect(s, color if on else art.WHITE, inner, border_radius=int(8 * k))
            if on:
                pts = [(n * 0.24, n * 0.52), (n * 0.43, n * 0.71), (n * 0.77, n * 0.31)]
                pygame.draw.lines(s, art.WHITE, False, pts, max(2, int(5 * k)))
                for p in (pts[0], pts[-1]):
                    pygame.draw.circle(s, art.WHITE, p, 2.4 * k)

        return art.supersampled((CHECK, CHECK), draw)

    return art.cached(key, make)


class ListCard(Card):
    """One list, paged five items at a time, tick boxes to tick items off."""

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
            self.rows = [{"id": int(r["id"]), "item": str(r["item"])} for r in got]
        else:
            self.rows = [{"id": i, "item": s} for i, s in zip(self._local_ids, self._local, strict=True)]
        self.page = max(0, min(self.page, self.pages - 1))

    @property
    def pages(self) -> int:
        return max(1, math.ceil(len(self.rows) / ROWS))

    def page_rows(self) -> list[dict[str, Any]]:
        return self.rows[self.page * ROWS:(self.page + 1) * ROWS]

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

    # ------------------------------------------------------------ touch and voice
    def _turn(self, page: int) -> bool:
        page = max(0, min(self.pages - 1, page))
        if page != self.page:
            self.page, self.page_t = page, self.t
        return True

    def _row_rect(self, i: int) -> pygame.Rect:
        return pygame.Rect(kit.PANEL.x + 14, ROW_TOP + i * ROW_H, kit.PANEL.w - 28, ROW_H - 4)

    def _arrows(self) -> tuple[tuple[int, int], tuple[int, int]]:
        return (kit.PANEL.x + 44, ARROW_Y), (kit.PANEL.right - 44, ARROW_Y)

    def tap(self, x: float, y: float) -> bool:
        if self.pages > 1:
            back, fwd = self._arrows()
            if math.hypot(x - back[0], y - back[1]) <= ARROW_HIT:
                return self.command("prev", {})
            if math.hypot(x - fwd[0], y - fwd[1]) <= ARROW_HIT:
                return self.command("next", {})
        for i, row in enumerate(self.page_rows()):
            if self._row_rect(i).inflate(0, 4).collidepoint(x, y):
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
                "pages": self.pages}

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
        enter = kit.appear(self.t - self.page_t, 0.0, 0.3)
        for i, row in enumerate(self.page_rows()):
            self._draw_row(surf, i, row, enter)
        if self.pages > 1:
            back, fwd = self._arrows()
            if self.page > 0:
                art.blit_center(surf, kit.arrow(-1, r=ARROW_R), back)
            if self.page < self.pages - 1:
                art.blit_center(surf, kit.arrow(1, r=ARROW_R), fwd)
            kit.page_dots(surf, self.pages, self.page, (kit.PANEL.centerx, ARROW_Y))

    def _draw_row(self, surf: pygame.Surface, i: int, row: dict[str, Any], enter: float) -> None:
        rect = self._row_rect(i)
        lift = (1 - art.ease_out_cubic(enter)) * 14
        cy = int(rect.centery + lift)
        tick_t = self.ticking.get(row["id"])
        on = tick_t is not None
        color = kit.ACCENTS[(row["id"]) % len(kit.ACCENTS)]
        if i:
            pygame.draw.line(surf, art.mix(kit.PANEL_BORDER, art.CREAM, 0.6), (rect.x + 8, rect.y - 2),
                             (rect.right - 8, rect.y - 2), 2)
        box = _check_box(on, art.MINT if on else color)
        surf.blit(box, box.get_rect(midleft=(rect.x + 6, cy)))
        text_x = rect.x + CHECK + 26
        words = row["item"][:1].upper() + row["item"][1:]
        words = kit.ellipsize(words, ITEM_PX, rect.right - text_x - 8)
        img = art.text(words, ITEM_PX, kit.TEXT_SOFT if on else kit.TEXT)
        r = img.get_rect(midleft=(text_x, cy + 2))
        if on:
            fade = art.clamp((self.t - (tick_t or 0.0)) / TICK_S, 0.0, 1.0)
            kit.blit_alpha(surf, img, r.topleft, 1.0 - 0.6 * fade)
            pygame.draw.line(surf, kit.TEXT_SOFT, (r.x - 4, r.centery), (r.x + int((r.w + 8) * min(1, fade * 2.5)),
                                                                          r.centery), 4)
        else:
            surf.blit(img, r)

    def _draw_empty(self, surf: pygame.Surface) -> None:
        box = kit.PANEL
        msg = art.text("Nothing on it yet", 48, kit.TEXT)
        surf.blit(msg, msg.get_rect(center=(box.centerx, box.centery - 30)))
        hint = "Say “add milk”" if self.list_name == SHOPPING else "Say “add ... to the list”"
        img = art.text(hint, BODY_PX, kit.TEXT_SOFT)
        surf.blit(img, img.get_rect(center=(box.centerx, box.centery + 34)))
        empty = _check_box(False, art.SKY)
        surf.blit(empty, empty.get_rect(center=(box.centerx, box.y + 60)))


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
    {"card": "lists", "list": "packing", "items": []},
]

__all__ = ["SAMPLES", "ListCard"]
