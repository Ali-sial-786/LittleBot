"""The "explain" card: the key points of an explanation, revealed one by one while the voice explains.

"Explain how rainbows form": the title, then 2-8 short points ("Sunlight enters a raindrop", "The drop
bends the light", "Each colour bends differently"), each a few words, never the sentences the voice is
saying. Each point may have a short ``details`` line under it, softer ("Light slows down in water"), and
the card may end with a one-line ``summary`` ("So: sunlight split by raindrops"). Points appear at a
gentle pace (``pace`` seconds apart, about how long the voice takes per point), "next" shows the next one
at once, and the newest point is highlighted; the summary comes after the last point. The 7" screen shows
them all together when they fit (up to MAX_LINES lines a page); more is paged, and the page turns when
the newest point is on the next one. An optional picture (``picture_query``) sits beside the points;
without one the points use the whole width, a little bigger.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens import base
from littlebot.screens.base import BODY_PX, Card, CardType, register
from littlebot.screens.cards import _kit as kit

MAX_POINTS = 8
PACE_S = 4.0  # seconds between points by default
FIRST_AT = 0.7  # the first point appears this long after the card
HOLD_S = 12.0  # after "prev" or a jump, wait this long before revealing more on its own
SIZE_WIDE = 44  # point text without a picture
SIZE_NARROW = 40  # ...and beside one
DETAIL_PX = BODY_PX  # a point's detail line (softer)
SUMMARY_PX = 40
POINT_LINES = 3  # at most this many lines for one point...
DETAIL_LINES = 2  # ...and for its detail
BADGE_R = 25
GAP = 18  # between points
DOTS_H = 30  # room kept for the page dots when there is more than one page
SUMMARY_AFTER = 0.6  # the summary follows the last point after this share of the pace
PIC_W = kit.PIC_W


pages_by_height = kit.pages_by_height  # (it lives in the kit now; kept here for callers)


class ExplainCard(Card):
    kind = "explain"

    def __init__(self, spec: dict[str, Any], host: Any = None) -> None:
        super().__init__(spec, host)
        self.title = kit.clean(spec.get("title"), 60) or "Here's how"
        self.points = kit.str_list(spec.get("points"), max_items=MAX_POINTS, limit=90)
        details = spec.get("details")
        details = details if isinstance(details, (list, tuple)) else []
        self.details = [kit.clean(details[i], 80) if i < len(details) else "" for i in range(len(self.points))]
        self.summary = kit.clean(spec.get("summary"), 100)
        try:
            self.pace = max(1.5, min(15.0, float(spec.get("pace") or PACE_S)))
        except (TypeError, ValueError):
            self.pace = PACE_S
        self.mood = "curious"
        self.shown = 0  # how many points are showing (the newest one is highlighted)
        self.next_at = FIRST_AT
        self.shown_at: list[float] = [0.0] * len(self.points)
        self.summary_at: float | None = None  # when the summary appears (after the last point)
        self.slot = kit.PictureSlot(self.host, kit.clean(spec.get("picture_query"), 80))
        self._layouts: dict[bool, dict[str, Any]] = {}
        self.page_t = 0.0  # when the page last turned (its entrance animation)
        self._page = 0

    # ------------------------------------------------------------ layout
    def layout(self) -> dict[str, Any]:
        """Text sizes, wrapped lines per point (and its detail), block heights and pages, for the layout with
        or without a picture. The summary, when there is one, is the last block."""
        pic = self.slot.wanted
        lay = self._layouts.get(pic)
        if lay is None:
            size = SIZE_NARROW if pic else SIZE_WIDE
            text_x = kit.PANEL.x + kit.PAD + 2 * BADGE_R + 18
            right = kit.PANEL.right - kit.PAD - (PIC_W + kit.GUTTER if pic else 0)
            width = right - text_x
            lines = [kit.wrap(p, size, width)[:POINT_LINES] for p in self.points]
            detail = [kit.wrap(d, DETAIL_PX, width)[:DETAIL_LINES] if d else () for d in self.details]
            heights = [self._block_h(ls, ds, size) for ls, ds in zip(lines, detail)]
            counts = [len(ls) + len(ds) for ls, ds in zip(lines, detail)]
            summary: tuple[str, ...] = ()
            if self.summary:
                summary = kit.wrap(self.summary, SUMMARY_PX, right - (kit.PANEL.x + kit.PAD) - 40)[:2]
                heights.append(kit.line_h(SUMMARY_PX) * (len(summary) - 1) + art.font(SUMMARY_PX).get_height() + 24)
                counts.append(len(summary))
            room = kit.PANEL.h - 2 * kit.PAD
            pages = pages_by_height(heights, counts, gap=GAP, room=room)
            if len(pages) > 1:
                pages = pages_by_height(heights, counts, gap=GAP, room=room - DOTS_H)
            lay = {"size": size, "x": text_x, "width": width, "right": right, "lines": lines, "detail": detail,
                   "summary": summary, "heights": heights, "pages": pages}
            self._layouts[pic] = lay
        return lay

    @staticmethod
    def _block_h(lines: Sequence[str], detail: Sequence[str], size: int) -> int:
        h = max(2 * BADGE_R, kit.line_h(size) * (len(lines) - 1) + art.font(size).get_height())
        if detail:
            h += 6 + kit.line_h(DETAIL_PX) * (len(detail) - 1) + art.font(DETAIL_PX).get_height()
        return h

    @property
    def current(self) -> int:
        """Index of the newest point showing (-1 before the first)."""
        return self.shown - 1

    @property
    def summary_shown(self) -> bool:
        return bool(self.summary) and self.summary_at is not None and self.t >= self.summary_at

    def page_of(self, i: int) -> int:
        for n, page in enumerate(self.layout()["pages"]):
            if i in page:
                return n
        return 0

    # ------------------------------------------------------------ logic
    def update(self, dt: float) -> None:
        super().update(dt)
        self.slot.update(self.t)
        if self.shown < len(self.points) and self.t >= self.next_at:
            self._reveal(self.shown + 1, manual=False)
        newest = len(self.points) if self.summary_shown else max(0, self.current)
        page = self.page_of(newest)
        if page != self._page:
            self._page, self.page_t = page, self.t

    def _reveal(self, n: int, *, manual: bool) -> None:
        n = max(1, min(len(self.points), n))
        for i in range(self.shown, n):
            self.shown_at[i] = self.t
        self.shown = n
        self.next_at = self.t + (max(self.pace, HOLD_S) if manual else self.pace)
        if n < len(self.points):
            self.summary_at = None
        elif self.summary_at is None:
            self.summary_at = self.t + self.pace * SUMMARY_AFTER

    def command(self, action: str, args: dict[str, Any]) -> bool:
        if action == "next":
            if self.shown >= len(self.points):
                if self.summary and not self.summary_shown:
                    self.summary_at = self.t
                return True
            self._reveal(self.shown + 1, manual=False)
            self.next_at = self.t + self.pace
            return True
        if action == "prev":
            self._reveal(max(1, self.shown - 1), manual=True)
            return True
        if action == "point":
            n = kit.as_int(args.get("n"), 0)
            if not 1 <= n <= len(self.points):
                return False
            self._reveal(n, manual=True)
            return True
        if action == "all":
            self._reveal(len(self.points), manual=True)
            self.summary_at = self.t if self.summary else None
            return True
        return False

    def tap(self, x: float, y: float) -> bool:
        return self.command("prev" if x < art.W * 0.33 else "next", {})

    def key(self, name: str) -> bool:
        if name in ("right", "space", "return", "down"):
            return self.command("next", {})
        if name in ("left", "up", "backspace"):
            return self.command("prev", {})
        return False

    def describe(self) -> str:
        if self.current < 0:
            return f"{self.title}: about to show {len(self.points)} points"
        detail = self.details[self.current]
        said = f"{self.title}: point {self.shown} of {len(self.points)} showing: {self.points[self.current]}"
        said += f" ({detail})" if detail else ""
        return said + (f". In short: {self.summary}" if self.summary_shown else "")

    def status(self) -> dict[str, Any]:
        return {**super().status(), "point": self.shown, "points": len(self.points), "summary": self.summary_shown,
                "picture": "shown" if self.slot.shown else ("loading" if self.slot.loading else "none")}

    # ------------------------------------------------------------ drawing
    def draw(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=23)
        kit.header_title(surf, self.title)
        kit.panel(surf)
        lay = self.layout()
        pages = lay["pages"]
        page = pages[self._page] if self._page < len(pages) else []
        heights = [lay["heights"][i] for i in page]
        total = sum(heights) + GAP * max(0, len(heights) - 1)
        inner_top = kit.PANEL.y + kit.PAD
        inner_h = kit.PANEL.h - 2 * kit.PAD - (DOTS_H if len(pages) > 1 else 0)
        y = inner_top + max(0, (inner_h - total) // 2)
        for i, h in zip(page, heights):
            if i == len(self.points):
                if self.summary_shown:
                    self._draw_summary(surf, y, h, lay)
            elif i < self.shown:
                self._draw_point(surf, i, y, h, lay)
            y += h + GAP
        if self.slot.wanted:
            pic_rect = pygame.Rect(kit.PANEL.right - kit.PAD - PIC_W, kit.PANEL.y + kit.PAD, PIC_W,
                                   kit.PANEL.h - 2 * kit.PAD)
            self.slot.draw(surf, pic_rect, self.t)
        kit.page_dots(surf, len(pages), self._page, (kit.PANEL.centerx, kit.PANEL.bottom - 20))

    def _draw_point(self, surf: pygame.Surface, i: int, y: int, h: int, lay: dict[str, Any]) -> None:
        start = max(self.shown_at[i], self.page_t if self.page_of(i) == self._page else 0.0)
        k = art.ease_out_cubic(kit.appear(self.t, start))
        if k <= 0:
            return
        size = lay["size"]
        color = kit.ACCENTS[i % len(kit.ACCENTS)]
        newest = i == self.current and not self.summary_shown
        x = lay["x"]
        dx = int((1 - k) * 40)
        if newest:  # a soft band behind the newest point
            band = pygame.Rect(kit.PANEL.x + 12, y - 9, lay["right"] - kit.PANEL.x - 4, h + 18)
            kit.blit_alpha(surf, art.rounded_rect(band.w, band.h, 20, art.mix(color, art.WHITE, 0.78)),
                           band.topleft, k)
        first = min(h, kit.line_h(size))
        badge = kit.number_badge(i + 1, color, BADGE_R)
        pop = art.ease_out_back(kit.appear(self.t, start, 0.4))
        bx, by = kit.PANEL.x + kit.PAD + BADGE_R, y + first // 2 + 2
        art.blit_center(surf, art.scaled(badge, max(0.05, pop)), (bx, by))
        text = kit.text_block(lay["lines"][i], size, kit.TEXT)
        ty = y + max(0, (first - art.font(size).get_height()) // 2)
        kit.blit_alpha(surf, text, (x + dx, ty), k)
        if lay["detail"][i]:
            dy = ty + text.get_height() + 6
            kit.blit_alpha(surf, kit.text_block(lay["detail"][i], DETAIL_PX, kit.TEXT_SOFT), (x + dx, dy), k)

    def _draw_summary(self, surf: pygame.Surface, y: int, h: int, lay: dict[str, Any]) -> None:
        """The one-line takeaway after the last point, in a soft lemon band with a star."""
        k = art.ease_out_cubic(kit.appear(self.t, self.summary_at or 0.0))
        band = pygame.Rect(kit.PANEL.x + 12, y, lay["right"] - kit.PANEL.x - 4, h)
        kit.blit_alpha(surf, art.rounded_rect(band.w, band.h, 20, art.mix(art.LEMON, art.WHITE, 0.6)), band.topleft, k)
        star = art.star(15, art.inkier(art.TANGERINE, 0.1))
        kit.blit_alpha(surf, star, star.get_rect(center=(kit.PANEL.x + kit.PAD + BADGE_R, y + h // 2)).topleft, k)
        text = kit.text_block(lay["summary"], SUMMARY_PX, kit.TEXT)
        kit.blit_alpha(surf, text, (lay["x"], y + (h - text.get_height()) // 2), k)


async def _build(spec: dict[str, Any], ctx: Any, host: Any) -> Card | None:
    card = ExplainCard(spec, host)
    return card if card.points else None


register(CardType(
    kind="explain",
    description=(
        "Key points shown while you explain something (\"explain how rainbows form\", \"how do vaccines work\"). "
        "A title and 2-8 short points that appear one at a time as you talk, the newest highlighted; each point "
        "may have a short detail line under it, and the card may end with a one-line summary; optionally a "
        "picture beside them. Each point is a few words (at most 8), the gist of one step of your explanation, "
        "NOT a sentence you are saying. Say \"next\" (the action) when you move on to the next point."
    ),
    fields={
        "title": {"type": "string", "maxLength": 60,
                  "description": "What is explained, 2-5 words (\"How rainbows form\")"},
        "points": {"type": "array", "items": {"type": "string", "maxLength": 90}, "minItems": 2,
                   "maxItems": MAX_POINTS,
                   "description": "2-8 key points in order, each 2-8 words (\"Sunlight enters a raindrop\")"},
        "details": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": MAX_POINTS,
                    "description": "Optional: one short detail per point, in the same order, at most 10 words "
                                   "each (\"Light slows down in water\"); \"\" for a point without one"},
        "summary": {"type": "string", "maxLength": 100,
                    "description": "Optional: the takeaway in one line, at most 12 words, shown after the last "
                                   "point (\"Sunlight split by raindrops makes a rainbow\")"},
        "picture_query": {"type": "string", "maxLength": 80,
                          "description": "Optional: a picture to show beside the points, as a Wikipedia-style "
                                         "subject (\"rainbow\", \"water cycle\"). Leave out for abstract topics"},
        "pace": {"type": "number", "minimum": 1.5, "maximum": 15,
                 "description": f"Optional: seconds between points (default {PACE_S:g}); about how long you speak "
                                "per point"},
    },
    required=("title", "points"),
    actions={base.PICTURE_ACTION: base.PICTURE_ACTION_HELP,
             "next": "show the next point now (after the last: the summary)", "prev": "go back one point",
             "point": "show point n (1-based): {n}", "all": "show every point and the summary"},
    build=_build,
))

SAMPLES: list[dict[str, Any]] = [
    {"card": "explain", "title": "How rainbows form", "picture_query": "rainbow",
     "points": ["Sunlight enters a raindrop", "The drop bends the light", "Each colour bends differently",
                "The colours fan out into an arc"],
     "details": ["Light slows down in water", "", "Red bends least, violet most", "Always opposite the Sun"],
     "summary": "Sunlight split by raindrops makes a rainbow"},
    {"card": "explain", "title": "Why babies need naps",
     "points": ["Sleep helps the brain grow", "New skills are stored while sleeping", "Rested babies feed better",
                "Most 10-month-olds nap twice a day", "Watch for rubbing eyes and yawns"],
     "summary": "Naps are part of growing, not a break from it"},
    {"card": "explain", "title": "How vaccines work", "picture_query": "white blood cell",
     "points": ["A safe copy of a germ", "The body learns to fight it", "Memory cells remember it",
                "Real germ? Beaten fast"],
     "details": ["Weakened, dead or just a piece of it", "It makes antibodies", "Some for many years", ""]},
    {"card": "explain", "title": "How a seed becomes a plant", "pace": 2,  # the longest: two pages
     "points": ["The seed soaks up water and swells", "A tiny root pushes down into the soil",
                "A shoot climbs up towards the light", "The first leaves open and catch sunlight",
                "Leaves make food from light, air and water", "The stem grows taller and sprouts more leaves",
                "Flowers bloom and get visited by bees", "New seeds form inside the fruit"],
     "details": ["It needs warmth too", "", "", "", "This is called photosynthesis", "", "", ""],
     "summary": "Water, light and time turn a seed into a plant"},
]

__all__ = ["SAMPLES", "ExplainCard"]
