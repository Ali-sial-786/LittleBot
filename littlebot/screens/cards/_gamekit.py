"""Shared look and plumbing for the voice games' cards (quiz, word_game, game_board).

The look is a game show at night: a deep navy background with a few faint stars, LittleBot's cyan as the accent,
white text, and four candy colours for the option letters. Laid out for the 1024x600 canvas like the other cards:
the presenter's eyes top left, the title and the score chips right of them (:data:`HEADER`), the game in
:data:`AREA` under both (the info cards' panel, littlebot/screens/cards/_kit.py ``PANEL``). Readability rules
(littlebot/screens/base.py): text at least BODY_PX (chip labels at least SMALL_PX), titles TITLE_PX, at most
MAX_LINES lines at a time, tap targets about 1 cm or more, the presenter's box left free.

:class:`GameCard` follows a live game: the card spec carries the game's ``session``; the builder finds the game
(the voice_games skill, else ``littlebot.games.voice.base.find``) and the card re-reads ``game.view`` whenever
``game.version`` changes, so it always shows what the game is at, whoever moved it (a spoken answer, a tool, a
tap). Without a game (a preview, a test) it shows its spec as it is. A tap on an option goes to
``game.request_tap(i)``, which the skill plays on its own loop; the card only lights the tile up at once.

Everything is a cached sprite (``art.cached`` / ``art.text`` / ``kit.text_block``): a frame is mostly blits.
"""

from __future__ import annotations

import functools
import logging
import math
import random
from collections.abc import Sequence
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, MAX_LINES, SMALL_PX, TITLE_PX, Card, CardHost, H, W
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
GOOD_BOX: art.Color = (30, 122, 72)  # a green box under white text (contrast ~5:1)
BAD: art.Color = (222, 70, 92)
BAD_BOX: art.Color = (170, 40, 62)  # a red one (~6:1)
GOLD: art.Color = (255, 204, 77)
DIM: art.Color = (70, 84, 128)  # an empty heart, star or pip
OUTLINE: art.Color = (10, 16, 40)  # big outlined numbers and words
LETTER_COLORS: tuple[art.Color, ...] = ((255, 106, 138), (74, 186, 255), (255, 184, 64), (100, 210, 140))

# The layout, from the info cards' (so the games line up with every other card): the title right of the
# presenter's eyes, the game under both.
HEADER = kit.HEADER.copy()
AREA = kit.PANEL.copy()
TOP = AREA.y  # the first y below the presenter's eyes
GAP = 16  # between two boxes
CHIP_PX = BODY_PX  # the score chips beside the title
HEART_PX = 42  # a life in the header
BIG_PX = 150  # the final score
PLAY_AGAIN = "Say \"play again\" for another round"
REVEAL_S = 2.2  # an answer's green / red stays this long before the next question shows
CHANGE_S = 0.8  # full frame rate this long after the game moved
OVER_LINGER_S = 20.0  # the final score stays this long, then the card closes itself


def background(surf: pygame.Surface) -> None:
    """The night sky, over the whole canvas."""

    def make() -> pygame.Surface:
        out = pygame.Surface((W, H))
        for y in range(H):
            out.fill(art.mix(BG_TOP, BG_BOTTOM, (y / (H - 1)) ** 0.8), (0, y, W, 1))
        rng = random.Random(42)
        layer = pygame.Surface((W, H), pygame.SRCALPHA)
        for _ in range(W * H // 6300):  # ~100 faint stars on the 1024x600 canvas
            x, y = rng.randint(0, W), rng.randint(0, H)
            pygame.draw.circle(layer, (200, 225, 255, rng.randint(30, 110)), (x, y), rng.choice((1, 1, 2, 2, 3)))
        glow = art.glow(W // 3, CYAN, 0.35)
        layer.blit(glow, glow.get_rect(center=(W // 2, -H // 12)))
        out.blit(layer, (0, 0))
        return out

    surf.blit(art.cached(("gk_bg", W, H), make), (0, 0))


def box(surf: pygame.Surface, rect: pygame.Rect, color: Sequence[int] = PANEL, *, border: Sequence[int] | None = BORDER,
        radius: int = 24, border_w: int = 3, alpha: float = 1.0) -> None:
    img = art.rounded_rect(rect.w, rect.h, radius, tuple(color), shadow=4, border=tuple(border) if border else None,
                           border_w=border_w)
    kit.blit_alpha(surf, img, rect.topleft, alpha)


def text(lines: Sequence[str], size: int, color: Sequence[int] = INK, *, align: str = "center") -> pygame.Surface:
    return kit.text_block(tuple(lines), size, tuple(color), align=align)


def block_h(size: int, n: int) -> int:
    """How tall ``n`` lines at ``size`` are as a :func:`text` block."""
    return kit.line_h(size) * (max(1, n) - 1) + art.font(size).get_height()


def fitted(s: str, sizes: Sequence[int], width: int, max_lines: int, height: int | None = None, *,
           balance: bool = False) -> tuple[int, list[str]]:
    """The biggest size at which ``s`` fits in ``max_lines`` lines of ``width`` (and ``height`` px, when given); at the
    smallest, the lines that fit (the last one ellipsized). ``balance``: lines about as long as each other (centred
    text: "What gets wetter / the more it dries?", not a last word on its own). Cached: a frame only looks it up."""
    size, lines = _fitted(str(s), tuple(int(x) for x in sizes), int(width), int(max_lines),
                          None if height is None else int(height), bool(balance))
    return size, list(lines)


@functools.lru_cache(maxsize=256)
def _fitted(s: str, sizes: tuple[int, ...], width: int, max_lines: int, height: int | None,
            balance: bool) -> tuple[int, tuple[str, ...]]:
    max_lines = max(1, min(MAX_LINES, max_lines))
    for size in sizes:
        lines = kit.wrap(s, size, width)
        if len(lines) <= max_lines and (height is None or block_h(size, len(lines)) <= height):
            if balance and len(lines) > 1:  # the narrowest width that still takes as many lines
                lo, hi = width // 2, width
                while hi - lo > 4:
                    mid = (lo + hi) // 2
                    if len(kit.wrap(s, size, mid)) == len(lines):
                        hi = mid
                    else:
                        lo = mid
                lines = kit.wrap(s, size, hi)
            return size, tuple(lines)
    size = sizes[-1]
    room = max_lines if height is None else max(1, min(max_lines, 1 + (height - art.font(size).get_height())
                                                       // kit.line_h(size)))
    lines = kit.wrap(s, size, width)
    if len(lines) > room:
        lines = (*lines[: room - 1], kit.ellipsize(" ".join(lines[room - 1:]), size, width))
    return size, tuple(lines)


def common_sizes(texts: Sequence[str], sizes: Sequence[int], rects: Sequence[pygame.Rect],
                 max_lines: int) -> tuple[int, ...]:
    """``sizes`` from the biggest that every one of ``texts`` fits its rect at: tiles side by side read as one
    set (four quiz answers, two would-you-rather choices), not one big and one small."""
    size = min((fitted(t, sizes, r.w, max_lines, r.h)[0] for t, r in zip(texts, rects) if t), default=sizes[0])
    return tuple(x for x in sizes if x <= size) or (sizes[-1],)


def blit_text_in(surf: pygame.Surface, s: str, rect: pygame.Rect, sizes: Sequence[int], max_lines: int,
                 color: Sequence[int] = INK, *, align: str = "center", alpha: float = 1.0) -> pygame.Rect | None:
    """``s`` as big as it fits in ``rect`` (centred with balanced lines, or left and middle), at most ``max_lines``
    lines."""
    if not s:
        return None
    size, lines = fitted(s, sizes, rect.w, max_lines, rect.h, balance=align == "center")
    img = text(lines, size, color, align=align)
    r = img.get_rect(center=rect.center) if align == "center" else img.get_rect(midleft=rect.midleft)
    kit.blit_alpha(surf, img, r.topleft, alpha)
    return r


def title(surf: pygame.Surface, s: str | Sequence[str], *, right: int | None = None) -> None:
    """The card's title beside the presenter's eyes, one line before ``right``: the first of ``s`` (a title, or
    titles from the fullest to the shortest: "Hangman · Around the house", "Around the house") that fits, else the
    last one ellipsized."""
    rect = HEADER.copy()
    if right is not None:
        rect.w = max(120, right - rect.x - 16)
    options = [s] if isinstance(s, str) else [str(x) for x in s if x] or [""]
    line = next((x for x in options if kit.text_w(x, TITLE_PX) <= rect.w), None)
    line = line if line is not None else kit.ellipsize(options[-1], TITLE_PX, rect.w)
    img = art.text_outlined(line, TITLE_PX, INK, art.mix(BG_TOP, (0, 0, 0), 0.4), 2)
    surf.blit(img, img.get_rect(midleft=(rect.x, rect.centery)))


def chip(label: str, *, fg: Sequence[int] = CYAN, bg: Sequence[int] = PANEL, size: int = CHIP_PX) -> pygame.Surface:
    return art.pill(label, max(SMALL_PX, size), tuple(fg), tuple(bg), pad=(18, 10), border=BORDER)


def chips_right(surf: pygame.Surface, labels: Sequence[pygame.Surface], y: int | None = None) -> int:
    """Chips in a row ending at the right edge of the header; returns the x where they start."""
    x = HEADER.right
    cy = HEADER.centery if y is None else y
    for img in reversed(labels):
        r = img.get_rect(midright=(x, cy))
        surf.blit(img, r)
        x = r.left - 10
    return x


def letter_badge(letter: str, color: Sequence[int], r: int = 32) -> pygame.Surface:
    def make() -> pygame.Surface:
        out = pygame.Surface((2 * r, 2 * r + 3), pygame.SRCALPHA)
        out.blit(art.circle(r, art.darken(color, 0.35)), (0, 3))
        out.blit(art.circle(r, color), (0, 0))
        t = art.text_outlined(letter, int(r * 1.5), INK, art.darken(color, 0.5), 2)
        out.blit(t, t.get_rect(center=(r, r)))
        return out

    return art.cached(("gk_badge", letter, tuple(color), r), make)


def mark(ok: bool, r: int = 26) -> pygame.Surface:
    """A white disc with a green tick (``ok``) or a red cross: on a revealed answer."""

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            c, rr = r * k, r * k
            pygame.draw.circle(s, INK, (c, c), rr)
            lw = max(2, int(r * 0.22 * k))
            if ok:
                pts = [(c - rr * 0.45, c + rr * 0.02), (c - rr * 0.12, c + rr * 0.36), (c + rr * 0.5, c - rr * 0.34)]
                pygame.draw.lines(s, art.darken(GOOD, 0.1), False, pts, lw)
            else:
                d = rr * 0.38
                pygame.draw.line(s, art.darken(BAD, 0.1), (c - d, c - d), (c + d, c + d), lw)
                pygame.draw.line(s, art.darken(BAD, 0.1), (c - d, c + d), (c + d, c - d), lw)

        return art.supersampled((2 * r, 2 * r), draw)

    return art.cached(("gk_mark", ok, r), make)


def heart(filled: bool, size: int = HEART_PX) -> pygame.Surface:
    return art.heart(size, BAD if filled else DIM, outline=filled, shine=filled)


def progress(width: int, done: int, total: int, *, current: bool = True, h: int = 12,
             fill: Sequence[int] = CYAN) -> pygame.Surface:
    """A row of ``total`` segments (a pip each, up to 30; else one bar): ``done`` filled (cyan), the one being played
    gold (``current``), the rest dim. Cached per state."""
    fill = tuple(fill)
    total = max(1, int(total))
    done = max(0, min(total, int(done)))

    def make() -> pygame.Surface:
        out = pygame.Surface((max(1, width), h), pygame.SRCALPHA)
        if total > 30:
            pygame.draw.rect(out, DIM, out.get_rect(), border_radius=h // 2)
            bar = out.get_rect()
            bar.w = int(width * done / total)
            if bar.w:
                pygame.draw.rect(out, fill, bar, border_radius=h // 2)
            return out
        gap = max(4, min(10, width // (total * 6)))
        seg = (width - gap * (total - 1)) / total
        for i in range(total):
            color = fill if i < done else GOLD if (current and i == done) else DIM
            r = pygame.Rect(round(i * (seg + gap)), 0, max(h, round(seg)), h)
            pygame.draw.rect(out, color, r, border_radius=h // 2)
        return out

    return art.cached(("gk_progress", width, done, total, current, h, fill), make)


def stars(share: float, r: int = 30, n: int = 5) -> pygame.Surface:
    """``n`` stars, as many gold as the share of the round won."""
    on = round(max(0.0, min(1.0, share)) * n)

    def make() -> pygame.Surface:
        step = int(r * 2.6)
        out = pygame.Surface((step * (n - 1) + 2 * r + 4, 2 * r + 6), pygame.SRCALPHA)
        for k in range(n):
            star = art.star(r, GOLD if k < on else DIM, rot_deg=0)
            out.blit(star, star.get_rect(center=(r + 2 + k * step, r + 3)))
        return out

    return art.cached(("gk_stars", on, r, n), make)


def stat_tile(value: str, label: str, *, color: Sequence[int] = GOLD) -> pygame.Surface:
    """A tile for the final score: a value big and a label under it ("85" / "points")."""

    def make() -> pygame.Surface:
        v = art.text_outlined(value, 56, tuple(color), OUTLINE, 2)
        lab = text([label], SMALL_PX + 4, INK_SOFT)
        w = max(240, max(v.get_width(), lab.get_width()) + 64)
        h = 22 + v.get_height() + 6 + lab.get_height() + 20
        out = art.rounded_rect(w, h, 22, PANEL, shadow=4, border=BORDER, border_w=3).copy()
        out.blit(v, v.get_rect(midtop=(w // 2, 22)))
        out.blit(lab, lab.get_rect(midtop=(w // 2, 22 + v.get_height() + 6)))
        return out

    return art.cached(("gk_stat", value, label, tuple(color)), make)


def tile_row(tiles: Sequence[tuple[str, str]], gap: int = 28) -> pygame.Surface:
    """:func:`stat_tile` s side by side, as one sprite."""
    tiles = tuple((str(v), str(lab)) for v, lab in tiles)

    def make() -> pygame.Surface:
        imgs = [stat_tile(v, lab) for v, lab in tiles]
        out = pygame.Surface((sum(i.get_width() for i in imgs) + gap * (len(imgs) - 1),
                              max(i.get_height() for i in imgs)), pygame.SRCALPHA)
        x = 0
        for img in imgs:
            out.blit(img, (x, 0))
            x += img.get_width() + gap
        return out

    return art.cached(("gk_tiles", tiles, gap), make)


def scoreboard(surf: pygame.Surface, t: float, start: float, big: str, label: str, share: float,
               tiles: Sequence[tuple[str, str]] = (), hint: str = PLAY_AGAIN) -> None:
    """The end of a round over the whole game area: stars, the score big (it pops in), what it counts, a row of
    stat tiles and how to play again, spread evenly down the panel."""
    box(surf, AREA, PANEL_HI, radius=28)
    rows: list[pygame.Surface] = [stars(share, 34)]
    big_img = art.text_outlined(big, BIG_PX, INK, OUTLINE, 4)
    rows.append(big_img)
    if label:
        rows.append(text([label], 52, GOLD))
    if tiles:
        rows.append(tile_row(tiles))
    if hint:
        rows.append(text([hint], BODY_PX, INK_SOFT))
    gap = max(8, (AREA.h - sum(r.get_height() for r in rows)) // (len(rows) + 1))
    y = AREA.y + gap
    k = art.ease_out_back(kit.appear(t, start, 0.6))
    for img in rows:
        if img is big_img and abs(k - 1.0) > 0.002:  # popping in (a little past full size, then back)
            w, h = max(1, int(img.get_width() * max(0.3, k))), max(1, int(img.get_height() * max(0.3, k)))
            pop = pygame.transform.smoothscale(img, (w, h))
            surf.blit(pop, pop.get_rect(center=(AREA.centerx, y + img.get_height() // 2)))
        else:
            surf.blit(img, img.get_rect(midtop=(AREA.centerx, y)))
        y += img.get_height() + gap


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


__all__ = ["AREA", "BAD", "BAD_BOX", "BG_BOTTOM", "BG_TOP", "BIG_PX", "BODY_PX", "BORDER", "CHIP_PX", "CYAN", "DIM",
           "GAP", "GOLD", "GOOD", "GOOD_BOX", "GameCard", "HEADER", "HEART_PX", "INK", "INK_SOFT", "LETTER_COLORS",
           "OUTLINE", "PANEL", "PANEL_HI", "PLAY_AGAIN", "REVEAL_S", "TOP", "background", "block_h", "blit_text_in",
           "box", "build_for", "chip", "chips_right", "common_sizes", "find_game", "fitted", "heart", "letter_badge",
           "mark", "progress", "pulse", "scoreboard", "stars", "stat_tile", "text", "tile_row", "title"]
