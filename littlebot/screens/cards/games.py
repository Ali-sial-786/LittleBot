"""The games menu: every on-screen game in a scrollable list; tap one to play it.

Opened by "show me the games" in a voice chat, or by a long press (about a second) on the idle face
(littlebot/face/display.py; a plain tap still makes LittleBot giggle). It lists the games of the
dashboard's Games page (littlebot/games/catalog.py ``GAMES``): a small drawn icon in the candy style,
the name, a few words on what happens, the ages.

* Drag to scroll (with a little fling), or the arrow keys (up / down move the highlight, Enter or
  space plays it). The voice can ``scroll`` / ``play`` too.
* A tap on a game starts it through ``ctx.games.start(id, source="screen")``, like the dashboard's Play
  button (the same sharing rules: a voice chat keeps talking while the game takes the screen). The game
  then takes the screen and this card goes. If the game can't start, the card says why for a moment.
* ``describe()`` lists the names in order, so the voice can read them out.
* Outside a chat (opened by touch) it goes after 60 s without a touch (``linger_seconds``).

Spec: ``{"card": "games", "ages": "all" | "child" | "little" | "big"}``; "child" keeps the games for the
child's age (Profile), "little" / "big" the dashboard's two groups. Default "all".
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

import pygame

from littlebot.games import art
from littlebot.games.catalog import GAMES, GameInfo
from littlebot.screens.base import (
    BODY_PX,
    SMALL_PX,
    TITLE_PX,
    Card,
    CardType,
    H,
    W,
    register,
)

log = logging.getLogger("littlebot.screens")

KIND = "games"
LIST_TOP = 106  # the list scrolls under the header (title + presenter)
ROW_PITCH = 140
ROW_H = 126
ROW_X = 22
ICON = 96
NAME_PX = 38
FRICTION = 4.5  # fling slow-down, 1/s
NOTICE_SECONDS = 3.5
TOUCH_LINGER = 60.0  # s without a touch before a menu opened outside a chat goes

# A few words each (the catalogue's descriptions are for parents and too long for the screen).
BLURBS = {
    "bubbles": "Tap the bubbles to pop!",
    "peekaboo": "Animals play peekaboo",
    "colours": "Tap to paint colours",
    "star": "Eyes follow the star",
    "memory": "Copy the light pattern",
    "tictactoe": "X and O with LittleBot",
    "reaction": "Wait for green, tap!",
    "rps": "Rock, paper, scissors",
}
COLOURS = {
    "bubbles": art.SKY, "peekaboo": art.TANGERINE, "colours": art.BUBBLEGUM, "star": art.LEMON,
    "memory": art.MINT, "tictactoe": art.PERIWINKLE, "reaction": art.STRAWBERRY, "rps": art.GRAPE,
}


def blurb(info: GameInfo) -> str:
    if info.id in BLURBS:
        return BLURBS[info.id]
    words = info.description.split()
    return " ".join(words[:4]).rstrip(".,-") + ("..." if len(words) > 4 else "")


def pick(games: tuple[GameInfo, ...] | list[GameInfo], ages: str, child_months: int | None) -> list[GameInfo]:
    """The games for ``ages``: "all", "child" (old enough for the child; all when the age is unknown),
    "little" or "big" (the dashboard's groups)."""
    ages = (ages or "all").strip().lower()
    if ages in ("little", "big"):
        return [g for g in games if g.audience == ages]
    if ages == "child" and child_months is not None:
        return [g for g in games if g.min_age_months <= child_months] or list(games)
    return list(games)


class GamesMenuCard(Card):
    kind = KIND

    def __init__(self, spec: dict[str, Any], host: Any = None, *, games: list[GameInfo] | None = None,
                 start: Callable[[str, Callable[[bool, str], None]], None] | None = None) -> None:
        super().__init__(spec, host)
        self.games = list(games if games is not None else GAMES)
        self.title = self.title or "Games"
        self.linger_seconds = TOUCH_LINGER  # the service: how long it stays after a chat / since the last touch
        self._start = start
        self.offset = 0.0  # px scrolled
        self.velocity = 0.0  # px/s (a fling after a drag)
        self.dragging = False
        self.selected = 0  # the highlight (arrow keys)
        self.starting: str | None = None  # the game being started
        self.notice = ""
        self.notice_until = 0.0
        self.pressed: int | None = None  # row under the finger (a squish)

    # ------------------------------------------------------------ layout
    @property
    def max_offset(self) -> float:
        return max(0.0, LIST_TOP + len(self.games) * ROW_PITCH + 10 - H)

    def row_rect(self, i: int) -> pygame.Rect:
        return pygame.Rect(ROW_X, int(LIST_TOP + i * ROW_PITCH - self.offset), W - 2 * ROW_X - 14, ROW_H)

    def row_at(self, x: float, y: float) -> int | None:
        if y < LIST_TOP:
            return None
        for i in range(len(self.games)):
            if self.row_rect(i).collidepoint(x, y):
                return i
        return None

    @property
    def animating(self) -> bool:
        return self.dragging or abs(self.velocity) > 1.0

    # ------------------------------------------------------------ logic
    def update(self, dt: float) -> None:
        super().update(dt)
        if not self.dragging and abs(self.velocity) > 1.0:
            self.offset += self.velocity * dt
            self.velocity *= math.exp(-FRICTION * dt)
        if not self.dragging:  # spring back inside the list
            lo, hi = 0.0, self.max_offset
            if self.offset < lo or self.offset > hi:
                target = lo if self.offset < lo else hi
                self.offset += (target - self.offset) * min(1.0, 12.0 * dt)
                self.velocity = 0.0
                if abs(self.offset - target) < 0.5:
                    self.offset = target
        if self.notice and self.t >= self.notice_until:
            self.notice = ""

    def scroll_by(self, dy: float) -> None:
        self.offset = art.clamp(self.offset + dy, -60.0, self.max_offset + 60.0)

    def drag(self, dx: float, dy: float) -> None:
        """The finger moved by (dx, dy) canvas px (the screen service calls this while dragging)."""
        self.dragging = True
        self.pressed = None
        self.scroll_by(-dy)
        self.velocity = art.lerp(self.velocity, -dy * 30.0, 0.5)  # ~30 fps of motion events

    def drag_end(self) -> None:
        self.dragging = False
        self.velocity = art.clamp(self.velocity, -2400.0, 2400.0)

    def tap(self, x: float, y: float) -> bool:
        self.velocity = 0.0
        i = self.row_at(x, y)
        if i is None:
            return False
        self.selected = i
        self.play(self.games[i].id)
        return True

    def key(self, name: str) -> bool:
        if name in ("down", "up"):
            n = len(self.games)
            self.selected = (self.selected + (1 if name == "down" else -1)) % max(1, n)
            self.show_row(self.selected)
            return True
        if name in ("page down", "page up"):
            self.scroll_by((1 if name == "page down" else -1) * ROW_PITCH * 2)
            return True
        if name in ("return", "enter", "space") and self.games:
            self.play(self.games[self.selected].id)
            return True
        return False

    def show_row(self, i: int) -> None:
        """Scroll so row ``i`` is fully on screen."""
        r = self.row_rect(i)
        if r.top < LIST_TOP:
            self.offset -= LIST_TOP - r.top + 6
        elif r.bottom > H - 6:
            self.offset += r.bottom - (H - 6)
        self.offset = art.clamp(self.offset, 0.0, self.max_offset)
        self.velocity = 0.0

    def command(self, action: str, args: dict[str, Any]) -> bool:
        if action in ("next", "down", "scroll") and str(args.get("direction", "down")).lower() != "up":
            self.scroll_by(ROW_PITCH * 2)
            return True
        if action in ("prev", "up", "scroll"):
            self.scroll_by(-ROW_PITCH * 2)
            return True
        if action == "play":
            want = str(args.get("id") or args.get("name") or "").strip().lower()
            for g in self.games:
                if want in (g.id, g.name.lower()):
                    self.play(g.id)
                    return True
        return False

    def play(self, game_id: str) -> None:
        if self.starting:
            return
        info = next((g for g in self.games if g.id == game_id), None)
        if info is None:
            return
        self.host.sound("pop")
        if self._start is None:
            self.say_notice(f"{info.name} can't start here.")
            return
        self.starting = game_id
        self._start(game_id, self._started)

    def _started(self, ok: bool, why: str) -> None:
        """The game started (the game service then takes the screen) or not (say why for a moment)."""
        name = next((g.name for g in self.games if g.id == self.starting), "That game")
        self.starting = None
        if not ok:
            self.say_notice(why or f"{name} can't start right now.")
            self.host.sound("uhoh")

    def say_notice(self, text: str) -> None:
        self.notice = str(text)[:60]
        self.notice_until = self.t + NOTICE_SECONDS

    def describe(self) -> str:
        names = ", ".join(g.name for g in self.games)
        return f"The games: {names}." if names else "No games to show."

    def status(self) -> dict[str, Any]:
        return {**super().status(), "games": [g.id for g in self.games], "selected": self.selected,
                "offset": round(self.offset), "starting": self.starting}

    # ------------------------------------------------------------ drawing
    def draw(self, surf: Any) -> None:
        surf.blit(art.background(), (0, 0))
        clip = surf.get_clip()
        surf.set_clip(pygame.Rect(0, LIST_TOP - 8, W, H - LIST_TOP + 8).clip(clip))
        for i, g in enumerate(self.games):
            r = self.row_rect(i)
            if r.bottom < LIST_TOP - 8 or r.top > H:
                continue
            self._draw_row(surf, r, g, i)
        surf.set_clip(clip)
        # header over the list: soft fade, title, hint
        surf.blit(_header_fade(), (0, 0))
        art.blit_text(surf, self.title, TITLE_PX, ((170 + W) / 2, 40))
        hint = "Tap a game to play" if len(self.games) * ROW_PITCH <= H - LIST_TOP else "Drag to see more, tap to play"
        art.blit_text(surf, hint, SMALL_PX, ((170 + W) / 2, 80), art.INK_SOFT)
        self._draw_scrollbar(surf)
        if self.notice:
            pill = art.pill(self.notice, BODY_PX, art.WHITE, art.STRAWBERRY)
            art.blit_center(surf, pill, (W / 2, H - 44))

    def _draw_row(self, surf: Any, r: pygame.Rect, g: GameInfo, i: int) -> None:
        col = COLOURS.get(g.id, art.CANDY[i % len(art.CANDY)])
        selected = i == self.selected
        busy = self.starting == g.id
        border = art.inkier(col, 0.25) if selected or busy else None
        surf.blit(art.rounded_rect(r.w, r.h, 30, art.WHITE, shadow=5, border=border, border_w=4), r.topleft)
        ic = (r.x + 18 + ICON // 2, r.centery)
        art.blit_center(surf, art.circle(ICON / 2 + 2, art.lighten(col, 0.55)), ic)
        art.blit_center(surf, game_icon(g.id, ICON - 18, col), ic)
        tx = r.x + 36 + ICON
        surf.blit(art.text(g.name, NAME_PX), (tx, r.y + 18))
        surf.blit(art.text(blurb(g), BODY_PX, art.INK_SOFT), (tx, r.y + 70))
        age = art.pill(g.ages, SMALL_PX, art.INK, art.lighten(col, 0.6), pad=(12, 5))
        surf.blit(age, (r.right - age.get_width() - 16, r.y + 14))
        play = art.circle(24, col)
        art.blit_center(surf, play, (r.right - 44, r.y + 88))
        x0, y0 = r.right - 44, r.y + 88
        tri = [(x0 - 7, y0 - 11), (x0 - 7, y0 + 11), (x0 + 12, y0)] if not busy else []
        if tri:
            pygame.draw.polygon(surf, art.WHITE, tri)
        else:  # starting: three little dots
            for k in range(3):
                on = int(self.t * 4) % 3 == k
                art.blit_center(surf, art.circle(4 if on else 3, art.WHITE), (x0 - 10 + k * 10, y0))

    def _draw_scrollbar(self, surf: Any) -> None:
        total = LIST_TOP + len(self.games) * ROW_PITCH + 10
        if total <= H:
            return
        track_h = H - LIST_TOP - 16
        thumb_h = max(40, int(track_h * (H - LIST_TOP) / (total - LIST_TOP)))
        p = art.clamp(self.offset / max(1.0, self.max_offset), 0.0, 1.0)
        y = LIST_TOP + 8 + int((track_h - thumb_h) * p)
        surf.blit(art.rounded_rect(8, track_h, 4, art.mix(art.BG_BOTTOM, art.INK_SOFT, 0.15)), (W - 16, LIST_TOP + 8))
        surf.blit(art.rounded_rect(8, thumb_h, 4, art.INK_SOFT), (W - 16, y))


def _header_fade() -> pygame.Surface:
    """The background's top, fading out over the list (rows slide under the title)."""

    def make() -> pygame.Surface:
        bg = art.background()
        s = pygame.Surface((W, LIST_TOP + 12), pygame.SRCALPHA)
        s.blit(bg, (0, 0))
        fade = pygame.Surface((W, LIST_TOP + 12), pygame.SRCALPHA)
        for y in range(LIST_TOP + 12):
            a = 255 if y < LIST_TOP - 10 else int(255 * (1 - (y - (LIST_TOP - 10)) / 22))
            fade.fill((255, 255, 255, max(0, a)), (0, y, W, 1))
        s.blit(fade, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
        return s

    return art.cached(("games-menu-header", LIST_TOP), make)


# ---------------------------------------------------------------- the little icons
def game_icon(game_id: str, size: int, color: art.Color) -> pygame.Surface:
    """A small drawn icon for ``game_id`` in the candy style (cached)."""
    size = int(size)
    draw = _ICONS.get(game_id, _icon_heart)
    return art.cached(("games-menu-icon", game_id, size, tuple(color)),
                      lambda: art.supersampled((size, size), lambda s, k: draw(s, size * k, color)))


def _shiny_circle(s: pygame.Surface, c: tuple[float, float], r: float, col: art.Color, ink: bool = True) -> None:
    if ink:
        pygame.draw.circle(s, art.inkier(col, 0.3), c, r)
        pygame.draw.circle(s, col, c, r * 0.86)
    else:
        pygame.draw.circle(s, col, c, r)
    pygame.draw.circle(s, art.lighten(col, 0.7), (c[0] - r * 0.35, c[1] - r * 0.35), r * 0.22)


def _icon_bubbles(s: pygame.Surface, z: float, col: art.Color) -> None:
    for fx, fy, fr, c in ((0.38, 0.6, 0.27, art.SKY), (0.7, 0.38, 0.2, art.BUBBLEGUM), (0.68, 0.76, 0.13, art.MINT),
                          (0.3, 0.24, 0.11, art.LEMON)):
        cx, cy, r = fx * z, fy * z, fr * z
        pygame.draw.circle(s, (*art.lighten(c, 0.35), 200), (cx, cy), r)
        pygame.draw.circle(s, art.inkier(c, 0.2), (cx, cy), r, max(2, int(z * 0.035)))
        pygame.draw.circle(s, art.WHITE, (cx - r * 0.38, cy - r * 0.38), r * 0.22)


def _icon_peekaboo(s: pygame.Surface, z: float, col: art.Color) -> None:
    block = pygame.Rect(z * 0.14, z * 0.5, z * 0.72, z * 0.42)
    pygame.draw.circle(s, art.inkier(art.TANGERINE, 0.3), (z * 0.5, z * 0.5), z * 0.3)  # a head peeking out
    pygame.draw.circle(s, art.lighten(art.TANGERINE, 0.3), (z * 0.5, z * 0.5), z * 0.26)
    for sx in (-1, 1):
        pygame.draw.circle(s, art.INK, (z * 0.5 + sx * z * 0.1, z * 0.44), z * 0.045)
        pygame.draw.circle(s, art.WHITE, (z * 0.5 + sx * z * 0.1 - z * 0.015, z * 0.43), z * 0.016)
        pygame.draw.ellipse(s, art.inkier(art.TANGERINE, 0.3),
                            pygame.Rect(z * 0.5 + sx * z * 0.24 - z * 0.08, z * 0.2, z * 0.16, z * 0.2))
    pygame.draw.rect(s, art.inkier(art.MINT, 0.3), block, border_radius=int(z * 0.1))
    pygame.draw.rect(s, art.MINT, block.inflate(-z * 0.07, -z * 0.07), border_radius=int(z * 0.08))
    pygame.draw.rect(s, art.lighten(art.MINT, 0.5), pygame.Rect(block.x + z * 0.08, block.y + z * 0.07, z * 0.3, z * 0.06),
                     border_radius=int(z * 0.03))


def _icon_colours(s: pygame.Surface, z: float, col: art.Color) -> None:
    for i, c in enumerate((art.STRAWBERRY, art.LEMON, art.SKY, art.MINT, art.GRAPE)):
        a = i / 5 * math.tau - math.pi / 2
        _shiny_circle(s, (z * 0.5 + math.cos(a) * z * 0.25, z * 0.5 + math.sin(a) * z * 0.25), z * 0.15, c)
    _shiny_circle(s, (z * 0.5, z * 0.5), z * 0.14, art.BUBBLEGUM)


def _icon_star(s: pygame.Surface, z: float, col: art.Color) -> None:
    c = z / 2
    pts = art.star_points(c, c * 1.04, z * 0.46, z * 0.23)
    pygame.draw.polygon(s, art.inkier(art.LEMON, 0.35), pts)
    pygame.draw.polygon(s, art.LEMON, art.star_points(c, c * 1.04, z * 0.38, z * 0.19))
    for sx in (-1, 1):
        pygame.draw.circle(s, art.INK, (c + sx * z * 0.08, c * 1.02), z * 0.04)
    pygame.draw.arc(s, art.INK, pygame.Rect(c - z * 0.07, c * 1.02, z * 0.14, z * 0.1), math.pi * 1.1, math.pi * 1.9,
                    max(2, int(z * 0.03)))


def _icon_memory(s: pygame.Surface, z: float, col: art.Color) -> None:
    for (fx, fy), c in zip(((0.08, 0.08), (0.52, 0.08), (0.08, 0.52), (0.52, 0.52)),
                           (art.STRAWBERRY, art.SKY, art.LEMON, art.MINT)):
        r = pygame.Rect(fx * z, fy * z, z * 0.4, z * 0.4)
        pygame.draw.rect(s, art.inkier(c, 0.3), r, border_radius=int(z * 0.1))
        pygame.draw.rect(s, c, r.inflate(-z * 0.06, -z * 0.06), border_radius=int(z * 0.08))
    pygame.draw.circle(s, (255, 255, 255, 170), (z * 0.28, z * 0.28), z * 0.12)  # one pad lit


def _icon_tictactoe(s: pygame.Surface, z: float, col: art.Color) -> None:
    lw = max(3, int(z * 0.06))
    for f in (1 / 3, 2 / 3):
        art.capsule(s, art.INK_SOFT, (f * z, z * 0.08), (f * z, z * 0.92), lw)
        art.capsule(s, art.INK_SOFT, (z * 0.08, f * z), (z * 0.92, f * z), lw)
    w2 = max(4, int(z * 0.09))
    for cx, cy in ((1 / 6, 1 / 6), (5 / 6, 5 / 6)):
        d = z * 0.1
        art.capsule(s, art.STRAWBERRY, (cx * z - d, cy * z - d), (cx * z + d, cy * z + d), w2)
        art.capsule(s, art.STRAWBERRY, (cx * z - d, cy * z + d), (cx * z + d, cy * z - d), w2)
    pygame.draw.circle(s, art.PERIWINKLE, (z / 2, z / 2), z * 0.11, w2)
    pygame.draw.circle(s, art.PERIWINKLE, (z * 5 / 6, z / 6), z * 0.11, w2)


def _icon_reaction(s: pygame.Surface, z: float, col: art.Color) -> None:
    bolt = [(0.58, 0.04), (0.2, 0.56), (0.47, 0.56), (0.38, 0.96), (0.8, 0.4), (0.53, 0.4), (0.66, 0.04)]
    pts = [(x * z, y * z) for x, y in bolt]
    pygame.draw.polygon(s, art.inkier(art.LEMON, 0.4), pts)
    inner = [(x * z * 0.86 + z * 0.07, y * z * 0.86 + z * 0.07) for x, y in bolt]
    pygame.draw.polygon(s, art.LEMON, inner)


def _icon_rps(s: pygame.Surface, z: float, col: art.Color) -> None:
    # paper (a sheet), rock (a pebble), scissors (two rings and blades)
    paper = pygame.Rect(z * 0.06, z * 0.08, z * 0.4, z * 0.5)
    pygame.draw.rect(s, art.INK_SOFT, paper, border_radius=int(z * 0.06))
    pygame.draw.rect(s, art.WHITE, paper.inflate(-z * 0.05, -z * 0.05), border_radius=int(z * 0.05))
    for k in range(3):
        y = paper.y + z * (0.14 + k * 0.11)
        art.capsule(s, art.lighten(art.INK_SOFT, 0.4), (paper.x + z * 0.09, y), (paper.right - z * 0.09, y), max(2, z * 0.03))
    pygame.draw.ellipse(s, art.inkier((170, 160, 175), 0.3), pygame.Rect(z * 0.1, z * 0.6, z * 0.4, z * 0.32))
    pygame.draw.ellipse(s, (190, 180, 196), pygame.Rect(z * 0.13, z * 0.63, z * 0.34, z * 0.26))
    lw = max(3, int(z * 0.06))
    art.capsule(s, art.INK, (z * 0.6, z * 0.12), (z * 0.86, z * 0.62), lw)
    art.capsule(s, art.INK, (z * 0.9, z * 0.14), (z * 0.62, z * 0.62), lw)
    for cx in (0.58, 0.9):
        pygame.draw.circle(s, art.GRAPE, (cx * z, z * 0.76), z * 0.11, max(3, int(z * 0.06)))


def _icon_heart(s: pygame.Surface, z: float, col: art.Color) -> None:
    pts = art.heart_points(z / 2, z / 2, z * 0.8)
    pygame.draw.polygon(s, art.inkier(col, 0.3), pts)
    pygame.draw.polygon(s, col, art.heart_points(z / 2, z / 2, z * 0.68))


_ICONS: dict[str, Callable[[pygame.Surface, float, art.Color], None]] = {
    "bubbles": _icon_bubbles, "peekaboo": _icon_peekaboo, "colours": _icon_colours, "star": _icon_star,
    "memory": _icon_memory, "tictactoe": _icon_tictactoe, "reaction": _icon_reaction, "rps": _icon_rps,
}


# ---------------------------------------------------------------- building
def _starter(ctx: Any) -> Callable[[str, Callable[[bool, str], None]], None] | None:
    """Start a game like the dashboard's Play button: ``ctx.games.start(id)`` on the robot's loop."""
    games = getattr(ctx, "games", None)
    if games is None:
        return None

    def start(game_id: str, done: Callable[[bool, str], None]) -> None:
        async def go() -> None:
            try:
                ok = bool(await games.start(game_id, source="screen"))
            except Exception:  # noqa: BLE001
                log.exception("starting %s from the games menu failed", game_id)
                ok = False
            why = "" if ok else str((games.status() or {}).get("last_error") or "")
            done(ok, why)

        import asyncio  # noqa: PLC0415

        runtime = getattr(ctx, "runtime", None)
        try:
            if runtime is not None and runtime.running:
                runtime.submit(go())
                return
            asyncio.get_running_loop().create_task(go())
        except RuntimeError:
            done(False, "LittleBot can't start a game right now.")

    return start


async def _build(spec: dict[str, Any], ctx: Any, host: Any) -> Card | None:
    child = None
    try:
        child = ctx.settings.child_age_months()
    except Exception:  # noqa: BLE001
        log.debug("no child age", exc_info=True)
    games = getattr(ctx, "games", None)
    catalog = list(GAMES)
    if games is not None and getattr(games, "catalog", None):
        catalog = list(games.catalog.values())
    chosen = pick(catalog, str(spec.get("ages") or "all"), child)
    if not chosen:
        return None
    return GamesMenuCard(spec, host, games=chosen, start=_starter(ctx))


register(CardType(
    kind=KIND,
    description="Every on-screen game LittleBot can play, in a list to scroll and tap (\"show me the games\").",
    fields={"ages": {"type": "string", "enum": ["all", "child", "little", "big"],
                     "description": "Which games: all (default), child (for the child's age), little (babies and "
                                    "toddlers) or big (kids and grown-ups)"}},
    build=_build,
    actions={"scroll": "scroll the list: {direction: \"down\" | \"up\"}", "play": "start a game: {id} or {name}"},
))

SAMPLES: list[dict[str, Any]] = [
    {"card": KIND},
    {"card": KIND, "ages": "big"},
]

__all__ = ["SAMPLES", "GamesMenuCard", "blurb", "game_icon", "pick"]
