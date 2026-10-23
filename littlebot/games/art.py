"""Shared look for every game: palette, cached anti-aliased sprites, text, LittleBot's eyes, particles.

Style: soft pink background (matching LittleBot's face), rounded candy-coloured shapes with a
white highlight, a dark plum "ink" for outlines and text, and LittleBot's own cyan eyes as the
host. Everything is drawn for a 720x480 canvas (the robot's screen mode).

Performance (Raspberry Pi 5, 30 fps): anything with curves is drawn once, super-sampled and
smooth-scaled down (clean edges), then cached; a frame is mostly blits. Sizes and angles are
quantised so caches stay small. No display is needed: sprites are converted to the display's
pixel format only when a window exists.
"""

from __future__ import annotations

import math
import random
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

import pygame

Color = tuple[int, int, int]

W, H = 720, 480  # logical canvas (the robot's 720x480 screen)

# ---------------------------------------------------------------- palette
BG_TOP: Color = (255, 238, 245)
BG_BOTTOM: Color = (255, 212, 230)
BG_SPOT: Color = (255, 247, 251)
INK: Color = (98, 50, 88)  # dark plum: outlines and text
INK_SOFT: Color = (166, 108, 150)
WHITE: Color = (255, 255, 255)
CREAM: Color = (255, 250, 246)
BLUSH: Color = (255, 150, 185)
EYE_DEFAULT: Color = (90, 210, 255)  # LittleBot's eye colour (config face.eye_color)
PUPIL: Color = (10, 22, 40)

STRAWBERRY: Color = (255, 102, 132)
TANGERINE: Color = (255, 160, 72)
LEMON: Color = (255, 210, 72)
MINT: Color = (92, 206, 140)
SKY: Color = (78, 192, 240)
PERIWINKLE: Color = (118, 138, 255)
GRAPE: Color = (176, 116, 250)
BUBBLEGUM: Color = (255, 122, 200)
CANDY: tuple[Color, ...] = (STRAWBERRY, TANGERINE, LEMON, MINT, SKY, PERIWINKLE, GRAPE, BUBBLEGUM)

SS = 3  # super-sampling factor for cached sprites


# ---------------------------------------------------------------- maths
def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def approach(cur: float, target: float, rate: float, dt: float) -> float:
    """Frame-rate independent exponential smoothing towards ``target`` (``rate`` per second)."""
    return target + (cur - target) * math.exp(-rate * max(0.0, dt))


def ease_out_cubic(t: float) -> float:
    t = clamp(t, 0.0, 1.0)
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    t = clamp(t, 0.0, 1.0)
    return t * t * (3 - 2 * t)


def ease_out_back(t: float, s: float = 1.70158) -> float:
    t = clamp(t, 0.0, 1.0) - 1
    return t * t * ((s + 1) * t + s) + 1


def mix(a: Sequence[int], b: Sequence[int], t: float) -> Color:
    t = clamp(t, 0.0, 1.0)
    return (int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t), int(a[2] + (b[2] - a[2]) * t))


def lighten(c: Sequence[int], k: float) -> Color:
    return mix(c, WHITE, k)


def darken(c: Sequence[int], k: float) -> Color:
    return mix(c, (0, 0, 0), k)


def inkier(c: Sequence[int], k: float = 0.45) -> Color:
    """Outline colour for a fill: the fill pulled towards the plum ink (warmer than plain black)."""
    return mix(c, INK, k)


def dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


# ---------------------------------------------------------------- caching
class _LRU(OrderedDict):  # type: ignore[type-arg]
    def __init__(self, maxsize: int) -> None:
        super().__init__()
        self.maxsize = maxsize

    def get_or(self, key: Any, make: Callable[[], Any]) -> Any:
        try:
            v = self[key]
            self.move_to_end(key)
            return v
        except KeyError:
            v = make()
            self[key] = v
            if len(self) > self.maxsize:
                self.popitem(last=False)
            return v


_SPRITES = _LRU(900)
_TEXT = _LRU(300)
_FONTS: dict[int, pygame.font.Font] = {}


def cached(key: Any, make: Callable[[], pygame.Surface]) -> pygame.Surface:
    return _SPRITES.get_or(key, lambda: _fast(make()))


def clear_caches() -> None:
    _SPRITES.clear()
    _TEXT.clear()


def _fast(surf: pygame.Surface) -> pygame.Surface:
    """Convert to the display's pixel format (faster blits) when a window exists."""
    try:
        if pygame.display.get_init() and pygame.display.get_surface() is not None:
            return surf.convert_alpha() if surf.get_flags() & pygame.SRCALPHA else surf.convert()
    except pygame.error:
        pass
    return surf


def supersampled(size: tuple[int, int], draw: Callable[[pygame.Surface, float], None], ss: int = SS) -> pygame.Surface:
    """Draw at ``ss`` x the size (``draw(surface, scale)``) and smooth-scale down: anti-aliased edges."""
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    big = pygame.Surface((w * ss, h * ss), pygame.SRCALPHA)
    draw(big, float(ss))
    return pygame.transform.smoothscale(big, (w, h))


# ---------------------------------------------------------------- text
def font(size: int) -> pygame.font.Font:
    f = _FONTS.get(size)
    if f is None:
        if not pygame.font.get_init():
            pygame.font.init()
        f = pygame.font.Font(None, size)  # pygame's bundled bold sans: same everywhere
        _FONTS[size] = f
    return f


def text(s: str, size: int, color: Sequence[int] = INK) -> pygame.Surface:
    key = (s, size, tuple(color))
    return _TEXT.get_or(key, lambda: font(size).render(s, True, color))


def text_outlined(s: str, size: int, color: Sequence[int] = WHITE, outline: Sequence[int] = INK,
                  width: int = 3) -> pygame.Surface:
    """Big friendly title text: fill colour with a rounded outline (readable on any colour)."""
    key = ("outlined", s, size, tuple(color), tuple(outline), width)

    def make() -> pygame.Surface:
        f = font(size)
        base = f.render(s, True, outline)
        top = f.render(s, True, color)
        surf = pygame.Surface((base.get_width() + 2 * width, base.get_height() + 2 * width + 2), pygame.SRCALPHA)
        for dx in range(-width, width + 1):
            for dy in range(-width, width + 1):
                if dx * dx + dy * dy <= width * width + 1:
                    surf.blit(base, (width + dx, width + dy + 2))
        surf.blit(top, (width, width))
        return surf

    return _TEXT.get_or(key, make)


def blit_text(surf: pygame.Surface, s: str, size: int, center: tuple[float, float],
              color: Sequence[int] = INK) -> pygame.Rect:
    img = text(s, size, color)
    r = img.get_rect(center=(int(center[0]), int(center[1])))
    surf.blit(img, r)
    return r


def pill(s: str, size: int = 30, fg: Sequence[int] = INK, bg: Sequence[int] = WHITE, *,
         pad: tuple[int, int] = (18, 8), border: Sequence[int] | None = None) -> pygame.Surface:
    """Rounded label (status / score chips)."""
    key = ("pill", s, size, tuple(fg), tuple(bg), pad, tuple(border) if border else None)

    def make() -> pygame.Surface:
        t = text(s, size, fg)
        w, h = t.get_width() + 2 * pad[0], t.get_height() + 2 * pad[1]
        box = rounded_rect(w, h, h // 2, bg, shadow=3, border=border)
        out = box.copy()
        out.blit(t, ((w - t.get_width()) // 2, (h - t.get_height()) // 2 + 1))
        return out

    return _TEXT.get_or(key, make)


# ---------------------------------------------------------------- backgrounds
def background(size: tuple[int, int] = (W, H), *, top: Color = BG_TOP, bottom: Color = BG_BOTTOM,
               spots: bool = True, seed: int = 7) -> pygame.Surface:
    """Soft vertical pink gradient with a few big, very faint bokeh circles (opaque, cached)."""
    key = ("bg", size, top, bottom, spots, seed)

    def make() -> pygame.Surface:
        w, h = size
        surf = pygame.Surface((w, h))
        for y in range(h):
            surf.fill(mix(top, bottom, y / max(1, h - 1)), (0, y, w, 1))
        if spots:
            rng = random.Random(seed)
            layer = pygame.Surface((w, h), pygame.SRCALPHA)
            for _ in range(9):
                r = rng.randint(40, 120)
                x, y = rng.randint(-20, w + 20), rng.randint(-20, h + 20)
                pygame.draw.circle(layer, (*BG_SPOT, rng.randint(26, 48)), (x, y), r)
            for _ in range(26):
                x, y = rng.randint(0, w), rng.randint(0, h)
                pygame.draw.circle(layer, (255, 255, 255, 70), (x, y), rng.choice((2, 3, 3, 4)))
            surf.blit(layer, (0, 0))
        return surf

    return _SPRITES.get_or(key, lambda: _fast(make()))


def veil(size: tuple[int, int], color: Sequence[int], alpha: int) -> pygame.Surface:
    alpha = int(clamp(alpha, 0, 255)) // 8 * 8
    key = ("veil", size, tuple(color), alpha)

    def make() -> pygame.Surface:
        s = pygame.Surface(size, pygame.SRCALPHA)
        s.fill((*color[:3], alpha))
        return s

    return cached(key, make)


# ---------------------------------------------------------------- shapes
def circle(r: float, color: Sequence[int], alpha: int = 255) -> pygame.Surface:
    r = max(1, int(round(r)))
    key = ("circle", r, tuple(color), alpha)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            pygame.draw.circle(s, (*color[:3], alpha), (r * k, r * k), r * k)

        return supersampled((2 * r, 2 * r), draw)

    return cached(key, make)


def ring(r: float, width: float, color: Sequence[int], alpha: int = 255) -> pygame.Surface:
    r = max(2, int(round(r)))
    width = max(1, int(round(width)))
    key = ("ring", r, width, tuple(color), alpha)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            pygame.draw.circle(s, (*color[:3], alpha), (r * k, r * k), r * k, int(width * k))

        return supersampled((2 * r, 2 * r), draw)

    return cached(key, make)


def glow(r: float, color: Sequence[int], strength: float = 1.0) -> pygame.Surface:
    """Soft radial glow (many concentric circles with rising alpha)."""
    r = max(4, int(round(r)))
    q = int(round(strength * 10))
    key = ("glow", r, tuple(color), q)

    def make() -> pygame.Surface:
        s = pygame.Surface((2 * r, 2 * r), pygame.SRCALPHA)
        s.fill((*color[:3], 0))  # clear pixels carry the colour, so smoothscale doesn't grey the edges
        steps = 24
        for i in range(steps):  # outside in: each ring overwrites the centre with a higher alpha
            f = 1 - i / steps
            a = int(clamp(150 * strength * (1 - f) ** 1.6, 0, 255))
            pygame.draw.circle(s, (*color[:3], a), (r, r), max(1, int(r * f)))
        return pygame.transform.smoothscale(pygame.transform.smoothscale(s, (max(2, r // 2), max(2, r // 2))),
                                            (2 * r, 2 * r))

    return cached(key, make)


def star_points(cx: float, cy: float, r_out: float, r_in: float, rot: float = 0.0,
                points: int = 5) -> list[tuple[float, float]]:
    pts = []
    for i in range(points * 2):
        a = rot - math.pi / 2 + i * math.pi / points
        rr = r_out if i % 2 == 0 else r_in
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


def star(r: float, color: Sequence[int], *, rot_deg: float = 0.0, inner: float = 0.56, outline: bool = True,
         shine: bool = True, face: bool = False) -> pygame.Surface:
    """Chunky rounded 5-point star (rounded by a thick outline stroke), optional cute face."""
    r = max(3, int(round(r)))
    rot_q = int(round(rot_deg / 6.0)) % 12  # 72 deg symmetry: 12 buckets of 6 deg
    key = ("star", r, tuple(color), rot_q, inner, outline, shine, face)

    def make() -> pygame.Surface:
        pad = max(2, r // 6)
        size = 2 * (r + pad)

        def draw(s: pygame.Surface, k: float) -> None:
            c = (r + pad) * k
            rot = math.radians(rot_q * 6.0)
            body = r * k * 0.86
            pts = star_points(c, c, body, body * inner, rot)
            edge = inkier(color, 0.35) if outline else color
            lw = max(2, int(r * k * 0.16))
            pygame.draw.polygon(s, edge, pts)
            for p in pts:  # round the tips
                pygame.draw.circle(s, edge, p, lw // 2 + 1)
            for a, b in zip(pts, pts[1:] + pts[:1]):
                pygame.draw.line(s, edge, a, b, lw)
            inner_pts = star_points(c, c, body - lw * 0.55, (body - lw * 0.55) * inner, rot)
            pygame.draw.polygon(s, color, inner_pts)
            if shine:
                pygame.draw.ellipse(s, lighten(color, 0.62),
                                    pygame.Rect(c - body * 0.36, c - body * 0.3, body * 0.26, body * 0.16))
            if face:
                ex = body * 0.2
                ey = c + body * 0.02
                er = max(2, body * 0.09)
                for sx in (-1, 1):
                    pygame.draw.circle(s, INK, (c + sx * ex, ey), er)
                    pygame.draw.circle(s, WHITE, (c + sx * ex - er * 0.35, ey - er * 0.35), er * 0.38)
                mw = body * 0.22
                rect = pygame.Rect(0, 0, mw, mw * 0.8)
                rect.center = (c, ey + body * 0.16)
                pygame.draw.arc(s, INK, rect, math.pi * 1.1, math.pi * 1.9, max(2, int(body * 0.05)))
                for sx in (-1, 1):
                    pygame.draw.circle(s, mix(color, BLUSH, 0.6), (c + sx * body * 0.36, ey + body * 0.15), body * 0.08)

        return supersampled((size, size), draw)

    return cached(key, make)


def heart_points(cx: float, cy: float, size: float, n: int = 48) -> list[tuple[float, float]]:
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((cx + x * size / 34, cy - y * size / 34 - size * 0.05))
    return pts


def heart(size: float, color: Sequence[int], *, outline: bool = True, shine: bool = True) -> pygame.Surface:
    size = max(4, int(round(size)))
    key = ("heart", size, tuple(color), outline, shine)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            c = size * k / 2
            sz = size * k * 0.92
            if outline:
                pygame.draw.polygon(s, inkier(color, 0.35), heart_points(c, c, sz))
                pygame.draw.polygon(s, color, heart_points(c, c + sz * 0.01, sz * 0.84))
            else:
                pygame.draw.polygon(s, color, heart_points(c, c, sz))
            if shine:
                pygame.draw.ellipse(s, lighten(color, 0.55),
                                    pygame.Rect(c - sz * 0.33, c - sz * 0.3, sz * 0.2, sz * 0.13))

        return supersampled((size, size), draw)

    return cached(key, make)


def rounded_rect(w: int, h: int, radius: int, color: Sequence[int], *, shadow: int = 0,
                 border: Sequence[int] | None = None, border_w: int = 3, shine: bool = False) -> pygame.Surface:
    """Rounded box sprite; ``shadow`` px soft drop shadow below it (the sprite grows by that much)."""
    w, h, radius = int(w), int(h), int(radius)
    key = ("rrect", w, h, radius, tuple(color), shadow, tuple(border) if border else None, border_w, shine)

    def make() -> pygame.Surface:
        out = pygame.Surface((w, h + shadow), pygame.SRCALPHA)
        if shadow:
            sh = supersampled((w, h), lambda s, k: pygame.draw.rect(
                s, (*INK, 46), s.get_rect(), border_radius=int(radius * k)), ss=2)
            out.blit(sh, (0, shadow))

        def draw(s: pygame.Surface, k: float) -> None:
            rect = s.get_rect()
            if border is not None:
                pygame.draw.rect(s, border, rect, border_radius=int(radius * k))
                inset = int(border_w * k)
                pygame.draw.rect(s, color, rect.inflate(-2 * inset, -2 * inset),
                                 border_radius=int(max(1, radius - border_w) * k))
            else:
                pygame.draw.rect(s, color, rect, border_radius=int(radius * k))
            if shine:
                hl = pygame.Rect(rect.x + rect.w * 0.08, rect.y + rect.h * 0.07, rect.w * 0.84, rect.h * 0.22)
                pygame.draw.rect(s, mix(color, WHITE, 0.28), hl, border_radius=int(hl.h / 2))

        out.blit(supersampled((w, h), draw, ss=2), (0, 0))
        return out

    return cached(key, make)


def shadow_ellipse(w: int, h: int, alpha: int = 50) -> pygame.Surface:
    w, h = max(2, int(w)), max(2, int(h))
    key = ("shadow", w, h, alpha)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            rect = s.get_rect()
            for i in range(6):
                f = 1 - i * 0.12
                rr = pygame.Rect(0, 0, rect.w * f, rect.h * f)
                rr.center = rect.center
                pygame.draw.ellipse(s, (*INK, int(alpha / 6)), rr)

        return supersampled((w, h), draw, ss=2)

    return cached(key, make)


def capsule(surf: pygame.Surface, color: Sequence[int], p0: tuple[float, float], p1: tuple[float, float],
            width: float) -> None:
    """Thick line with round ends and a true perpendicular width (pygame's own thick diagonal
    lines come out thinner than their end caps)."""
    (x0, y0), (x1, y1) = p0, p1
    d = math.hypot(x1 - x0, y1 - y0) or 1.0
    nx, ny = -(y1 - y0) / d * width / 2, (x1 - x0) / d * width / 2
    pygame.draw.polygon(surf, color, [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)])
    pygame.draw.circle(surf, color, (x0, y0), width / 2)
    pygame.draw.circle(surf, color, (x1, y1), width / 2)


def blit_center(surf: pygame.Surface, sprite: pygame.Surface, pos: tuple[float, float],
                alpha: int | None = None) -> pygame.Rect:
    r = sprite.get_rect(center=(int(round(pos[0])), int(round(pos[1]))))
    if alpha is not None and alpha < 255:
        if alpha <= 0:
            return r
        sprite.set_alpha(int(alpha))
        surf.blit(sprite, r)
        sprite.set_alpha(255)  # not None: that would switch off per-pixel blending for good
    else:
        surf.blit(sprite, r)
    return r


def scaled(sprite: pygame.Surface, k: float) -> pygame.Surface:
    """Scaled copy of a sprite for pop-in animations (smooth; only used while animating)."""
    k = max(0.02, k)
    if abs(k - 1.0) < 0.01:
        return sprite
    w, h = sprite.get_size()
    return pygame.transform.smoothscale(sprite, (max(1, int(w * k)), max(1, int(h * k))))


def rotated(sprite: pygame.Surface, deg: float, key: Any = None, step: float = 2.0) -> pygame.Surface:
    """Rotated sprite, cached per ``step``-degree bucket when ``key`` identifies the sprite."""
    q = int(round(deg / step))
    if q == 0:
        return sprite
    if key is None:
        return pygame.transform.rotozoom(sprite, q * step, 1.0)
    return _SPRITES.get_or(("rot", key, q, step), lambda: pygame.transform.rotozoom(sprite, q * step, 1.0))


# ---------------------------------------------------------------- LittleBot's eyes
@dataclass
class EyeState:
    """Animated host eyes. Call ``update(dt)``; set ``target_gaze`` / ``mood``; ``draw(surf)``."""

    x: float
    y: float
    scale: float = 1.0
    color: Color = EYE_DEFAULT
    gaze: tuple[float, float] = (0.0, 0.0)  # -1..1 each axis (pupil offset)
    target_gaze: tuple[float, float] = (0.0, 0.0)
    mood: str = "neutral"  # neutral | happy | surprised | sad
    mood_until: float = 0.0
    blink_t: float = -1.0  # >= 0 while blinking
    next_blink: float = 2.5
    t: float = 0.0
    bob: float = 0.0  # vertical bounce offset (px), decays

    def look_at_point(self, px: float, py: float, reach: float = 260.0) -> None:
        dx, dy = px - self.x, py - self.y
        self.target_gaze = (clamp(dx / reach, -1, 1), clamp(dy / (reach * 0.8), -1, 1))

    def look(self, gx: float, gy: float) -> None:
        self.target_gaze = (clamp(gx, -1, 1), clamp(gy, -1, 1))

    def feel(self, mood: str, seconds: float = 1.2) -> None:
        self.mood = mood
        self.mood_until = self.t + seconds
        if mood in ("happy", "surprised"):
            self.bob = 10.0 * self.scale

    def update(self, dt: float, rng: random.Random | None = None) -> None:
        self.t += dt
        gx = approach(self.gaze[0], self.target_gaze[0], 9.0, dt)
        gy = approach(self.gaze[1], self.target_gaze[1], 9.0, dt)
        self.gaze = (gx, gy)
        if self.mood != "neutral" and self.t >= self.mood_until:
            self.mood = "neutral"
        self.bob = approach(self.bob, 0.0, 6.0, dt)
        if self.blink_t >= 0:
            self.blink_t += dt
            if self.blink_t > 0.16:
                self.blink_t = -1.0
        elif self.t >= self.next_blink:
            self.blink_t = 0.0
            self.next_blink = self.t + (rng.uniform(2.2, 5.5) if rng else 3.6)

    def openness(self) -> float:
        if self.blink_t < 0:
            return 1.0
        p = self.blink_t / 0.16
        return clamp(abs(1 - 2 * p), 0.08, 1.0)

    def draw(self, surf: pygame.Surface) -> None:
        bounce = -abs(math.sin(self.t * 10)) * self.bob
        draw_eyes(surf, (self.x, self.y + bounce), self.scale, gaze=self.gaze, openness=self.openness(),
                  mood=self.mood, color=self.color)


EYE_W, EYE_H, EYE_GAP = 64, 84, 112  # at scale 1.0


def eye_body(w: int, h: int, color: Color, open_q: int) -> pygame.Surface:
    """One eye: rim + vertical gradient body; ``open_q`` 1..10 squashes it vertically (blink)."""
    key = ("eye", w, h, tuple(color), open_q)

    def make() -> pygame.Surface:
        hh = max(4, int(h * open_q / 10))

        def draw(s: pygame.Surface, k: float) -> None:
            rect = s.get_rect()
            rad = int(min(rect.w, rect.h) * 0.48)
            pygame.draw.rect(s, mix(color, PUPIL, 0.42), rect, border_radius=rad)
            inner = rect.inflate(-int(w * 0.1 * k), -int(w * 0.1 * k))
            grad = pygame.Surface(inner.size, pygame.SRCALPHA)
            top, bottom = mix(color, PUPIL, 0.18), lighten(color, 0.32)
            for y in range(inner.h):
                grad.fill(mix(top, bottom, y / max(1, inner.h - 1)), (0, y, inner.w, 1))
            mask = pygame.Surface(inner.size, pygame.SRCALPHA)
            pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(), border_radius=max(1, rad - int(w * 0.05 * k)))
            grad.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
            s.blit(grad, inner.topleft)

        return supersampled((w, hh), draw, ss=2)

    return cached(key, make)


def happy_arc(w: int, h: int, color: Color) -> pygame.Surface:
    """Closed happy eye: a thick upturned arc (^)."""
    key = ("happy", w, h, tuple(color))

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            rect = s.get_rect()
            lw = int(w * 0.24 * k)
            arc_rect = pygame.Rect(rect.x + lw // 2, rect.y + lw // 2, rect.w - lw, rect.h * 1.4)
            for i in range(3):
                pygame.draw.arc(s, mix(color, PUPIL, 0.12), arc_rect.inflate(-i, -i), math.radians(18),
                                math.radians(162), lw)
            for a in (18, 162):  # round caps
                ang = math.radians(a)
                cx = arc_rect.centerx + math.cos(ang) * (arc_rect.w / 2 - lw / 2)
                cy = arc_rect.centery - math.sin(ang) * (arc_rect.h / 2 - lw / 2)
                pygame.draw.circle(s, mix(color, PUPIL, 0.12), (cx, cy), lw / 2)

        return supersampled((w, int(h * 0.62)), draw)

    return cached(key, make)


def draw_eyes(surf: pygame.Surface, center: tuple[float, float], scale: float = 1.0, *,
              gaze: tuple[float, float] = (0.0, 0.0), openness: float = 1.0, mood: str = "neutral",
              color: Color = EYE_DEFAULT, blush: bool = True) -> None:
    """LittleBot's two eyes (matches the real face: rim, gradient, big pupil, two catchlights)."""
    cx, cy = center
    big = 1.12 if mood == "surprised" else 1.0
    w, h = int(EYE_W * scale * big), int(EYE_H * scale * big)
    gap = EYE_GAP * scale
    gx, gy = clamp(gaze[0], -1, 1), clamp(gaze[1], -1, 1)
    for side in (-1, 1):
        ex = cx + side * gap / 2 + gx * 6 * scale
        ey = cy + gy * 4 * scale
        if mood == "happy":
            arc = happy_arc(w, h, color)
            blit_center(surf, arc, (ex, ey - h * 0.12))
            if blush:
                blit_center(surf, circle(w * 0.2, BLUSH, 120), (ex + side * w * 0.18, ey + h * 0.32))
            continue
        if mood == "sad":
            openness = min(openness, 0.72)
        open_q = int(clamp(round(openness * 10), 1, 10))
        body = eye_body(w, h, color, open_q)
        r = blit_center(surf, body, (ex, ey))
        if open_q <= 3:
            continue
        pr = w * (0.26 if mood == "surprised" else 0.34)
        px = ex + gx * (w * 0.5 - pr - w * 0.07)
        py = ey + gy * (r.h * 0.5 - pr - w * 0.07) * 0.9
        if mood == "sad":
            py += h * 0.06
        pupil = circle(pr, PUPIL)
        prc = pupil.get_rect(center=(int(px), int(py)))
        clip = surf.get_clip()
        surf.set_clip(r.inflate(-int(w * 0.12), -int(w * 0.12)).clip(clip))  # keep the pupil inside the eye
        surf.blit(pupil, prc)
        surf.set_clip(clip)
        blit_center(surf, circle(w * 0.15, WHITE), (ex - w * 0.2, ey - r.h * 0.22))
        blit_center(surf, circle(w * 0.07, WHITE), (ex + w * 0.17, ey + r.h * 0.12))


# ---------------------------------------------------------------- particles
@dataclass
class Particle:
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max_life: float
    size: float
    color: Color
    kind: str = "dot"  # dot | star | heart | confetti | spark
    rot: float = 0.0
    spin: float = 0.0
    gravity: float = 0.0
    drag: float = 1.5


class Particles:
    """Tiny particle system (sparkles, droplets, confetti, hearts). Pure data + cached sprites."""

    MAX = 360

    def __init__(self, rng: random.Random | None = None) -> None:
        self.items: list[Particle] = []
        self.rng = rng or random.Random()

    def __len__(self) -> int:
        return len(self.items)

    def add(self, p: Particle) -> None:
        self.items.append(p)
        if len(self.items) > self.MAX:
            del self.items[: len(self.items) - self.MAX]

    def burst(self, x: float, y: float, n: int, colors: Sequence[Color], *, speed: tuple[float, float] = (80, 260),
              life: tuple[float, float] = (0.5, 1.0), size: tuple[float, float] = (5, 11),
              kinds: Sequence[str] = ("dot",), gravity: float = 0.0, drag: float = 1.5,
              spread: tuple[float, float] = (0.0, 2 * math.pi), radius: float = 0.0) -> None:
        rng = self.rng
        for i in range(n):
            a = rng.uniform(*spread)
            sp = rng.uniform(*speed)
            lf = rng.uniform(*life)
            self.add(Particle(x + math.cos(a) * radius, y + math.sin(a) * radius, math.cos(a) * sp,
                              math.sin(a) * sp, lf, lf, rng.uniform(*size), colors[i % len(colors)],
                              rng.choice(list(kinds)), rng.uniform(0, 360), rng.uniform(-360, 360), gravity, drag))

    def confetti(self, w: int, n: int = 60, colors: Sequence[Color] = CANDY) -> None:
        rng = self.rng
        for i in range(n):
            lf = rng.uniform(1.6, 2.8)
            self.add(Particle(rng.uniform(0, w), rng.uniform(-60, -5), rng.uniform(-40, 40), rng.uniform(120, 260), lf,
                              lf, rng.uniform(7, 12), colors[i % len(colors)], "confetti", rng.uniform(0, 360),
                              rng.uniform(-300, 300), 60.0, 0.3))

    def update(self, dt: float) -> None:
        if not self.items:
            return
        keep = []
        for p in self.items:
            p.life -= dt
            if p.life <= 0:
                continue
            f = math.exp(-p.drag * dt)
            p.vx *= f
            p.vy = p.vy * f + p.gravity * dt
            p.x += p.vx * dt
            p.y += p.vy * dt
            p.rot += p.spin * dt
            keep.append(p)
        self.items = keep

    def draw(self, surf: pygame.Surface) -> None:
        for p in self.items:
            k = p.life / p.max_life
            alpha = int(255 * min(1.0, k / 0.35))
            size = p.size * (0.55 + 0.45 * min(1.0, k / 0.5))
            if p.kind == "star":
                spr = star(size, p.color, rot_deg=p.rot, outline=False, shine=False)
            elif p.kind == "heart":
                spr = heart(size * 2, p.color, outline=False, shine=False)
            elif p.kind == "spark":
                spr = sparkle(size * 2.2, p.color)
            elif p.kind == "confetti":
                spr = confetti_piece(int(size), p.color, p.rot)
            else:
                spr = circle(size, p.color)
            blit_center(surf, spr, (p.x, p.y), alpha)


def sparkle(size: float, color: Sequence[int] = WHITE) -> pygame.Surface:
    """Four-point twinkle."""
    size = max(4, int(round(size)))
    key = ("sparkle", size, tuple(color))

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            c = size * k / 2
            r = c * 0.98
            n = c * 0.2
            pts = [(c, c - r), (c + n, c - n), (c + r, c), (c + n, c + n), (c, c + r), (c - n, c + n), (c - r, c),
                   (c - n, c - n)]
            pygame.draw.polygon(s, color, pts)
            pygame.draw.circle(s, (255, 255, 255), (c, c), n * 1.1)

        return supersampled((size, size), draw)

    return cached(key, make)


def confetti_piece(size: int, color: Sequence[int], rot: float) -> pygame.Surface:
    q = int(rot // 30) % 6  # 180-degree symmetry: 6 buckets
    key = ("confetti", size, tuple(color), q)

    def make() -> pygame.Surface:
        s = pygame.Surface((size * 2, size), pygame.SRCALPHA)
        pygame.draw.rect(s, color, s.get_rect(), border_radius=max(1, size // 3))
        return pygame.transform.rotozoom(s, q * 30, 1.0)

    return cached(key, make)


def cute_face(surf: pygame.Surface, cx: float, cy: float, size: float, *, look: tuple[float, float] = (0, 0),
              smile: bool = True, blush: bool = True, ink: Color = INK) -> None:
    """Small kawaii face (two glossy dot eyes, smile, cheeks) drawn directly (used on shapes)."""
    ex = size * 0.22
    er = max(2.0, size * 0.085)
    lx, ly = clamp(look[0], -1, 1) * er * 0.5, clamp(look[1], -1, 1) * er * 0.5
    for sx in (-1, 1):
        blit_center(surf, circle(er, ink), (cx + sx * ex + lx, cy + ly))
        blit_center(surf, circle(er * 0.38, WHITE), (cx + sx * ex + lx - er * 0.3, cy + ly - er * 0.35))
        if blush:
            blit_center(surf, circle(size * 0.085, BLUSH, 150), (cx + sx * size * 0.36, cy + size * 0.17))
    if smile:
        mw = size * 0.2
        rect = pygame.Rect(0, 0, int(mw), int(mw * 0.8))
        rect.center = (int(cx), int(cy + size * 0.1))
        pygame.draw.arc(surf, ink, rect, math.pi * 1.1, math.pi * 1.9, max(2, int(size * 0.045)))


def pick_colors(rng: random.Random, n: int, palette: Iterable[Color] = CANDY) -> list[Color]:
    pal = list(palette)
    rng.shuffle(pal)
    return [pal[i % len(pal)] for i in range(n)]
