"""The conversation gestures (nod, head_shake, tilt_curious ...) on the simulated servos, with any subset of joints."""

from __future__ import annotations

import pytest

from littlebot.core.config import MotionConfig
from littlebot.core.interfaces import GESTURE_NAMES
from littlebot.motion import gestures as G
from littlebot.motion.joints import HEAD_PAN, HEAD_TILT, build_joint_specs
from littlebot.testing import FakeMotion

from motion_testkit import record_writes

NEW_GESTURES = ("nod", "nod_small", "head_shake", "tilt_curious", "look_up_think", "shrug", "perk_up",
                "point_screen", "wave_hello", "wave_goodbye", "arm_wiggle", "droop", "idle_shift")
ALL = {k: v.model_dump() for k, v in MotionConfig().servos.items()}
SUBSETS = {
    "full": None,
    "no_tilt": {k: v for k, v in ALL.items() if k != HEAD_TILT},
    "head_only": {k: v for k, v in ALL.items() if k in (HEAD_PAN, HEAD_TILT)},
    "arms_only": {k: v for k, v in ALL.items() if k in ("left_arm", "right_arm")},
}


def test_shared_vocabulary_matches_the_library():
    assert set(G.gesture_names()) == set(GESTURE_NAMES)
    assert set(NEW_GESTURES) <= set(GESTURE_NAMES)
    assert {"celebrate", "dance", "wave", "shake", "neutral"} <= set(GESTURE_NAMES)
    assert FakeMotion().gestures() == list(GESTURE_NAMES)


@pytest.mark.parametrize("subset", list(SUBSETS))
def test_every_gesture_valid_for_any_subset_of_joints(subset):
    servos = SUBSETS[subset]
    specs = build_joint_specs(MotionConfig(**({"servos": servos} if servos else {})))
    assert G.validate_gestures(specs) == []
    for g in G.GESTURES.values():
        assert G.gesture_joints(g, specs) <= set(specs)


def test_head_gestures_use_pan_and_tilt():
    specs = build_joint_specs(MotionConfig())
    assert G.gesture_joints(G.GESTURES["nod"], specs) == {HEAD_TILT}
    assert G.gesture_joints(G.GESTURES["head_shake"], specs) == {HEAD_PAN, HEAD_TILT}
    assert G.gesture_joints(G.GESTURES["shrug"], specs) == {HEAD_PAN, HEAD_TILT, "left_arm", "right_arm"}
    assert G.gesture_joints(G.GESTURES["point_screen"], specs) >= {"right_arm"}
    assert HEAD_TILT in G.gesture_joints(G.GESTURES["celebrate"], specs)
    no_tilt = {k: v for k, v in specs.items() if k != HEAD_TILT}
    assert G.gesture_joints(G.GESTURES["nod"], no_tilt) == frozenset()  # nothing to nod with: refused
    assert G.gesture_joints(G.GESTURES["neutral"], no_tilt) == set(no_tilt)


def test_new_gestures_are_small_and_gentle():
    for name in NEW_GESTURES:
        g = G.GESTURES[name]
        assert g.description and g.keyframes
        for kf in g.keyframes:
            assert 0 < kf.speed <= 0.8, name
            assert all(abs(v) <= 0.85 for v in kf.pose.values()), name
        assert all(v == 0 for v in g.keyframes[-1].pose.values()), f"{name} must end at neutral"
    for name in ("nod", "nod_small", "head_shake", "perk_up", "arm_wiggle", "idle_shift"):  # the frequent ones
        assert all(abs(v) <= 0.45 for kf in G.GESTURES[name].keyframes for v in kf.pose.values()), name
    assert all(kf.speed <= 0.3 for kf in G.GESTURES["idle_shift"].keyframes)  # idle: slow and tiny
    assert all(abs(v) <= 0.1 for kf in G.GESTURES["idle_shift"].keyframes for v in kf.pose.values())


@pytest.mark.parametrize("subset", list(SUBSETS))
async def test_each_new_gesture_runs_on_the_mock_within_limits(make_controller, subset, caplog):
    servos = SUBSETS[subset]
    c = await make_controller(**({"servos": servos} if servos else {}))
    writes = record_writes(c)
    specs = c.specs
    with caplog.at_level("DEBUG", logger="littlebot.motion"):
        for name in NEW_GESTURES:
            joints = G.gesture_joints(G.GESTURES[name], specs)
            assert await c.run_gesture(name) is bool(joints), (subset, name)
            st = c.status()
            assert st["moving"] is False and st["gesture"] is None
            for j in joints:  # every gesture comes home
                assert st["joints"][j]["angle"] == pytest.approx(specs[j].neutral, abs=0.05), (name, j)
    assert "rate limiter engaged" not in caplog.text  # the planner alone keeps every joint within its limits
    by_channel = {s.channel: s for s in specs.values()}
    series: dict[int, list[tuple[float, float]]] = {}
    for t, ch, pulse in writes:
        spec = by_channel[ch]
        angle = spec.pulse_to_angle(pulse)
        assert spec.min_angle - 0.01 <= angle <= spec.max_angle + 0.01
        series.setdefault(ch, []).append((t, angle))
    for ch, pts in series.items():
        vmax = by_channel[ch].speed_limit
        for (t0, a0), (t1, a1) in zip(pts, pts[1:]):
            assert abs(a1 - a0) <= vmax * max(t1 - t0, 1e-9) * 1.05 + 1e-3


async def test_nod_is_quick_enough_for_a_sentence_end(make_controller):
    from littlebot.core.clock import ManualClock

    clock = ManualClock()
    c = await make_controller(clock=clock, settle_seconds=0.0)
    t0 = clock.now()
    assert await c.run_gesture("nod_small") is True
    assert clock.now() - t0 < 1.5  # done before the next sentence is far along
