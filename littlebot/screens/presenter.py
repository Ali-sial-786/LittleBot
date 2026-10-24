"""The presenter: LittleBot's own eyes, small, in a corner of an info card, lip-synced to its voice.

``draw(surf, box, mood=..., mouth=..., color=..., t=...)`` paints them into ``box`` (x, y, w, h on
the 720x480 canvas, :attr:`littlebot.screens.base.Card.presenter_box`): the games' eyes
(littlebot/games/art.py ``draw_eyes``: rim, gradient, big pupil, two catchlights) on a little patch
of LittleBot's pink face, with the real face's mouth under them (the same lip outline,
littlebot/face/frame.py ``mouth_edges``, and the same spoken shapes, littlebot/face/mouth.py), so it
reads as LittleBot presenting rather than a sticker.

Everything is a pure function of its arguments, so a frame can be drawn from any thread and a
screenshot is reproducible:

* ``mouth`` (0..1) is the voice's loudness right now (``Topics.AUDIO_VISEME`` ``level``) and ``shape``
  its viseme ("ah", "ee", "oo", ...); silence gives the mood's resting mouth;
* ``t`` (seconds the card has been up) drives blinks every few seconds (now and then a double
  blink), a glance at the card every few seconds, and a little pop-in when the card appears.

Sprites are cached (art.cached), so a frame is a handful of blits.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import pygame

from littlebot.face.frame import MouthFrame, mouth_edges
from littlebot.face.mouth import MouthPose, level_viseme, spoken_pose
from littlebot.games import art
from littlebot.screens.base import H, W

Color = tuple[int, int, int]

# LittleBot's face colours (config face.background_color; littlebot/face/renderer.py MOUTH_*).
FACE_PINK: Color = (255, 214, 228)
MOUTH_INK: Color = (66, 32, 62)
MOUTH_INSIDE: Color = (146, 40, 82)
TONGUE: Color = (255, 120, 146)
TEETH: Color = (255, 255, 255)

# The real face (480x320): eyes 120 wide, mouth 104 px below the eye centre line, 54 px wide.
FACE_EYE_W = 120.0
MOUTH_DROP = 0.72  # eye centre -> mouth centre, as a share of the eye height (the face has 0.75)
MOUTH_GROW = 1.4  # the mouth a touch bigger than to scale: it has to read at this size

BLINK_SECONDS = 0.16
POP_SECONDS = 0.35
GLANCE_EVERY = 5.5  # s between glances at the card
GLANCE_SECONDS = 1.3

# The mood's resting mouth (the face's expressions, roughly) and how the eyes show it.
REST: dict[str, MouthPose] = {
    "neutral": MouthPose(open=0.0, width=1.0, smile=0.45),
    "happy": MouthPose(open=0.22, width=1.12, smile=0.85),
    "surprised": MouthPose(open=0.42, width=0.62, smile=0.0, round=1.0),
    "curious": MouthPose(open=0.0, width=0.82, smile=0.22),
}
EYE_MOOD = {"neutral": "neutral", "happy": "happy", "surprised": "surprised", "curious": "neutral", "sad": "sad"}


def _frac(v: float) -> float:
    return v - math.floor(v)


def _hash(n: int, salt: float) -> float:
    """A fixed pseudo-random 0..1 for slot ``n`` (no RNG state: the same t always looks the same)."""
    return _frac(math.sin(n * 12.9898 + salt * 78.233) * 43758.5453)


def openness(t: float) -> float:
    """How open the eyes are at ``t``: 1, except for a blink every 3..5.5 s (sometimes a double one)."""
    slot = 4.2
    n = math.floor(t / slot)
    at = n * slot + 0.6 + 2.6 * _hash(n, 1.0)  # when this slot's blink starts
    starts = [at]
    if _hash(n, 2.0) < 0.2:
        starts.append(at + BLINK_SECONDS + 0.12)
    for s in starts:
        p = (t - s) / BLINK_SECONDS
        if 0.0 <= p <= 1.0:
            return art.clamp(abs(1.0 - 2.0 * p), 0.08, 1.0)
    return 1.0


def gaze(t: float, box: Sequence[float], mood: str = "neutral") -> tuple[float, float]:
    """Where the pupils look: at the viewer mostly, now and then a glance at the card (canvas centre)."""
    x, y, w, h = box
    dx, dy = W / 2 - (x + w / 2), H * 0.55 - (y + h / 2)
    d = math.hypot(dx, dy) or 1.0
    card = (art.clamp(dx / d * 1.25, -1, 1), art.clamp(dy / d * 1.1, -1, 1))
    home = (0.12, -0.28) if mood == "curious" else (0.0, 0.05)
    if t < POP_SECONDS + 0.5:  # first a look at what is being presented
        k = 1.0
    else:
        p = _frac((t - 1.2) / GLANCE_EVERY) * GLANCE_EVERY  # s into this glance cycle
        ramp = 0.22
        k = art.clamp(min(p / ramp, (GLANCE_SECONDS - p) / ramp), 0.0, 1.0) if p < GLANCE_SECONDS else 0.0
        k = art.ease_in_out(k)
    return (art.lerp(home[0], card[0], k), art.lerp(home[1], card[1], k))


def mouth_pose(mood: str, mouth: float, shape: str = "") -> MouthPose:
    """The mouth for ``mood`` while the voice is at ``mouth`` loudness (the face's own rules)."""
    rest = REST.get(mood, REST["neutral"])
    level = art.clamp(float(mouth or 0.0), 0.0, 1.0)
    if level < 0.04 and shape in ("", "closed"):
        return rest
    return spoken_pose(shape or level_viseme(level), level, rest)


def layout(box: Sequence[float]) -> tuple[float, float, float, float]:
    """(eye centre x, eye centre y, eye scale, mouth y) for ``box``: as big as fits, mouth included."""
    x, y, w, h = box
    # eye height 84s; below the eye centre the mouth sits 0.72 eye heights down and opens ~20s more
    s = min((w - 18) / (art.EYE_GAP + art.EYE_W), (h - 10) / (art.EYE_H * (0.5 + MOUTH_DROP) + 20))
    s = max(0.2, s)
    top = y + (h - art.EYE_H * s * (0.5 + MOUTH_DROP) - 20 * s) / 2
    cy = top + art.EYE_H * s / 2
    return x + w / 2, cy, s, cy + art.EYE_H * s * MOUTH_DROP


def draw(surf: Any, box: Sequence[float], *, mood: str = "neutral", mouth: float = 0.0,
         color: Sequence[int] = art.EYE_DEFAULT, t: float = 0.0, shape: str = "", backing: bool = True) -> None:
    """LittleBot's eyes and mouth in ``box`` (x, y, w, h) of ``surf``.

    ``mood``: "neutral", "happy", "surprised" or "curious"; ``mouth``: the voice's level 0..1 (and
    ``shape`` its viseme, when known); ``color``: the eye colour (config face.eye_color); ``t``: seconds
    the card has been up (blinks, glances, pop-in). ``backing``: the little pink face patch behind them.
    """
    x, y, w, h = (float(v) for v in box)
    col: Color = (int(color[0]), int(color[1]), int(color[2]))
    pop = art.ease_out_back(art.clamp(t / POP_SECONDS, 0.0, 1.0)) if t < POP_SECONDS else 1.0
    if pop <= 0.02:
        return
    if backing:
        _backing(surf, (x, y, w, h), pop)
    cx, cy, s, my = layout((x, y, w, h))
    talking = art.clamp(float(mouth or 0.0), 0.0, 1.0)
    bob = -1.6 * s * talking  # a tiny lift on loud syllables, like the face's speaking bounce
    eye_mood = EYE_MOOD.get(mood, "neutral")
    s_pop = s * pop
    art.draw_eyes(surf, (cx, cy + bob + (1 - pop) * h * 0.2), s_pop, gaze=gaze(t, (x, y, w, h), mood),
                  openness=openness(t), mood=eye_mood, color=col, blush=True)
    if pop >= 0.6:
        pose = mouth_pose(mood, talking, shape)
        k = art.EYE_W * s / FACE_EYE_W * MOUTH_GROW
        art.blit_center(surf, mouth_sprite(pose, k), (cx, my + bob))


def _backing(surf: Any, box: tuple[float, float, float, float], pop: float) -> None:
    """A soft patch of LittleBot's pink face (with a white rim and a drop shadow) for the eyes to sit on."""
    x, y, w, h = box
    bw, bh = int(w * (0.7 + 0.3 * pop)), int(h * (0.7 + 0.3 * pop))
    radius = int(min(bw, bh) * 0.32)
    patch = art.rounded_rect(bw, bh, radius, FACE_PINK, shadow=4, border=art.WHITE, border_w=3)
    surf.blit(patch, (int(x + (w - bw) / 2), int(y + (h - bh) / 2)))


def _q(v: float, step: float) -> float:
    return round(v / step) * step


def mouth_sprite(pose: MouthPose, k: float) -> pygame.Surface:
    """The face's mouth at ``k`` x its size: an ink lip line along both edges; when open, a raspberry
    inside with teeth (and a little tongue on a wide "ah"), as littlebot/face/renderer.py draws it."""
    o, wd, sm = _q(pose.open, 0.05), _q(pose.width, 0.05), _q(pose.smile, 0.05)
    rd, th = _q(pose.round, 0.1), _q(pose.teeth, 0.25)
    k = _q(k, 0.02)
    key = ("presenter-mouth", o, wd, sm, rd, th, k)

    def make() -> pygame.Surface:
        ss = 4
        m = MouthFrame(cx=0.0, cy=0.0, open=o, alpha=1.0, width=54.0 * wd, smile=sm, round=rd, teeth=th)
        top, bot = mouth_edges(m, 40)
        lw = max(2.2, 5.5 * k * 1.1)  # the lip line, a little bolder than to scale so it stays crisp
        ext_x = max(abs(px) for px, _ in top) * k + lw
        ext_y = max(max(abs(py) for _, py in top), max(abs(py) for _, py in bot)) * k + lw
        sw, sh = math.ceil(2 * ext_x + 4), math.ceil(2 * ext_y + 4)
        big = pygame.Surface((sw * ss, sh * ss), pygame.SRCALPHA)
        ox, oy = sw * ss / 2, sh * ss / 2

        def pt(p: tuple[float, float]) -> tuple[float, float]:
            return ox + p[0] * k * ss, oy + p[1] * k * ss

        gap = max(b[1] - a[1] for a, b in zip(top, bot)) * k
        curves: tuple[list[tuple[float, float]], ...] = (top,)
        if gap >= 1.2:
            pygame.draw.polygon(big, (*MOUTH_INSIDE, 255), [pt(p) for p in top] + [pt(p) for p in reversed(bot)])
            n = len(top) - 1
            if th > 0.05:  # upper teeth just under the top lip
                t_h = min(gap / k * 0.55, 8.0) * th + lw / k / 2
                edge = [(px, min(py + t_h * (1.0 - (2.0 * i / n - 1.0) ** 2) ** 0.35, b[1]))
                        for i, ((px, py), b) in enumerate(zip(top, bot))]
                pygame.draw.polygon(big, (*TEETH, 255), [pt(p) for p in top] + [pt(p) for p in reversed(edge)])
            elif o > 0.45 and rd < 0.5:  # a little tongue on the bottom lip
                hw = max(abs(px) for px, _ in top) * 0.55
                low = [(px, py) for (px, py) in bot if abs(px) < hw]
                if len(low) >= 3:
                    floor = max(py for _, py in low)
                    tg = gap / k * 0.35
                    upper = [(px, min(py, max(a[1], floor - tg * math.sqrt(max(0.0, 1.0 - (px / hw) ** 2)))))
                             for (px, py), a in zip(bot, top) if abs(px) < hw]
                    pygame.draw.polygon(big, (*TONGUE, 255), [pt(p) for p in upper] + [pt(p) for p in reversed(low)])
            curves = (top, bot)
        r = lw * ss / 2
        for curve in curves:  # overlapping round dots: an even, round-capped stroke
            for i in range(len(curve) - 1):
                (ax, ay), (bx, by) = pt(curve[i]), pt(curve[i + 1])
                steps = max(1, int(math.hypot(bx - ax, by - ay) / (r * 0.5)))
                for j in range(steps + 1):
                    f = j / steps
                    pygame.draw.circle(big, (*MOUTH_INK, 255), (ax + (bx - ax) * f, ay + (by - ay) * f), r)
        return pygame.transform.smoothscale(big, (sw, sh))

    return art.cached(key, make)


__all__ = ["draw", "gaze", "layout", "mouth_pose", "mouth_sprite", "openness"]
