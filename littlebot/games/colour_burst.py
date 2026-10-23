"""Colour Burst: every tap paints a splash of colour or stamps a smiling shape where the finger
landed, with a sparkle and a musical note (low notes on the left, high notes on the right).
Paint fades away slowly, so the canvas never fills up. If nobody taps for a while LittleBot
paints something itself to invite the next tap."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pygame

from littlebot.games import art
from littlebot.games.base import Game

NOTES = ("C4", "D4", "E4", "G4", "A4", "C5", "D5", "E5", "G5")  # pentatonic: every note sounds nice
MAX_MARKS = 20
LIFE = 9.0
FADE = 2.0
APPEAR = 0.42
IDLE_INVITE = 5.0
SHAPES = ("star", "heart", "circle", "flower")
CANVAS_TOP: art.Color = (255, 247, 251)
CANVAS_BOTTOM: art.Color = (255, 228, 240)


@dataclass(eq=False)
class Mark:
    x: float
    y: float
    kind: str  # splat | shape
    shape: str
    color: art.Color
    size: int
    born: float
    rot: float
    seed: int
    auto: bool = False
    dying: float = -1.0  # >= 0: fading out early (canvas full)


def splat_sprite(seed: int, color: art.Color, size: int) -> pygame.Surface:
    def make() -> pygame.Surface:
        import random

        rng = random.Random(seed)
        span = int(size * 2.6)

        def draw(s: pygame.Surface, k: float) -> None:
            c = span * k / 2
            r = size * k * 0.5
            dark = art.mix(color, art.INK, 0.12)
            blobs = []
            for i in range(rng.randint(7, 10)):
                a = rng.uniform(0, 2 * math.pi)
                d = rng.uniform(0.45, 0.85) * r
                blobs.append((c + math.cos(a) * d, c + math.sin(a) * d, rng.uniform(0.32, 0.55) * r))
            drops = []
            for i in range(rng.randint(5, 8)):
                a = rng.uniform(0, 2 * math.pi)
                d = rng.uniform(1.15, 1.65) * r
                drops.append((c + math.cos(a) * d, c + math.sin(a) * d, rng.uniform(0.07, 0.16) * r))
                drops.append((c + math.cos(a) * d * 0.82, c + math.sin(a) * d * 0.82, rng.uniform(0.04, 0.08) * r))
            for x, y, rr in [(c, c, r * 0.95), *blobs, *drops]:
                pygame.draw.circle(s, dark, (x, y), rr + 2.5 * k)
            for x, y, rr in [(c, c, r * 0.95), *blobs, *drops]:
                pygame.draw.circle(s, color, (x, y), rr)
            pygame.draw.circle(s, art.lighten(color, 0.38), (c - r * 0.32, c - r * 0.34), r * 0.24)
            pygame.draw.circle(s, art.lighten(color, 0.55), (c - r * 0.5, c - r * 0.05), r * 0.08)

        return art.supersampled((span, span), draw, ss=2)

    return art.cached(("splat", seed, color, size), make)


def stamp_sprite(shape: str, color: art.Color, size: int) -> pygame.Surface:
    """A big friendly shape with a smiling face."""

    def make() -> pygame.Surface:
        span = int(size * 1.25)
        out = pygame.Surface((span, span), pygame.SRCALPHA)
        c = span / 2
        face_y = c
        if shape == "star":
            spr = art.star(size * 0.6, color, face=False)
            art.blit_center(out, spr, (c, c))
            face_y = c + size * 0.04
        elif shape == "heart":
            art.blit_center(out, art.heart(size * 1.18, color), (c, c))
            face_y = c - size * 0.04
        elif shape == "flower":
            petal = art.circle(size * 0.2, art.lighten(color, 0.1))
            edge = art.circle(size * 0.2 + 2.5, art.inkier(color, 0.35))
            for i in range(6):
                a = i * math.pi / 3
                art.blit_center(out, edge, (c + math.cos(a) * size * 0.3, c + math.sin(a) * size * 0.3))
            for i in range(6):
                a = i * math.pi / 3
                art.blit_center(out, petal, (c + math.cos(a) * size * 0.3, c + math.sin(a) * size * 0.3))
            art.blit_center(out, art.circle(size * 0.25 + 2.5, art.inkier(art.LEMON, 0.35)), (c, c))
            art.blit_center(out, art.circle(size * 0.25, art.LEMON), (c, c))
        else:
            art.blit_center(out, art.circle(size * 0.45 + 3, art.inkier(color, 0.35)), (c, c))
            art.blit_center(out, art.circle(size * 0.45, color), (c, c))
            shine = art.circle(size * 0.12, art.lighten(color, 0.55), 170)
            art.blit_center(out, shine, (c - size * 0.2, c - size * 0.22))
        art.cute_face(out, c, face_y, size * (0.62 if shape == "flower" else 0.8))
        return out

    return art.cached(("stamp", shape, color, size), make)


class ColourBurst(Game):
    id = "colours"

    def __init__(self, host: Any = None, *, size: tuple[int, int] = (art.W, art.H), seed: int | None = None) -> None:
        super().__init__(host, size=size, seed=seed)
        self.marks: list[Mark] = []
        self.last_tap = 0.0
        self.next_invite = 1.0
        self.painted = 0
        self._ci = self.rng.randrange(len(art.CANDY))
        self._kind_i = 0
        self.eyes = art.EyeState(self.w / 2, self.h - 50, 0.62, self.eye_color)

    def begin(self) -> None:
        super().begin()
        self.host.sound("boop", volume=0.6)

    # ------------------------------------------------------------ logic
    def note_for(self, x: float) -> str:
        i = int(art.clamp(x / self.w, 0.0, 0.999) * len(NOTES))
        return NOTES[i]

    def add_mark(self, x: float, y: float, *, auto: bool = False) -> Mark:
        self._ci = (self._ci + self.rng.choice((1, 2, 3))) % len(art.CANDY)
        color = art.CANDY[self._ci]
        self._kind_i += 1
        kind = "shape" if self._kind_i % 2 == 0 else "splat"
        size = self.rng.choice((92, 104, 116)) if not auto else 80
        m = Mark(x=float(x), y=float(y), kind=kind, shape=self.rng.choice(SHAPES), color=color, size=size,
                 born=self.t, rot=self.rng.uniform(-14, 14), seed=self.rng.randrange(1 << 30), auto=auto)
        self.marks.append(m)
        live = [mk for mk in self.marks if mk.dying < 0]
        for old in live[: max(0, len(live) - MAX_MARKS)]:
            old.dying = 0.0
        self.burst(x, y, [color, art.lighten(color, 0.4), art.WHITE], 14, speed=(140, 320), life=(0.45, 0.9),
                   size=(4, 9), radius=size * 0.3)
        self.burst(x, y, [art.WHITE, art.LEMON], 5, speed=(60, 180), life=(0.5, 0.9), size=(7, 11), kinds=("spark",))
        note = self.note_for(x)
        self.host.melody([(note, 0.5)], tempo_bpm=150)
        self.eyes.look_at_point(x, y, 320)
        self.eyes.feel("happy" if not auto else "neutral", 0.6)
        if not auto:
            self.painted += 1
            self.score += 1
        return m

    def step(self, dt: float) -> None:
        for m in list(self.marks):
            if m.dying >= 0:
                m.dying += dt
                if m.dying >= 0.6:
                    self.marks.remove(m)
            elif self.t - m.born > LIFE:
                self.marks.remove(m)
        if self.t - self.last_tap > IDLE_INVITE and not self.in_finale:
            self.next_invite -= dt
            if self.next_invite <= 0:
                self.add_mark(self.rng.uniform(90, self.w - 90), self.rng.uniform(80, self.h - 140), auto=True)
                self.next_invite = 3.5
        self.eyes.update(dt, self.rng)

    def on_tap(self, x: float, y: float) -> bool:
        self.last_tap = self.t
        self.next_invite = 1.0
        self.add_mark(x, y)
        return True

    def on_key(self, name: str) -> bool:
        if name in ("space", "return"):
            self.on_tap(self.rng.uniform(80, self.w - 80), self.rng.uniform(80, self.h - 80))
            return True
        return False

    # ------------------------------------------------------------ drawing
    def alpha_of(self, m: Mark) -> int:
        if m.dying >= 0:
            return int(255 * (1 - m.dying / 0.6))
        age = self.t - m.born
        if age > LIFE - FADE:
            return int(255 * max(0.0, (LIFE - age) / FADE))
        return 255

    def render(self, surf: pygame.Surface) -> None:
        surf.blit(art.background(self.size, top=CANVAS_TOP, bottom=CANVAS_BOTTOM, seed=3), (0, 0))
        for m in self.marks:
            age = self.t - m.born
            if m.kind == "splat":
                spr = splat_sprite(m.seed, m.color, m.size)
            else:  # smiling shapes rock gently for a few seconds (cached rotation steps: cheap)
                rock = 7 * math.sin(age * 4 + m.seed % 7) * max(0.0, 1 - age / 4) if age >= APPEAR else 0.0
                spr = art.rotated(stamp_sprite(m.shape, m.color, m.size), m.rot + rock,
                                  key=("stamp", m.shape, m.color, m.size), step=3.0)
            if age < APPEAR:
                spr = art.scaled(spr, art.ease_out_back(age / APPEAR, 2.4))
            art.blit_center(surf, spr, (m.x, m.y), self.alpha_of(m))
        self.eyes.draw(surf)

    def summary(self) -> str:
        return f"{self.painted} splash{'es' if self.painted != 1 else ''} of colour"

    def finale_text(self) -> str:
        return "What a painting!"

    def status_extra(self) -> dict[str, Any]:
        return {"painted": self.painted, "marks": len(self.marks)}
