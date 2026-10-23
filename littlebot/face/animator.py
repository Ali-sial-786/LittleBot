"""Pure, time-based face animation logic (no pygame, no threads).

``FaceAnimator`` owns everything that moves: expression transitions, blinking,
gaze (explicit look / soft tracking / idle wandering with micro-saccades),
breathing, speech pulses, listening indicator, one-shot animations, eyelid
closing and the hidden (video) fade. Call :meth:`update` with a time step and
:meth:`frame` to get a renderer-agnostic :class:`FaceFrame`.

It is deterministic for a given RNG seed and sequence of ``update(dt)`` calls,
which is what the unit tests rely on. It is *not* thread-safe on its own; the
:class:`~littlebot.face.controller.FaceController` serialises access.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from typing import Any

from littlebot.core.config import FaceConfig
from littlebot.core.events import Topics
from littlebot.core.interfaces import FACE_ANIMATIONS, FACE_STATES
from littlebot.face import effects
from littlebot.face.effects import Motion, clamp01, smootherstep, smoothstep
from littlebot.face.expressions import (
    DEFAULT_EXPRESSION_FOR_STATE, EXPRESSION_PARAMS, EXTRAS, STATE_TINTS, ExpressionParams, lerp_expression,
)
from littlebot.face.frame import (
    EYE_H, EYE_SPACING, EYE_W, EYE_Y, GAZE_DX, GAZE_DY, MIN_LINE, MOUTH_Y, NATIVE_H, NATIVE_W, EyeFrame,
    FaceFrame, MouthFrame, Sprite,
)

log = logging.getLogger("littlebot.face")

# ---------------------------------------------------------------- tunables
BLINK_CLOSE = 0.07  # seconds to close
BLINK_HOLD = 0.04  # seconds fully closed
BLINK_OPEN = 0.13  # seconds to reopen
BLINK_GAP = 0.07  # pause between the two blinks of a double blink
CLOSE_SECONDS = 0.22  # close_eyes() / falling asleep
OPEN_SECONDS = 0.16  # open_eyes() / waking
POP_SECONDS = 0.45  # little "boing" when the eyes reopen after being closed
TRACK_TIMEOUT = 1.2  # a tracking target older than this is ignored
LEVEL_TIMEOUT = 0.25  # no audio.level for this long while speaking -> synthesize
SPEECH_MIN_TIMEOUT = 8.0  # safety: stop the talking animation if speech_finished never arrives
HIDE_SECONDS = 0.45
EXTRAS_FADE_SECONDS = 0.3
MICRO_AMPLITUDE = (0.03, 0.025)
MAX_ANIMATIONS = 6
MAX_STEP = 0.5  # clamp huge dt (e.g. after a stall) so nothing jumps wildly

# Spring stiffness (rad/s) for each gaze source: higher = snappier.
OMEGA = {"explicit": 18.0, "track": 9.0, "idle": 24.0, "rest": 5.0}

# Extras that move quickly enough to need the full frame rate.
FAST_EXTRAS = ("dots", "sparkles", "confetti", "hearts")


def blink_amount(elapsed: float, speed: float = 1.0, double: bool = False) -> float:
    """Eyelid closure 0..1 ``elapsed`` seconds into a blink (0 outside it)."""
    single = (BLINK_CLOSE + BLINK_HOLD + BLINK_OPEN) * speed
    if double:
        if elapsed >= single + BLINK_GAP * speed:
            elapsed -= single + BLINK_GAP * speed
        elif elapsed >= single:
            return 0.0
    if elapsed < 0 or elapsed >= single:
        return 0.0
    c, h = BLINK_CLOSE * speed, BLINK_HOLD * speed
    if elapsed < c:
        return smoothstep(elapsed / c)
    if elapsed < c + h:
        return 1.0
    return 1.0 - smoothstep((elapsed - c - h) / (BLINK_OPEN * speed))


def blink_duration(speed: float = 1.0, double: bool = False) -> float:
    single = (BLINK_CLOSE + BLINK_HOLD + BLINK_OPEN) * speed
    return single * 2 + BLINK_GAP * speed if double else single


def _spring(x: float, v: float, target: float, omega: float, dt: float) -> tuple[float, float]:
    """Exact critically-damped spring step (stable for any dt)."""
    d = x - target
    e = math.exp(-omega * dt)
    tmp = (v + omega * d) * dt
    return target + (d + tmp) * e, (v - omega * tmp) * e


def _approach(cur: float, target: float, rate: float, dt: float) -> float:
    """Move linearly towards target at ``rate`` units/second."""
    step = rate * dt
    if cur < target:
        return min(target, cur + step)
    return max(target, cur - step)


def _ease_exp(cur: float, target: float, rate: float, dt: float) -> float:
    return target + (cur - target) * math.exp(-rate * dt)


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def _mix(a: tuple[float, ...], b: tuple[float, ...], k: float) -> tuple[float, float, float]:
    return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k)


@dataclass
class _ActiveAnim:
    name: str
    start: float
    duration: float
    seed: int


class FaceAnimator:
    """All face animation state. Not thread-safe (see module docstring)."""

    def __init__(
        self,
        config: FaceConfig | None = None,
        *,
        width: int = NATIVE_W,
        height: int = NATIVE_H,
        rng: random.Random | None = None,
        seed: int | None = None,
    ) -> None:
        self.cfg = config or FaceConfig()
        self.rng = rng if rng is not None else random.Random(seed)
        self.width, self.height = int(width), int(height)
        self.t = 0.0
        self.state = "idle"
        self.expression = "neutral"
        neutral = EXPRESSION_PARAMS["neutral"]
        self._from: ExpressionParams = neutral
        self._to: ExpressionParams = neutral
        self.params: ExpressionParams = neutral
        self._trans_elapsed = 0.0
        self._trans_duration = 0.0
        self.hold_until: float | None = None
        self.extras_alpha: dict[str, float] = {n: 0.0 for n in EXTRAS}
        self._state_tint_mix = 0.0
        self._state_tint = (175.0, 165.0, 255.0)
        # blinking
        self._blink_start: float | None = None
        self._blink_double = False
        self._blink_speed = 1.0
        self.next_blink = self._blink_interval()
        self.blink_count = 0
        # eyelids
        self.eyes_closed = False
        self.closure = 0.0
        self._pop_start: float | None = None
        # gaze (internal units -1..1, 0 = centre)
        self.gx = self.gy = 0.0
        self._vx = self._vy = 0.0
        self._explicit: tuple[float, float, float] | None = None  # x, y, until
        self._track: tuple[float, float, float] | None = None  # x, y, last update time
        self._wander = (0.0, 0.0)
        self._next_wander = self.t + self.rng.uniform(0.8, 2.0)
        self._micro = [0.0, 0.0]
        self._micro_target = (0.0, 0.0)
        self._next_micro = 0.0
        self.gaze_source = "idle"
        # breathing
        self._breath_phase = 0.0
        # speech
        self.speech_active = False
        self._speech_deadline = 0.0
        self.level = 0.0
        self._level_target = 0.0
        self._last_level_t = -1e9
        self._next_syllable = 0.0
        self.mouth_alpha = 0.0
        # listening / hidden
        self.listening_active = False
        self._listen_alpha = 0.0
        self.hidden_amount = 0.0
        # one-shot animations
        self.animations: list[_ActiveAnim] = []
        # notifications for the controller: (topic, payload)
        self._events: list[tuple[str, dict[str, Any]]] = []

    # ================================================================ commands
    def set_expression(self, name: str, hold: float | None = None) -> bool:
        if name not in EXPRESSION_PARAMS:
            return False
        if hold is not None and (not math.isfinite(hold) or hold <= 0):
            hold = None
        self.hold_until = self.t + hold if hold is not None else None
        self._go(name)
        return True

    def set_state(self, state: str) -> bool:
        if state not in FACE_STATES:
            return False
        if state == self.state:
            return True
        old = self.state
        self.state = state
        self.hold_until = None
        self._go(DEFAULT_EXPRESSION_FOR_STATE[state])
        if (old == "hidden") != (state == "hidden"):
            self._emit(Topics.FACE_VISIBLE, {"visible": state != "hidden"})
        if state in ("sleeping", "hidden"):
            self._blink_start = None
        return True

    def look_at(self, x: float, y: float, hold: float | None = None) -> bool:
        if not (math.isfinite(x) and math.isfinite(y)):
            return False
        if hold is None or not math.isfinite(hold) or hold <= 0:
            hold = self.cfg.look_hold_seconds
        tx, ty = _clamp((x - 0.5) * 2), _clamp((y - 0.5) * 2)
        jump = math.hypot(tx - self.gx, ty - self.gy)
        self._explicit = (tx, ty, self.t + hold)
        if jump > 0.6 and self._blink_start is None and self._can_blink() and self.rng.random() < 0.3:
            self._start_blink(False)
        return True

    def track_gaze(self, x: float, y: float) -> bool:
        if not (math.isfinite(x) and math.isfinite(y)):
            return False
        self._track = (_clamp((x - 0.5) * 2), _clamp((y - 0.5) * 2), self.t)
        return True

    def clear_gaze(self) -> None:
        self._explicit = None
        self._track = None
        self._next_wander = self.t + self.rng.uniform(0.6, 1.5)
        self._wander = (0.0, 0.0)

    def blink(self, double: bool = False) -> None:
        if self._can_blink():
            self._start_blink(bool(double))
        self.next_blink = self.t + self._blink_interval()

    def close_eyes(self) -> None:
        self.eyes_closed = True
        self._blink_start = None

    def open_eyes(self) -> None:
        self.eyes_closed = False

    def play_animation(self, name: str) -> bool:
        if name not in FACE_ANIMATIONS:
            return False
        self.animations = [a for a in self.animations if a.name != name]
        self.animations.append(_ActiveAnim(name, self.t, effects.ANIMATION_DURATIONS[name],
                                           self.rng.randrange(1 << 30)))
        del self.animations[:-MAX_ANIMATIONS]
        if name == "peek":
            self.eyes_closed = False
        return True

    # -- bus-driven inputs ----------------------------------------------
    def speech_started(self, duration_estimate: float | None = None) -> None:
        self.speech_active = True
        est = float(duration_estimate or 0.0) if duration_estimate is not None else 0.0
        if not math.isfinite(est):
            est = 0.0
        self._speech_deadline = self.t + max(SPEECH_MIN_TIMEOUT, est * 1.5 + 4.0)

    def speech_finished(self) -> None:
        self.speech_active = False

    def speech_level(self, level: float) -> None:
        try:
            level = float(level)
        except (TypeError, ValueError):
            return
        if not math.isfinite(level):
            return
        self._level_target = clamp01(level)
        self._last_level_t = self.t
        if self.speech_active:
            self._speech_deadline = max(self._speech_deadline, self.t + 3.0)

    def listening_started(self) -> None:
        self.listening_active = True

    def listening_stopped(self) -> None:
        self.listening_active = False

    # ================================================================ queries
    @property
    def talking(self) -> bool:
        return self.speech_active or self.state == "speaking" or self.expression == "speaking"

    @property
    def visible(self) -> bool:
        return self.state != "hidden"

    @property
    def transitioning(self) -> bool:
        return self._trans_elapsed < self._trans_duration

    @property
    def blinking(self) -> bool:
        return self._blink_start is not None

    def gaze(self) -> tuple[float, float]:
        """Current gaze in 0..1 screen coordinates (includes micro-movements)."""
        x = _clamp(self.gx + self._micro[0] * self._micro_gain())
        y = _clamp(self.gy + self._micro[1] * self._micro_gain())
        return (x + 1) / 2, (y + 1) / 2

    def current_blink(self) -> float:
        if self._blink_start is None:
            return 0.0
        return blink_amount(self.t - self._blink_start, self._blink_speed, self._blink_double)

    def active_animations(self) -> list[str]:
        return [a.name for a in self.animations]

    def is_animating(self) -> bool:
        """True while something moves fast enough to need the full frame rate."""
        if self.state == "hidden" and self.hidden_amount >= 1.0:
            return False
        if self.transitioning or self.blinking or self.animations or self.talking:
            return True
        if self._pop_start is not None:
            return True
        closed_target = 1.0 if self._closed_target() else 0.0
        if abs(self.closure - closed_target) > 1e-3:
            return True
        if 0.0 < self.hidden_amount < 1.0:
            return True
        if math.hypot(self._vx, self._vy) > 0.05:
            return True
        for name in FAST_EXTRAS:
            if self.extras_alpha.get(name, 0.0) > 0.02:
                return True
        if any(0.0 < a < 1.0 for a in self.extras_alpha.values()):
            return True
        if self.params.bob > 0.5:
            return True
        return False

    def pop_events(self) -> list[tuple[str, dict[str, Any]]]:
        ev, self._events = self._events, []
        return ev

    # ================================================================ update
    def update(self, dt: float) -> None:
        if not math.isfinite(dt) or dt <= 0:
            return
        dt = min(dt, MAX_STEP)
        self.t += dt
        t = self.t

        # expression hold expiry
        if self.hold_until is not None and t >= self.hold_until:
            self.hold_until = None
            self._go(DEFAULT_EXPRESSION_FOR_STATE[self.state])

        # transition
        if self._trans_elapsed < self._trans_duration:
            self._trans_elapsed = min(self._trans_duration, self._trans_elapsed + dt)
            k = smootherstep(self._trans_elapsed / self._trans_duration)
            self.params = lerp_expression(self._from, self._to, k)
        else:
            self.params = self._to

        # extras fade
        for name in EXTRAS:
            target = 1.0 if name in self._to.extras else 0.0
            self.extras_alpha[name] = _approach(self.extras_alpha[name], target, 1 / EXTRAS_FADE_SECONDS, dt)

        # state colour mood
        tint = STATE_TINTS.get(self.state)
        if tint is not None:
            self._state_tint = tint[0]
        self._state_tint_mix = _approach(self._state_tint_mix, tint[1] if tint else 0.0, 1.5, dt)

        self._update_lids(dt)
        self._update_blink()
        self._update_gaze(dt)

        if self.cfg.breathing:
            period = 6.0 if self.state == "sleeping" else 4.2
            self._breath_phase = (self._breath_phase + dt * 2 * math.pi / period) % (2 * math.pi)

        self._update_speech(dt)

        listen = self.listening_active or self.state == "listening"
        self._listen_alpha = _approach(self._listen_alpha, 1.0 if listen else 0.0, 1 / EXTRAS_FADE_SECONDS, dt)
        self.hidden_amount = _approach(self.hidden_amount, 1.0 if self.state == "hidden" else 0.0, 1 / HIDE_SECONDS, dt)

        if self.animations:
            self.animations = [a for a in self.animations if t - a.start < a.duration]

    # -- pieces -------------------------------------------------------------
    def _closed_target(self) -> bool:
        if any(a.name == "peek" for a in self.animations):
            return False
        return self.eyes_closed or self.state == "sleeping"

    def _update_lids(self, dt: float) -> None:
        target = 1.0 if self._closed_target() else 0.0
        before = self.closure
        rate = 1 / CLOSE_SECONDS if target > self.closure else 1 / OPEN_SECONDS
        self.closure = _approach(self.closure, target, rate, dt)
        if target == 0.0 and before >= 0.9 and self.closure < before:
            self._pop_start = self.t  # reopening: little boing
        if self._pop_start is not None and self.t - self._pop_start > POP_SECONDS + OPEN_SECONDS:
            self._pop_start = None

    def _can_blink(self) -> bool:
        if self.state in ("sleeping", "hidden") or self.closure > 0.3:
            return False
        return not any(a.name in ("peek", "spin_eyes") for a in self.animations)

    def _blink_interval(self) -> float:
        lo, hi = self.cfg.blink_interval
        lo, hi = max(0.2, float(lo)), max(float(lo), float(hi))
        rate = self.params.blink_rate if hasattr(self, "params") else 1.0
        return self.rng.uniform(lo, hi) * max(0.2, rate)

    def _start_blink(self, double: bool) -> None:
        self._blink_start = self.t
        self._blink_double = double
        self._blink_speed = max(0.3, self.params.blink_speed)
        self.blink_count += 1
        self._emit(Topics.FACE_BLINK, {"double": double})

    def _update_blink(self) -> None:
        t = self.t
        if self._blink_start is not None:
            if t - self._blink_start >= blink_duration(self._blink_speed, self._blink_double):
                self._blink_start = None
        if self._blink_start is None and t >= self.next_blink:
            if self._can_blink():
                self._start_blink(self.rng.random() < self.cfg.double_blink_chance)
            self.next_blink = t + self._blink_interval()

    def _micro_gain(self) -> float:
        if self.state in ("sleeping", "hidden") or self.closure > 0.5:
            return 0.0
        return 0.5 if self.gaze_source == "track" else 1.0

    def _update_gaze(self, dt: float) -> None:
        t, p = self.t, self.params
        source = "idle"
        tx = ty = 0.0
        if self._explicit is not None:
            if t < self._explicit[2]:
                tx, ty, source = self._explicit[0], self._explicit[1], "explicit"
            else:
                self._explicit = None
        if source == "idle" and self._track is not None:
            if t - self._track[2] <= TRACK_TIMEOUT:
                tx, ty, source = self._track[0], self._track[1], "track"
            else:
                self._track = None
        if source == "idle":
            if self.state in ("sleeping", "hidden") or self.closure > 0.5:
                source = "rest"
            elif self.cfg.idle_gaze and p.wander > 0.01:
                if t >= self._next_wander:
                    self._pick_wander()
                tx, ty = self._wander[0] * p.wander, self._wander[1] * p.wander
        bias = 1.0 if source in ("idle", "rest") else 0.3
        if source == "rest":
            bias = 0.5
        tx = _clamp(tx + p.gaze_x * bias)
        ty = _clamp(ty + p.gaze_y * bias)
        self.gaze_source = source
        omega = OMEGA[source]
        self.gx, self._vx = _spring(self.gx, self._vx, tx, omega, dt)
        self.gy, self._vy = _spring(self.gy, self._vy, ty, omega, dt)
        self.gx, self.gy = _clamp(self.gx), _clamp(self.gy)

        # micro-saccades
        if t >= self._next_micro:
            ax, ay = MICRO_AMPLITUDE
            self._micro_target = (self.rng.uniform(-ax, ax), self.rng.uniform(-ay, ay))
            self._next_micro = t + self.rng.uniform(0.35, 1.1)
        self._micro[0] = _ease_exp(self._micro[0], self._micro_target[0], 14.0, dt)
        self._micro[1] = _ease_exp(self._micro[1], self._micro_target[1], 14.0, dt)

    def _pick_wander(self) -> None:
        r = self.rng
        if r.random() < 0.42:
            nx, ny = r.uniform(-0.12, 0.12), r.uniform(-0.1, 0.1)
        else:
            nx, ny = r.uniform(-0.8, 0.8), r.uniform(-0.6, 0.5)
        if math.hypot(nx - self._wander[0], ny - self._wander[1]) > 0.7 and self._blink_start is None \
                and self._can_blink() and r.random() < 0.25:
            self._start_blink(False)  # people often blink with big eye movements
        self._wander = (nx, ny)
        self._next_wander = self.t + r.uniform(0.9, 3.4) / max(0.3, self.params.wander)

    def _update_speech(self, dt: float) -> None:
        t = self.t
        if self.speech_active and t > self._speech_deadline:
            log.debug("speech_finished never arrived; stopping talking animation")
            self.speech_active = False
        talking = self.talking
        if talking:
            if t - self._last_level_t > LEVEL_TIMEOUT and t >= self._next_syllable:
                r = self.rng
                self._next_syllable = t + r.uniform(0.09, 0.21)
                self._level_target = 0.05 if r.random() < 0.18 else r.uniform(0.35, 1.0)
        else:
            self._level_target = 0.0
        rate = 30.0 if self._level_target > self.level else 14.0
        self.level = _ease_exp(self.level, self._level_target, rate, dt)
        self.mouth_alpha = _approach(self.mouth_alpha, 1.0 if talking else 0.0, 1 / 0.2, dt)

    def _go(self, name: str) -> None:
        if name == self.expression and self._to is EXPRESSION_PARAMS[name]:
            return
        self._from = self.params
        self._to = EXPRESSION_PARAMS[name]
        self._trans_elapsed = 0.0
        self._trans_duration = max(0.0, float(self.cfg.transition_seconds))
        if self._trans_duration <= 0:
            self.params = self._to
        changed = name != self.expression
        self.expression = name
        if changed:
            self._emit(Topics.FACE_EXPRESSION, {"expression": name})

    def _emit(self, topic: str, data: dict[str, Any]) -> None:
        if len(self._events) < 64:
            self._events.append((topic, data))

    # ================================================================ frame
    def motion(self) -> Motion:
        """Combined transform of all running one-shot animations."""
        m = Motion()
        for a in self.animations:
            m.add(effects.animation_motion(a.name, (self.t - a.start) / a.duration))
        return m

    def eye_color(self) -> tuple[int, int, int]:
        p = self.params
        base = tuple(float(c) for c in self.cfg.eye_color)
        c = _mix(base, p.tint, clamp01(p.tint_mix))
        if self._state_tint_mix > 0:
            c = _mix(c, self._state_tint, self._state_tint_mix)
        b = max(0.0, p.brightness) * (1.0 - 0.15 * smoothstep(self.closure) * (self.state == "sleeping"))
        return tuple(int(max(0, min(255, round(v * b)))) for v in c)  # type: ignore[return-value]

    def frame(self) -> FaceFrame:
        p, t = self.params, self.t
        m = self.motion()
        cx0 = NATIVE_W / 2
        gx, gy = self.gaze()
        gx, gy = _clamp(gx * 2 - 1 + m.gx), _clamp(gy * 2 - 1 + m.gy)
        breath = math.sin(self._breath_phase) if self.cfg.breathing else 0.0
        breath_dy = breath * (3.0 if self.state == "sleeping" else 1.8)
        breath_h = 1.0 + 0.012 * breath
        hop = p.bob * abs(math.sin(math.pi * p.bob_hz * t)) if p.bob > 0 and p.bob_hz > 0 else 0.0
        rot = m.rot + (p.sway * math.sin(2 * math.pi * 0.45 * t) if p.sway else 0.0)
        level = self.level if self.talking or self.level > 0.01 else 0.0
        blink = self.current_blink()
        closure = smoothstep(self.closure)
        pop = 1.0
        if self._pop_start is not None:
            u = (t - self._pop_start) / (POP_SECONDS + OPEN_SECONDS)
            pop = 1.0 + 0.09 * math.sin(math.pi * clamp01(u)) * (1 - clamp01(u))
        sin_r, cos_r = math.sin(math.radians(rot)), math.cos(math.radians(rot))

        eyes: list[EyeFrame] = []
        for side, ep in ((-1, p.left), (1, p.right)):
            w = EYE_W * ep.width * ep.scale
            h = EYE_H * ep.height * ep.scale
            persp = 1.0 + 0.07 * gx * side
            w *= persp * (1.0 - 0.05 * abs(gx))
            h *= persp * breath_h * (1.0 + 0.05 * level)
            w *= (1.0 - 0.015 * level) * m.sx * pop
            h *= m.sy * pop
            openness = clamp01(ep.openness) * (1.0 - blink) * (1.0 - closure) * clamp01(m.open_mul)
            h_eff = max(MIN_LINE, h * openness)
            ox = side * (EYE_SPACING + p.spacing) / 2 + ep.dx
            oy = ep.dy
            # rotate the eye's offset around the face centre for whole-face tilt / wiggle
            rx = ox * cos_r + oy * sin_r
            ry = -ox * sin_r + oy * cos_r
            cx = cx0 + rx + gx * GAZE_DX + m.dx
            cy = EYE_Y + ry + gy * GAZE_DY + breath_dy - hop + m.dy + (h - h_eff) * 0.1
            radius = max(0.0, min(ep.roundness * min(w, h_eff), h_eff / 2, w / 2))
            lid_fade = clamp01(openness / 0.35)
            closed_curve = clamp01((closure - 0.9) / 0.1) if closure > 0.9 else 0.0
            eyes.append(EyeFrame(
                cx=cx, cy=cy, w=w, h=h_eff, radius=radius,
                side=side, lid_top=clamp01(ep.lid_top) * lid_fade, lid_angle=ep.lid_angle,
                lid_bottom=clamp01(ep.lid_bottom) * lid_fade, smile=clamp01(ep.smile) * lid_fade,
                tilt=-side * ep.tilt + rot + m.eye_rot, shine=max(0.0, p.shine) * clamp01(openness * 1.6),
                closed_curve=closed_curve, full_h=h, look_x=gx, look_y=gy, pupil=max(0.0, ep.pupil),
            ))

        color = self.eye_color()
        mouth = None
        if self.mouth_alpha > 0.01:
            mouth = MouthFrame(
                cx=cx0 + gx * GAZE_DX * 0.6 + m.dx,
                cy=MOUTH_Y + p.left.dy * 0.5 + gy * GAZE_DY * 0.5 + breath_dy - hop + m.dy,
                open=clamp01(level), alpha=self.mouth_alpha, width=46.0,
            )
        sprites: list[Sprite] = effects.extras_sprites(self.extras_alpha, t, eyes, color)
        for a in self.animations:
            sprites.extend(effects.animation_sprites(a.name, t - a.start, a.seed, color))
        if self.hidden_amount > 0.0:
            pulse = 0.85 + 0.15 * math.sin(t * 1.3)
            sprites.append(Sprite("play", NATIVE_W - 34, NATIVE_H - 30, 22,
                                  0.4 * self.hidden_amount * pulse, color=color))
        ring_strength = max(self.extras_alpha.get("ring", 0.0), self._listen_alpha)
        ring = ring_strength * (0.72 + 0.28 * math.sin(t * 2 * math.pi * 0.7)) if ring_strength > 0.01 else 0.0
        glow = p.glow * (1.0 + 0.3 * level) if self.cfg.glow else 0.0
        return FaceFrame(
            width=NATIVE_W, height=NATIVE_H, t=t, background=tuple(self.cfg.background_color),  # type: ignore[arg-type]
            color=color, glow=glow, eyes=(eyes[0], eyes[1]), mouth=mouth, sprites=tuple(sprites),
            ring=ring, hidden=self.hidden_amount, expression=self.expression, state=self.state,
            debug={"gaze_x": round(gx, 3), "gaze_y": round(gy, 3), "level": round(level, 3),
                   "blink": round(blink, 3), "closure": round(self.closure, 3)},
        )
