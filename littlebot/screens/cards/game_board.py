"""The game board card (kind "game_board", internal): a voice game's big title, its text or one or two options,
and a status line. Used by riddles (the riddle), would you rather (the two choices, tappable, with the tally) and
twenty questions (what LittleBot is thinking of and a question counter).

Shown by the voice games only (never offered to the AI); it follows the live game
(littlebot/screens/cards/_gamekit.py). A riddle's answer flashes green (solved) or gold (revealed); a
would-you-rather pick lights up its tile with the tally for a moment before the next pair.

On the 1024x600 screen: the text as big as fits (a short riddle at 96 px, a long one in up to five lines) in a
panel over the game area, twenty questions' counter as a pip per question under it; would you rather's two
choices as tall tiles (about 430 x 380 px) either side of an "OR"; the question number as a chip beside the
title; the status line under the board; at the end what it was and how it went, and how to play again.

Spec (JSON): title, text, options [0-2], status, picked (index | null), tally [a, b], counter [n, of],
result {id, answer, right} (riddles) | {id, options, picked, tally, mine} (would you rather) | null, over,
session.
"""

from __future__ import annotations

from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, CardHost, CardType, register
from littlebot.screens.cards import _gamekit as gk
from littlebot.screens.cards import _kit as kit

# The board over the game area, the status line under it (how to play, the score so far).
STATUS_H = 56
STATUS_RECT = pygame.Rect(gk.AREA.x, gk.AREA.bottom - STATUS_H, gk.AREA.w, STATUS_H)
BODY_RECT = pygame.Rect(gk.AREA.x, gk.AREA.y, gk.AREA.w, STATUS_RECT.y - gk.GAP - gk.AREA.y)
TEXT_SIZES = (96, 88, 80, 72, 64, 60, 56, 52, 48, 44, 40, BODY_PX)
TEXT_LINES = 5
OPTION_SIZES = (72, 64, 60, 56, 52, 48, 44, 40, BODY_PX)
OPTION_LINES = 4
ANSWER_SIZES = (88, 80, 72, 64, 56, 52, 48, 44, 40, BODY_PX)
STATUS_SIZES = (40, BODY_PX)
END_SIZES = (64, 60, 56, 52, 48, 44, 40, BODY_PX)  # how a game went, under its end
OR_R = 46
TAG_H = 44  # the "LittleBot" tag on would-you-rather's tile
BAR_H = 30  # the tally's bar...
COUNT_H = 46  # ...and its count
PIP_H = 18  # twenty questions: a pip per question asked

SAMPLES = [
    {"card": "game_board", "title": "Riddle 2 of 5", "text": "What gets wetter the more it dries?", "options": [],
     "status": "Score 1  ·  say your answer, \"hint\" or \"give up\"", "result": None, "over": False},
    {"card": "game_board", "title": "Would you rather...", "text": "",
     "options": ["Be able to fly", "Be invisible"], "status": "Picked here: Flying 3  ·  Being invisible 1",
     "picked": None, "tally": [3, 1], "number": 2, "total": 5, "result": None, "over": False},
    {"card": "game_board", "title": "Twenty Questions", "text": "I'm thinking of an animal...", "options": [],
     "status": "Question 6 of 20", "counter": [6, 20], "result": None, "over": False},
]


def option_rects(n: int) -> list[pygame.Rect]:
    """Where the options are: two tall tiles side by side with the "OR" between them (each about 430 x 380 px)."""
    if n <= 1:
        return [BODY_RECT.inflate(-40, -40)]
    w = (BODY_RECT.w - 2 * OR_R - 2 * gk.GAP) // 2
    left = pygame.Rect(BODY_RECT.x, BODY_RECT.y, w, BODY_RECT.h)
    right = pygame.Rect(BODY_RECT.right - w, BODY_RECT.y, w, BODY_RECT.h)
    return [left, right]


def _pair(value: Any) -> tuple[int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return kit.as_int(value[0]), kit.as_int(value[1])
    return None


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
        over = bool(v.get("over"))
        counter = _pair(v.get("counter"))
        right = None
        if not over:  # how far into the round: twenty questions' counter, else question n of total
            shown = counter or (_pair((v.get("number"), v.get("total"))) if v.get("options") and v.get("total")
                                else None)
            if shown is not None:
                right = gk.chips_right(surf, [gk.chip(f"{shown[0]} / {shown[1]}")])
        gk.title(surf, str(v.get("title") or ""), right=right)
        f = self.flash if self.flashing else None
        if f is not None and "options" in f:
            self._options(surf, list(f.get("options") or []), picked=f.get("picked"), tally=f.get("tally"),
                          mine=f.get("mine"))
        elif f is not None:
            self._answer(surf, f)
            return  # the answer takes the whole area; the next riddle's status waits for it
        elif v.get("options"):
            self._options(surf, list(v.get("options") or []), picked=self.pressed, tally=None, mine=None)
        elif over:
            self._end(surf, str(v.get("text") or ""), str(v.get("status") or ""))
            gk.blit_text_in(surf, gk.PLAY_AGAIN, STATUS_RECT, STATUS_SIZES, 1, gk.INK_SOFT)
            return
        else:
            self._text(surf, str(v.get("text") or ""), counter)
        status = str(v.get("status") or "") if f is None else ""  # the next one's status waits for it
        if status:
            gk.blit_text_in(surf, status, STATUS_RECT, STATUS_SIZES, 1, gk.INK_SOFT)

    def _text(self, surf: pygame.Surface, s: str, counter: tuple[int, int] | None) -> None:
        intro = art.ease_out_cubic(kit.appear(self.t, self.changed_at, 0.35))
        r = BODY_RECT.move(0, int((1 - intro) * 16))
        gk.box(surf, r, gk.PANEL_HI, radius=28)
        inner = r.inflate(-64, -48)
        if counter is not None:  # a pip per question asked
            n, of = counter
            of = max(1, of or 20)
            strip = gk.progress(inner.w, n, of, current=False, h=PIP_H, fill=gk.GOLD if n >= of else gk.CYAN)
            surf.blit(strip, (inner.x, inner.bottom - PIP_H))
            inner.h -= PIP_H + 24
        gk.blit_text_in(surf, s, inner, TEXT_SIZES, TEXT_LINES)

    def _end(self, surf: pygame.Surface, s: str, status: str) -> None:
        """The game is over: what it was (twenty questions' secret, else a thank you) big, how it went under it in
        gold."""
        gk.box(surf, BODY_RECT, gk.PANEL_HI, border=gk.GOLD, radius=28, border_w=4)
        inner = BODY_RECT.inflate(-64, -48)
        s = s or "Thanks for playing!"
        size, lines = gk.fitted(status, END_SIZES, inner.w, 2) if status else (BODY_PX, [])
        low = gk.block_h(size, len(lines)) + 24 if lines else 0
        top = pygame.Rect(inner.x, inner.y, inner.w, inner.h - low)
        gk.blit_text_in(surf, s, top, TEXT_SIZES, TEXT_LINES)
        if lines:
            img = gk.text(lines, size, gk.GOLD)
            surf.blit(img, img.get_rect(midbottom=(inner.centerx, inner.bottom)))

    def _options(self, surf: pygame.Surface, options: list[str], *, picked: Any, tally: Any, mine: Any) -> None:
        rects = option_rects(len(options))
        votes = _pair(tally)
        total = sum(votes) if votes else 0
        tagged = mine in (0, 1)  # what LittleBot would pick: a tag on its tile (room kept on both, so they line up)
        areas = []
        for r in rects:
            area = r.inflate(-48, -48)
            if tagged:
                area.y = r.y + 18 + TAG_H + 12
            bottom = r.bottom - 28 - BAR_H - 6 - COUNT_H - 12 if votes and total else r.bottom - 24
            area.h = bottom - area.y
            areas.append(area)
        sizes = gk.common_sizes(options, OPTION_SIZES, areas, OPTION_LINES)
        for i, (opt, r, area) in enumerate(zip(options, rects, areas)):
            fill, border, width = gk.PANEL_HI, gk.BORDER, 3
            if picked == i:
                fill, border, width = art.mix(gk.PANEL_HI, (gk.LETTER_COLORS[1], gk.LETTER_COLORS[0])[i % 2], 0.55), \
                    gk.CYAN, 5
            gk.box(surf, r, fill, border=border, radius=28, border_w=width)
            if tagged and mine == i:
                tag = art.pill("LittleBot too!" if picked == i else "LittleBot", gk.CHIP_PX, gk.INK, gk.PANEL,
                               pad=(16, 6), border=gk.CYAN)
                surf.blit(tag, tag.get_rect(midtop=(r.centerx, r.y + 18)))
            if votes and total and i < 2:  # how many picked it here: a count over a gold bar
                bar = pygame.Rect(r.x + 28, r.bottom - 28 - BAR_H, r.w - 56, BAR_H)
                pygame.draw.rect(surf, gk.PANEL, bar, border_radius=BAR_H // 2)
                share = votes[i] / total
                if share:
                    fill_bar = bar.copy()
                    fill_bar.w = max(BAR_H, int(bar.w * share))
                    pygame.draw.rect(surf, gk.GOLD, fill_bar, border_radius=BAR_H // 2)
                n = art.text_outlined(str(votes[i]), 52, gk.INK, gk.OUTLINE, 2)
                surf.blit(n, n.get_rect(midbottom=(r.centerx, bar.y - 6)))
            gk.blit_text_in(surf, opt, area, sizes, OPTION_LINES)
        if len(options) == 2:
            mid = (rects[0].right + rects[1].left) // 2
            badge = art.circle(OR_R, gk.CYAN)
            surf.blit(badge, badge.get_rect(center=(mid, BODY_RECT.centery)))
            t = art.text_outlined("OR", 44, gk.INK, (10, 40, 70), 2)
            surf.blit(t, t.get_rect(center=(mid, BODY_RECT.centery)))

    def _answer(self, surf: pygame.Surface, f: dict[str, Any]) -> None:
        right = bool(f.get("right"))
        r = gk.AREA
        if right:  # solved: green; given up or out of tries: the answer on the board, framed in gold
            gk.box(surf, r, gk.GOOD_BOX, border=art.lighten(gk.GOOD, 0.3), radius=28, border_w=5)
        else:
            gk.box(surf, r, gk.PANEL_HI, border=gk.GOLD, radius=28, border_w=5)
        head = art.text_outlined("You got it!" if right else "The answer:", 64, gk.INK if right else gk.GOLD,
                                 gk.OUTLINE, 3)
        hr = head.get_rect(midtop=(r.centerx, r.y + 28))
        surf.blit(head, hr)
        room = pygame.Rect(r.x + 40, hr.bottom + 16, r.w - 80, r.bottom - 32 - hr.bottom - 16)
        gk.blit_text_in(surf, str(f.get("answer") or ""), room, ANSWER_SIZES, 4)


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
