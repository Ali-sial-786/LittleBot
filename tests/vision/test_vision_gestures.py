"""Experimental gesture heuristics on synthetic moving-blob sequences (no real images).

These tests pin down the heuristic's behaviour; they say nothing about how it
performs with real children (it is untested there - see docs/modules/vision.md).
"""

from __future__ import annotations

import math

import numpy as np

from littlebot.vision.detector import Detection
from littlebot.vision.gestures import GestureDetector
from littlebot.vision.imaging import draw_ellipse, draw_rect

W, H = 80, 60
FPS = 10.0
FACE = Detection(0.5, 0.3, 0.2, 0.27)  # normalised box of a (static) face


def scene(rng: np.random.Generator, blob_x: float | None = None, blob_y: float = 0.35, blob: int = 8) -> np.ndarray:
    """Grey frame with a static face, sensor noise and optionally a bright 'hand' blob at normalised x/y."""
    frame = np.full((H, W), 60, np.uint8)
    draw_ellipse(frame, (FACE.cx) * W, FACE.cy * H, FACE.w * W / 2, FACE.h * H / 2, 170)
    if blob_x is not None:
        draw_rect(frame, int(blob_x * W - blob / 2), int(blob_y * H - blob / 2), blob, blob, 230)
    noise = rng.normal(0, 3, frame.shape)
    return np.clip(frame + noise, 0, 255).astype(np.uint8)


def run(det: GestureDetector, xs: list[float | None], *, face: Detection | None = FACE, blob_y: float = 0.35,
        blob: int = 8, start: float = 0.0) -> list[tuple[float, str, float]]:
    rng = np.random.default_rng(1)
    events = []
    for i, x in enumerate(xs):
        t = start + i / FPS
        for ev in det.update(scene(rng, x, blob_y, blob), t, face):
            events.append((t, ev.gesture, ev.confidence))
    return events


def waving(seconds: float, hz: float = 1.5, centre: float = 0.32, amp: float = 0.1) -> list[float | None]:
    return [centre + amp * math.sin(2 * math.pi * hz * i / FPS) for i in range(int(seconds * FPS))]


def test_static_scene_produces_nothing():
    det = GestureDetector()
    assert run(det, [None] * 40) == []
    assert run(GestureDetector(), [0.3] * 40) == []  # a hand held still is not a gesture
    assert det.last_energy < 0.01


def test_wave_beside_the_face_is_detected():
    events = run(GestureDetector(), [None] * 5 + waving(2.5))
    waves = [e for e in events if e[1] == "wave"]
    assert len(waves) == 1
    t, _, conf = waves[0]
    assert 0.5 < t <= 3.0 and 0.5 <= conf <= 1.0


def test_one_off_motion_is_motion_not_wave():
    sweep = [0.1 + 0.8 * i / 10 for i in range(11)]  # a big blob crosses once (someone walks past)
    events = run(GestureDetector(), [None] * 3 + sweep + [None] * 10, blob=16)
    kinds = [e[1] for e in events]
    assert "motion" in kinds and "wave" not in kinds
    assert kinds.count("motion") == 1  # rate-limited by the cooldown


def test_no_face_means_no_wave():
    events = run(GestureDetector(), [None] * 5 + waving(2.5), face=None)
    assert all(e[1] != "wave" for e in events)


def test_motion_inside_the_face_box_is_ignored_for_waves():
    head_shake = [FACE.cx + 0.05 * math.sin(2 * math.pi * 1.5 * i / FPS) for i in range(25)]
    events = run(GestureDetector(), [None] * 5 + head_shake, blob_y=FACE.cy)
    assert all(e[1] != "wave" for e in events)


def test_slow_drift_is_not_a_wave():
    drift = [0.22 + 0.2 * i / 30 for i in range(30)]  # hand slides slowly one way
    events = run(GestureDetector(), [None] * 3 + drift)
    assert all(e[1] != "wave" for e in events)


def test_continuous_waving_is_rate_limited():
    events = run(GestureDetector(wave_cooldown=3.0), [None] * 3 + waving(7.0))
    times = [t for t, g, _ in events if g == "wave"]
    assert 1 <= len(times) <= 3
    assert all(b - a >= 3.0 for a, b in zip(times, times[1:], strict=False))


def test_reset_forgets_previous_frame():
    det = GestureDetector()
    rng = np.random.default_rng(2)
    det.update(scene(rng, None), 0.0)
    det.reset()
    # first frame after reset has nothing to diff against -> no events, even with a sudden change
    assert det.update(np.full((H, W), 255, np.uint8), 0.1) == []
    assert det.update(scene(rng, None), 0.2) == []  # one big change, but needs two frames for "motion"
