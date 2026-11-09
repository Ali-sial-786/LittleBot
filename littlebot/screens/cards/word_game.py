"""The word game card (kind "word_game", internal): hangman's blanks, the whole alphabet with the letters tried
(green: in the word, red and crossed out: not), lives as hearts.

Shown by the voice games (littlebot/games/voice/word_game.py), never offered to the AI. It follows the live game
(littlebot/screens/cards/_gamekit.py): a right letter pops into its blanks, a wrong one breaks a heart, and when a
word ends the whole word shows in green (solved) or red (out of lives) for a moment before the next one.

On the 1024x600 screen: the blanks across the top (letters up to 120 px; 13 letters still about 80 px), under them
the alphabet in two rows of 13 keys (which letters are left is as plain as which were tried) and the word number
and the words solved; the lives as hearts beside the title. The keys only show: Hangman takes letters by voice.

Spec (JSON): pattern [letter or "" per slot], guessed [letters], wrong [letters], lives, max_lives, category,
number, total, score, result {id, word, won} | null, last {id, letter, hit} | null, over, session, title.
"""

from __future__ import annotations

import string
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, CardHost, CardType, register
from littlebot.screens.cards import _gamekit as gk
from littlebot.screens.cards import _kit as kit

# The word's blanks across the top of the game area; under them the alphabet, two rows of 13 keys, and a line
# with the word number and the words solved.
WORD_RECT = pygame.Rect(gk.AREA.x, gk.AREA.y, gk.AREA.w, 200)
TRIED_RECT = pygame.Rect(gk.AREA.x, WORD_RECT.bottom + gk.GAP, gk.AREA.w, gk.AREA.bottom - WORD_RECT.bottom - gk.GAP)
ALPHABET = string.ascii_uppercase
KEYS_PER_ROW = 13
KEY_GAP = 8
KEY_W = (TRIED_RECT.w - 2 * 22 - (KEYS_PER_ROW - 1) * KEY_GAP) // KEYS_PER_ROW
KEY_H = 70
KEY_PX = 44
SLOT_MAX = 104  # a letter's blank, at most (a short word); 13 letters still get about 70 px each
LETTER_MAX_PX = 120
POP_S = 0.45

SAMPLES = [
    {"card": "word_game", "title": "Hangman", "category": "Animals", "pattern": ["", "L", "E", "", "", "A", "N", ""],
     "guessed": ["E", "A", "L", "N"], "wrong": ["S", "O"], "lives": 4, "max_lives": 6, "number": 2, "total": 5,
     "score": 1, "result": None, "last": {"id": 6, "letter": "N", "hit": True}, "over": False},
]


def key_rect(i: int) -> pygame.Rect:
    """Where letter ``i`` of the alphabet (0 = A) sits."""
    row, col = divmod(i, KEYS_PER_ROW)
    width = KEYS_PER_ROW * KEY_W + (KEYS_PER_ROW - 1) * KEY_GAP
    x0 = TRIED_RECT.centerx - width // 2
    return pygame.Rect(x0 + col * (KEY_W + KEY_GAP), TRIED_RECT.y + 18 + row * (KEY_H + 10), KEY_W, KEY_H)


def key(letter: str, state: str) -> pygame.Surface:
    """A letter key: "" not tried yet, "hit" in the word (green), "miss" not in it (red, crossed out)."""

    def make() -> pygame.Surface:
        fill, edge, ink = gk.PANEL_HI, gk.BORDER, gk.INK
        if state == "hit":
            fill, edge = gk.GOOD_BOX, art.lighten(gk.GOOD, 0.35)
        elif state == "miss":
            fill, edge, ink = gk.BAD_BOX, art.lighten(gk.BAD, 0.3), art.mix(gk.INK, gk.BAD, 0.25)
        out = art.rounded_rect(KEY_W, KEY_H, 16, fill, shadow=3, border=edge, border_w=3).copy()
        t = art.text(letter, KEY_PX, ink)
        out.blit(t, t.get_rect(center=(KEY_W // 2, KEY_H // 2 + 1)))
        if state == "miss":
            pygame.draw.line(out, ink, (12, KEY_H - 14), (KEY_W - 12, 14), 4)
        return out

    return art.cached(("gk_key", letter, state, KEY_W, KEY_H), make)


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
        max_lives = max(1, kit.as_int(v.get("max_lives"), 6))
        lives = max(0, kit.as_int(v.get("lives")))
        shake = 0
        if self.t - self.miss_at < 0.5:
            shake = int(8 * (1 - (self.t - self.miss_at) / 0.5) * (1 if int(self.t * 30) % 2 else -1))
        x = gk.HEADER.right
        hearts_left = None
        if not v.get("over") and not self.flashing:
            for n in reversed(range(max_lives)):
                img = gk.heart(n < lives)
                r = img.get_rect(midright=(x + (shake if n == lives else 0), gk.HEADER.centery))
                surf.blit(img, r)
                x = r.left - 6
                hearts_left = x
        cat = "" if self.flashing else str(v.get("category") or "")  # the next word's category waits for it
        gk.title(surf, [f"Hangman · {cat}", cat] if cat else "Hangman", right=hearts_left)
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
        slot = min(SLOT_MAX, (WORD_RECT.w - 64) // n)
        size = max(48, min(LETTER_MAX_PX, int(slot * 1.15)))
        x0 = WORD_RECT.centerx - slot * n // 2
        return slot, size, x0

    def _blanks(self, surf: pygame.Surface, pattern: list[str], color: Any = None) -> None:
        gk.box(surf, WORD_RECT, gk.PANEL_HI, radius=28)
        slot, size, x0 = self._slots(len(pattern))
        cap = art.font(size).get_ascent()  # the letters sit on their blanks, the pair centred in the panel
        base_y = WORD_RECT.centery + cap // 2 - 4
        for i, c in enumerate(pattern):
            cx = x0 + slot * i + slot // 2
            line = pygame.Rect(0, 0, max(12, slot - 16), 8)
            line.midtop = (cx, base_y + 10)
            pygame.draw.rect(surf, gk.CYAN if not c else gk.INK_SOFT, line, border_radius=4)
            if not c:
                continue
            img, k = art.text_outlined(c, size, color or gk.INK, gk.OUTLINE, 3), 1.0
            if c == self.pop_letter and self.t - self.pop_at < POP_S:  # a right guess pops in, big then settling
                k = 1.0 + 0.45 * (1 - art.ease_out_cubic((self.t - self.pop_at) / POP_S))
                img = pygame.transform.smoothscale(img, (int(img.get_width() * k), int(img.get_height() * k)))
            surf.blit(img, img.get_rect(midbottom=(cx, base_y + 8 + int(size * k) // 7)))

    def _tried(self, surf: pygame.Surface, guessed: list[str], wrong: list[str], v: dict[str, Any]) -> None:
        gk.box(surf, TRIED_RECT, gk.PANEL, radius=24)
        hit = {str(c).upper() for c in guessed}
        miss = {str(c).upper() for c in wrong}
        for i, c in enumerate(ALPHABET):
            img = key(c, "hit" if c in hit else "miss" if c in miss else "")
            surf.blit(img, key_rect(i))
        number = f"Word {v.get('number', 0)} of {v.get('total', 0)}"
        status = f"{number}  ·  say a letter, or the whole word!" if not (hit or miss) else \
            f"{number}  ·  solved {v.get('score', 0)}"
        line = pygame.Rect(TRIED_RECT.x + 20, key_rect(len(ALPHABET) - 1).bottom + 4, TRIED_RECT.w - 40,
                           TRIED_RECT.bottom - key_rect(len(ALPHABET) - 1).bottom - 8)
        gk.blit_text_in(surf, status, line, (BODY_PX,), 1, gk.INK_SOFT)

    def _word_end(self, surf: pygame.Surface, f: dict[str, Any]) -> None:
        word = str(f.get("word") or "")
        won = bool(f.get("won"))
        self._blanks(surf, list(word), color=art.lighten(gk.GOOD, 0.35) if won else art.lighten(gk.BAD, 0.35))
        edge = art.lighten(gk.GOOD if won else gk.BAD, 0.3)
        gk.box(surf, TRIED_RECT, gk.GOOD_BOX if won else gk.BAD_BOX, border=edge, radius=24, border_w=5)
        msg = "You got it!" if won else "The word was..."
        img = art.text_outlined(msg, 72, gk.INK, gk.OUTLINE, 3)
        surf.blit(img, img.get_rect(center=TRIED_RECT.center))

    def _final(self, surf: pygame.Surface, v: dict[str, Any]) -> None:
        score, total = kit.as_int(v.get("score")), kit.as_int(v.get("total"))
        gk.scoreboard(surf, self.t, self.over_at or 0.0, f"{score} / {total}",
                      "word solved" if score == 1 else "words solved", score / max(1, total))


async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> WordGameCard:
    return await gk.build_for(WordGameCard, spec, ctx, host)  # type: ignore[return-value]


register(CardType(
    kind="word_game",
    description="A voice game's hangman word: blanks, the letters tried and the lives left (voice games only).",
    fields={"pattern": {"type": "array", "items": {"type": "string"}}, "session": {"type": "string"}},
    build=build,
    offered=False,
))
