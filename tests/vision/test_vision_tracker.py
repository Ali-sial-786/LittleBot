"""Tracker: gaze mapping (mirroring, eye-line bias), smoothing, presence hysteresis, primary face."""

from __future__ import annotations

import pytest

from littlebot.vision.detector import Detection
from littlebot.vision.tracker import FaceTracker, GazeMapper


def face(cx: float, cy: float = 0.5, w: float = 0.2, h: float | None = None) -> Detection:
    return Detection.centred(cx, cy, w, h)


# ------------------------------------------------------------------ mapping
def test_mirroring_image_right_means_gaze_left():
    mirrored = GazeMapper(mirror=True, eye_line=0.5)
    gx, _ = mirrored.map(face(0.9))
    assert gx == pytest.approx(0.1)  # child on the robot's right -> pupils toward screen-left
    gx, _ = mirrored.map(face(0.5))
    assert gx == pytest.approx(0.5)
    plain = GazeMapper(mirror=False, eye_line=0.5)
    assert plain.map(face(0.9))[0] == pytest.approx(0.9)


def test_vertical_bias_aims_at_the_eyes_not_the_box_centre():
    m = GazeMapper(mirror=True)  # default eye_line 0.4 of the box height
    _, gy = m.map(face(0.5, cy=0.5, h=0.4))
    assert gy == pytest.approx(0.3 + 0.4 * 0.4)  # top 0.3 + 40% of height = 0.46
    assert gy < 0.5
    _, gy_off = GazeMapper(vertical_offset=0.1).map(face(0.5, cy=0.5, h=0.4))
    assert gy_off == pytest.approx(gy + 0.1)
    # clamped to the screen
    assert GazeMapper(vertical_offset=5).map(face(0.5))[1] == 1.0
    assert GazeMapper(mirror=False).map(Detection(1.2, -0.5, 0.2, 0.2))[0] == 1.0


# ---------------------------------------------------------------- smoothing
def test_first_detection_snaps_then_exponential_smoothing():
    t = FaceTracker(smoothing=0.5, present_after=0.0, mirror=False)
    u = t.update([face(0.2)], 0.0)
    assert u.gaze[0] == pytest.approx(0.2)  # no sweep from the centre on first sight
    u = t.update([face(0.6)], 0.1)
    assert u.gaze[0] == pytest.approx(0.4)  # 0.5 * 0.2 + 0.5 * 0.6
    u = t.update([face(0.6)], 0.2)
    assert u.gaze[0] == pytest.approx(0.5)
    assert u.faces[0].primary and u.faces[0].gaze_x == pytest.approx(0.5)  # published gaze is the smoothed one


def test_zero_smoothing_is_raw_and_invalid_values_rejected():
    t = FaceTracker(smoothing=0.0, present_after=0.0, mirror=False)
    t.update([face(0.2)], 0.0)
    assert t.update([face(0.7)], 0.1).gaze[0] == pytest.approx(0.7)
    with pytest.raises(ValueError):
        FaceTracker(smoothing=1.0)


def test_reacquired_face_snaps_instead_of_sweeping():
    t = FaceTracker(smoothing=0.9, present_after=0.0, left_after=3.0, gap_tolerance=0.5, mirror=False)
    t.update([face(0.2)], 0.0)
    t.update([], 0.3)
    t.update([], 1.0)  # gap > tolerance, still present (< left_after)
    u = t.update([face(0.8)], 1.1)
    assert u.present and u.gaze[0] == pytest.approx(0.8)
    assert not u.arrived  # no second greeting


# --------------------------------------------------------------- hysteresis
def test_present_only_after_continuous_detection():
    t = FaceTracker(present_after=0.6, left_after=3.0)
    arrived = []
    for i in range(8):  # 5 Hz detections
        u = t.update([face(0.5)], i * 0.2)
        arrived.append(u.arrived)
    assert arrived == [False, False, False, True, False, False, False, False]
    assert t.present


def test_single_false_detection_never_arrives():
    t = FaceTracker(present_after=0.6, gap_tolerance=0.5)
    t.update([face(0.5)], 0.0)
    for i in range(1, 15):
        u = t.update([], i * 0.2)
        assert not u.arrived and not u.left
    assert not t.present and not t.busy and t.gaze is None
    # a later real sighting starts the timer from scratch
    assert not t.update([face(0.5)], 3.0).arrived
    assert not t.update([face(0.5)], 3.3).arrived
    assert t.update([face(0.5)], 3.6).arrived


def test_brief_misses_do_not_reset_arrival():
    t = FaceTracker(present_after=0.6, gap_tolerance=0.5)
    t.update([face(0.5)], 0.0)
    t.update([], 0.2)  # a missed detection...
    assert not t.update([face(0.5)], 0.4).arrived  # ...0.4 s gap (< gap_tolerance) is tolerated
    assert t.update([face(0.5)], 0.6).arrived


def test_left_only_after_left_after_seconds():
    t = FaceTracker(present_after=0.0, left_after=3.0)
    assert t.update([face(0.5)], 10.0).arrived
    for s in (10.5, 11.0, 12.0, 12.9):
        u = t.update([], s)
        assert u.present and not u.left
        assert u.gaze is not None  # keep looking where the person was
    u = t.update([], 13.0)
    assert u.left and not u.present and u.gaze is None
    assert not t.update([], 14.0).left  # only once
    assert t.update([face(0.5)], 15.0).arrived


def test_zero_hysteresis_is_immediate():
    t = FaceTracker(present_after=0.0, left_after=0.0)
    assert t.update([face(0.5)], 0.0).arrived
    assert t.update([], 0.0).left


# ------------------------------------------------------------------ primary
def test_primary_is_largest_with_stickiness():
    t = FaceTracker(smoothing=0.0, present_after=0.0, mirror=False, switch_ratio=1.3)
    a, b = face(0.2, w=0.30), face(0.7, w=0.20)
    u = t.update([b, a], 0.0)
    assert u.primary.det == a and [f.primary for f in u.faces] == [True, False]
    assert u.faces[1].gaze_x == pytest.approx(0.7)  # non-primary faces get raw mapped gaze
    # b becomes slightly bigger: keep following a (no flip-flopping between two similar faces)
    a2, b2 = face(0.21, w=0.30), face(0.7, w=0.32)
    assert t.update([a2, b2], 0.2).primary.det == a2
    # b clearly bigger (much closer): switch
    a3, b3 = face(0.22, w=0.30), face(0.7, w=0.45)
    assert t.update([a3, b3], 0.4).primary.det == b3


def test_faces_are_capped_and_empty_boxes_ignored():
    t = FaceTracker(present_after=0.0, max_faces=3)
    dets = [face(0.1 * i + 0.05, w=0.05 + 0.01 * i) for i in range(6)] + [Detection(0.5, 0.5, 0.0, 0.0)]
    u = t.update(dets, 0.0)
    assert len(u.faces) == 3
    assert t.update([Detection(0.5, 0.5, 0.0, 0.1)], 1.0).faces == []
