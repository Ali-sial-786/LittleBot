"""The "explain" card: the key points of an explanation, shown one at a time while the voice explains.

"Explain how rainbows form": the title, then 2-5 short points ("Sunlight enters a raindrop", "The
drop bends the light", "Each colour bends differently"), each a few words, never the sentences the
voice is saying. Points appear at a gentle pace (``pace`` seconds apart, about how long the voice
takes per point) and "next" shows the next one at once; the newest point is highlighted. More points
than fit on the screen are paged: the page turns when the newest point is on the next one. An
optional picture (``picture_query``) sits beside the points; without one the points use the whole
width.
"""

from __future__ import annotations

from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import MAX_LINES, Card, CardType, register
from littlebot.screens.cards import _kit as kit

MAX_POINTS = 8
PACE_S = 4.0  # seconds between points by default
FIRST_AT = 0.7  # the first point appears this long after the card
HOLD_S = 12.0  # after "prev" or a jump, wait this long before revealing more on its own
SIZE_WIDE = 42  # point text without a picture
SIZE_NARROW = 36  # ...and beside one
BADGE_R = 22
PIC_W = 262


class ExplainCard(Card):
    kind = "explain"

    def __init__(self, spec: dict[str, Any], host: Any = None) -> None:
        super().__init__(spec, host)
        self.title = kit.clean(spec.get("title"), 60) or "Here's how"
        self.points = kit.str_list(spec.get("points"), max_items=MAX_POINTS, limit=90)
        try:
            self.pace = max(1.5, min(15.0, float(spec.get("pace") or PACE_S)))
        except (TypeError, ValueError):
            self.pace = PACE_S
        self.mood = "curious"
        self.shown = 0  # how many points are showing (the newest one is highlighted)
        self.next_at = FIRST_AT
        self.shown_at: list[float] = [0.0] * len(self.points)
        self.slot = kit.PictureSlot(self.host, kit.clean(spec.get("picture_query"), 80))
        self._layouts: dict[bool, dict[str, Any]] = {}
        self.page_t = 0.0  # when the page last turned (its entrance animation)
        self._page = 0

    # ------------------------------------------------------------ layout
    def layout(self) -> dict[str, Any]:
        """Text size, wrapped lines per point and pages, for the layout with or without a picture."""
        pic = self.slot.wanted
        lay = self._layouts.get(pic)
        if lay is None:
            size = SIZE_NARROW if pic else SIZE_WIDE
            text_x = kit.PANEL.x + kit.PAD + 2 * BADGE_R + 16
            right = kit.PANEL.right - kit.PAD - (PIC_W + 18 if pic else 0)
            width = right - text_x
            lines = [kit.wrap(p, size, width) for p in self.points]
            lines = [ls if len(ls) <= MAX_LINES else ls[:MAX_LINES] for ls in lines]
            lay = {"size": size, "x": text_x, "width": width, "lines": lines,
                   "pages": kit.paged([len(ls) for ls in lines])}
            self._layouts[pic] = lay
        return lay

    @property
    def current(self) -> int:
        """Index of the newest point showing (-1 before the first)."""
        return self.shown - 1

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
        page = self.page_of(max(0, self.current))
        if page != self._page:
            self._page, self.page_t = page, self.t

    def _reveal(self, n: int, *, manual: bool) -> None:
        n = max(1, min(len(self.points), n))
        for i in range(self.shown, n):
            self.shown_at[i] = self.t
        self.shown = n
        self.next_at = self.t + (max(self.pace, HOLD_S) if manual else self.pace)

    def command(self, action: str, args: dict[str, Any]) -> bool:
        if action == "next":
            if self.shown >= len(self.points):
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
        return f"{self.title}: point {self.shown} of {len(self.points)} showing: {self.points[self.current]}"

    def status(self) -> dict[str, Any]:
        return {**super().status(), "point": self.shown, "points": len(self.points),
                "picture": "shown" if self.slot.shown else ("loading" if self.slot.loading else "none")}

    # ------------------------------------------------------------ drawing
    def draw(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=23)
        kit.header_title(surf, self.title)
        kit.panel(surf)
        lay = self.layout()
        pages = lay["pages"]
        page = pages[self._page] if self._page < len(pages) else []
        size = lay["size"]
        pitch = kit.line_h(size)
        heights = [max(2 * BADGE_R, pitch * (len(lay["lines"][i]) - 1) + art.font(size).get_height()) for i in page]
        gap = 22
        total = sum(heights) + gap * max(0, len(heights) - 1)
        inner_top = kit.PANEL.y + kit.PAD
        inner_h = kit.PANEL.h - 2 * kit.PAD - (16 if len(pages) > 1 else 0)
        y = inner_top + max(0, (inner_h - total) // 2)
        for i, h in zip(page, heights):
            if i < self.shown:
                self._draw_point(surf, i, y, h, lay)
            y += h + gap
        if self.slot.wanted:
            pic_rect = pygame.Rect(kit.PANEL.right - kit.PAD - PIC_W, kit.PANEL.y + kit.PAD, PIC_W,
                                   kit.PANEL.h - 2 * kit.PAD)
            self.slot.draw(surf, pic_rect, self.t)
        kit.page_dots(surf, len(pages), self._page, (kit.PANEL.centerx, kit.PANEL.bottom - 18))

    def _draw_point(self, surf: pygame.Surface, i: int, y: int, h: int, lay: dict[str, Any]) -> None:
        start = max(self.shown_at[i], self.page_t if self.page_of(i) == self._page else 0.0)
        k = art.ease_out_cubic(kit.appear(self.t, start))
        if k <= 0:
            return
        size = lay["size"]
        color = kit.ACCENTS[i % len(kit.ACCENTS)]
        newest = i == self.current
        x = lay["x"]
        dx = int((1 - k) * 36)
        if newest:  # a soft band behind the newest point
            band = pygame.Rect(kit.PANEL.x + 10, y - 9, x + lay["width"] - kit.PANEL.x - 2, h + 18)
            kit.blit_alpha(surf, art.rounded_rect(band.w, band.h, 18, art.mix(color, art.WHITE, 0.78)),
                           band.topleft, k)
        badge = kit.number_badge(i + 1, color, BADGE_R)
        pop = art.ease_out_back(kit.appear(self.t, start, 0.4))
        bx, by = kit.PANEL.x + kit.PAD + BADGE_R, y + min(h, kit.line_h(size)) // 2 + 2
        art.blit_center(surf, art.scaled(badge, max(0.05, pop)), (bx, by))
        text = kit.text_block(lay["lines"][i], size, kit.TEXT)
        kit.blit_alpha(surf, text, (x + dx, y + max(0, (min(h, kit.line_h(size)) - art.font(size).get_height()) // 2)),
                       k)


async def _build(spec: dict[str, Any], ctx: Any, host: Any) -> Card | None:
    card = ExplainCard(spec, host)
    return card if card.points else None


register(CardType(
    kind="explain",
    description=(
        "Key points shown while you explain something (\"explain how rainbows form\", \"how do vaccines work\"). "
        "A title and 2-5 short points that appear one at a time as you talk, the newest highlighted; optionally "
        "a picture beside them. Each point is a few words (at most 8), the gist of one step of your "
        "explanation, NOT a sentence you are saying. Say \"next\" (the action) when you move on to the next point."
    ),
    fields={
        "title": {"type": "string", "description": "What is explained, 2-5 words (\"How rainbows form\")"},
        "points": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5,
                   "description": "2-5 key points in order, each 2-8 words (\"Sunlight enters a raindrop\")"},
        "picture_query": {"type": "string",
                          "description": "Optional: a picture to show beside the points, as a Wikipedia-style "
                                         "subject (\"rainbow\", \"water cycle\"). Leave out for abstract topics"},
        "pace": {"type": "number", "description": f"Optional: seconds between points (default {PACE_S:g}); "
                                                  "about how long you speak per point"},
    },
    required=("title", "points"),
    actions={"next": "show the next point now", "prev": "go back one point", "point": "show point n (1-based): {n}",
             "all": "show every point"},
    build=_build,
))

SAMPLES: list[dict[str, Any]] = [
    {"card": "explain", "title": "How rainbows form", "picture_query": "rainbow",
     "points": ["Sunlight enters a raindrop", "The drop bends the light", "Each colour bends differently",
                "The colours fan out into an arc"]},
    {"card": "explain", "title": "Why babies need naps",
     "points": ["Sleep helps the brain grow", "New skills are stored while sleeping", "Rested babies feed better",
                "Most 10-month-olds nap twice a day", "Watch for rubbing eyes and yawns"]},
    {"card": "explain", "title": "How vaccines work", "picture_query": "white blood cell",
     "points": ["A safe copy of a germ", "The body learns to fight it", "Memory cells remember it",
                "Real germ? Beaten fast"]},
]

__all__ = ["SAMPLES", "ExplainCard"]
