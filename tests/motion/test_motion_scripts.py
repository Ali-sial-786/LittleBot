"""scripts/calibrate_servos.py and scripts/servo_test_pattern.py (simulation + refusal paths)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from littlebot.core.paths import PROJECT_ROOT
from littlebot.motion.joints import build_joint_specs, load_calibration
from littlebot.testing import make_test_config

from motion_testkit import write_calibration


def load_script(name: str):
    path = PROJECT_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"littlebot_script_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Console:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.lines: list[str] = []

    def input(self, prompt: str) -> str:
        self.lines.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def print(self, text: str = "") -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def cfg(tmp_path: Path, **motion):
    motion.setdefault("calibration_file", str(tmp_path / "calibration.yaml"))
    return make_test_config(motion=motion)


# ----------------------------------------------------------------- calibrate_servos
def test_calibrate_refuses_hardware_without_risk_flag(tmp_path, smbus2_blocker):
    mod = load_script("calibrate_servos")
    con = Console([])
    assert mod.main(["--hardware"], input_fn=con.input, output=con.print, config=cfg(tmp_path)) == 2
    assert "SAFETY CHECKLIST" in con.text and "--i-understand-the-risks" in con.text
    assert smbus2_blocker.attempts == 0


def test_calibrate_refuses_hardware_when_physical_disabled(tmp_path, smbus2_blocker):
    mod = load_script("calibrate_servos")
    con = Console(["yes"])
    rc = mod.main(["--hardware", "--i-understand-the-risks"], input_fn=con.input, output=con.print,
                  config=cfg(tmp_path, physical_enabled=False))
    assert rc == 2 and "physical_enabled is false" in con.text
    assert smbus2_blocker.attempts == 0


def test_calibrate_hardware_requires_typed_confirmation(tmp_path, smbus2_blocker):
    mod = load_script("calibrate_servos")
    con = Console(["no"])
    rc = mod.main(["--hardware", "--i-understand-the-risks"], input_fn=con.input, output=con.print,
                  config=cfg(tmp_path, physical_enabled=True))
    assert rc == 2 and "nothing was energised" in con.text
    assert smbus2_blocker.attempts == 0  # never got as far as opening the bus


def test_calibrate_simulated_session_writes_valid_file(tmp_path):
    mod = load_script("calibrate_servos")
    out = tmp_path / "calibration.yaml"
    answers = [
        "",  # energise head at neutral (90)
        "5", "-", "-", "-", "-", "-", "-", "-",  # 90 -> 55
        "[",  # min = 55
        "++", "++", "++", "++", "++", "++", "++", "++", "++", "++", "++", "++", "++", "++",  # 55 -> 125
        "]",  # max = 125
        "2", "c",  # walk back toward configured neutral in 2-degree steps
        "+",  # 92
        "n",  # neutral = 92
        "d", "y",  # + = robot's left -> positive_toward max
        "s",
        "y",  # write file
    ]
    con = Console(answers)
    rc = mod.main(["--simulate", "--joint", "head", "--output", str(out)], input_fn=con.input, output=con.print,
                  config=cfg(tmp_path))
    assert rc == 0, con.text
    cal = load_calibration(out)
    head = cal.servos["head"]
    assert head["min_angle"] == 55 and head["max_angle"] == 125 and head["neutral"] == 92
    assert head["positive_toward"] == "max" and head["channel"] == 0
    assert "SIMULATION" in con.text
    assert "not yet calibrated: left_arm, right_arm" in con.text
    build_joint_specs(cfg(tmp_path).motion, cal)


def test_calibrate_steps_are_small_and_bounded(tmp_path):
    mod = load_script("calibrate_servos")
    from littlebot.motion.drivers import MockPCA9685

    specs = build_joint_specs(cfg(tmp_path).motion)
    driver = MockPCA9685()
    con = Console([])
    session = mod.JointCalibration("head", specs["head"], driver, say=con.print, ask=con.input, step=50)
    assert session.step == 5  # step size capped at 5 degrees
    session.energise_at_neutral()
    for _ in range(100):
        session.move_by(+90)  # each call moves at most 5 degrees
    assert session.angle == 180
    assert driver.pulse_us(0) <= 2500 + 5
    session.marks = {"min_angle": 60, "neutral": 50, "max_angle": 120}
    assert session.result() is None  # min < neutral < max enforced


def test_calibrate_quit_saves_nothing(tmp_path):
    mod = load_script("calibrate_servos")
    out = tmp_path / "calibration.yaml"
    con = Console(["", "+", "q"])
    rc = mod.main(["--simulate", "--output", str(out)], input_fn=con.input, output=con.print, config=cfg(tmp_path))
    assert rc == 130 and not out.exists()
    assert "nothing saved" in con.text


# ----------------------------------------------------------------- servo_test_pattern
def test_pattern_simulated_fast_sweep_stays_within_limits(tmp_path, smbus2_blocker):
    mod = load_script("servo_test_pattern")
    con = Console([])
    rc = mod.main(["--fast", "--joint", "left_arm"], input_fn=con.input, output=con.print, config=cfg(tmp_path))
    assert rc == 0, con.text
    assert "Sweep complete" in con.text and "SIMULATION" in con.text
    assert smbus2_blocker.attempts == 0


def test_pattern_sweep_respects_limits_and_speed(tmp_path):
    mod = load_script("servo_test_pattern")
    from littlebot.motion.drivers import MockPCA9685

    spec = build_joint_specs(cfg(tmp_path).motion)["head"]
    driver = MockPCA9685()
    pulses: list[float] = []
    original = driver.set_pulse_us
    driver.set_pulse_us = lambda ch, us: (pulses.append(us), original(ch, us))  # type: ignore[method-assign]
    mod.sweep_joint(spec, driver, speed_factor=0.3, update_hz=50, cycles=1, sleep=lambda s: None, say=lambda s: None)
    angles = [spec.pulse_to_angle(p) for p in pulses]
    assert min(angles) == pytest.approx(spec.min_angle) and max(angles) == pytest.approx(spec.max_angle)
    assert max(abs(b - a) for a, b in zip(angles, angles[1:])) <= spec.speed_limit * 0.3 / 50 + 1e-6
    assert driver.chip.energised_channels() == []  # de-energised at the end


@pytest.mark.parametrize("argv,needle", [
    (["--hardware"], "--i-understand-the-risks"),
    (["--hardware", "--i-understand-the-risks"], "physical_enabled is false"),
    (["--fast", "--speed-factor", "0.9"], "--speed-factor"),
    (["--hardware", "--i-understand-the-risks", "--fast"], "simulation only"),
])
def test_pattern_refusals(tmp_path, argv, needle, smbus2_blocker):
    mod = load_script("servo_test_pattern")
    con = Console([])
    assert mod.main(argv, input_fn=con.input, output=con.print, config=cfg(tmp_path)) == 2
    assert needle in con.text
    assert smbus2_blocker.attempts == 0


def test_pattern_hardware_needs_calibration(tmp_path, smbus2_blocker):
    mod = load_script("servo_test_pattern")
    con = Console(["yes"])
    rc = mod.main(["--hardware", "--i-understand-the-risks"], input_fn=con.input, output=con.print,
                  config=cfg(tmp_path, physical_enabled=True))
    assert rc == 2 and "calibration" in con.text
    # with a calibration the gate opens; without smbus2 the driver is unavailable (exit 3), nothing crashes
    write_calibration(tmp_path / "calibration.yaml")
    con = Console(["yes"])
    rc = mod.main(["--hardware", "--i-understand-the-risks"], input_fn=con.input, output=con.print,
                  config=cfg(tmp_path, physical_enabled=True))
    assert rc == 3 and "PCA9685 not available" in con.text
