"""The pan/tilt head: head_tilt joint defaults, soft limits, per-joint speed / acceleration, easing, subsets."""

from __future__ import annotations

import pytest

from littlebot.core.config import MotionConfig, ServoChannelConfig
from littlebot.motion.controller import decide_backend
from littlebot.motion.joints import (
    HEAD_PAN, HEAD_TILT, MotionConfigError, build_joint_specs, load_calibration,
)

from motion_testkit import DEFAULT_JOINTS, FULL_CALIBRATION, record_writes, run_until, write_calibration

ARMS_AND_PAN = {
    "head": {"channel": 0, "neutral": 90, "min_angle": 50, "max_angle": 130, "max_speed_dps": 60},
    "left_arm": {"channel": 1, "neutral": 20, "min_angle": 10, "max_angle": 150},
    "right_arm": {"channel": 2, "neutral": 160, "min_angle": 30, "max_angle": 170, "inverted": True,
                  "positive_toward": "min"},
}


def velocities(controller, writes, channel):
    spec = next(s for s in controller.specs.values() if s.channel == channel)
    series = [(t, spec.pulse_to_angle(p)) for t, ch, p in writes if ch == channel]
    return [(t1, (a1 - a0) / (t1 - t0)) for (t0, a0), (t1, a1) in zip(series, series[1:]) if t1 > t0], series


# ----------------------------------------------------------------- defaults + joints
def test_default_head_has_pan_and_tilt_with_own_limits():
    specs = build_joint_specs(MotionConfig())
    assert set(specs) == DEFAULT_JOINTS
    tilt, pan = specs[HEAD_TILT], specs[HEAD_PAN]
    assert HEAD_PAN == "head"  # backward compatible: "head" is the pan axis
    assert tilt.channel == 3 and tilt.min_angle < tilt.neutral < tilt.max_angle
    assert tilt.max_angle - tilt.min_angle <= 40  # a small, safe nodding range
    assert tilt.positive_toward == "max" and tilt.speed_limit == 60 and tilt.accel_limit == 200
    assert len({s.channel for s in specs.values()}) == len(specs)
    assert pan.speed_limit == 60


def test_soft_limits_sit_inside_the_hard_ones():
    spec = build_joint_specs(MotionConfig())[HEAD_TILT]
    assert spec.soft_margin_deg == 2.0
    assert spec.soft_min == spec.min_angle + 2 and spec.soft_max == spec.max_angle - 2
    assert spec.soft_clamp(0) == spec.soft_min and spec.soft_clamp(180) == spec.soft_max
    assert spec.soft_clamp(spec.neutral + 3) == spec.neutral + 3
    assert spec.clamp(0) == spec.min_angle  # the hard clamp is unchanged
    with pytest.raises(ValueError):
        spec.soft_clamp(float("nan"))
    # positive direction respects positive_toward (the right arm is mounted mirrored)
    arm = build_joint_specs(MotionConfig())["right_arm"]
    assert arm.toward(100, 10) == 90 and spec.toward(90, 10) == 100


@pytest.mark.parametrize("margin", [-1.0, 25.0, float("nan")])
def test_invalid_soft_margin_rejected(margin):
    servos = MotionConfig().servos
    servos[HEAD_TILT] = ServoChannelConfig.model_validate({**servos[HEAD_TILT].model_dump(), "soft_margin_deg": margin})
    with pytest.raises(MotionConfigError, match="soft_margin_deg"):
        build_joint_specs(MotionConfig(servos=servos))


def test_calibration_covers_head_tilt_and_soft_margin(tmp_path):
    data = {"version": 1, "servos": {**FULL_CALIBRATION["servos"]}}
    data["servos"][HEAD_TILT] = {**data["servos"][HEAD_TILT], "soft_margin_deg": 4.0}
    cal = load_calibration(write_calibration(tmp_path / "cal.yaml", data), known_joints=DEFAULT_JOINTS,
                           require_all=True)
    specs = build_joint_specs(MotionConfig(), cal)
    assert specs[HEAD_TILT].neutral == 88 and specs[HEAD_TILT].soft_margin_deg == 4.0
    assert specs[HEAD_TILT].soft_max == 104


def test_calibration_without_head_tilt_keeps_motion_simulated(tmp_path):
    """The two-key gate is as strict as before: every configured joint must be calibrated."""
    data = {"version": 1, "servos": {k: v for k, v in FULL_CALIBRATION["servos"].items() if k != HEAD_TILT}}
    cal = write_calibration(tmp_path / "cal.yaml", data)
    d = decide_backend(MotionConfig(backend="pca9685", physical_enabled=True, calibration_file=str(cal)))
    assert d.use_physical is False and "head_tilt" in d.reason
    # a robot without the tilt servo lists only the joints it has: then that calibration is complete
    d = decide_backend(MotionConfig(backend="pca9685", physical_enabled=True, calibration_file=str(cal),
                                    servos=ARMS_AND_PAN))
    assert d.use_physical is True


# ----------------------------------------------------------------- moving the tilt axis
async def test_head_tilt_moves_smoothly_within_its_own_limits(make_controller):
    c = await make_controller(settle_seconds=0.0)
    writes = record_writes(c)
    spec = c.specs[HEAD_TILT]
    assert await c.move_joint(HEAD_TILT, 200.0) is True  # clamped to the hard limit
    assert c.status()["joints"][HEAD_TILT]["angle"] == pytest.approx(spec.max_angle, abs=0.05)
    vel, series = velocities(c, writes, spec.channel)
    assert max(a for _t, a in series) <= spec.max_angle + 0.01
    peak = max(abs(v) for _t, v in vel)
    assert peak <= spec.speed_limit * 1.05
    # minimum-jerk easing: starts and ends gently (no step at either end)
    assert abs(vel[0][1]) < 0.25 * peak and abs(vel[-1][1]) < 0.25 * peak
    accels = [(v1 - v0) / (t1 - t0) for (t0, v0), (t1, v1) in zip(vel, vel[1:])]
    assert max(abs(a) for a in accels) <= spec.accel_limit * 1.2  # sampled at 50 Hz: small discretisation slack
    assert await c.move_joint(HEAD_TILT, -50) is True
    assert c.status()["joints"][HEAD_TILT]["angle"] == pytest.approx(spec.min_angle, abs=0.05)


async def test_tilt_speed_is_per_joint(make_controller):
    servos = {**{k: v.model_dump() for k, v in MotionConfig().servos.items()}}
    servos[HEAD_TILT]["max_speed_dps"] = 20.0
    c = await make_controller(settle_seconds=0.0, servos=servos)
    writes = record_writes(c)
    await c.move_joint(HEAD_TILT, 105)
    vel, _ = velocities(c, writes, c.specs[HEAD_TILT].channel)
    assert max(abs(v) for _t, v in vel) <= 20.0 * 1.05


async def test_status_reports_head_tilt_and_position(make_controller):
    c = await make_controller(settle_seconds=0.0)
    st = c.status()["joints"][HEAD_TILT]
    assert st["channel"] == 3 and st["positive_toward"] == "max" and st["position"] == 0.0
    await c.move_joint(HEAD_TILT, c.specs[HEAD_TILT].max_angle)
    assert c.status()["joints"][HEAD_TILT]["position"] == pytest.approx(1.0)


async def test_robot_without_tilt_servo_still_works(make_controller):
    c = await make_controller(servos=ARMS_AND_PAN, settle_seconds=0.0)
    assert HEAD_TILT not in c.status()["joints"]
    assert await c.run_gesture("nod") is False  # nothing to nod with
    assert await c.run_gesture("celebrate") is True  # the tilt part is simply left out
    assert await c.run_gesture("tilt_curious") is True
    await run_until(lambda: not c.status()["moving"])
    assert c.look_at(0.9, 0.1) is True  # pan only
