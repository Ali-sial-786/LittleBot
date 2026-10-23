"""Bubble Pop: soap bubbles float up; tap one to pop it (pop + sparkle), or tap empty space to
blow a new one. Bubbles that nobody pops pop quietly by themselves near the top. Now and then a
golden star bubble floats by. LittleBot's eyes peek from the bottom and watch."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pygame

from littlebot.games import art
from littlebot.games.base import Game

MAX_BUBBLES = 8
TAP_SLOP = 18.0  # px of forgiveness around a bubble (little fingers, uncalibrated touch)
GOLDEN_EVERY = (14.0, 22.0)
NEW_BUBBLE_COOLDOWN = 0.22
RADII = (38, 44, 50, 56, 62)  # a few fixed sizes keep the sprite cache small


@dataclass(eq=False)
class Bubble:
    x0: float
    y: float
    r: float
    color: art.Color
    vy: float
    phase: float
    wobble: float
    pop_y: float
    born: float
    golden: bool = False
    x: float = 0.0
    grow: float = 1.0  # 0..1 pop-in scale


@dataclass(eq=False)
class Ring:
    x: float
    y: float
    r: float
    color: art.Color
    age: float = 0.0


def bubble_sprite(r: int, color: art.Color) -> pygame.Surface:
    key = ("bubble", r, color)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            c = r * k
            R = r * k
            pygame.draw.circle(s, (*art.lighten(color, 0.3), 105), (c, c), R)
            pygame.draw.circle(s, (*color, 215), (c, c), R, max(2, int(R * 0.15)))
            pygame.draw.circle(s, (*art.lighten(color, 0.45), 190), (c, c), R * 0.86, max(2, int(R * 0.05)))
            pygame.draw.circle(s, (*art.inkier(color, 0.25), 170), (c, c), R, max(1, int(R * 0.035)))
            sheen = art.mix(color, art.SKY if color != art.SKY else art.BUBBLEGUM, 0.65)
            arc = pygame.Rect(0, 0, R * 1.58, R * 1.58)
            arc.center = (int(c), int(c))
            pygame.draw.arc(s, (*sheen, 190), arc, math.radians(-80), math.radians(-8), max(2, int(R * 0.09)))
            hl = pygame.Rect(0, 0, R * 1.32, R * 1.32)
            hl.center = (int(c), int(c))
            pygame.draw.arc(s, (255, 255, 255, 240), hl, math.radians(102), math.radians(168), max(2, int(R * 0.12)))
            pygame.draw.circle(s, (255, 255, 255, 240), (c - R * 0.5, c - R * 0.04), R * 0.07)
            pygame.draw.circle(s, (255, 255, 255, 200), (c + R * 0.4, c + R * 0.42), R * 0.08)

        return art.supersampled((2 * r, 2 * r), draw)

    return art.cached(key, make)


class BubblePop(Game):
    id = "bubbles"

    def __init__(self, host: Any = None, *, size: tuple[int, int] = (art.W, art.H), seed: int | None = None) -> None:
        super().__init__(host, size=size, seed=seed)
        self.bubbles: list[Bubble] = []
        self.rings: list[Ring] = []
        self.spawn_in = 0.4
        self.golden_in = self.rng.uniform(*GOLDEN_EVERY)
        self.popped = 0
        self.self_popped = 0
        self.last_new = -1.0
        self.eyes = art.EyeState(self.w / 2, self.h - 52, 0.75, self.eye_color)
        self._color_i = self.rng.randrange(len(art.CANDY))

    # ------------------------------------------------------------ lifecycle
    def begin(self) -> None:
        super().begin()
        for i in range(4):
            b = self.spawn(y=self.h * (0.35 + 0.17 * i) + self.rng.uniform(-20, 20))
            b.grow = 0.0
        self.host.sound("boop", volume=0.6)

    # ------------------------------------------------------------ logic
    def next_color(self) -> art.Color:
        self._color_i = (self._color_i + self.rng.choice((1, 2, 3))) % len(art.CANDY)
        return art.CANDY[self._color_i]

    def spawn(self, x: float | None = None, y: float | None = None, r: float | None = None,
              golden: bool = False) -> Bubble:
        r = float(round(r)) if r is not None else float(self.rng.choice(RADII))
        if golden:
            r = 70.0
        if x is None:
            # spread out: avoid starting right under the newest bubble
            for _ in range(6):
                x = self.rng.uniform(r + 10, self.w - r - 10)
                if not self.bubbles or abs(x - self.bubbles[-1].x0) > 120:
                    break
        y = y if y is not None else self.h + r + 4
        speed = self.rng.uniform(42, 70) * (1.25 - (r - 38) / 100)
        b = Bubble(x0=float(x), y=float(y), r=float(r), color=art.LEMON if golden else self.next_color(),
                   vy=-speed * (0.8 if golden else 1.0), phase=self.rng.uniform(0, 2 * math.pi),
                   wobble=self.rng.uniform(10, 26), pop_y=self.rng.uniform(56, 150), born=self.t, golden=golden,
                   x=float(x), grow=0.0)
        self.bubbles.append(b)
        return b

    def step(self, dt: float) -> None:
        self.spawn_in -= dt
        if self.spawn_in <= 0 and len(self.bubbles) < MAX_BUBBLES and not self.in_finale:
            self.spawn()
            self.spawn_in = self.rng.uniform(0.8, 1.5)
        self.golden_in -= dt
        if self.golden_in <= 0 and not self.in_finale:
            if not any(b.golden for b in self.bubbles):
                self.spawn(golden=True)
            self.golden_in = self.rng.uniform(*GOLDEN_EVERY)
        for b in list(self.bubbles):
            b.grow = min(1.0, b.grow + dt / 0.35)
            b.y += b.vy * dt
            b.x = b.x0 + math.sin(self.t * 1.3 + b.phase) * b.wobble
            b.x = art.clamp(b.x, b.r * 0.6, self.w - b.r * 0.6)
            if b.y <= b.pop_y:
                self.pop(b, by_tap=False)
        for ring in list(self.rings):
            ring.age += dt
            if ring.age > 0.35:
                self.rings.remove(ring)
        target = self.bubbles[-1] if self.bubbles else None
        if target is not None and self.eyes.mood == "neutral":
            self.eyes.look_at_point(target.x, target.y, 300)
        self.eyes.update(dt, self.rng)

    def bubble_at(self, x: float, y: float) -> Bubble | None:
        for b in reversed(self.bubbles):  # front-most first
            if art.dist(x, y, b.x, b.y) <= b.r * max(0.6, b.grow) + TAP_SLOP:
                return b
        return None

    def pop(self, b: Bubble, *, by_tap: bool) -> None:
        if b not in self.bubbles:
            return
        self.bubbles.remove(b)
        light = art.lighten(b.color, 0.45)
        self.rings.append(Ring(b.x, b.y, b.r, light))
        if by_tap:
            self.popped += 1
            self.score += 1
            self.burst(b.x, b.y, [b.color, light, art.WHITE], 16, speed=(120, 300), life=(0.4, 0.8),
                       size=(4, 9), radius=b.r * 0.6)
            self.burst(b.x, b.y, [art.WHITE, art.LEMON], 5, speed=(40, 140), life=(0.5, 0.9), size=(6, 10),
                       kinds=("spark",))
            self.eyes.look_at_point(b.x, b.y, 300)
            if b.golden:
                self.burst(b.x, b.y, list(art.CANDY), 40, speed=(150, 380), life=(0.8, 1.4), size=(6, 11),
                           kinds=("star", "confetti", "dot"), gravity=260)
                self.eyes.feel("happy", 1.6)
                self.host.sound("sparkle")
            else:
                self.eyes.feel("happy", 0.7)
                self.host.sound("pop")
        else:
            self.self_popped += 1
            self.burst(b.x, b.y, [b.color, light], 8, speed=(60, 160), life=(0.3, 0.6), size=(3, 6),
                       radius=b.r * 0.6)
            self.host.sound("pop", volume=0.3)

    def on_tap(self, x: float, y: float) -> bool:
        b = self.bubble_at(x, y)
        if b is not None:
            self.pop(b, by_tap=True)
            return True
        if self.t - self.last_new >= NEW_BUBBLE_COOLDOWN:
            self.last_new = self.t
            nb = self.spawn(x=art.clamp(x, 40, self.w - 40), y=art.clamp(y, 40, self.h - 20),
                            r=self.rng.choice(RADII[:2]))
            nb.vy *= 1.15
            self.burst(x, y, [art.WHITE], 4, speed=(40, 100), life=(0.3, 0.5), size=(5, 8), kinds=("spark",))
            self.host.sound("blip", volume=0.5)
            self.eyes.look_at_point(x, y, 300)
        return True

    def on_key(self, name: str) -> bool:
        if name in ("space", "return") and self.bubbles:
            self.pop(min(self.bubbles, key=lambda b: b.y), by_tap=True)
            return True
        return False

    # ------------------------------------------------------------ drawing
    def render(self, surf: pygame.Surface) -> None:
        surf.blit(art.background(self.size), (0, 0))
        self.eyes.draw(surf)
        for b in self.bubbles:
            r = int(round(b.r + 1.4 * math.sin(self.t * 3.1 + b.phase)))  # gentle breathing
            spr = bubble_sprite(r, b.color)
            k = art.ease_out_back(b.grow) if b.grow < 1 else 1.0
            if k != 1.0:
                spr = art.scaled(spr, k)
            if b.golden:
                art.blit_center(surf, art.glow(b.r * 1.5, art.LEMON, 0.9), (b.x, b.y))
            art.blit_center(surf, spr, (b.x, b.y))
            if b.golden:
                st = art.star(b.r * 0.55 * k, art.LEMON, rot_deg=self.t * 40, face=True)
                art.blit_center(surf, st, (b.x, b.y))
        for ring in self.rings:
            p = ring.age / 0.35
            spr = art.ring(ring.r * (1.0 + 0.45 * art.ease_out_cubic(p)), max(2, ring.r * 0.08 * (1 - p)), ring.color)
            art.blit_center(surf, spr, (ring.x, ring.y), int(230 * (1 - p)))

    def summary(self) -> str:
        return f"{self.popped} bubble{'s' if self.popped != 1 else ''} popped"

    def finale_text(self) -> str:
        return "Pop pop hooray!"

    def status_extra(self) -> dict[str, Any]:
        return {"popped": self.popped, "bubbles": len(self.bubbles)}
