"""The note card: a short message, big ("Dinner's ready!", "Back in 5 minutes", a phone number, a quote).

Sent from the phone (the dashboard's Assistant page: "send to the screen"), or shown by the brain when the words
themselves are the point (a name to remember, a number to copy, a message for someone). A title and the message as
big as it fits the panel (up to 96 px for a few words, never under BODY_PX, at most MAX_LINES lines); optionally a
picture in a column beside it, left out when the message needs the whole width. Lingers like any card; a tap closes
it.
"""

from __future__ import annotations

import functools
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, MAX_LINES, TITLE_PX, Card, CardHost, CardType, register
from littlebot.screens.cards import _kit as kit

TEXT_SIZES = (96, 84, 72, 64, 56, TITLE_PX, 46, 42, BODY_PX)
INNER = kit.PANEL.inflate(-2 * kit.PAD, -2 * kit.PAD)  # 940 x 402
WORDS_BESIDE = pygame.Rect(INNER.x, INNER.y, INNER.w - kit.PIC_W - kit.GUTTER, INNER.h)  # beside a picture
PICTURE = pygame.Rect(INNER.right - kit.PIC_W, INNER.y, kit.PIC_W, INNER.h)


@functools.lru_cache(maxsize=64)
def note_layout(text: str, width: int, height: int) -> tuple[int, tuple[str, ...]] | None:
    """The biggest size at which ``text`` fits ``width`` x ``height`` in at most MAX_LINES lines, with its lines;
    None when it doesn't fit even at BODY_PX."""
    for size in TEXT_SIZES:
        lines = kit.wrap(text, size, width)
        if len(lines) <= MAX_LINES and kit.line_h(size) * (len(lines) - 1) + art.font(size).get_height() <= height:
            return size, _balanced(text, size, width, len(lines))
    return None


def _balanced(text: str, size: int, width: int, n: int) -> tuple[str, ...]:
    """``text`` on ``n`` lines of about the same length (no lone word on the last line of a centred message): wrapped
    to the narrowest width that still takes ``n`` lines."""
    lo, hi = width // 3, width
    while lo < hi:
        mid = (lo + hi) // 2
        if len(kit.wrap(text, size, mid)) <= n:
            hi = mid
        else:
            lo = mid + 1
    return kit.wrap(text, size, hi)


class NoteCard(Card):
    kind = "note"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None) -> None:
        super().__init__(spec, host)
        self.title = kit.clean(spec.get("title"), 60) or "Note"
        self.text = kit.clean(spec.get("text"), 400)
        self.mood = "happy"
        query = kit.clean(spec.get("picture_query"), 80)
        url = str(spec.get("picture_url") or "")[:500]
        if not url.startswith("https://"):
            url = ""
        self.slot = kit.PictureSlot(self.host, query, url)
        self.picture = self.slot.picture
        # beside a picture only when the words still fit there; else they get the whole panel
        self._beside = note_layout(self.text, WORDS_BESIDE.w, WORDS_BESIDE.h) if self.text else None
        self._wide = self._fit_wide()

    def _fit_wide(self) -> tuple[int, tuple[str, ...]]:
        got = note_layout(self.text, INNER.w, INNER.h) if self.text else None
        if got is not None:
            return got
        lines = kit.wrap(self.text, BODY_PX, INNER.w)  # more than the panel holds: cut short with "..."
        return BODY_PX, (*lines[:MAX_LINES - 1], kit.ellipsize(" ".join(lines[MAX_LINES - 1:]), BODY_PX, INNER.w))

    def update(self, dt: float) -> None:
        super().update(dt)
        self.slot.update(self.t)

    def draw(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=23)
        kit.header_title(surf, self.title, size=TITLE_PX)
        kit.panel(surf)
        pic = self.slot.wanted and self._beside is not None
        box = WORDS_BESIDE if pic else INNER
        if pic:
            self.slot.draw(surf, PICTURE, self.t)
        if not self.text:
            return
        size, lines = self._beside if pic and self._beside is not None else self._wide
        img = kit.text_block(list(lines), size, kit.TEXT, align="center")
        k = art.ease_out_back(kit.appear(self.t, 0.0, 0.45))
        art.blit_center(surf, art.scaled(img, max(0.05, k)), box.center)

    def tap(self, x: float, y: float) -> bool:
        self.done = True
        return True

    def describe(self) -> str:
        return f"a note: {self.title}" + (f" ({self.text[:80]})" if self.text else "")


async def _build(spec: dict[str, Any], ctx: Any, host: CardHost) -> Card | None:
    if not (kit.clean(spec.get("text")) or kit.clean(spec.get("title"))):
        return None
    return NoteCard(spec, host)


register(CardType(
    kind="note",
    description=("A short message shown big, when the words themselves are the point: a name or number to remember "
                 "or copy, a message for someone, a quote, a short reminder (not for explanations: use explain)."),
    fields={"title": {"type": "string", "description": "2-5 words"},
            "text": {"type": "string", "description": "the message, at most about 25 words"},
            "picture_query": {"type": "string", "description": "optional: a picture to show beside it"}},
    build=_build,
    required=("text",),
))
