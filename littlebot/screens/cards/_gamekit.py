"""Shared look and plumbing for the voice games' cards (quiz, word_game, game_board).

The look is a game show at night: a deep navy background with a few faint stars, LittleBot's cyan as the accent,
white text, and four candy colours for the option letters. Readability rules (littlebot/screens/base.py): text
at least BODY_PX, the presenter's box (top left) left free, a handful of lines at a time.

:class:`GameCard` follows a live game: the card spec carries the game's ``session``; the builder finds the game
(the voice_games skill, else ``littlebot.games.voice.base.find``) and the card re-reads ``game.view`` whenever
``game.version`` changes, so it always shows what the game is at, whoever moved it (a spoken answer, a tool, a
tap). Without a game (a preview, a test) it shows its spec as it is. A tap on an option goes to
``game.request_tap(i)``, which the skill plays on its own loop; the card only lights the tile up at once.
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Sequence
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, PRESENTER_BOX, SMALL_PX, TITLE_PX, Card, CardHost, H, W
from littlebot.screens.cards import _kit as kit

log = logging.getLogger("littlebot.screens")

BG_TOP: art.Color = (20, 30, 70)
BG_BOTTOM: art.Color = (7, 11, 32)
PANEL: art.Color = (28, 42, 94)
PANEL_HI: art.Color = (40, 60, 124)
BORDER: art.Color = (66, 100, 182)
CYAN: art.Color = (90, 210, 255)
INK: art.Color = (244, 248, 255)
INK_SOFT: art.Color = (178, 196, 232)
GOOD: art.Color = (52, 184, 108)
BAD: art.Color = (222, 70, 92)
GOLD: art.Color = (255, 204, 77)
LETTER_COLORS: tuple[art.Color, ...] = ((255, 106, 138), (74, 186, 255), (255, 184, 64), (100, 210, 140))

_PX, _PY, _PW, _PH = PRESENTER_BOX
TOP = _PY + _PH + 10  # the first y below the presenter's eyes
HEADER = pygame.Rect(_PX + _PW + 16, 8, W - (_PX + _PW + 16) - 12, _PY + _PH - 4)
REVEAL_S = 2.2  # an answer's green / red stays this long before the next question shows
CHANGE_S = 0.8  # full frame rate this long after the game moved
OVER_LINGER_S = 20.0  # the final score stays this long, then the card closes itself


def background(surf: pygame.Surface) -> None:
    def make() -> pygame.Surface:
        out = pygame.Surface((W, H))
        for y in range(H):
            out.fill(art.mix(BG_TOP, BG_BOTTOM, (y / (H - 1)) ** 0.8), (0, y, W, 1))
        rng = random.Random(42)
        layer = pygame.Surface((W, H), pygame.SRCALPHA)
        for _ in range(55):
            x, y = rng.randint(0, W), rng.randint(0, H)
            pygame.draw.circle(layer, (200, 225, 255, rng.randint(30, 110)), (x, y), rng.choice((1, 1, 2, 2, 3)))
        glow = art.glow(260, CYAN, 0.35)
        layer.blit(glow, glow.get_rect(center=(W // 2, -40)))
        out.blit(layer, (0, 0))
        return out

    surf.blit(art.cached(("gk_bg", W, H), make), (0, 0))


def box(surf: pygame.Surface, rect: pygame.Rect, color: Sequence[int] = PANEL, *, border: Sequence[int] | None = BORDER,
        radius: int = 22, border_w: int = 3, alpha: float = 1.0) -> None:
    img = art.rounded_rect(rect.w, rect.h, radius, tuple(color), shadow=4, border=tuple(border) if border else None,
                           border_w=border_w)
    kit.blit_alpha(surf, img, rect.topleft, alpha)


def text(lines: Sequence[str], size: int, color: Sequence[int] = INK, *, align: str = "center") -> pygame.Surface:
    return kit.text_block(tuple(lines), size, tuple(color), align=align)


def fitted(s: str, sizes: Sequence[int], width: int, max_lines: int) -> tuple[int, list[str]]:
    """The biggest size at which ``s`` fits in ``max_lines`` lines of ``width``; at the smallest, the lines it
    takes (the last one ellipsized)."""
    size, lines = kit.fit(s, tuple(sizes), width, max_lines)
    lines = list(lines)
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [kit.ellipsize(" ".join(lines[max_lines - 1:]), size, width)]
    return size, lines


def blit_text_in(surf: pygame.Surface, s: str, rect: pygame.Rect, sizes: Sequence[int], max_lines: int,
                 color: Sequence[int] = INK, *, align: str = "center", alpha: float = 1.0) -> None:
    if not s:
        return
    size, lines = fitted(s, sizes, rect.w, max_lines)
    img = text(lines, size, color, align=align)
    r = img.get_rect(center=rect.center) if align == "center" else img.get_rect(midleft=rect.midleft)
    kit.blit_alpha(surf, img, r.topleft, alpha)


def title(surf: pygame.Surface, s: str, *, right: int | None = None) -> None:
    """The card's title beside the presenter's eyes (one line, ellipsized before ``right``)."""
    rect = HEADER.copy()
    if right is not None:
        rect.w = max(80, right - rect.x - 10)
    line = kit.ellipsize(s, TITLE_PX, rect.w)
    img = art.text_outlined(line, TITLE_PX, INK, art.mix(BG_TOP, (0, 0, 0), 0.4), 2)
    surf.blit(img, img.get_rect(midleft=(rect.x, rect.centery)))


def chip(label: str, *, fg: Sequence[int] = CYAN, bg: Sequence[int] = PANEL,
         size: int = SMALL_PX + 4) -> pygame.Surface:
    return art.pill(label, size, tuple(fg), tuple(bg), pad=(16, 8), border=BORDER)


def chips_right(surf: pygame.Surface, labels: Sequence[pygame.Surface], y: int | None = None) -> int:
    """Chips in a row ending at the right edge of the header; returns the x where they start."""
    x = W - 14
    cy = HEADER.centery if y is None else y
    for img in reversed(labels):
        r = img.get_rect(midright=(x, cy))
        surf.blit(img, r)
        x = r.left - 8
    return x


def letter_badge(letter: str, color: Sequence[int], r: int = 25) -> pygame.Surface:
    def make() -> pygame.Surface:
        out = pygame.Surface((2 * r, 2 * r + 3), pygame.SRCALPHA)
        out.blit(art.circle(r, art.darken(color, 0.35)), (0, 3))
        out.blit(art.circle(r, color), (0, 0))
        t = art.text_outlined(letter, int(r * 1.5), INK, art.darken(color, 0.5), 2)
        out.blit(t, t.get_rect(center=(r, r)))
        return out

    return art.cached(("gk_badge", letter, tuple(color), r), make)


def heart(filled: bool, size: int = 30) -> pygame.Surface:
    return art.heart(size, BAD if filled else (70, 84, 128), outline=filled, shine=filled)


class GameCard(Card):
    """A card that follows a live voice game (see the module docstring)."""

    kind = ""

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None, game: Any = None) -> None:
        super().__init__(spec, host)
        self.game = game
        self.session = str(spec.get("session") or "")
        self.view: dict[str, Any] = dict(spec)
        self._version = getattr(game, "version", -1) if game is not None else -1
        if game is not None and getattr(game, "view", None):
            self.view = dict(game.view)
        self.changed_at = 0.0
        self.over_at: float | None = 0.0 if self.view.get("over") else None
        self.flash_id: Any = self._flash_key(self.view)  # what is already on screen is not flashed again
        self.flash_at = -1e9
        self.flash: dict[str, Any] | None = None
        self.pressed: int | None = None
        self.pressed_at = -1e9
        self.title = str(self.view.get("title") or spec.get("title") or self.kind)[:80]

    # ------------------------------------------------------------ following the game
    def refresh(self, spec: dict[str, Any]) -> None:
        """A new spec for the same game (the skill showed it again): only matters without a live game."""
        if self.game is None:
            self._set_view(dict(spec))

    def _flash_key(self, view: dict[str, Any]) -> Any:
        r = view.get("reveal") or view.get("result")
        return r.get("id") if isinstance(r, dict) else None

    def _set_view(self, view: dict[str, Any]) -> None:
        old = self.view
        self.view = view
        self.changed_at = self.t
        key = self._flash_key(view)
        if key is not None and key != self.flash_id:
            self.flash_id = key
            self.flash_at = self.t
            self.flash = dict(view.get("reveal") or view.get("result") or {})
            self.on_flash(self.flash)
        if view.get("over") and self.over_at is None:
            self.over_at = self.t
        self.pressed = None
        self.title = str(view.get("title") or self.title)[:80]
        self.on_view(old, view)

    def on_view(self, old: dict[str, Any], new: dict[str, Any]) -> None:
        pass

    def on_flash(self, flash: dict[str, Any]) -> None:
        pass

    @property
    def flashing(self) -> bool:
        return self.flash is not None and self.t - self.flash_at < REVEAL_S

    def update(self, dt: float) -> None:
        super().update(dt)
        g = self.game
        if g is not None:
            v = getattr(g, "version", self._version)
            if v != self._version:
                self._version = v
                view = getattr(g, "view", None)
                if isinstance(view, dict) and view:
                    self._set_view(dict(view))
        if self.over_at is not None and not self.flashing and self.t - self.over_at > OVER_LINGER_S:
            self.done = True

    @property
    def animating(self) -> bool:
        return (self.t - self.changed_at < CHANGE_S or self.flashing or self.t - self.pressed_at < 1.0
                or (self.over_at is not None and self.t - self.over_at < 3.0))

    def press(self, index: int) -> bool:
        """A tap on option ``index``: light it up now, and hand it to the game."""
        if self.view.get("over") or self.flashing:
            return False
        self.pressed, self.pressed_at = index, self.t
        g = self.game
        ok = bool(g.request_tap(index)) if g is not None and hasattr(g, "request_tap") else False
        if ok:
            self.host.sound("click")
        return ok

    def status(self) -> dict[str, Any]:
        return {**super().status(), "game": self.view.get("game"), "session": self.session,
                "over": bool(self.view.get("over"))}


async def build_for(cls: type[GameCard], spec: dict[str, Any], ctx: Any, host: CardHost) -> GameCard:
    """The card for ``spec``'s game: the one already on screen for it (kept, so nothing flickers), else a new one
    bound to the live game (or a still card from the spec, when there is no game)."""
    session = str(spec.get("session") or "")
    screens = getattr(ctx, "screens", None)
    cur = getattr(screens, "current", None)
    if session and isinstance(cur, cls) and cur.session == session and not cur.done:
        cur.refresh(spec)
        return cur
    return cls(spec, host, game=find_game(ctx, session))


def find_game(ctx: Any, session: str) -> Any:
    if not session:
        return None
    skills = getattr(ctx, "skills", None)
    get = getattr(skills, "get", None)
    if callable(get):
        try:
            sk = get("voice_games")
            g = sk.game_for(session) if sk is not None and hasattr(sk, "game_for") else None
            if g is not None:
                return g
        except Exception:  # noqa: BLE001
            log.debug("screens: no voice_games skill", exc_info=True)
    try:
        from littlebot.games.voice.base import find  # noqa: PLC0415

        return find(session)
    except Exception:  # noqa: BLE001
        return None


def pulse(t: float, speed: float = 3.0) -> float:
    return 0.5 + 0.5 * math.sin(t * speed)


__all__ = ["BAD", "BG_BOTTOM", "BG_TOP", "BODY_PX", "BORDER", "CYAN", "GOLD", "GOOD", "GameCard", "HEADER", "INK",
           "INK_SOFT", "LETTER_COLORS", "PANEL", "PANEL_HI", "REVEAL_S", "TOP", "background", "blit_text_in", "box",
           "build_for", "chip", "chips_right", "find_game", "fitted", "heart", "letter_badge", "pulse", "text",
           "title"]
