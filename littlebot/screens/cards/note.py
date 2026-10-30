"""The note card: a short message, big ("Dinner's ready!", "Back in 5 minutes", a phone number, a quote).

Sent from the phone (the dashboard's Assistant page: "send to the screen"), or shown by the brain when the words
themselves are the point (a name to remember, a number to copy, a message for someone). A title and up to five
lines of text, as big as they fit; optionally a picture beside them. Lingers like any card; a tap closes it.
"""

from __future__ import annotations

from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, TITLE_PX, Card, CardHost, CardType, register
from littlebot.screens.cards import _kit as kit

TEXT_SIZES = (64, 54, 46, 40, BODY_PX + 4, BODY_PX)


class NoteCard(Card):
    kind = "note"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None) -> None:
        super().__init__(spec, host)
        self.title = kit.clean(spec.get("title"), 60) or "Note"
        self.text = kit.clean(spec.get("text"), 400)
        self.mood = "happy"
        query = kit.clean(spec.get("picture_query"), 80)
        url = str(spec.get("picture_url") or "")[:500]
        self.picture = self.host.picture(query, url) if (query or url.startswith("https://")) else None

    def draw(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=23)
        kit.header_title(surf, self.title, size=TITLE_PX)
        kit.panel(surf)
        box = kit.PANEL.inflate(-2 * kit.PAD, -2 * kit.PAD)
        pic = getattr(self.picture, "surface", None) if self.picture is not None else None
        if pic is not None:
            side = min(box.h, box.w // 2 - 10)
            img = art.scaled(pic, min(side / max(1, pic.get_width()), side / max(1, pic.get_height())))
            surf.blit(img, img.get_rect(midright=box.midright))
            box.w -= side + 18
        if not self.text:
            return
        size, lines = kit.fit(self.text, TEXT_SIZES, box.w)
        img = kit.text_block(list(lines)[:6], size, kit.TEXT, align="center")
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
