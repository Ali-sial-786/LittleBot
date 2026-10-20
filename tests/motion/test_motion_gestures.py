"""Gesture library: names, limits, gentleness."""

from __future__ import annotations

from littlebot.core.config import MotionConfig
from littlebot.motion import gestures as G
from littlebot.motion.joints import build_joint_specs, load_calibration
from littlebot.testing import FakeMotion

from motion_testkit import write_calibration


def test_gesture_names_cover_the_shared_vocabulary():
    names = G.gesture_names()
    assert set(FakeMotion().gestures()) <= set(names)
    assert "shake" in names
    assert names[-1] == "neutral"


def test_all_gesture_keyframes_within_limits_for_default_config():
    specs = build_joint_specs(MotionConfig())
    assert G.validate_gestures(specs) == []
    for gesture in G.GESTURES.values():
        for kf in gesture.keyframes:
            for joint, angle in G.resolve_pose(kf, specs).items():
                s = specs[joint]
                assert s.min_angle <= angle <= s.max_angle, (gesture.name, joint, angle)
            assert 0 < kf.speed <= 1.0
        assert all(v == 0 for v in gesture.keyframes[-1].pose.values()), f"{gesture.name} must end at neutral"


def test_gestures_stay_within_calibrated_limits(tmp_path):
    cal = load_calibration(write_calibration(tmp_path / "cal.yaml"))
    specs = build_joint_specs(MotionConfig(), cal)
    assert G.validate_gestures(specs) == []


def test_gestures_are_gentle():
    for gesture in G.GESTURES.values():
        for kf in gesture.keyframes:
            assert kf.speed <= 0.8, gesture.name
            assert all(abs(v) <= 0.85 for v in kf.pose.values()), gesture.name
    for kf in G.GESTURES["dance"].keyframes:  # dance: slow and small
        assert kf.speed <= 0.4
        assert all(abs(v) <= 0.35 for v in kf.pose.values())


def test_gesture_joints_only_include_moved_joints():
    specs = build_joint_specs(MotionConfig())
    assert G.gesture_joints(G.GESTURES["look_left"], specs) == {"head"}
    assert G.gesture_joints(G.GESTURES["wave"], specs) == {"head", "right_arm"}
    assert G.gesture_joints(G.GESTURES["neutral"], specs) == set(specs)
    # joints that are not configured are skipped
    head_only = {"head": specs["head"]}
    assert G.resolve_pose(G.GESTURES["raise_arms"].keyframes[0], head_only) == {}
