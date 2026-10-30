"""The game board card (kind "game_board", internal): a voice game's big title, its text or one or two options,
and a status line. Used by riddles (the riddle), would you rather (the two choices, tappable, with the tally) and
twenty questions (what LittleBot is thinking of and a question counter).

Shown by the voice games only (never offered to the AI); it follows the live game
(littlebot/screens/cards/_gamekit.py). A riddle's answer flashes green (solved) or gold (revealed); a
would-you-rather pick lights up its tile with the tally for a moment before the next pair.

Spec (JSON): title, text, options [0-2], status, picked (index | null), tally [a, b], counter [n, of],
result {id, answer, right} (riddles) | {id, options, picked, tally, mine} (would you rather) | null, over,
session.
"""

from __future__ import annotations

from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, CardHost, CardType, W, register
from littlebot.screens.cards import _gamekit as gk
from littlebot.screens.cards import _kit as kit

STATUS_RECT = pygame.Rect(12, 480 - 12 - 54, W - 24, 54)
BODY_RECT = pygame.Rect(12, gk.TOP, W - 24, STATUS_RECT.y - gk.TOP - 10)
TEXT_SIZES = (52, 48, 44, 40, 36, BODY_PX)
OPTION_SIZES = (44, 40, 36, BODY_PX)
OR_R = 34

SAMPLES = [
    {"card": "game_board", "title": "Riddle 2 of 5", "text": "What gets wetter the more it dries?", "options": [],
     "status": "Score 1  ·  say your answer, \"hint\" or \"give up\"", "result": None, "over": False},
    {"card": "game_board", "title": "Would you rather...", "text": "",
     "options": ["Be able to fly", "Be invisible"], "status": "Picked here: Flying 3  ·  Being invisible 1",
     "picked": None, "tally": [3, 1], "result": None, "over": False},
    {"card": "game_board", "title": "Twenty Questions", "text": "I'm thinking of an animal...", "options": [],
     "status": "Question 6 of 20", "counter": [6, 20], "result": None, "over": False},
]


def option_rects(n: int) -> list[pygame.Rect]:
    if n <= 1:
        return [BODY_RECT.inflate(-40, -40)]
    w = (BODY_RECT.w - 2 * OR_R - 24) // 2
    left = pygame.Rect(BODY_RECT.x, BODY_RECT.y + 8, w, BODY_RECT.h - 16)
    right = pygame.Rect(BODY_RECT.right - w, BODY_RECT.y + 8, w, BODY_RECT.h - 16)
    return [left, right]


class GameBoardCard(gk.GameCard):
    kind = "game_board"

    def update(self, dt: float) -> None:
        super().update(dt)
        if self.flashing and self.flash:
            right = self.flash.get("right")
            self.mood = "happy" if right or "picked" in self.flash else "surprised"
        elif self.view.get("over"):
            self.mood = "happy"
        else:
            self.mood = "curious"

    def tap(self, x: float, y: float) -> bool:
        options = self.view.get("options") or []
        if len(options) < 2:
            return False
        for i, r in enumerate(option_rects(len(options))):
            if r.collidepoint(x, y):
                return self.press(i)
        return False

    def key(self, name: str) -> bool:
        if len(self.view.get("options") or []) >= 2 and name.lower() in ("1", "2", "left", "right"):
            return self.press(0 if name.lower() in ("1", "left") else 1)
        return False

    def describe(self) -> str:
        v = self.view
        parts = [str(v.get("title") or "")]
        if v.get("text"):
            parts.append(str(v["text"]))
        if v.get("options"):
            parts.append(" or ".join(str(o) for o in v["options"]))
        if v.get("status"):
            parts.append(str(v["status"]))
        return ": ".join(p for p in parts if p)[:200]

    # ------------------------------------------------------------ frames
    def draw(self, surf: pygame.Surface) -> None:
        gk.background(surf)
        v = self.view
        counter = v.get("counter")
        right = W - 14
        if isinstance(counter, (list, tuple)) and len(counter) == 2 and not v.get("over"):
            right = gk.chips_right(surf, [gk.chip(f"{kit.as_int(counter[0])} / {kit.as_int(counter[1])}",
                                                  size=BODY_PX + 4)])
        gk.title(surf, str(v.get("title") or ""), right=right)
        f = self.flash if self.flashing else None
        if f is not None and "options" in f:
            self._options(surf, list(f.get("options") or []), picked=f.get("picked"), tally=f.get("tally"),
                          mine=f.get("mine"))
        elif f is not None:
            self._answer(surf, f)
        elif v.get("options"):
            self._options(surf, list(v.get("options") or []), picked=self.pressed, tally=None, mine=None)
        else:
            self._text(surf, str(v.get("text") or ""), counter if not v.get("over") else None)
        status = str(v.get("status") or "") if f is None else ""  # the next one's status waits for it
        if status:
            gk.blit_text_in(surf, status, STATUS_RECT, (BODY_PX,), 1, gk.INK_SOFT)

    def _text(self, surf: pygame.Surface, s: str, counter: Any) -> None:
        intro = art.ease_out_cubic(kit.appear(self.t, self.changed_at, 0.35))
        r = BODY_RECT.move(0, int((1 - intro) * 16))
        gk.box(surf, r, gk.PANEL_HI, radius=26)
        inner = r.inflate(-44, -36)
        if isinstance(counter, (list, tuple)) and len(counter) == 2:
            n, of = kit.as_int(counter[0]), max(1, kit.as_int(counter[1], 20))
            bar = pygame.Rect(inner.x, inner.bottom - 22, inner.w, 22)
            pygame.draw.rect(surf, gk.PANEL, bar, border_radius=11)
            fill = bar.copy()
            fill.w = max(22, int(bar.w * min(1.0, n / of))) if n else 0
            if fill.w:
                pygame.draw.rect(surf, gk.GOLD if n >= of else gk.CYAN, fill, border_radius=11)
            inner.h -= 40
        gk.blit_text_in(surf, s, inner, TEXT_SIZES, 4)

    def _options(self, surf: pygame.Surface, options: list[str], *, picked: Any, tally: Any, mine: Any) -> None:
        rects = option_rects(len(options))
        for i, (opt, r) in enumerate(zip(options, rects)):
            fill, border = gk.PANEL_HI, gk.BORDER
            if picked == i:
                fill, border = art.mix(gk.PANEL_HI, (gk.LETTER_COLORS[1], gk.LETTER_COLORS[0])[i % 2], 0.55), gk.CYAN
            gk.box(surf, r, fill, border=border, radius=26, border_w=4 if picked == i else 3)
            area = r.inflate(-36, -40)
            if isinstance(tally, (list, tuple)) and len(tally) == 2 and sum(int(x) for x in tally):
                area.h -= 50
                total = sum(int(x) for x in tally)
                share = int(tally[i]) / total
                bar = pygame.Rect(r.x + 20, r.bottom - 48, r.w - 40, 26)
                pygame.draw.rect(surf, gk.PANEL, bar, border_radius=13)
                fill_bar = bar.copy()
                fill_bar.w = max(26, int(bar.w * share)) if share else 0
                if fill_bar.w:
                    pygame.draw.rect(surf, gk.GOLD, fill_bar, border_radius=13)
                n = gk.text([str(int(tally[i]))], BODY_PX, gk.INK)
                surf.blit(n, n.get_rect(midbottom=(r.centerx, bar.y - 2)))
                area.h -= 30
            gk.blit_text_in(surf, opt, area, OPTION_SIZES, 4)
            if mine == i:
                tag = art.pill("LittleBot too!" if picked == i else "LittleBot", 26, gk.INK, gk.PANEL, pad=(12, 5),
                               border=gk.CYAN)
                surf.blit(tag, tag.get_rect(midtop=(r.centerx, r.y + 10)))
        if len(options) == 2:
            mid = (rects[0].right + rects[1].left) // 2
            badge = art.circle(OR_R, gk.CYAN)
            surf.blit(badge, badge.get_rect(center=(mid, BODY_RECT.centery)))
            t = art.text_outlined("OR", 34, gk.INK, (10, 40, 70), 2)
            surf.blit(t, t.get_rect(center=(mid, BODY_RECT.centery)))

    def _answer(self, surf: pygame.Surface, f: dict[str, Any]) -> None:
        right = bool(f.get("right"))
        r = BODY_RECT
        gk.box(surf, r, gk.GOOD if right else art.mix(gk.GOLD, gk.PANEL, 0.35), border=None, radius=26)
        head = art.text_outlined("You got it!" if right else "The answer:", 50, gk.INK, (10, 16, 40), 3)
        surf.blit(head, head.get_rect(midtop=(r.centerx, r.y + 22)))
        gk.blit_text_in(surf, str(f.get("answer") or ""), pygame.Rect(r.x + 24, r.y + 90, r.w - 48, r.h - 110),
                        TEXT_SIZES, 3)


async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> GameBoardCard:
    return await gk.build_for(GameBoardCard, spec, ctx, host)  # type: ignore[return-value]


register(CardType(
    kind="game_board",
    description="A voice game's board: a title, its text or one or two options, a status line (voice games only).",
    fields={"title": {"type": "string"}, "text": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}}, "status": {"type": "string"},
            "session": {"type": "string"}},
    build=build,
    offered=False,
))
