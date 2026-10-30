"""Joint limits, angle -> pulse mapping, configuration validation and calibration files."""

from __future__ import annotations

import math

import pytest
import yaml

from littlebot.core.config import MotionConfig, ServoChannelConfig
from littlebot.core.paths import CONFIG_DIR
from littlebot.motion.joints import (
    CalibrationError, JointSpec, MotionConfigError, build_joint_specs, load_calibration, save_calibration,
    validate_specs,
)
from littlebot.motion.drivers import MockPCA9685, pulse_to_counts, prescale_for

from motion_testkit import DEFAULT_JOINTS, FULL_CALIBRATION, write_calibration


def spec(**kw) -> JointSpec:
    base = dict(name="j", channel=0, neutral=90.0, min_angle=0.0, max_angle=180.0)
    base.update(kw)
    return JointSpec(**base)


def default_specs() -> dict[str, JointSpec]:
    return build_joint_specs(MotionConfig())


# ----------------------------------------------------------------- mapping
@pytest.mark.parametrize("angle,pulse", [(0, 500), (45, 1000), (90, 1500), (135, 2000), (180, 2500)])
def test_angle_to_pulse_linear(angle, pulse):
    assert spec().angle_to_pulse(angle) == pytest.approx(pulse)


@pytest.mark.parametrize("angle,pulse", [(0, 2500), (45, 2000), (90, 1500), (180, 500)])
def test_angle_to_pulse_inverted(angle, pulse):
    s = spec(inverted=True)
    assert s.angle_to_pulse(angle) == pytest.approx(pulse)
    assert s.pulse_to_angle(pulse) == pytest.approx(angle)


def test_default_joints_map_and_register_counts():
    specs = default_specs()
    # head neutral 90 -> 1500 us -> 307 counts at 50 Hz
    assert specs["head"].angle_to_pulse(90) == pytest.approx(1500)
    # right arm is inverted: joint 160 == servo 20 == left arm joint 20
    assert specs["right_arm"].angle_to_pulse(160) == pytest.approx(500 + 20 / 180 * 2000)
    assert specs["right_arm"].angle_to_pulse(160) == pytest.approx(specs["left_arm"].angle_to_pulse(20))
    drv = MockPCA9685(frequency_hz=50)
    for name, s in specs.items():
        pulse = s.angle_to_pulse(s.neutral)
        drv.set_pulse_us(s.channel, pulse)
        counts = drv.chip.channel_registers(s.channel)["off"]
        assert counts == round(pulse / 20000 * 4096) == pulse_to_counts(pulse, prescale_for(50))


def test_commands_are_hard_clamped():
    head = default_specs()["head"]  # 50..130
    assert head.clamp(500) == 130 and head.clamp(-90) == 50
    assert head.angle_to_pulse(500) == pytest.approx(head.angle_to_pulse(130))
    assert head.angle_to_pulse(-90) == pytest.approx(head.angle_to_pulse(50))
    with pytest.raises(ValueError):
        head.clamp(float("nan"))
    with pytest.raises(ValueError):
        head.clamp(float("inf"))


def test_pulses_never_leave_absolute_bounds():
    # Even a (deliberately invalid, unvalidated) spec cannot produce pulses outside 500..2500.
    s = spec(min_pulse_us=100, max_pulse_us=3500)
    for a in range(0, 181, 5):
        assert 500 <= s.angle_to_pulse(a) <= 2500
    narrow = spec(min_pulse_us=600, max_pulse_us=2400)
    assert narrow.angle_to_pulse(0) == pytest.approx(600)
    assert narrow.angle_to_pulse(180) == pytest.approx(2400)


def test_normalised_positions_follow_positive_direction():
    specs = default_specs()
    head, left, right = specs["head"], specs["left_arm"], specs["right_arm"]
    assert head.resolve(0) == 90 and head.resolve(1) == 130 and head.resolve(-1) == 50
    assert head.resolve(0.5) == pytest.approx(110)
    assert left.resolve(1) == 150 and left.resolve(-1) == 10
    assert right.resolve(1) == 30 and right.resolve(-1) == 170  # raise = toward min for the right arm
    assert right.resolve(5) == 30  # clamped to +-1
    with pytest.raises(ValueError):
        head.resolve(math.nan)
    for s in (head, left, right):  # normalise() is the inverse of resolve()
        for v in (-1.0, -0.3, 0.0, 0.25, 0.8, 1.0):
            assert s.normalise(s.resolve(v)) == pytest.approx(v)
    assert right.normalise(30) == 1.0 and right.normalise(170) == -1.0


# ----------------------------------------------------------------- validation
def test_default_config_is_valid():
    specs = default_specs()
    assert set(specs) == {"head", "head_tilt", "left_arm", "right_arm"}
    for s in specs.values():
        assert s.min_angle < s.neutral < s.max_angle


@pytest.mark.parametrize("override,needle", [
    ({"neutral": 10.0, "min_angle": 20.0}, "min_angle < neutral"),
    ({"neutral": 140.0}, "min_angle < neutral"),
    ({"max_angle": 200.0}, "<= 180"),
    ({"max_speed_dps": 0.0}, "max_speed_dps"),
    ({"max_speed_dps": 500.0}, "max_speed_dps"),
    ({"max_accel_dps2": 5000.0}, "max_accel_dps2"),
    ({"min_pulse_us": 300}, "min_pulse_us"),
    ({"max_pulse_us": 2600}, "min_pulse_us"),
    ({"channel": 16}, "channel"),
    ({"neutral": float("nan")}, "finite"),
])
def test_invalid_joint_config_rejected(override, needle):
    servos = MotionConfig().servos
    servos["head"] = ServoChannelConfig.model_validate({**servos["head"].model_dump(), **override})
    with pytest.raises(MotionConfigError) as e:
        build_joint_specs(MotionConfig(servos=servos))
    assert needle in str(e.value)


def test_duplicate_channels_and_global_settings_rejected():
    servos = MotionConfig().servos
    servos["left_arm"] = ServoChannelConfig.model_validate({**servos["left_arm"].model_dump(), "channel": 0})
    with pytest.raises(MotionConfigError, match="already used"):
        build_joint_specs(MotionConfig(servos=servos))
    with pytest.raises(MotionConfigError, match="pwm_frequency"):
        build_joint_specs(MotionConfig(pwm_frequency=200))
    with pytest.raises(MotionConfigError, match="update_hz"):
        build_joint_specs(MotionConfig(update_hz=1000))
    with pytest.raises(MotionConfigError, match="speed_scale"):
        build_joint_specs(MotionConfig(speed_scale=2.0))
    assert validate_specs({}) == ["no servos configured"]


# ----------------------------------------------------------------- calibration files
def test_calibration_loads_and_overrides(tmp_path):
    path = write_calibration(tmp_path / "cal.yaml")
    cal = load_calibration(path, known_joints=DEFAULT_JOINTS, require_all=True)
    specs = build_joint_specs(MotionConfig(), cal)
    assert specs["head"].neutral == 92 and specs["head"].min_angle == 55 and specs["head"].max_angle == 125
    assert specs["head"].max_speed_dps == 60  # not in calibration -> config value kept
    assert specs["right_arm"].inverted is True


def test_example_calibration_file_is_valid():
    cal = load_calibration(CONFIG_DIR / "calibration.example.yaml", known_joints=DEFAULT_JOINTS, require_all=True)
    build_joint_specs(MotionConfig(), cal)


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.pop("servos"), "servos"),
    (lambda d: d.update(version=2), "version"),
    (lambda d: d.update(colour="red"), "unknown top-level"),
    (lambda d: d["servos"].update(tail={"neutral": 1, "min_angle": 0, "max_angle": 2}), "unknown joint"),
    (lambda d: d["servos"]["head"].update(neutrl=90), "unknown keys"),
    (lambda d: d["servos"]["head"].pop("neutral"), "missing"),
    (lambda d: d["servos"]["head"].update(neutral="ninety"), "finite number"),
    (lambda d: d["servos"]["head"].update(inverted="yes"), "inverted"),
    (lambda d: d["servos"]["head"].update(channel=True), "channel"),
    (lambda d: d["servos"]["head"].update(positive_toward="up"), "positive_toward"),
])
def test_calibration_validation_errors(tmp_path, mutate, needle):
    import copy

    data = copy.deepcopy(FULL_CALIBRATION)
    mutate(data)
    path = write_calibration(tmp_path / "cal.yaml", data)
    with pytest.raises(CalibrationError, match=needle):
        load_calibration(path, known_joints=["head", "left_arm", "right_arm"])


def test_calibration_file_errors(tmp_path):
    with pytest.raises(CalibrationError, match="not found"):
        load_calibration(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("servos: [unclosed", encoding="utf-8")
    with pytest.raises(CalibrationError, match="cannot read"):
        load_calibration(bad)
    bad.write_text("- just a list", encoding="utf-8")
    with pytest.raises(CalibrationError, match="mapping"):
        load_calibration(bad)
    partial = write_calibration(tmp_path / "partial.yaml",
                                {"servos": {"head": {"neutral": 90, "min_angle": 50, "max_angle": 130}}})
    with pytest.raises(CalibrationError, match="missing joints"):
        load_calibration(partial, known_joints=["head", "left_arm"], require_all=True)


def test_calibration_with_unsafe_limits_is_rejected_after_merge(tmp_path):
    import copy

    data = copy.deepcopy(FULL_CALIBRATION)
    data["servos"]["head"].update(neutral=150.0)  # neutral above max
    cal = load_calibration(write_calibration(tmp_path / "cal.yaml", data))
    with pytest.raises(MotionConfigError, match="head"):
        build_joint_specs(MotionConfig(), cal)


def test_save_calibration_round_trip(tmp_path):
    joints = {name: dict(values) for name, values in FULL_CALIBRATION["servos"].items()}
    path = save_calibration(tmp_path / "out" / "calibration.yaml", joints, notes="test rig")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# LittleBot servo calibration")
    data = yaml.safe_load(text)
    assert data["version"] == 1 and data["notes"] == "test rig"
    cal = load_calibration(path, known_joints=joints, require_all=True)
    assert build_joint_specs(MotionConfig(), cal)["left_arm"].max_angle == 140
