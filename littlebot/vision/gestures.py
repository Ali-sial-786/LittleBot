"""EXPERIMENTAL gesture heuristics (disabled unless settings ``vision.gestures_enabled``).

Honest status: these heuristics are tuned on synthetic moving-blob sequences
only. They have NOT been validated with real children, real rooms or the real
camera. Expect false positives (a parent walking past, a swinging toy, light
flicker) and misses (babies "wave" by opening/closing a hand or flapping both
arms). Use the events for playful, low-stakes reactions only - never for
anything safety-relevant.

How it works (cheap: a few hundred microseconds per frame)
----------------------------------------------------------
* frames are reduced to a tiny grey image (``width`` px, 80 by default) and
  differenced with the previous one; pixels that changed more than
  ``diff_threshold`` form a motion mask; nothing else is kept.
* ``motion``: the fraction of changed pixels exceeds ``motion_fraction`` for
  two consecutive frames (rate-limited by ``motion_cooldown``).
* ``wave``: needs a detected face. In a region beside and above that face
  (the face box itself is excluded so head movement does not count) we follow
  the horizontal centroid of motion. A wave is several left/right direction
  reversals with enough amplitude within ``wave_window`` seconds.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from littlebot.vision.detector import Detection
from littlebot.vision.imaging import resize_to_width, to_gray


@dataclass(frozen=True)
class GestureEvent:
    gesture: str  # "wave" | "motion"
    confidence: float


class GestureDetector:
    def __init__(self, *, width: int = 80, diff_threshold: int = 25, motion_fraction: float = 0.03,
                 motion_cooldown: float = 5.0, wave_cooldown: float = 3.0, wave_window: float = 2.0,
                 wave_min_reversals: int = 3, wave_min_amplitude: float = 0.4, wave_min_step: float = 0.05,
                 roi_min_motion: float = 0.01) -> None:
        self.width = width
        self.diff_threshold = diff_threshold
        self.motion_fraction = motion_fraction
        self.motion_cooldown = motion_cooldown
        self.wave_cooldown = wave_cooldown
        self.wave_window = wave_window
        self.wave_min_reversals = wave_min_reversals
        self.wave_min_amplitude = wave_min_amplitude  # in face widths
        self.wave_min_step = wave_min_step  # centroid moves smaller than this (face widths) are jitter
        self.roi_min_motion = roi_min_motion
        self.reset()

    def reset(self) -> None:
        """Forget everything (called when the camera closes / privacy turns it off)."""
        self._prev: np.ndarray | None = None
        self._motion_frames = 0
        self._last_motion = -1e9
        self._last_wave = -1e9
        self._track: deque[tuple[float, float]] = deque(maxlen=64)
        self.last_energy = 0.0

    def update(self, frame: np.ndarray, now: float, face: Detection | None = None) -> list[GestureEvent]:
        small = to_gray(resize_to_width(frame, self.width)).astype(np.int16)
        prev, self._prev = self._prev, small
        if prev is None or prev.shape != small.shape:
            return []
        mask = np.abs(small - prev) > self.diff_threshold
        energy = float(mask.mean())
        self.last_energy = energy
        events: list[GestureEvent] = []

        if energy >= self.motion_fraction:
            self._motion_frames += 1
        else:
            self._motion_frames = 0
        if self._motion_frames >= 2 and now - self._last_motion >= self.motion_cooldown:
            self._last_motion = now
            events.append(GestureEvent("motion", round(min(1.0, energy / (self.motion_fraction * 4)), 2)))

        wave = self._wave(mask, now, face)
        if wave is not None:
            events.append(wave)
        return events

    # ------------------------------------------------------------------ wave
    def _wave(self, mask: np.ndarray, now: float, face: Detection | None) -> GestureEvent | None:
        while self._track and now - self._track[0][0] > self.wave_window:
            self._track.popleft()
        if face is None or face.w <= 0:
            self._track.clear()
            return None
        H, W = mask.shape
        # Region: 1.5 face widths either side, from one face height above down to the chin.
        rx0, rx1 = face.x - 1.5 * face.w, face.x + 2.5 * face.w
        ry0, ry1 = face.y - 1.0 * face.h, face.y + face.h
        x0, x1 = max(0, int(rx0 * W)), min(W, int(np.ceil(rx1 * W)))
        y0, y1 = max(0, int(ry0 * H)), min(H, int(np.ceil(ry1 * H)))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return None
        roi = mask[y0:y1, x0:x1].copy()
        # Exclude the (slightly enlarged) face box: nodding / head turns are not waves.
        fx0 = max(0, int((face.x - 0.1 * face.w) * W) - x0)
        fx1 = min(x1 - x0, int(np.ceil((face.x + 1.1 * face.w) * W)) - x0)
        fy0 = max(0, int((face.y - 0.1 * face.h) * H) - y0)
        fy1 = min(y1 - y0, int(np.ceil((face.y + 1.1 * face.h) * H)) - y0)
        if fx1 > fx0 and fy1 > fy0:
            roi[fy0:fy1, fx0:fx1] = False
        if roi.mean() < self.roi_min_motion:
            return None
        xs = np.nonzero(roi)[1]
        cx = (x0 + float(xs.mean()) + 0.5) / W  # normalised frame x of the motion centroid
        self._track.append((now, (cx - face.cx) / face.w))  # in face widths, relative to the face
        return self._check_oscillation(now)

    def _check_oscillation(self, now: float) -> GestureEvent | None:
        pts = [p for _, p in self._track]
        if len(pts) < 4 or now - self._last_wave < self.wave_cooldown:
            return None
        reversals, direction, last_turn = 0, 0, pts[0]
        for p in pts[1:]:
            step = p - last_turn
            if abs(step) < self.wave_min_step:
                continue
            d = 1 if step > 0 else -1
            if direction and d != direction:
                reversals += 1
            direction, last_turn = d, p
        amplitude = max(pts) - min(pts)
        if reversals >= self.wave_min_reversals and amplitude >= self.wave_min_amplitude:
            self._last_wave = now
            self._track.clear()
            return GestureEvent("wave", round(min(1.0, 0.4 + 0.15 * reversals), 2))
        return None
