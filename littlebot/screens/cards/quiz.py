"""The quiz card (kind "quiz", internal): a trivia question big, four tappable option tiles A to D.

Shown by the voice games (littlebot/games/voice/trivia.py), never offered to the AI. It follows the live game
(littlebot/screens/cards/_gamekit.py): a new question slides in; an answer flashes the right tile green (and a
wrong pick red) for a moment, with the fact's question still showing, before the next question; a fifty-fifty
dims two tiles; the end shows the final score. A tap on a tile answers (the game speaks the result).

Spec (JSON): question, options [4], number, total, score, right, streak, category, removed [indexes],
reveal {id, question, options, chosen, answer, right} | null, over, final {right, total, points, streak},
session (the game's), title.
"""

from __future__ import annotations

from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, CardHost, CardType, W, register
from littlebot.screens.cards import _gamekit as gk
from littlebot.screens.cards import _kit as kit

LETTERS = "ABCD"
Q_RECT = pygame.Rect(12, gk.TOP, W - 24, 150)
TILE_H = 96
ROWS = (Q_RECT.bottom + 10, Q_RECT.bottom + 10 + TILE_H + 10)
COLS = (12, 12 + (W - 36) // 2 + 12)
TILE_W = (W - 36) // 2
Q_SIZES = (46, 42, 38, 35, BODY_PX)
OPT_SIZES = (38, 35, BODY_PX)

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
            if i not in removed and tile_rect(i).inflate(8, 8).collidepoint(x, y):
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
        if not v.get("over"):
            chips.append(gk.chip(f"{v.get('number', 0)}/{v.get('total', 0)}"))
        chips.append(gk.chip(f"{v.get('score', 0)} pts", fg=gk.GOLD))
        if int(v.get("streak") or 0) >= 2 and not v.get("over"):
            chips.insert(0, gk.chip(f"x{int(v['streak'])}", fg=gk.GOOD))
        left = gk.chips_right(surf, chips)
        cat = str(v.get("category") or "")
        gk.title(surf, f"Trivia · {cat}" if cat and cat != "Mixed" else "Trivia", right=left)
        if self.flashing and self.flash:
            f = self.flash
            self._question(surf, str(f.get("question") or ""), list(f.get("options") or []), reveal=f)
            return
        if v.get("over"):
            self._final(surf, v.get("final") or {})
            return
        self._question(surf, str(v.get("question") or ""), list(v.get("options") or []), reveal=None)

    def _question(self, surf: pygame.Surface, question: str, options: list[str], reveal: dict[str, Any] | None) -> None:
        intro = kit.appear(self.t, self.changed_at, 0.35) if reveal is None else 1.0
        dy = int((1 - art.ease_out_cubic(intro)) * 18)
        q = Q_RECT.move(0, dy)
        gk.box(surf, q, gk.PANEL_HI, radius=24)
        gk.blit_text_in(surf, question, q.inflate(-36, -20), Q_SIZES, 3)
        removed = set(self.view.get("removed") or []) if reveal is None else set()
        for i, opt in enumerate(options[:4]):
            r = tile_rect(i).move(0, dy)
            fill, border, alpha = gk.PANEL, gk.BORDER, 1.0
            if reveal is not None:
                if i == reveal.get("answer"):
                    fill, border = gk.GOOD, art.lighten(gk.GOOD, 0.4)
                    r = r.inflate(int(6 * gk.pulse(self.t, 9)), int(6 * gk.pulse(self.t, 9)))
                elif i == reveal.get("chosen"):
                    fill, border = gk.BAD, art.lighten(gk.BAD, 0.4)
                else:
                    alpha = 0.45
            elif i in removed:
                alpha = 0.25
            elif i == self.pressed:
                fill, border = gk.PANEL_HI, gk.CYAN
            gk.box(surf, r, fill, border=border, radius=20, alpha=alpha)
            badge = gk.letter_badge(LETTERS[i], gk.LETTER_COLORS[i])
            kit.blit_alpha(surf, badge, badge.get_rect(center=(r.x + 38, r.centery)).topleft, alpha)
            area = pygame.Rect(r.x + 72, r.y + 6, r.w - 84, r.h - 12)
            gk.blit_text_in(surf, opt, area, OPT_SIZES, 2, align="left", alpha=alpha)

    def _final(self, surf: pygame.Surface, f: dict[str, Any]) -> None:
        panel = pygame.Rect(12, gk.TOP, W - 24, 480 - gk.TOP - 12)
        gk.box(surf, panel, gk.PANEL_HI, radius=28)
        k = art.ease_out_back(kit.appear(self.t, self.over_at or 0.0, 0.6))
        big = art.text_outlined(f"{f.get('right', 0)} / {f.get('total', 0)}", max(40, int(110 * max(0.3, k))), gk.INK,
                                (10, 16, 40), 3)
        surf.blit(big, big.get_rect(center=(panel.centerx, panel.y + 105)))
        pts = gk.text([f"{f.get('points', 0)} points"], 44, gk.GOLD)
        surf.blit(pts, pts.get_rect(center=(panel.centerx, panel.y + 195)))
        streak = int(f.get("streak") or 0)
        if streak >= 2:
            s = gk.text([f"Best streak: {streak} in a row"], BODY_PX, gk.INK_SOFT)
            surf.blit(s, s.get_rect(center=(panel.centerx, panel.y + 255)))
        for n in range(5):  # a little row of stars, filled by the share right
            share = (f.get("right", 0) / max(1, f.get("total", 1))) * 5
            star = art.star(20, gk.GOLD if n < round(share) else (70, 84, 128), rot_deg=0)
            surf.blit(star, star.get_rect(center=(panel.centerx + (n - 2) * 56, panel.bottom - 50)))


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
