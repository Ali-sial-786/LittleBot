"""The word game card (kind "word_game", internal): hangman's blanks, the letters tried, lives as hearts.

Shown by the voice games (littlebot/games/voice/word_game.py), never offered to the AI. It follows the live game
(littlebot/screens/cards/_gamekit.py): a right letter pops into its blanks, a wrong one breaks a heart, and when a
word ends the whole word shows in green (solved) or red (out of lives) for a moment before the next one.

Spec (JSON): pattern [letter or "" per slot], guessed [letters], wrong [letters], lives, max_lives, category,
number, total, score, result {id, word, won} | null, last {id, letter, hit} | null, over, session, title.
"""

from __future__ import annotations

from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, CardHost, CardType, W, register
from littlebot.screens.cards import _gamekit as gk
from littlebot.screens.cards import _kit as kit

WORD_RECT = pygame.Rect(12, gk.TOP, W - 24, 200)
TRIED_RECT = pygame.Rect(12, WORD_RECT.bottom + 12, W - 24, 480 - WORD_RECT.bottom - 24)
POP_S = 0.45

SAMPLES = [
    {"card": "word_game", "title": "Hangman", "category": "Animals", "pattern": ["", "L", "E", "", "", "A", "N", ""],
     "guessed": ["E", "A", "L", "N"], "wrong": ["S", "O"], "lives": 4, "max_lives": 6, "number": 2, "total": 5,
     "score": 1, "result": None, "last": {"id": 6, "letter": "N", "hit": True}, "over": False},
]


class WordGameCard(gk.GameCard):
    kind = "word_game"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None, game: Any = None) -> None:
        super().__init__(spec, host, game)
        last = self.view.get("last")
        self.last_id = last.get("id") if isinstance(last, dict) else None
        self.pop_letter = ""
        self.pop_at = -1e9
        self.miss_at = -1e9

    def on_view(self, old: dict[str, Any], new: dict[str, Any]) -> None:
        last = new.get("last")
        if isinstance(last, dict) and last.get("id") != self.last_id:
            self.last_id = last.get("id")
            if last.get("hit"):
                self.pop_letter, self.pop_at = str(last.get("letter") or ""), self.t
            else:
                self.miss_at = self.t
        elif int(new.get("lives") or 0) < int(old.get("lives") or 0) and new.get("number") == old.get("number"):
            self.miss_at = self.t  # a wrong whole-word guess

    def update(self, dt: float) -> None:
        super().update(dt)
        if self.flashing and self.flash:
            self.mood = "happy" if self.flash.get("won") else "surprised"
        elif self.t - self.miss_at < 0.8:
            self.mood = "surprised"
        elif self.t - self.pop_at < 0.8 or self.view.get("over"):
            self.mood = "happy"
        else:
            self.mood = "curious"

    @property
    def animating(self) -> bool:  # type: ignore[override]
        return super().animating or self.t - self.pop_at < POP_S + 0.2 or self.t - self.miss_at < 0.9

    def describe(self) -> str:
        v = self.view
        if v.get("over"):
            return f"Hangman over: {v.get('score', 0)} of {v.get('total', 0)} words solved"
        shown = " ".join(c or "_" for c in v.get("pattern") or [])
        return f"Hangman ({v.get('category', '')}): {shown}; {v.get('lives', 0)} lives left"

    # ------------------------------------------------------------ frames
    def draw(self, surf: pygame.Surface) -> None:
        gk.background(surf)
        v = self.view
        max_lives = max(1, int(v.get("max_lives") or 6))
        lives = max(0, int(v.get("lives") or 0))
        shake = 0
        if self.t - self.miss_at < 0.5:
            shake = int(6 * (1 - (self.t - self.miss_at) / 0.5) * (1 if int(self.t * 30) % 2 else -1))
        x = W - 14
        hearts_left = x
        if not v.get("over") and not self.flashing:
            for n in reversed(range(max_lives)):
                img = gk.heart(n < lives, 30)
                r = img.get_rect(midright=(x + (shake if n == lives else 0), gk.HEADER.centery - 2))
                surf.blit(img, r)
                x = r.left - 4
                hearts_left = x
        cat = "" if self.flashing else str(v.get("category") or "")  # the next word's category waits for it
        gk.title(surf, f"Hangman · {cat}" if cat else "Hangman", right=hearts_left)
        if self.flashing and self.flash:
            self._word_end(surf, self.flash)
            return
        if v.get("over"):
            self._final(surf, v)
            return
        self._blanks(surf, list(v.get("pattern") or []))
        self._tried(surf, list(v.get("guessed") or []), list(v.get("wrong") or []), v)

    def _slots(self, n: int) -> tuple[int, int, int]:
        n = max(1, n)
        slot = min(66, (WORD_RECT.w - 40) // n)
        size = max(40, min(80, int(slot * 1.2)))
        x0 = WORD_RECT.centerx - slot * n // 2
        return slot, size, x0

    def _blanks(self, surf: pygame.Surface, pattern: list[str], color: Any = None) -> None:
        gk.box(surf, WORD_RECT, gk.PANEL_HI, radius=26)
        slot, size, x0 = self._slots(len(pattern))
        base_y = WORD_RECT.centery + size // 3
        for i, c in enumerate(pattern):
            cx = x0 + slot * i + slot // 2
            line = pygame.Rect(0, 0, max(10, slot - 12), 6)
            line.midtop = (cx, base_y + 6)
            pygame.draw.rect(surf, gk.CYAN if not c else gk.INK_SOFT, line, border_radius=3)
            if not c:
                continue
            k = 1.0
            if c == self.pop_letter and self.t - self.pop_at < POP_S:
                k = 1.0 + 0.45 * (1 - art.ease_out_cubic((self.t - self.pop_at) / POP_S))
            img = art.text_outlined(c, int(size * k), color or gk.INK, (10, 16, 40), 2)
            surf.blit(img, img.get_rect(midbottom=(cx, base_y + 4)))

    def _tried(self, surf: pygame.Surface, guessed: list[str], wrong: list[str], v: dict[str, Any]) -> None:
        gk.box(surf, TRIED_RECT, gk.PANEL, radius=22)
        letters = [(c, True) for c in guessed] + [(c, False) for c in wrong]
        letters.sort(key=lambda p: p[0])
        label = gk.text(["Tried:"], BODY_PX, gk.INK_SOFT, align="left")
        surf.blit(label, label.get_rect(topleft=(TRIED_RECT.x + 20, TRIED_RECT.y + 16)))
        if not letters:
            hint = gk.text(["Say a letter, or the whole word!"], BODY_PX, gk.INK, align="left")
            surf.blit(hint, hint.get_rect(midleft=(TRIED_RECT.x + 20, TRIED_RECT.centery + 18)))
        x, y = TRIED_RECT.x + 20 + label.get_width() + 14, TRIED_RECT.y + 12
        for c, hit in letters:
            img = art.pill(c, 36, gk.INK, gk.GOOD if hit else gk.BAD, pad=(14, 4))
            if x + img.get_width() > TRIED_RECT.right - 16:
                x, y = TRIED_RECT.x + 20, y + img.get_height() + 8
            surf.blit(img, (x, y))
            x += img.get_width() + 8
        status = f"Word {v.get('number', 0)} of {v.get('total', 0)}  ·  solved {v.get('score', 0)}"
        s = gk.text([status], BODY_PX, gk.INK_SOFT)
        surf.blit(s, s.get_rect(midbottom=(TRIED_RECT.centerx, TRIED_RECT.bottom - 10)))

    def _word_end(self, surf: pygame.Surface, f: dict[str, Any]) -> None:
        word = str(f.get("word") or "")
        won = bool(f.get("won"))
        self._blanks(surf, list(word), color=art.lighten(gk.GOOD, 0.35) if won else art.lighten(gk.BAD, 0.35))
        gk.box(surf, TRIED_RECT, gk.GOOD if won else gk.BAD, border=None, radius=22)
        msg = "You got it!" if won else "The word was..."
        img = art.text_outlined(msg, 56, gk.INK, (10, 16, 40), 3)
        surf.blit(img, img.get_rect(center=TRIED_RECT.center))

    def _final(self, surf: pygame.Surface, v: dict[str, Any]) -> None:
        panel = pygame.Rect(12, gk.TOP, W - 24, 480 - gk.TOP - 12)
        gk.box(surf, panel, gk.PANEL_HI, radius=28)
        k = art.ease_out_back(kit.appear(self.t, self.over_at or 0.0, 0.6))
        big = art.text_outlined(f"{v.get('score', 0)} / {v.get('total', 0)}", max(40, int(110 * max(0.3, k))),
                                gk.INK, (10, 16, 40), 3)
        surf.blit(big, big.get_rect(center=(panel.centerx, panel.y + 120)))
        words = gk.text(["words solved"], 44, gk.GOLD)
        surf.blit(words, words.get_rect(center=(panel.centerx, panel.y + 215)))
        again = gk.text(["Say \"play again\" for another round"], BODY_PX, gk.INK_SOFT)
        surf.blit(again, again.get_rect(center=(panel.centerx, panel.bottom - 50)))


async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> WordGameCard:
    return await gk.build_for(WordGameCard, spec, ctx, host)  # type: ignore[return-value]


register(CardType(
    kind="word_game",
    description="A voice game's hangman word: blanks, the letters tried and the lives left (voice games only).",
    fields={"pattern": {"type": "array", "items": {"type": "string"}}, "session": {"type": "string"}},
    build=build,
    offered=False,
))
