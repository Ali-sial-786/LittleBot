"""Plain data describing one rendered face frame (animator -> renderer contract).

The animator (pure Python, no pygame) produces a :class:`FaceFrame`; the
renderer turns it into pixels. Keeping this boundary explicit is what lets the
whole animation system be unit-tested without a window.

All coordinates are pixels in the native 480x320 design space.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ----------------------------------------------------------------- layout
NATIVE_W = 480
NATIVE_H = 320
EYE_W = 108.0  # base eye width
EYE_H = 132.0  # base eye height
EYE_SPACING = 204.0  # distance between eye centres
EYE_Y = 144.0  # eye centre line
MOUTH_Y = 250.0
GAZE_DX = 58.0  # max horizontal eye travel for gaze -1..1
GAZE_DY = 38.0  # max vertical eye travel
MIN_LINE = 7.0  # thickness of a closed eye

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
