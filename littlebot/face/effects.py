"""One-shot animations and decorative overlays as pure functions of time.

Nothing here keeps per-frame state: every motion curve and particle position
is computed from ``(progress, elapsed, seed)``, so frames are deterministic,
cheap and trivially testable. The animator combines the results; the renderer
just draws :class:`~littlebot.face.frame.Sprite` objects.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Iterable

from littlebot.core.interfaces import FACE_ANIMATIONS
from littlebot.face.frame import EYE_Y, MOUTH_Y, NATIVE_H, NATIVE_W, Color, EyeFrame, Sprite

# Seconds each one-shot animation lasts.
ANIMATION_DURATIONS: dict[str, float] = {
    "bounce": 0.75,
    "wiggle": 0.9,
    "nod": 0.9,
    "shake": 0.9,
    "spin_eyes": 1.3,
    "sparkle": 1.5,
    "hearts": 2.3,
    "zzz": 2.8,
    "confetti": 3.0,
    "peek": 1.25,
}
assert set(ANIMATION_DURATIONS) == set(FACE_ANIMATIONS)

PASTELS: tuple[Color, ...] = (
    (255, 140, 190),  # pink
    (255, 214, 102),  # sunshine
    (120, 230, 180),  # mint
    (120, 200, 255),  # sky
    (190, 160, 255),  # lavender
    (255, 170, 120),  # peach
)
HEART_COLORS: tuple[Color, ...] = ((255, 110, 170), (255, 140, 190), (255, 90, 140))
SPARKLE_COLOR: Color = (255, 248, 215)
BLUSH_COLOR: Color = (255, 120, 170)


# ----------------------------------------------------------------- easing
def clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def smoothstep(x: float) -> float:
    x = clamp01(x)
    return x * x * (3.0 - 2.0 * x)


def smootherstep(x: float) -> float:
    x = clamp01(x)
    return x * x * x * (x * (x * 6.0 - 15.0) + 10.0)


def ease_out_back(x: float, s: float = 1.70158) -> float:
    x = clamp01(x) - 1.0
    return x * x * ((s + 1.0) * x + s) + 1.0


def bump(u: float, a: float, b: float) -> float:
    """Half-sine bump that is 0 outside (a, b) and 1 at the middle."""
    if u <= a or u >= b:
        return 0.0
    return math.sin(math.pi * (u - a) / (b - a))


# ----------------------------------------------------------------- motion
@dataclass
class Motion:
    """Whole-face transform contribution of an animation."""

    dx: float = 0.0
    dy: float = 0.0
    sx: float = 1.0
    sy: float = 1.0
    rot: float = 0.0  # face rotation (degrees, CCW positive)
    eye_rot: float = 0.0  # each eye spins about its own centre
    gx: float = 0.0  # gaze offset (-1..1 units)
    gy: float = 0.0
    open_mul: float = 1.0

    def add(self, o: "Motion") -> None:
        self.dx += o.dx
        self.dy += o.dy
        self.sx *= o.sx
        self.sy *= o.sy
        self.rot += o.rot
        self.eye_rot += o.eye_rot
        self.gx += o.gx
        self.gy += o.gy
        self.open_mul *= o.open_mul


def animation_motion(name: str, u: float) -> Motion:
    """Transform for animation ``name`` at progress ``u`` (0..1). Identity at u=0 and u=1."""
    u = clamp01(u)
    m = Motion()
    env = math.sin(math.pi * u)
    if name == "bounce":
        # squash -> hop with stretch -> land squash -> settle
        m.dy = 6 * bump(u, 0.0, 0.22) - 26 * bump(u, 0.16, 0.70) + 5 * bump(u, 0.66, 0.86) - 1.5 * bump(u, 0.86, 1.0)
        m.sy = (1 - 0.12 * bump(u, 0.0, 0.22) + 0.08 * bump(u, 0.14, 0.36) + 0.05 * bump(u, 0.5, 0.7)
                - 0.10 * bump(u, 0.66, 0.86) + 0.03 * bump(u, 0.86, 1.0))
        m.sx = 1 + (1 - m.sy) * 0.7
    elif name == "wiggle":
        w = math.sin(2 * math.pi * 3 * u) * env
        m.rot = 9 * w
        m.dx = 7 * w
    elif name == "nod":
        w = math.sin(2 * math.pi * 2 * u) * env
        m.dy = 12 * w
        m.gy = 0.35 * w
    elif name == "shake":
        w = math.sin(2 * math.pi * 3 * u) * env
        m.dx = 16 * w
        m.gx = 0.25 * w
    elif name == "spin_eyes":
        k = smootherstep(u)
        a = 2 * math.pi * k - math.pi / 2
        r = 0.65 * env
        m.gx = r * math.cos(a)
        m.gy = r * math.sin(a) + r  # start and end at the centre
        m.eye_rot = 360.0 * k
    elif name == "sparkle":
        p = 1 + 0.05 * bump(u, 0.0, 0.3)
        m.sx = m.sy = p
    elif name == "hearts":
        p = 1 + 0.04 * bump(u, 0.0, 0.35)
        m.sx = m.sy = p
    elif name == "confetti":
        m.dy = -8 * bump(u, 0.0, 0.3)
        m.sy = 1 + 0.06 * bump(u, 0.0, 0.3)
    elif name == "peek":
        # duck down with eyes shut, rise back up with a bounce and pop them open wide ("peek-a-boo!")
        if u < 0.18:
            m.dy = 70 * smoothstep(u / 0.18)
        else:
            m.dy = 70 * (1 - ease_out_back((u - 0.18) / 0.37, 1.4))
        if u < 0.42:
            m.open_mul = 1 - smoothstep(u / 0.1)
        else:
            m.open_mul = smoothstep((u - 0.42) / 0.12)
        p = 1 + 0.16 * bump(u, 0.45, 0.85)
        m.sx = m.sy = p
    # "zzz" is overlay-only
    return m


# ----------------------------------------------------------------- particles
@dataclass(frozen=True)
class _Particle:
    x: float
    y: float
    delay: float
    life: float
    size: float
    phase: float
    spin: float
    color: Color


def _particles(seed: int, n: int, kind: str) -> list[_Particle]:
    rng = random.Random(seed)
    out: list[_Particle] = []
    for i in range(n):
        if kind == "confetti":
            out.append(_Particle(
                x=rng.uniform(10, NATIVE_W - 10), y=rng.uniform(-60, -8), delay=rng.uniform(0, 0.7),
                life=rng.uniform(1.6, 2.3), size=rng.uniform(8, 13), phase=rng.uniform(0, 6.28),
                spin=rng.uniform(-2.5, 2.5), color=PASTELS[i % len(PASTELS)],
            ))
        elif kind == "sparkle":
            # around the eyes, avoiding their centres
            ang = rng.uniform(0, 2 * math.pi)
            side = -1 if i % 2 == 0 else 1
            cx = NATIVE_W / 2 + side * 98
            out.append(_Particle(
                x=cx + math.cos(ang) * rng.uniform(70, 95), y=EYE_Y + math.sin(ang) * rng.uniform(72, 92),
                delay=rng.uniform(0, 0.75), life=rng.uniform(0.55, 0.8), size=rng.uniform(20, 32),
                phase=rng.uniform(0, 90), spin=rng.uniform(40, 120), color=SPARKLE_COLOR,
            ))
        elif kind == "hearts":
            out.append(_Particle(
                x=70 + (NATIVE_W - 140) * (i + rng.uniform(0.2, 0.8)) / n, y=NATIVE_H + 16,
                delay=i * 0.14 + rng.uniform(0, 0.1), life=rng.uniform(1.3, 1.6), size=rng.uniform(22, 34),
                phase=rng.uniform(0, 6.28), spin=rng.uniform(-12, 12), color=HEART_COLORS[i % len(HEART_COLORS)],
            ))
        else:  # zzz
            out.append(_Particle(
                x=356 + i * 6, y=96, delay=i * 0.55, life=1.7, size=18 + i * 4,
                phase=i * 1.7, spin=0, color=(255, 255, 255),
            ))
    return out


_PARTICLE_COUNTS = {"confetti": 40, "sparkle": 8, "hearts": 6, "zzz": 3}
_CACHE: dict[tuple[str, int], list[_Particle]] = {}


def _cached_particles(kind: str, seed: int) -> list[_Particle]:
    key = (kind, seed)
    ps = _CACHE.get(key)
    if ps is None:
        if len(_CACHE) > 64:
            _CACHE.clear()
        ps = _CACHE[key] = _particles(seed, _PARTICLE_COUNTS[kind], kind)
    return ps


def _confetti_sprite(p: _Particle, v: float, eye_color: Color, alpha: float = 1.0) -> Sprite:
    y = p.y + v * (NATIVE_H + 80)
    x = p.x + 14 * math.sin(p.phase + v * 7.0)
    fade = clamp01((1 - v) * 4) * clamp01(v * 10)
    return Sprite("confetti", x, y, p.size, alpha * fade, angle=p.spin * 360 * v, color=p.color)


def _sparkle_sprite(p: _Particle, v: float, alpha: float = 1.0) -> Sprite:
    s = math.sin(math.pi * clamp01(v))
    return Sprite("sparkle", p.x, p.y, p.size * (0.35 + 0.65 * s), alpha * s, angle=p.phase + p.spin * v,
                  color=p.color)


def _heart_sprite(p: _Particle, v: float, alpha: float = 1.0, rise: float = 250.0) -> Sprite:
    y = p.y - rise * smoothstep(v) - 20 * v
    x = p.x + 12 * math.sin(p.phase + v * 5.0)
    a = alpha * clamp01(v * 5) * clamp01((1 - v) * 3)
    size = p.size * (0.6 + 0.4 * clamp01(v * 4))
    return Sprite("heart", x, y, size, a, angle=p.spin * math.sin(v * 6), color=p.color)


def _z_sprite(x: float, y: float, v: float, size: float, color: Color, alpha: float = 1.0) -> Sprite:
    a = alpha * math.sin(math.pi * clamp01(v)) ** 0.7
    return Sprite("z", x + 46 * v + 5 * math.sin(v * 6.0), y - 92 * v, size * (0.6 + 0.6 * v), a,
                  angle=-12 + 8 * math.sin(v * 4.0), color=color)


def animation_sprites(name: str, elapsed: float, seed: int, eye_color: Color) -> list[Sprite]:
    """Particles for one-shot animation ``name`` ``elapsed`` seconds after it started."""
    out: list[Sprite] = []
    if name == "confetti":
        for p in _cached_particles("confetti", seed):
            v = (elapsed - p.delay) / p.life
            if 0.0 < v < 1.0:
                out.append(_confetti_sprite(p, v, eye_color))
    elif name == "sparkle":
        for p in _cached_particles("sparkle", seed):
            v = (elapsed - p.delay) / p.life
            if 0.0 < v < 1.0:
                out.append(_sparkle_sprite(p, v))
    elif name == "hearts":
        for p in _cached_particles("hearts", seed):
            v = (elapsed - p.delay) / p.life
            if 0.0 < v < 1.0:
                out.append(_heart_sprite(p, v))
    elif name == "zzz":
        for p in _cached_particles("zzz", seed):
            v = (elapsed - p.delay) / p.life
            if 0.0 < v < 1.0:
                out.append(_z_sprite(p.x, p.y, v, p.size, _soft(eye_color)))
    return out


# ----------------------------------------------------------------- looping extras
_LOOP_SEED = 1337
_LOOP_CONFETTI = _particles(_LOOP_SEED, 22, "confetti")
_LOOP_SPARKLES = (
    (58, 66, 0.0, 24), (424, 58, 0.35, 28), (436, 222, 0.7, 20), (46, 230, 0.15, 22), (240, 34, 0.55, 18),
)
_LOOP_HEARTS = (
    _Particle(x=62, y=NATIVE_H + 10, delay=0.0, life=3.3, size=26, phase=0.0, spin=8, color=HEART_COLORS[0]),
    _Particle(x=418, y=NATIVE_H + 10, delay=1.1, life=3.3, size=30, phase=2.0, spin=-8, color=HEART_COLORS[1]),
    _Particle(x=240, y=NATIVE_H + 10, delay=2.2, life=3.3, size=22, phase=4.0, spin=6, color=HEART_COLORS[2]),
)


def _soft(c: Color, k: float = 0.85) -> Color:
    return (int(c[0] + (255 - c[0]) * (1 - k)), int(c[1] + (255 - c[1]) * (1 - k)), int(c[2] + (255 - c[2]) * (1 - k)))


def extras_sprites(
    alphas: dict[str, float], t: float, eyes: Iterable[EyeFrame], eye_color: Color
) -> list[Sprite]:
    """Continuous overlays requested by the current expression (faded by ``alphas``)."""
    out: list[Sprite] = []
    eyes = tuple(eyes)
    a = alphas.get("blush", 0.0)
    if a > 0.01:
        for e in eyes:
            fh = e.full_h or e.h
            out.append(Sprite("blush", e.cx + e.side * 20, e.cy + fh * 0.5 + 4, 50, 0.6 * a, color=BLUSH_COLOR))
    a = alphas.get("confetti", 0.0)
    if a > 0.01:
        for p in _LOOP_CONFETTI:
            v = ((t + p.delay * 4.0) / (p.life + 0.6)) % 1.0
            out.append(_confetti_sprite(p, v, eye_color, a * 0.9))
    a = alphas.get("sparkles", 0.0)
    if a > 0.01:
        for x, y, ph, size in _LOOP_SPARKLES:
            v = (t * 0.55 + ph) % 1.0
            s = math.sin(math.pi * v) ** 2
            if s > 0.02:
                out.append(Sprite("sparkle", x, y, size * (0.4 + 0.6 * s), a * s, angle=45 * v, color=SPARKLE_COLOR))
    a = alphas.get("hearts", 0.0)
    if a > 0.01:
        for p in _LOOP_HEARTS:
            v = ((t + p.delay) / p.life) % 1.0
            out.append(_heart_sprite(p, v, a * 0.9, rise=200.0))
    a = alphas.get("zzz", 0.0)
    if a > 0.01:
        zc = _soft(eye_color)
        for i in range(3):
            v = ((t / 3.3) + i / 3.0) % 1.0
            out.append(_z_sprite(360, 92, v, 18 + 4 * i, zc, a))
    a = alphas.get("question", 0.0)
    if a > 0.01:
        out.append(Sprite("question", 418, 62 + 4 * math.sin(t * 2.4), 46, a, angle=-10 + 8 * math.sin(t * 1.6),
                          color=_soft(eye_color, 0.9)))
    a = alphas.get("dots", 0.0)
    if a > 0.01:
        for i in range(3):
            ph = (t * 1.5 - i * 0.18) % 1.0
            lift = math.sin(math.pi * clamp01(ph / 0.45))
            out.append(Sprite("dot", NATIVE_W / 2 - 26 + 26 * i, MOUTH_Y + 6 - 10 * lift, 14,
                              a * (0.7 + 0.3 * lift), color=_soft(eye_color, 0.9)))
    return out
