"""The quiz card (kind "quiz", internal): a trivia question big, four tappable option tiles A to D.

Shown by the voice games (littlebot/games/voice/trivia.py), never offered to the AI. It follows the live game
(littlebot/screens/cards/_gamekit.py): a new question slides in; an answer flashes the right tile green (and a
wrong pick red) for a moment, with the fact's question still showing, before the next question; a fifty-fifty
dims two tiles; the end shows the final score. A tap on a tile answers (the game speaks the result).

On the 1024x600 screen: the question across the top (as big as it fits, up to four lines) with the round's
progress, a pip per question, under it; the answers as a 2 x 2 grid of big tiles (about 490 x 120 px each, all at
one text size), a tick on the right one and a cross on a wrong pick; the streak, the question number and the points
as chips beside the title; at the end stars, the score big, the points and the best streak.

Spec (JSON): question, options [4], number, total, score, right, streak, category, removed [indexes],
reveal {id, question, options, chosen, answer, right} | null, over, final {right, total, points, streak},
session (the game's), title.
"""

from __future__ import annotations

from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, CardHost, CardType, register
from littlebot.screens.cards import _gamekit as gk
from littlebot.screens.cards import _kit as kit

LETTERS = "ABCD"
# The question over the full width, the four answers as a 2 x 2 grid of big tiles under it (each about 490 x 120
# px on the 1024x600 canvas: an easy tap).
Q_RECT = pygame.Rect(gk.AREA.x, gk.AREA.y, gk.AREA.w, 186)
PROGRESS_H = 12  # the round's progress, a pip per question, along the bottom of the question box
TILE_W = (gk.AREA.w - gk.GAP) // 2
TILE_H = (gk.AREA.bottom - Q_RECT.bottom - 2 * gk.GAP) // 2
ROWS = (Q_RECT.bottom + gk.GAP, Q_RECT.bottom + 2 * gk.GAP + TILE_H)
COLS = (gk.AREA.x, gk.AREA.right - TILE_W)
BADGE_R = 32
Q_SIZES = (64, 60, 56, 52, 48, 44, 40, BODY_PX)
Q_LINES = 4  # a model's question is up to 200 characters
OPT_SIZES = (52, 48, 44, 40, BODY_PX)
OPT_LINES = 3  # an option is up to 60 characters

SAMPLES = [
    {"card": "quiz", "title": "Trivia", "category": "Science", "number": 3, "total": 10, "score": 25, "right": 2,
     "streak": 2, "question": "What is the largest planet in our solar system?",
     "options": ["Saturn", "Jupiter", "Neptune", "Earth"], "removed": [], "reveal": None, "over": False},
    {"card": "quiz", "title": "Trivia", "category": "History", "number": 10, "total": 10, "score": 85, "right": 7,
     "streak": 0, "question": "", "options": [], "over": True,
     "final": {"right": 7, "total": 10, "points": 85, "streak": 4}},
]


def tile_rect(i: int) -> pygame.Rect:
    return pygame.Rect(COLS[i % 2], ROWS[i // 2], TILE_W, TILE_H)


def text_area(tile: pygame.Rect, right: int = 0) -> pygame.Rect:
    """Where an answer's words go on its tile: right of the letter badge (and ``right`` px short of the end)."""
    x = tile.x + 20 + 2 * BADGE_R + 18
    return pygame.Rect(x, tile.y + 10, tile.right - 18 - right - x, tile.h - 20)


class QuizCard(gk.GameCard):
    kind = "quiz"

    def update(self, dt: float) -> None:
        super().update(dt)
        if self.flashing and self.flash:
            self.mood = "happy" if self.flash.get("right") else "surprised"
        else:
            self.mood = "happy" if self.view.get("over") else "curious"

    # ------------------------------------------------------------ input
    def tap(self, x: float, y: float) -> bool:
        options = self.view.get("options") or []
        removed = set(self.view.get("removed") or [])
        for i in range(min(4, len(options))):
            if i not in removed and tile_rect(i).inflate(gk.GAP, gk.GAP).collidepoint(x, y):
                return self.press(i)
        return False

    def key(self, name: str) -> bool:
        k = name.lower()
        if k in ("a", "b", "c", "d", "1", "2", "3", "4"):
            i = "abcd".index(k) if k.isalpha() else int(k) - 1
            return self.press(i)
        return False

    def command(self, action: str, args: dict[str, Any]) -> bool:
        return False  # steered by the game, not the screen tools

    def describe(self) -> str:
        v = self.view
        if v.get("over"):
            f = v.get("final") or {}
            return f"Trivia over: {f.get('right', 0)} of {f.get('total', 0)} right, {f.get('points', 0)} points"
        opts = "; ".join(f"{LETTERS[i]}) {o}" for i, o in enumerate(v.get("options") or []))
        return f"Trivia question {v.get('number')} of {v.get('total')}: {v.get('question', '')} {opts}".strip()

    # ------------------------------------------------------------ frames
    def draw(self, surf: pygame.Surface) -> None:
        gk.background(surf)
        v = self.view
        chips = []
        if not v.get("over") or self.flashing:
            number = (self.flash or {}).get("id") if self.flashing else v.get("number", 0)  # the answered one
            if int(v.get("streak") or 0) >= 2 and not v.get("over"):
                chips.append(gk.chip(f"x{int(v['streak'])}", fg=gk.GOOD))
            chips.append(gk.chip(f"{number} / {v.get('total', 0)}"))
            chips.append(gk.chip(f"{v.get('score', 0)} pts", fg=gk.GOLD))
        left = gk.chips_right(surf, chips) if chips else None
        cat = str(v.get("category") or "")
        gk.title(surf, [f"Trivia · {cat}", cat] if cat and cat != "Mixed" else "Trivia", right=left)
        if self.flashing and self.flash:
            f = self.flash
            self._question(surf, str(f.get("question") or ""), list(f.get("options") or []), reveal=f)
            return
        if v.get("over"):
            self._final(surf, v.get("final") or {})
            return
        self._question(surf, str(v.get("question") or ""), list(v.get("options") or []), reveal=None)

    def _question(self, surf: pygame.Surface, question: str, options: list[str], reveal: dict[str, Any] | None) -> None:
        v = self.view
        intro = kit.appear(self.t, self.changed_at, 0.35) if reveal is None else 1.0
        dy = int((1 - art.ease_out_cubic(intro)) * 18)
        q = Q_RECT.move(0, dy)
        border: Any = gk.BORDER
        if reveal is not None:
            border = art.lighten(gk.GOOD if reveal.get("right") else gk.BAD, 0.25)
        gk.box(surf, q, gk.PANEL_HI, border=border, radius=26, border_w=4 if reveal is not None else 3)
        inner = q.inflate(-56, -36)
        bar_y = inner.bottom - PROGRESS_H
        inner.h -= PROGRESS_H + 12
        gk.blit_text_in(surf, question, inner, Q_SIZES, Q_LINES)
        total = kit.as_int(v.get("total"))
        if total > 1:  # the round so far: a pip per question, the one being played gold
            number = kit.as_int(reveal.get("id") if reveal is not None else v.get("number"), 1)
            done = number if reveal is not None else number - 1
            strip = gk.progress(inner.w, done, total, current=reveal is None, h=PROGRESS_H)
            surf.blit(strip, (inner.x, bar_y))
        removed = set(v.get("removed") or []) if reveal is None else set()
        tick_w = gk.mark(True).get_width() + 12 if reveal is not None else 0  # room for a tick or a cross
        sizes = gk.common_sizes(options[:4], OPT_SIZES, [text_area(tile_rect(i), tick_w) for i in range(4)], OPT_LINES)
        for i, opt in enumerate(options[:4]):
            r = tile_rect(i).move(0, dy)
            fill, edge, alpha, width = gk.PANEL, gk.BORDER, 1.0, 3
            tick: pygame.Surface | None = None
            if reveal is not None:
                if i == reveal.get("answer"):
                    fill, edge, width = gk.GOOD_BOX, art.lighten(gk.GOOD, 0.35), 4
                    grow = int(8 * gk.pulse(self.t, 9))
                    r = r.inflate(grow, grow)
                    tick = gk.mark(True)
                elif i == reveal.get("chosen"):
                    fill, edge, width = gk.BAD_BOX, art.lighten(gk.BAD, 0.35), 4
                    tick = gk.mark(False)
                else:
                    alpha = 0.45
            elif i in removed:
                alpha = 0.25
            elif i == self.pressed:
                fill, edge, width = gk.PANEL_HI, gk.CYAN, 5
            gk.box(surf, r, fill, border=edge, radius=22, border_w=width, alpha=alpha)
            badge = gk.letter_badge(LETTERS[i], gk.LETTER_COLORS[i], BADGE_R)
            kit.blit_alpha(surf, badge, badge.get_rect(center=(r.x + 20 + BADGE_R, r.centery)).topleft, alpha)
            if tick is not None:  # a tick or a cross at the tile's right end
                surf.blit(tick, tick.get_rect(midright=(r.right - 18, r.centery)))
            gk.blit_text_in(surf, opt, text_area(r, tick_w), sizes, OPT_LINES, align="left", alpha=alpha)

    def _final(self, surf: pygame.Surface, f: dict[str, Any]) -> None:
        right, total = kit.as_int(f.get("right")), kit.as_int(f.get("total"))
        tiles = [(str(kit.as_int(f.get("points"))), "points")]
        streak = kit.as_int(f.get("streak"))
        if streak >= 2:
            tiles.append((f"{streak} in a row", "best streak"))
        gk.scoreboard(surf, self.t, self.over_at or 0.0, f"{right} / {total}",
                      "right answer" if right == 1 else "right answers", right / max(1, total), tiles)


async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> QuizCard:
    return await gk.build_for(QuizCard, spec, ctx, host)  # type: ignore[return-value]


register(CardType(
    kind="quiz",
    description="A voice game's trivia question with four tappable options A-D (shown by the voice games only).",
    fields={"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
            "session": {"type": "string"}},
    build=build,
    offered=False,
))
