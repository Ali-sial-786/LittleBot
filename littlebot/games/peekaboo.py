"""Peekaboo Friends: animals hide behind three toy blocks and pop up ("peekaboo!") with their
sound - moo, woof, quack. Tap a block to find who is hiding; tap an animal to make it giggle and
jump. Nothing to read: pure cause and effect for babies and toddlers."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import pygame

from littlebot.games import art
from littlebot.games.base import Game

R = 62  # head radius (px)
BLOCK_W, BLOCK_H, BLOCK_TOP = 176, 152, 262
FLOOR_Y = 398
STAY_UP = 2.6
RISE_S, FALL_S = 0.42, 0.3
ANIMALS = ("cat", "dog", "duck", "cow", "frog", "sheep")
PHRASES = {"cat": "Meow!", "dog": "Woof woof!", "duck": "Quack quack!", "cow": "Moooo!", "frog": "Ribbit!",
           "sheep": "Baa!"}
BLOCK_COLORS = (art.SKY, art.LEMON, art.GRAPE)
ICONS = ("star", "heart", "circle")
NOSE_INK: art.Color = (70, 40, 66)


# ------------------------------------------------------------------ animal art
def _outline(s: pygame.Surface, color: art.Color, rect: pygame.Rect, lw: int, ellipse: bool = True) -> None:
    edge = art.inkier(color, 0.4)
    if ellipse:
        pygame.draw.ellipse(s, edge, rect)
        pygame.draw.ellipse(s, color, rect.inflate(-2 * lw, -2 * lw))
    else:
        pygame.draw.rect(s, edge, rect, border_radius=rect.w // 2)
        pygame.draw.rect(s, color, rect.inflate(-2 * lw, -2 * lw), border_radius=rect.w // 2)


def _ell(cx: float, cy: float, w: float, h: float) -> pygame.Rect:
    r = pygame.Rect(0, 0, int(w), int(h))
    r.center = (int(cx), int(cy))
    return r


def _eyes(s: pygame.Surface, cx: float, cy: float, rr: float, dx: float = 0.38, dy: float = -0.05,
          size: float = 0.15) -> None:
    for sx in (-1, 1):
        ex, ey = cx + sx * dx * rr, cy + dy * rr
        pygame.draw.ellipse(s, NOSE_INK, _ell(ex, ey, rr * size * 1.7, rr * size * 2.1))
        pygame.draw.circle(s, art.WHITE, (ex - rr * size * 0.3, ey - rr * size * 0.45), rr * size * 0.42)
        pygame.draw.circle(s, art.WHITE, (ex + rr * size * 0.32, ey + rr * size * 0.35), rr * size * 0.18)


def _cheeks(s: pygame.Surface, cx: float, cy: float, rr: float, dx: float = 0.62, dy: float = 0.28,
            on: art.Color = (255, 220, 200)) -> None:
    for sx in (-1, 1):  # opaque mix: drawing with alpha on a sprite would punch a hole, not blend
        pygame.draw.ellipse(s, art.mix(on, art.BLUSH, 0.6), _ell(cx + sx * dx * rr, cy + dy * rr, rr * 0.32, rr * 0.2))


def _smile(s: pygame.Surface, cx: float, cy: float, w: float, lw: int, color: art.Color = NOSE_INK) -> None:
    pygame.draw.arc(s, color, _ell(cx, cy - w * 0.25, w, w * 0.7), math.pi * 1.12, math.pi * 1.88, lw)


def _w_mouth(s: pygame.Surface, cx: float, cy: float, w: float, lw: int) -> None:
    for sx in (-1, 1):
        pygame.draw.arc(s, NOSE_INK, _ell(cx + sx * w * 0.25, cy, w * 0.5, w * 0.45), math.pi * 1.05,
                        math.pi * 1.95, lw)


def draw_cat(s: pygame.Surface, cx: float, cy: float, rr: float, lw: int) -> None:
    fur, inner = (255, 172, 88), (255, 186, 196)
    edge = art.inkier(fur, 0.4)
    for sx in (-1, 1):
        base = [(cx + sx * 0.18 * rr, cy - 0.78 * rr), (cx + sx * 0.92 * rr, cy - 0.42 * rr),
                (cx + sx * 0.8 * rr, cy - 1.28 * rr)]
        pygame.draw.polygon(s, edge, base)
        pygame.draw.lines(s, edge, True, base, lw * 2)
        inner_pts = [(cx + sx * 0.36 * rr, cy - 0.74 * rr), (cx + sx * 0.8 * rr, cy - 0.52 * rr),
                     (cx + sx * 0.74 * rr, cy - 1.08 * rr)]
        pygame.draw.polygon(s, fur, [(cx + sx * 0.24 * rr, cy - 0.76 * rr), (cx + sx * 0.86 * rr, cy - 0.46 * rr),
                                     (cx + sx * 0.78 * rr, cy - 1.18 * rr)])
        pygame.draw.polygon(s, inner, inner_pts)
    _outline(s, fur, _ell(cx, cy, 2.1 * rr, 1.86 * rr), lw)
    for i, dx in enumerate((-0.2, 0.0, 0.2)):
        end = (cx + dx * rr * 0.8, cy - (0.6 - 0.06 * (i == 1)) * rr)
        pygame.draw.line(s, art.darken(fur, 0.18), (cx + dx * rr, cy - 0.86 * rr), end, lw * 2)
    _eyes(s, cx, cy, rr)
    pygame.draw.polygon(s, (255, 120, 150), [(cx - 0.11 * rr, cy + 0.14 * rr), (cx + 0.11 * rr, cy + 0.14 * rr),
                                            (cx, cy + 0.26 * rr)])
    _w_mouth(s, cx, cy + 0.36 * rr, 0.42 * rr, lw)
    for sx in (-1, 1):
        for dy, ang in ((0.2, 0.12), (0.32, -0.04)):
            x0, y0 = cx + sx * 0.5 * rr, cy + dy * rr
            pygame.draw.line(s, art.inkier(fur, 0.6), (x0, y0), (x0 + sx * 0.5 * rr, y0 - ang * rr), max(1, lw))
    _cheeks(s, cx, cy, rr, 0.6, 0.3, on=fur)


def draw_dog(s: pygame.Surface, cx: float, cy: float, rr: float, lw: int) -> None:
    fur, ear, snout = (222, 168, 116), (150, 98, 72), (250, 228, 200)
    for sx in (-1, 1):
        ears = pygame.Surface((int(0.62 * rr) + 8, int(1.25 * rr) + 8), pygame.SRCALPHA)
        _outline(ears, ear, ears.get_rect().inflate(-6, -6), lw)
        ears = pygame.transform.rotozoom(ears, -sx * 24, 1.0)
        s.blit(ears, ears.get_rect(center=(int(cx + sx * 0.86 * rr), int(cy + 0.12 * rr))))
    _outline(s, fur, _ell(cx, cy, 2.0 * rr, 1.9 * rr), lw)
    pygame.draw.ellipse(s, art.darken(fur, 0.14), _ell(cx + 0.36 * rr, cy - 0.12 * rr, 0.6 * rr, 0.62 * rr))
    _outline(s, snout, _ell(cx, cy + 0.38 * rr, 1.0 * rr, 0.68 * rr), lw)
    _eyes(s, cx, cy, rr, dy=-0.12)
    pygame.draw.ellipse(s, NOSE_INK, _ell(cx, cy + 0.2 * rr, 0.34 * rr, 0.24 * rr))
    pygame.draw.ellipse(s, art.WHITE, _ell(cx - 0.06 * rr, cy + 0.15 * rr, 0.1 * rr, 0.06 * rr))
    pygame.draw.ellipse(s, (255, 120, 140), _ell(cx, cy + 0.6 * rr, 0.22 * rr, 0.26 * rr))
    _w_mouth(s, cx, cy + 0.44 * rr, 0.4 * rr, lw)
    _cheeks(s, cx, cy, rr, 0.62, 0.22, on=fur)


def draw_duck(s: pygame.Surface, cx: float, cy: float, rr: float, lw: int) -> None:
    body, beak = (255, 222, 86), (255, 150, 62)
    for i, dx in enumerate((-0.12, 0.04, 0.18)):
        _outline(s, body, _ell(cx + dx * rr, cy - 0.98 * rr + abs(dx) * 0.4 * rr, 0.26 * rr, 0.46 * rr), lw)
    _outline(s, body, _ell(cx, cy, 1.96 * rr, 1.9 * rr), lw)
    _eyes(s, cx, cy, rr, dx=0.36, dy=-0.18)
    _outline(s, beak, _ell(cx, cy + 0.32 * rr, 1.02 * rr, 0.44 * rr), lw)
    pygame.draw.line(s, art.inkier(beak, 0.5), (cx - 0.36 * rr, cy + 0.33 * rr), (cx + 0.36 * rr, cy + 0.33 * rr), lw)
    _cheeks(s, cx, cy, rr, 0.66, 0.14, on=body)


def draw_cow(s: pygame.Surface, cx: float, cy: float, rr: float, lw: int) -> None:
    white, spot, muzzle, horn = (252, 250, 250), (78, 62, 76), (255, 186, 196), (246, 226, 180)
    for sx in (-1, 1):
        _outline(s, horn, _ell(cx + sx * 0.5 * rr, cy - 0.98 * rr, 0.28 * rr, 0.44 * rr), lw)
        ear = pygame.Surface((int(0.7 * rr) + 8, int(0.4 * rr) + 8), pygame.SRCALPHA)
        _outline(ear, white, ear.get_rect().inflate(-6, -6), lw)
        pygame.draw.ellipse(ear, muzzle, ear.get_rect().inflate(-int(0.3 * rr), -int(0.18 * rr)))
        ear = pygame.transform.rotozoom(ear, sx * 18, 1.0)
        s.blit(ear, ear.get_rect(center=(int(cx + sx * 1.0 * rr), int(cy - 0.42 * rr))))
    _outline(s, white, _ell(cx, cy, 1.94 * rr, 1.92 * rr), lw)
    pygame.draw.ellipse(s, spot, _ell(cx - 0.42 * rr, cy - 0.22 * rr, 0.62 * rr, 0.58 * rr))
    pygame.draw.ellipse(s, spot, _ell(cx + 0.52 * rr, cy - 0.66 * rr, 0.42 * rr, 0.3 * rr))
    _outline(s, muzzle, _ell(cx, cy + 0.48 * rr, 1.32 * rr, 0.74 * rr), lw)
    for sx in (-1, 1):
        pygame.draw.ellipse(s, art.inkier(muzzle, 0.55), _ell(cx + sx * 0.26 * rr, cy + 0.44 * rr, 0.14 * rr, 0.2 * rr))
    for sx in (-1, 1):  # white sclera keeps the eye on the dark spot readable
        pygame.draw.ellipse(s, art.WHITE, _ell(cx + sx * 0.38 * rr, cy - 0.16 * rr, 0.36 * rr, 0.42 * rr))
    _eyes(s, cx, cy, rr, dy=-0.16)
    _smile(s, cx, cy + 0.72 * rr, 0.36 * rr, lw)


def draw_frog(s: pygame.Surface, cx: float, cy: float, rr: float, lw: int) -> None:
    green = (122, 206, 112)
    for sx in (-1, 1):
        _outline(s, green, _ell(cx + sx * 0.5 * rr, cy - 0.62 * rr, 0.84 * rr, 0.84 * rr), lw)
    _outline(s, green, _ell(cx, cy + 0.06 * rr, 2.24 * rr, 1.6 * rr), lw)
    for sx in (-1, 1):
        ex, ey = cx + sx * 0.5 * rr, cy - 0.64 * rr
        pygame.draw.circle(s, art.WHITE, (ex, ey), 0.3 * rr)
        pygame.draw.circle(s, NOSE_INK, (ex + sx * 0.02 * rr, ey + 0.03 * rr), 0.17 * rr)
        pygame.draw.circle(s, art.WHITE, (ex - 0.05 * rr, ey - 0.05 * rr), 0.06 * rr)
    for sx in (-1, 1):
        pygame.draw.circle(s, art.inkier(green, 0.5), (cx + sx * 0.12 * rr, cy + 0.0 * rr), max(2, 0.04 * rr))
    _smile(s, cx, cy + 0.3 * rr, 1.1 * rr, lw + 1)
    _cheeks(s, cx, cy, rr, 0.72, 0.3, on=green)


def draw_sheep(s: pygame.Surface, cx: float, cy: float, rr: float, lw: int) -> None:
    wool, face, ear = (255, 255, 255), (250, 230, 214), (120, 100, 116)
    edge = art.inkier((236, 230, 240), 0.35)
    puffs = [(math.cos(a) * 0.86 * rr, math.sin(a) * 0.8 * rr) for a in
             [math.radians(d) for d in range(150, 400, 26)]]
    for dx, dy in puffs:
        pygame.draw.circle(s, edge, (cx + dx, cy + dy - 0.1 * rr), 0.42 * rr)
    for dx, dy in puffs:
        pygame.draw.circle(s, wool, (cx + dx, cy + dy - 0.1 * rr), 0.42 * rr - lw)
    for sx in (-1, 1):
        e = pygame.Surface((int(0.62 * rr) + 8, int(0.3 * rr) + 8), pygame.SRCALPHA)
        _outline(e, ear, e.get_rect().inflate(-6, -6), lw)
        e = pygame.transform.rotozoom(e, sx * 14, 1.0)
        s.blit(e, e.get_rect(center=(int(cx + sx * 0.82 * rr), int(cy - 0.06 * rr))))
    _outline(s, face, _ell(cx, cy + 0.12 * rr, 1.36 * rr, 1.56 * rr), lw)
    for dx in (-0.3, 0.0, 0.3):  # forehead tuft
        pygame.draw.circle(s, edge, (cx + dx * rr, cy - 0.58 * rr), 0.28 * rr)
    for dx in (-0.3, 0.0, 0.3):
        pygame.draw.circle(s, wool, (cx + dx * rr, cy - 0.58 * rr), 0.28 * rr - lw)
    _eyes(s, cx, cy, rr, dx=0.28, dy=0.02, size=0.14)
    pygame.draw.ellipse(s, (255, 150, 170), _ell(cx, cy + 0.38 * rr, 0.22 * rr, 0.14 * rr))
    _w_mouth(s, cx, cy + 0.5 * rr, 0.32 * rr, lw)
    _cheeks(s, cx, cy, rr, 0.44, 0.34, on=face)


DRAWERS: dict[str, Callable[[pygame.Surface, float, float, float, int], None]] = {
    "cat": draw_cat, "dog": draw_dog, "duck": draw_duck, "cow": draw_cow, "frog": draw_frog, "sheep": draw_sheep,
}
PAW_COLORS: dict[str, art.Color] = {
    "cat": (255, 172, 88), "dog": (222, 168, 116), "duck": (255, 150, 62), "cow": (252, 250, 250),
    "frog": (122, 206, 112), "sheep": (250, 230, 214),
}


def animal_sprite(name: str, rr: int = R) -> pygame.Surface:
    def make() -> pygame.Surface:
        size = int(rr * 2.9)

        def draw(s: pygame.Surface, k: float) -> None:
            DRAWERS[name](s, size * k / 2, size * k / 2 + rr * k * 0.12, rr * k, max(2, int(rr * k * 0.05)))

        return art.supersampled((size, size), draw, ss=2)

    return art.cached(("animal", name, rr), make)


def block_sprite(color: art.Color, icon: str) -> pygame.Surface:
    def make() -> pygame.Surface:
        w, h = BLOCK_W, BLOCK_H
        out = pygame.Surface((w, h + 8), pygame.SRCALPHA)
        out.blit(art.rounded_rect(w, h, 30, art.inkier(color, 0.35)), (0, 8))

        def draw(s: pygame.Surface, k: float) -> None:
            rect = s.get_rect()
            pygame.draw.rect(s, art.inkier(color, 0.32), rect, border_radius=int(30 * k))
            inner = rect.inflate(-int(8 * k), -int(8 * k))
            pygame.draw.rect(s, color, inner, border_radius=int(26 * k))
            top = pygame.Rect(inner.x + 10 * k, inner.y + 8 * k, inner.w - 20 * k, inner.h * 0.2)
            pygame.draw.rect(s, art.lighten(color, 0.32), top, border_radius=int(top.h / 2))
            face = inner.inflate(-int(34 * k), -int(34 * k))
            face.y += int(6 * k)
            pygame.draw.rect(s, art.lighten(color, 0.28), face, border_radius=int(20 * k))
            c = face.center
            ink = art.WHITE
            if icon == "star":
                pygame.draw.polygon(s, ink, art.star_points(c[0], c[1], 34 * k, 15 * k))
            elif icon == "heart":
                pygame.draw.polygon(s, ink, art.heart_points(c[0], c[1] + 2 * k, 70 * k))
            else:
                pygame.draw.circle(s, ink, c, 30 * k)

        out.blit(art.supersampled((w, h), draw, ss=2), (0, 0))
        return out

    return art.cached(("block", color, icon), make)


def paw_sprite(color: art.Color) -> pygame.Surface:
    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            r = s.get_rect()
            pygame.draw.ellipse(s, art.inkier(color, 0.4), r)
            pygame.draw.ellipse(s, color, r.inflate(-int(6 * k), -int(6 * k)))
            for dx in (-0.22, 0.0, 0.22):
                pygame.draw.line(s, art.inkier(color, 0.45), (r.centerx + dx * r.w, r.y + r.h * 0.12),
                                 (r.centerx + dx * r.w, r.y + r.h * 0.42), max(2, int(3 * k)))

        return art.supersampled((46, 36), draw)

    return art.cached(("paw", color), make)


# ------------------------------------------------------------------ the game
@dataclass(eq=False)
class Spot:
    x: float
    color: art.Color
    icon: str
    animal: str = "cat"
    state: str = "hidden"  # hidden | rising | up | falling
    pos: float = 0.0  # 0 hidden .. 1 up
    timer: float = 0.0
    jump: float = 0.0  # extra hop (s left)
    peeks: int = 0
    hearts: list[Any] = field(default_factory=list)

    @property
    def visible(self) -> bool:
        return self.state in ("rising", "up")


class PeekabooFriends(Game):
    id = "peekaboo"

    def __init__(self, host: Any = None, *, size: tuple[int, int] = (art.W, art.H), seed: int | None = None) -> None:
        super().__init__(host, size=size, seed=seed)
        xs = (self.w * 0.21, self.w * 0.5, self.w * 0.79)
        self.spots = [Spot(x, c, i) for x, c, i in zip(xs, BLOCK_COLORS, ICONS)]
        for sp in self.spots:
            sp.animal = self.rng.choice(ANIMALS)
        self.next_peek = 1.2
        self.recent: list[str] = []
        self.last_say = -9.0
        self.peeks = 0
        self.giggles = 0
        self.eyes = art.EyeState(self.w / 2, 82, 0.72, self.eye_color)

    def begin(self) -> None:
        super().begin()
        self.host.sound("boop", volume=0.6)

    # ------------------------------------------------------------ logic
    def pick_animal(self) -> str:
        options = [a for a in ANIMALS if a not in self.recent[-3:]] or list(ANIMALS)
        a = self.rng.choice(options)
        self.recent.append(a)
        del self.recent[:-6]
        return a

    def reveal(self, sp: Spot, *, by_tap: bool) -> None:
        if sp.visible:
            return
        if sp.state == "hidden":
            sp.animal = self.pick_animal()
        sp.state, sp.timer = "rising", STAY_UP + (1.0 if by_tap else 0.0)
        sp.peeks += 1
        self.peeks += 1
        self.score += 1 if by_tap else 0
        self.eyes.feel("surprised" if by_tap else "happy", 0.9)
        if not by_tap:
            self.host.sound("peekaboo")  # the tapped peek answers at once with the animal's voice
        if self.t - self.last_say > 1.1:
            self.last_say = self.t
            self.host.say(PHRASES[sp.animal])
        elif by_tap:
            self.host.sound("boing")
        self.burst(sp.x, BLOCK_TOP - 10, [art.WHITE, art.LEMON, sp.color], 10, speed=(80, 220), life=(0.5, 0.9),
                   size=(6, 10), kinds=("spark", "star"), spread=(math.pi * 1.1, math.pi * 1.9))

    def giggle(self, sp: Spot) -> None:
        sp.jump = 0.5
        sp.timer = max(sp.timer, 1.8)
        self.giggles += 1
        self.score += 1
        self.eyes.feel("happy", 1.0)
        self.host.sound("giggle", max_wait=2.0)  # may queue behind "Moo!": a late giggle beats none
        y = self.head_y(sp) - R * 0.6
        self.burst(sp.x, y, [art.STRAWBERRY, art.BUBBLEGUM, art.BLUSH], 7, speed=(70, 160), life=(0.8, 1.3),
                   size=(9, 13), kinds=("heart",), gravity=-60, spread=(math.pi * 1.15, math.pi * 1.85))

    def step(self, dt: float) -> None:
        for sp in self.spots:
            if sp.state == "rising":
                sp.pos = min(1.0, sp.pos + dt / RISE_S)
                if sp.pos >= 1.0:
                    sp.state = "up"
            elif sp.state == "up":
                sp.timer -= dt
                if sp.timer <= 0:
                    sp.state = "falling"
            elif sp.state == "falling":
                sp.pos = max(0.0, sp.pos - dt / FALL_S)
                if sp.pos <= 0:
                    sp.state = "hidden"
            sp.jump = max(0.0, sp.jump - dt)
        if not any(sp.state != "hidden" for sp in self.spots) and not self.in_finale:
            self.next_peek -= dt
            if self.next_peek <= 0:
                hidden = [sp for sp in self.spots if sp.state == "hidden"]
                self.reveal(self.rng.choice(hidden), by_tap=False)
                self.next_peek = self.rng.uniform(0.9, 2.2)
        up = [sp for sp in self.spots if sp.visible]
        if up:
            self.eyes.look_at_point(up[-1].x, BLOCK_TOP, 340)
        else:
            self.eyes.look(math.sin(self.t * 0.9) * 0.6, 0.3)
        self.eyes.update(dt, self.rng)

    def spot_at(self, x: float, y: float) -> Spot | None:
        if y < BLOCK_TOP - 2.2 * R:
            return None
        best = min(self.spots, key=lambda sp: abs(sp.x - x))
        return best if abs(best.x - x) <= BLOCK_W / 2 + 26 else None

    def on_tap(self, x: float, y: float) -> bool:
        sp = self.spot_at(x, y)
        if sp is None:
            self.eyes.feel("happy", 0.6)
            self.burst(x, y, [art.WHITE, art.LEMON], 5, speed=(40, 120), life=(0.4, 0.7), size=(6, 9), kinds=("spark",))
            self.host.sound("boop", volume=0.6)
            return True
        if sp.visible:
            self.giggle(sp)
        else:
            self.reveal(sp, by_tap=True)
        return True

    def on_key(self, name: str) -> bool:
        idx = {"1": 0, "2": 1, "3": 2, "left": 0, "down": 1, "up": 1, "right": 2}.get(name)
        if idx is None:
            return False
        sp = self.spots[idx]
        self.on_tap(sp.x, BLOCK_TOP + 40)
        return True

    # ------------------------------------------------------------ drawing
    def head_y(self, sp: Spot) -> float:
        hidden_y, up_y = BLOCK_TOP + 1.32 * R, BLOCK_TOP - 0.5 * R
        k = art.ease_out_back(sp.pos, 2.2) if sp.state in ("rising", "up") else art.ease_in_out(sp.pos)
        hop = math.sin(math.pi * (1 - sp.jump / 0.5)) * 34 if sp.jump > 0 else 0.0
        return art.lerp(hidden_y, up_y, k) - hop

    def render(self, surf: pygame.Surface) -> None:
        surf.blit(art.background(self.size, seed=11), (0, 0))
        rug = art.rounded_rect(self.w + 80, 200, 90, (255, 228, 240), border=(255, 196, 222), border_w=4)
        surf.blit(rug, (-40, FLOOR_Y - 10))
        for i in range(14):
            x = 40 + i * 50
            surf.blit(art.circle(4, (255, 255, 255), 200), (x, FLOOR_Y + 22))
        self.eyes.draw(surf)
        for sp in self.spots:
            surf.blit(art.shadow_ellipse(BLOCK_W + 20, 34, 70), (sp.x - BLOCK_W / 2 - 10, BLOCK_TOP + BLOCK_H - 14))
            if sp.state != "hidden":
                spr = animal_sprite(sp.animal)
                wig = math.sin(self.t * 7 + sp.x) * 7 if sp.state == "up" else 0.0
                if sp.jump > 0:
                    wig = math.sin(self.t * 22) * 10
                spr = art.rotated(spr, wig, key=("animal", sp.animal), step=2.0)
                art.blit_center(surf, spr, (sp.x, self.head_y(sp)))
            block = block_sprite(sp.color, sp.icon)
            surf.blit(block, (int(sp.x - BLOCK_W / 2), BLOCK_TOP))
            if sp.state != "hidden" and sp.pos > 0.55:
                paw = paw_sprite(PAW_COLORS[sp.animal])
                lift = 0 if sp.jump <= 0 else -math.sin(math.pi * (1 - sp.jump / 0.5)) * 16
                for sx in (-1, 1):
                    art.blit_center(surf, paw, (sp.x + sx * R * 0.58, BLOCK_TOP + 6 + lift))

    def summary(self) -> str:
        return f"{self.peeks} peekaboo{'s' if self.peeks != 1 else ''}"

    def finale_text(self) -> str:
        return "Bye-bye friends!"

    def status_extra(self) -> dict[str, Any]:
        return {"peeks": self.peeks, "giggles": self.giggles,
                "up": [sp.animal for sp in self.spots if sp.visible]}
