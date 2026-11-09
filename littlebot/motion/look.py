"""Gaze target -> head pan / tilt, with the "eyes lead, head follows" rule.

Pure logic (no threads, no clock of its own: every call takes ``now``), used by
``MotionController.look_at``.

Coordinates
-----------
``x, y`` are normalised view coordinates, the ones vision publishes as a face's
``gaze_x`` / ``gaze_y`` and FaceAPI.look_at takes: 0..1, 0.5/0.5 = centre, x toward
screen-right = the robot's LEFT (the screen faces the viewer), y down. They map to the
joints' normalised directions: ``head`` (pan) + = the robot's left, ``head_tilt`` + = up.

* ``camera_on_head`` (default, the camera sits in the head's top pod): x/y are relative to
  where the head points now, so the head turns *by* ``(x - 0.5) * fov_h_deg``.
* otherwise x/y are absolute: the head turns *to* ``neutral + (x - 0.5) * fov_h_deg``.

Eyes lead, head follows
-----------------------
The eyes (the face) follow every small shift at once; that is the caller's job. The head
only moves when the target has stayed more than ``follow_offset`` (of the view) away from
where the head points for longer than ``follow_after_seconds``, and then turns ``gain`` of
the way (no overshoot; a camera on the head sees the person drift to the centre, so it
converges in 2-3 steps). While the head moves, and for ``settle_seconds`` afterwards (the
camera catching up), nothing is judged. Targets beyond the soft limits are clamped; a turn
smaller than ``MIN_TURN_DEG`` is not worth a movement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from littlebot.core.config import LookConfig
from littlebot.motion.joints import HEAD_PAN, HEAD_TILT, JointSpec

MIN_TURN_DEG = 1.5


def _f(value: Any, default: float, lo: float, hi: float) -> float:
    """Config number clamped into a sane range (bad values never disable looking)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return min(hi, max(lo, v))


@dataclass(frozen=True)
class LookSettings:
    camera_on_head: bool = True
    fov_h_deg: float = 70.0
    fov_v_deg: float = 45.0
    follow_offset: float = 0.15
    follow_after_seconds: float = 0.4
    gain: float = 0.8
    settle_seconds: float = 0.6
    speed: float = 0.5

    @classmethod
    def from_config(cls, cfg: LookConfig | None) -> "LookSettings":
        if cfg is None:
            return cls()
        return cls(
            camera_on_head=bool(cfg.camera_on_head),
            fov_h_deg=_f(cfg.fov_h_deg, 70.0, 10.0, 180.0),
            fov_v_deg=_f(cfg.fov_v_deg, 45.0, 10.0, 180.0),
            follow_offset=_f(cfg.follow_offset, 0.15, 0.0, 0.5),
            follow_after_seconds=_f(cfg.follow_after_seconds, 0.4, 0.0, 10.0),
            gain=_f(cfg.gain, 0.8, 0.1, 1.0),
            settle_seconds=_f(cfg.settle_seconds, 0.6, 0.0, 10.0),
            speed=_f(cfg.speed, 0.5, 0.05, 1.0),
        )


def _rel(spec: JointSpec, angle: float) -> float:
    """Signed degrees from neutral in the joint's positive direction."""
    return (float(angle) - spec.neutral) * (1.0 if spec.positive_toward == "max" else -1.0)


class LookPlanner:
    """Decides when (and where) the head follows a gaze target. Not thread-safe (the controller locks)."""

    def __init__(self, settings: LookSettings | None = None) -> None:
        self.s = settings or LookSettings()
        self.target: tuple[float, float] | None = None
        self.target_at: float | None = None
        self.off_since: float | None = None
        self.settle_until = -math.inf
        self.error = 0.0
        self.follows = 0

    def reset(self) -> None:
        self.off_since = None
        self.settle_until = -math.inf
        self.error = 0.0

    def update(self, x: float, y: float, now: float, *, pan: JointSpec | None, tilt: JointSpec | None,
               pan_angle: float | None = None, tilt_angle: float | None = None,
               busy: bool = False) -> dict[str, float] | None:
        """Record a gaze target; return head targets {joint: angle} when the head should turn now."""
        x = min(1.0, max(0.0, float(x)))
        y = min(1.0, max(0.0, float(y)))
        self.target, self.target_at = (x, y), now
        axes: list[tuple[str, JointSpec, float, float, float]] = []  # name, spec, current angle, offset, fov
        if pan is not None:
            axes.append((HEAD_PAN, pan, pan.neutral if pan_angle is None else pan_angle, x - 0.5, self.s.fov_h_deg))
        if tilt is not None:
            axes.append((HEAD_TILT, tilt, tilt.neutral if tilt_angle is None else tilt_angle, 0.5 - y,
                         self.s.fov_v_deg))
        if not axes:
            return None
        if busy:  # the head is moving (a look or a gesture): judge again once it has settled
            self.settle_until = max(self.settle_until, now + self.s.settle_seconds)
            self.off_since = None
            return None
        if now < self.settle_until:
            self.off_since = None
            return None

        targets: dict[str, float] = {}
        error = 0.0
        for name, spec, angle, offset, fov in axes:
            current = _rel(spec, angle)
            wanted = current + offset * fov if self.s.camera_on_head else offset * fov
            goal = spec.soft_clamp(spec.toward(spec.neutral, current + self.s.gain * (wanted - current)))
            if abs(goal - angle) < MIN_TURN_DEG:
                continue  # already there, or held at a soft limit: nothing to gain
            error = max(error, abs(wanted - current) / fov)
            targets[name] = goal
        self.error = error
        if not targets or error <= self.s.follow_offset:
            self.off_since = None  # small shifts: the eyes take them
            return None
        if self.off_since is None:
            self.off_since = now
        if now - self.off_since < self.s.follow_after_seconds:
            return None
        self.off_since = None
        self.settle_until = now + self.s.settle_seconds
        self.follows += 1
        return targets

    def status(self) -> dict[str, Any]:
        return {
            "target": None if self.target is None else {"x": round(self.target[0], 3), "y": round(self.target[1], 3)},
            "off_centre": round(self.error, 3),
            "waiting": self.off_since is not None,
            "follows": self.follows,
            "camera_on_head": self.s.camera_on_head,
        }


__all__ = ["LookPlanner", "LookSettings", "MIN_TURN_DEG"]
