"""Smooth, limit-respecting trajectories for single joints.

Every movement is a **minimum-jerk** profile (``10t^3 - 15t^4 + 6t^5``): it starts and
ends with zero velocity and zero acceleration, so servos never jerk. Its peaks are known
in closed form, which makes the speed/acceleration limits easy to guarantee:

* peak velocity     = 1.875  * D / T
* peak acceleration = 5.7735 * D / T^2   (10 / sqrt(3))

``plan_duration`` picks the smallest T that keeps both under the joint limits.
Stopping mid-move (``cancel``) uses a ``BrakeSegment``: velocity decays from its current
value to zero along a smoothstep, with deceleration <= the joint's acceleration limit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MINJERK_PEAK_VELOCITY = 1.875
MINJERK_PEAK_ACCEL = 10.0 / math.sqrt(3.0)
BRAKE_PEAK_DECEL = 1.5  # smoothstep'(0.5): peak deceleration = 1.5 * v0 / T
_EPS = 1e-9


def _clip01(x: float) -> float:
    return 0.0 if x <= 0.0 else 1.0 if x >= 1.0 else x


def smoothstep(x: float) -> float:
    """3x^2 - 2x^3: zero slope at both ends."""
    x = _clip01(x)
    return x * x * (3.0 - 2.0 * x)


def minimum_jerk(x: float) -> float:
    """10x^3 - 15x^4 + 6x^5 (a.k.a. smootherstep): zero velocity and acceleration at both ends."""
    x = _clip01(x)
    return x * x * x * (10.0 + x * (-15.0 + 6.0 * x))


def minimum_jerk_rate(x: float) -> float:
    """d/dx of minimum_jerk: 30x^2 - 60x^3 + 30x^4."""
    if x <= 0.0 or x >= 1.0:
        return 0.0
    return 30.0 * x * x * (1.0 - x) * (1.0 - x)


def plan_duration(distance: float, max_speed: float, max_accel: float, min_duration: float = 0.0) -> float:
    """Shortest minimum-jerk duration covering ``distance`` within the speed and accel limits."""
    d = abs(float(distance))
    if d < _EPS:
        return max(0.0, float(min_duration))
    if max_speed <= 0 or max_accel <= 0:
        raise ValueError("speed and acceleration limits must be positive")
    t_speed = MINJERK_PEAK_VELOCITY * d / max_speed
    t_accel = math.sqrt(MINJERK_PEAK_ACCEL * d / max_accel)
    return max(t_speed, t_accel, float(min_duration))


@dataclass
class MinJerkSegment:
    start: float
    end: float
    t0: float
    duration: float

    @property
    def end_time(self) -> float:
        return self.t0 + self.duration

    @property
    def target(self) -> float:
        return self.end

    def _x(self, t: float) -> float:
        if self.duration <= _EPS:
            return 1.0
        return (t - self.t0) / self.duration

    def position(self, t: float) -> float:
        return self.start + (self.end - self.start) * minimum_jerk(self._x(t))

    def velocity(self, t: float) -> float:
        if self.duration <= _EPS:
            return 0.0
        return (self.end - self.start) * minimum_jerk_rate(self._x(t)) / self.duration

    def done(self, t: float) -> bool:
        return t >= self.end_time - _EPS

    def shift(self, dt: float) -> None:
        self.t0 += dt

    @property
    def peak_velocity(self) -> float:
        return 0.0 if self.duration <= _EPS else MINJERK_PEAK_VELOCITY * abs(self.end - self.start) / self.duration


@dataclass
class BrakeSegment:
    """Decelerate from velocity ``v0`` to rest: v(x) = v0 * (1 - smoothstep(x))."""

    start: float
    v0: float
    t0: float
    duration: float

    @property
    def end_time(self) -> float:
        return self.t0 + self.duration

    @property
    def end(self) -> float:
        return self.start + self.v0 * self.duration / 2.0

    @property
    def target(self) -> float:
        return self.end

    def _x(self, t: float) -> float:
        if self.duration <= _EPS:
            return 1.0
        return _clip01((t - self.t0) / self.duration)

    def position(self, t: float) -> float:
        x = self._x(t)
        # integral of (1 - 3u^2 + 2u^3) du from 0 to x = x - x^3 + x^4 / 2
        return self.start + self.v0 * self.duration * (x - x ** 3 + 0.5 * x ** 4)

    def velocity(self, t: float) -> float:
        return self.v0 * (1.0 - smoothstep(self._x(t)))

    def done(self, t: float) -> bool:
        return t >= self.end_time - _EPS

    def shift(self, dt: float) -> None:
        self.t0 += dt


Segment = MinJerkSegment | BrakeSegment


def brake_duration(velocity: float, max_accel: float, min_duration: float = 0.05) -> float:
    return max(float(min_duration), BRAKE_PEAK_DECEL * abs(float(velocity)) / max(max_accel, _EPS))


def make_brake(position: float, velocity: float, t0: float, max_accel: float,
               min_duration: float = 0.05) -> BrakeSegment:
    return BrakeSegment(start=position, v0=velocity, t0=t0,
                        duration=brake_duration(velocity, max_accel, min_duration))


def limit_step(previous: float, desired: float, max_step: float) -> float:
    """Final per-tick safety net: never move more than ``max_step`` degrees in one tick."""
    delta = desired - previous
    if delta > max_step:
        return previous + max_step
    if delta < -max_step:
        return previous - max_step
    return desired


def sample(segment: Segment, dt: float) -> list[float]:
    """Positions of a segment sampled every ``dt`` seconds, end included (tests / scripts)."""
    n = max(1, int(math.ceil(segment.duration / dt)))
    return [segment.position(segment.t0 + min(i * dt, segment.duration)) for i in range(n + 1)]
