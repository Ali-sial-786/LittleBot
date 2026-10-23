"""Plain data describing one rendered face frame (animator -> renderer contract).

The animator (pure Python, no pygame) produces a :class:`FaceFrame`; the
renderer turns it into pixels. Keeping this boundary explicit is what lets the
whole animation system be unit-tested without a window.

All coordinates are pixels in the native 480x320 design space.

The cartoon eye anatomy (where the pupil sits, where the catchlights go) is
plain geometry too and lives here as pure functions (:func:`pupil_geometry`,
:func:`catchlights`), so it can be tested without pygame.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ----------------------------------------------------------------- layout
NATIVE_W = 480
NATIVE_H = 320
EYE_W = 120.0  # base eye width
EYE_H = 138.0  # base eye height
EYE_SPACING = 206.0  # distance between eye centres
EYE_Y = 142.0  # eye centre line
MOUTH_Y = 250.0
GAZE_DX = 48.0  # max horizontal eye travel for gaze -1..1 (the pupils add their own travel)
GAZE_DY = 30.0  # max vertical eye travel
MIN_LINE = 7.0  # thickness of a closed eye

# ----------------------------------------------------------------- eye shape
LID_BOW = 0.10  # upper-lid arch as a fraction of eye height
SMILE_RADIUS = 0.75  # happy crescent cut radius (fraction of eye width)
SMILE_DEPTH = 0.8
RIM = 5.0  # px, darker iris rim drawn just inside the eye outline (the cartoon "ink" line)
PUPIL_RADIUS = 0.30  # pupil half-width as a fraction of eye width (big pupils read as cute)
PUPIL_ASPECT = 1.08  # pupils are slightly tall ovals
PUPIL_SMILE_FADE = (0.38, 0.58)  # happy crescents are closed smiling eyes: no pupil
CATCHLIGHT_BIG = (-0.20, -0.40, 0.125)  # (x * w, y * visible half-height, r * w), from the visible centre
CATCHLIGHT_SMALL = (0.19, 0.30, 0.058)

Color = tuple[int, int, int]


@dataclass(frozen=True)
class EyeFrame:
    cx: float
    cy: float
    w: float
    h: float  # current (possibly squashed / blinking) height
    radius: float  # corner radius px (already clamped to the shape)
    side: int  # -1 = viewer's left eye, +1 = right eye
    lid_top: float = 0.0  # 0..1 fraction of h covered from the top
    lid_angle: float = 0.0  # degrees, + droops towards the outer corner
    lid_bottom: float = 0.0  # 0..1 flat lower lid
    smile: float = 0.0  # 0..1 happy crescent
    tilt: float = 0.0  # degrees, pygame convention (counter-clockwise positive)
    shine: float = 1.0  # specular highlight strength 0..~1.5
    closed_curve: float = 0.0  # >0: draw as a closed, gently curved line (sleeping / peekaboo)
    full_h: float = 0.0  # height before blinking/closing (for anchoring cheeks etc.)
    look_x: float = 0.0  # where the pupil looks inside the eye: -1 (left) .. 1 (right)
    look_y: float = 0.0  # -1 (up) .. 1 (down)
    pupil: float = 1.0  # pupil size multiplier (0 = no pupil)


@dataclass(frozen=True)
class MouthFrame:
    cx: float
    cy: float
    open: float  # 0 closed smile .. 1 wide open
    alpha: float  # 0..1
    width: float = 46.0


@dataclass(frozen=True)
class Sprite:
    """A decorative overlay element (hearts, sparkles, zzz, confetti ...)."""

    kind: str  # heart | sparkle | z | question | dot | confetti | blush | play
    x: float
    y: float
    size: float
    alpha: float = 1.0
    angle: float = 0.0
    color: Color = (255, 255, 255)


@dataclass(frozen=True)
class FaceFrame:
    width: int
    height: int
    t: float
    background: Color
    color: Color  # eye colour after tint / brightness
    glow: float  # 0 disables glow
    eyes: tuple[EyeFrame, EyeFrame]
    mouth: MouthFrame | None = None
    sprites: tuple[Sprite, ...] = ()
    ring: float = 0.0  # listening indicator strength 0..1 (already pulsed)
    hidden: float = 0.0  # 0 face visible .. 1 calm "video playing" screen
    expression: str = "neutral"
    state: str = "idle"
    debug: dict[str, float] = field(default_factory=dict)


# ----------------------------------------------------------------- eye anatomy (pure geometry)
def _smoothstep(x: float) -> float:
    x = 0.0 if x < 0.0 else 1.0 if x > 1.0 else x
    return x * x * (3.0 - 2.0 * x)


def rotate(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate an offset the way ``pygame.transform.rotozoom`` turns a surface (CCW on screen, y down)."""
    if not deg:
        return x, y
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


def open_span_at(e: EyeFrame, x: float) -> tuple[float, float]:
    """Top and bottom of the open part of the eye at eye-local ``x`` (px from the eye centre).

    Eye-local px before the eye's tilt (0 = eye centre): the rounded outline, the upper lid
    (with its arch and slant), a squint lid and the happy crescent's cut all count.
    """
    h, w = e.h, e.w
    rad = max(0.0, min(e.radius, h / 2, w / 2))
    ax = abs(x) - (w / 2 - rad)
    dy = rad - math.sqrt(max(0.0, rad * rad - ax * ax)) if ax > 0 else 0.0
    top, bottom = -h / 2 + dy, h / 2 - dy
    if e.lid_top > 0.003:
        u = max(-1.0, min(1.0, x / max(1.0, w / 2)))
        slope = math.tan(math.radians(e.lid_angle)) * e.side
        bow = LID_BOW * h * min(1.0, e.lid_top * 3)
        top = max(top, -h / 2 + e.lid_top * h + slope * x - bow * (1 - u * u))
    if e.lid_bottom > 0.003:
        bottom = min(bottom, h / 2 - e.lid_bottom * h)
    if e.smile > 0.003:
        r = w * SMILE_RADIUS
        if abs(x) < r:
            bottom = min(bottom, h / 2 + r - e.smile * h * SMILE_DEPTH - math.sqrt(r * r - x * x))
    return top, max(top, bottom)


def eye_span(e: EyeFrame) -> tuple[float, float]:
    """Top and bottom of the open part of the eye on its centre line (see :func:`open_span_at`)."""
    return open_span_at(e, 0.0)


_RING = tuple((math.cos(2 * math.pi * i / 24), math.sin(2 * math.pi * i / 24)) for i in range(24))


def _ellipse_inside(x: float, y: float, rx: float, ry: float, hw: float, hh: float, rad: float) -> bool:
    """Is the ellipse at (x, y) inside the rounded rect (half sizes hw x hh, corner radius rad)?"""
    if abs(x) + rx > hw or abs(y) + ry > hh:
        return False
    cx, cy = hw - rad, hh - rad
    if abs(x) + rx <= cx or abs(y) + ry <= cy:
        return True  # clear of the rounded corners
    r2 = rad * rad
    for c, s in _RING:
        ax, ay = abs(x + rx * c) - cx, abs(y + ry * s) - cy
        if ax > 0.0 and ay > 0.0 and ax * ax + ay * ay > r2:
            return False
    return True


def pupil_geometry(e: EyeFrame) -> tuple[float, float, float, float, float] | None:
    """The pupil as ``(ox, oy, rx, ry, alpha)``, or None when none is drawn.

    ``(ox, oy)`` is the pupil centre's offset from the eye centre in screen px (the eye's
    tilt already applied); ``rx`` / ``ry`` are its radii before the tilt. The pupil sits in
    the middle of the open part of the eye, follows ``look_x`` / ``look_y`` and always stays
    inside the eye outline (lids may cover it). It squashes with a blink (and is gone before
    the eye is a thin line) and shrinks away on happy crescents, which read as closed
    smiling eyes.
    """
    if e.pupil <= 0.01 or e.closed_curve > 0.0:
        return None
    lo, hi = PUPIL_SMILE_FADE
    smile_fade = 1.0 - _smoothstep((e.smile - lo) / (hi - lo))  # shrinks away into a happy crescent
    full_h = max(e.h, e.full_h)
    squash = e.h / full_h
    alpha = min(1.0, 2.0 * smile_fade) * _smoothstep((squash - 0.22) / 0.18)  # gone before the lid line
    if alpha <= 0.01:
        return None
    half_w = e.w / 2 - RIM - 1.5  # room inside the rim
    half_h = e.h / 2 - RIM - 1.5
    rx = min(PUPIL_RADIUS * e.w * e.pupil, half_w)
    ry = min(rx * PUPIL_ASPECT, full_h / 2 - RIM - 1.5) * squash
    if rx < 2.0 or ry < 1.5:
        return None
    top, bottom = eye_span(e)
    lx, ly = rotate(e.look_x, e.look_y, -e.tilt)  # screen direction -> eye-local
    n = math.hypot(lx, ly)
    if n > 1.0:
        lx, ly = lx / n, ly / n
    tx, ty = max(0.0, half_w - rx), max(0.0, half_h - ry)
    x = lx * tx
    y = max(-ty, min(ty, (top + bottom) / 2 + ly * ty))
    # the box above ignores the rounded corners: pull a diagonal look back inside the rim if needed
    hw, hh = e.w / 2 - RIM - 0.5, e.h / 2 - RIM - 0.5
    rad = max(0.0, min(e.radius, e.h / 2, e.w / 2) - RIM - 0.5)
    if not _ellipse_inside(x, y, rx, ry, hw, hh, rad):
        lo, hi = 0.0, 1.0
        for _ in range(7):
            mid = (lo + hi) / 2
            if _ellipse_inside(x * mid, y * mid, rx, ry, hw, hh, rad):
                lo = mid
            else:
                hi = mid
        x, y = x * lo, y * lo
    ox, oy = rotate(x, y, e.tilt)
    rx, ry = rx * smile_fade, ry * smile_fade
    if rx < 1.0 or ry < 1.0:
        return None
    return ox, oy, rx, ry, alpha


def catchlights(e: EyeFrame) -> list[tuple[float, float, float]]:
    """White catchlight circles ``(ox, oy, r)``: offsets from the eye centre in screen px.

    They belong to the light, not to the pupil, so they stay put while the pupil looks
    around (and do not spin with the eye). Each one is kept wholly inside the open part of
    the eye (inside the rim, below a lid, above a crescent's cut, whatever the eye's tilt),
    shrinks away as the eye blinks shut; on happy crescents the big one gets smaller and the
    small one fades out.
    """
    if e.closed_curve > 0.0:
        return []
    full_h = max(e.h, e.full_h)
    k = _smoothstep((e.h / full_h - 0.2) / 0.45)
    top, bottom = eye_span(e)
    vc, vh = (top + bottom) / 2, (bottom - top) / 2
    if k <= 0.02 or vh < 6.0:
        return []
    out: list[tuple[float, float, float]] = []
    big = 1.0 - 0.45 * _smoothstep((e.smile - 0.2) / 0.4)  # a smaller sparkle on a smiling crescent
    small = 1.0 - _smoothstep((e.smile - 0.12) / 0.25)
    margin = RIM * 0.6 + 1.0
    for (fx, fy, fr), weight in ((CATCHLIGHT_BIG, big), (CATCHLIGHT_SMALL, small)):
        r = fr * e.w * k * weight
        lx, ly = rotate(fx * e.w, vc + fy * vh, -e.tilt)  # where the light wants it, in eye-local px
        reach = e.w / 2 - margin
        r = min(r, reach)
        lx = max(-(reach - r), min(reach - r, lx))
        t, b = open_span_at(e, lx)
        r = min(r, (b - t) / 2 - margin)
        if r < 1.2:
            continue
        ly = max(t + margin + r, min(b - margin - r, ly))
        ox, oy = rotate(lx, ly, e.tilt)
        out.append((ox, oy, r))
    return out
