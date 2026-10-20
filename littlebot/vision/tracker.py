"""Face tracking: primary face, gaze mapping, smoothing and presence hysteresis.

Pure logic - no OpenCV, no threads, no clock of its own: every call takes ``now``
so tests can drive it with exact timestamps.

Image -> screen gaze mapping (why ``mirror`` defaults to True)
---------------------------------------------------------------
The camera and the screen both face the child. A camera image is what the
robot "sees": something on the robot's own right appears on the image's
RIGHT. The screen, however, is looked at from the front, so the robot's right
is the viewer's LEFT, i.e. screen x near 0. For the eyes to look *at* a child
who sits to the robot's right (image right, ``cx`` near 1), the pupils must move
toward screen-left: ``gaze_x = 1 - cx``. That is ``mirror=True``. Use
``mirror=False`` only if the camera driver already delivers a mirrored image.
Vertical needs no flip (image top = up = screen top).

Vertical bias: a face box is centred near the nose; people's eyes sit at about
40 % of the box height, so we aim there (``eye_line``) - the robot then looks
into the child's eyes rather than at their mouth. ``vertical_offset`` can
compensate for a camera mounted above/below the screen.

Presence hysteresis
-------------------
* present after faces were seen (with gaps no longer than ``gap_tolerance``)
  for ``present_after`` seconds - a single false detection never "arrives";
* left after no face for ``left_after`` seconds - a turned head or a missed
  detection never "leaves".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from littlebot.vision.detector import Detection


@dataclass(frozen=True)
class TrackedFace:
    det: Detection
    gaze_x: float
    gaze_y: float
    primary: bool = False


@dataclass
class TrackerUpdate:
    faces: list[TrackedFace] = field(default_factory=list)  # primary first
    gaze: tuple[float, float] | None = None  # smoothed gaze of the primary face
    present: bool = False
    arrived: bool = False  # became present on this update
    left: bool = False  # became absent on this update

    @property
    def primary(self) -> TrackedFace | None:
        return self.faces[0] if self.faces and self.faces[0].primary else None


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


class GazeMapper:
    """Normalised image box -> normalised screen gaze (0..1, 0.5/0.5 = centre)."""

    def __init__(self, *, mirror: bool = True, eye_line: float = 0.4, vertical_offset: float = 0.0) -> None:
        self.mirror = mirror
        self.eye_line = eye_line
        self.vertical_offset = vertical_offset

    def map(self, det: Detection) -> tuple[float, float]:
        ex = det.cx
        ey = det.y + self.eye_line * det.h
        gx = 1.0 - ex if self.mirror else ex
        return _clamp01(gx), _clamp01(ey + self.vertical_offset)


class FaceTracker:
    def __init__(self, *, smoothing: float = 0.35, present_after: float = 0.6, left_after: float = 3.0,
                 mirror: bool = True, eye_line: float = 0.4, vertical_offset: float = 0.0,
                 gap_tolerance: float = 0.5, switch_ratio: float = 1.3, max_faces: int = 5) -> None:
        if not 0.0 <= smoothing < 1.0:
            raise ValueError("smoothing must be in [0, 1)")
        self.smoothing = smoothing  # 0 = raw; higher = smoother (weight kept from the previous gaze per update)
        self.present_after = max(0.0, present_after)
        self.left_after = max(0.0, left_after)
        self.gap_tolerance = max(0.0, gap_tolerance)
        self.switch_ratio = max(1.0, switch_ratio)
        self.max_faces = max(1, max_faces)
        self.mapper = GazeMapper(mirror=mirror, eye_line=eye_line, vertical_offset=vertical_offset)
        self.reset()

    def reset(self) -> None:
        self._present = False
        self._seen_since: float | None = None
        self._last_seen: float | None = None
        self._gaze: tuple[float, float] | None = None
        self._primary: Detection | None = None

    # ---------------------------------------------------------------- state
    @property
    def present(self) -> bool:
        return self._present

    @property
    def gaze(self) -> tuple[float, float] | None:
        return self._gaze

    @property
    def busy(self) -> bool:
        """True while timing matters (a person is present or arriving)."""
        return self._present or self._seen_since is not None

    # --------------------------------------------------------------- update
    def _pick_primary(self, dets: list[Detection]) -> Detection:
        largest = dets[0]
        prev = self._primary
        if prev is None:
            return largest
        # Stickiness: keep following the same person unless someone clearly bigger/closer appears.
        best, best_d = None, None
        for d in dets:
            dist = ((d.cx - prev.cx) ** 2 + (d.cy - prev.cy) ** 2) ** 0.5
            if dist <= max(prev.w, d.w) and (best_d is None or dist < best_d):
                best, best_d = d, dist
        if best is not None and best.area * self.switch_ratio >= largest.area:
            return best
        return largest

    def update(self, detections: Sequence[Detection], now: float) -> TrackerUpdate:
        dets = sorted((d for d in detections if d.area > 0), key=lambda d: d.area, reverse=True)[: self.max_faces]
        upd = TrackerUpdate()
        if dets:
            primary = self._pick_primary(dets)
            raw = self.mapper.map(primary)
            reacquired = self._gaze is None or (self._last_seen is not None and now - self._last_seen > self.gap_tolerance)
            if reacquired:
                self._gaze = raw  # snap to a new person instead of sweeping across the room
            else:
                s = self.smoothing
                gx, gy = self._gaze  # type: ignore[misc]
                self._gaze = (s * gx + (1 - s) * raw[0], s * gy + (1 - s) * raw[1])
            self._primary = primary
            if self._seen_since is None or reacquired and not self._present:
                self._seen_since = now
            self._last_seen = now
            if not self._present and now - self._seen_since >= self.present_after:
                self._present = True
                upd.arrived = True
            gaze = self._gaze
            upd.faces = [TrackedFace(primary, gaze[0], gaze[1], True)]
            for d in dets:
                if d is not primary:
                    gx, gy = self.mapper.map(d)
                    upd.faces.append(TrackedFace(d, gx, gy, False))
        elif self._last_seen is not None:
            since = now - self._last_seen
            if self._present and since >= self.left_after:
                self.reset()
                upd.left = True
            elif not self._present and self._seen_since is not None and since > self.gap_tolerance:
                self.reset()  # a brief false detection: forget it
        upd.gaze = self._gaze
        upd.present = self._present
        return upd
