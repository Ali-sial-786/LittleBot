#!/usr/bin/env python3
"""Interactive, deliberately cautious servo calibration for LittleBot.

    python scripts/calibrate_servos.py --simulate                 # practise, no hardware (default)
    python scripts/calibrate_servos.py --hardware --i-understand-the-risks [--joint head]

Calibrates ONE servo at a time: it starts at the configured neutral, moves only in small
steps (1-5 degrees) that you type, and records min / neutral / max (plus which direction
is "positive") per joint into config/calibration.yaml (git-ignored).

Real hardware is driven only when ALL of these hold:
  * --hardware AND --i-understand-the-risks are given,
  * motion.physical_enabled is true in your config (config/local.yaml),
  * you confirm the safety checklist by typing "yes".
Read docs/MOTION_SAFETY.md first. Software checks do not make a toy physically safe.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from littlebot.motion.drivers import ABS_MAX_PULSE_US, ABS_MIN_PULSE_US  # noqa: E402

SAFETY_CHECKLIST = """
================== LittleBot servo calibration - SAFETY CHECKLIST ==================
 [ ] The child is NOT in the room (and pets are out). Nobody leans over the robot.
 [ ] Servos are powered from a SEPARATE regulated 5-6 V supply rated for the stall
     current of every servo together (MG90S: ~0.7-1 A EACH at stall -> >= 3 A for 3).
 [ ] Servo power does NOT come from the Raspberry Pi's 5 V pin.
 [ ] COMMON GROUND: supply (-), PCA9685 GND and Pi GND are connected.
 [ ] PCA9685: V+ (screw terminal) = servo supply; VCC = Pi 3.3 V logic only.
 [ ] Arms are DETACHED / horns removed for the first test of each servo.
 [ ] Fingers, hair and cables are clear of every horn, gear and pinch point.
 [ ] You can cut servo power instantly (supply switch, or pull the PCA9685 OE pin high).
 [ ] I2C is enabled (raspi-config) and `i2cdetect -y 1` shows the board at 0x40.
 [ ] LittleBot itself is NOT running (sudo systemctl stop littlebot) - one program per chip.
 Software limits and tests do NOT establish physical toy safety (docs/MOTION_SAFETY.md).
=====================================================================================
"""

HELP = """commands (type then Enter):
  +  / -     step up / down by the current step size      ++ / --  step by 5 degrees
  1..5       set the step size in degrees                   ?        this help
  n          mark the current angle as NEUTRAL (resting pose)
  [ or min   mark the current angle as MIN                   ] or max  mark as MAX
  d          direction check (which way is 'raise' / 'look left')
  c          walk back to neutral in small steps            o        de-energise (limp)
  r          re-energise at the current angle
  s          save this joint and go to the next             k        skip this joint
  q          quit WITHOUT saving anything"""

MAX_STEP_DEG = 5.0
MIN_STEP_DEG = 1.0
DEFAULT_STEP_DEG = 2.0


class Abort(Exception):
    """User quit (q / Ctrl-C / EOF)."""


class JointCalibration:
    """State for one joint while calibrating (pure logic; I/O through callables)."""

    def __init__(self, name: str, spec: Any, driver: Any, *, say: Callable[[str], None],
                 ask: Callable[[str], str], step: float = DEFAULT_STEP_DEG, hardware: bool = False) -> None:
        from littlebot.motion.joints import JointSpec

        self.name = name
        self.spec = spec
        # Exploration uses the full 0..180 range (that is what we are measuring) with the
        # joint's pulse mapping and inversion; pulses stay within the absolute bounds.
        self.mapping = JointSpec(name=name, channel=spec.channel, neutral=spec.neutral, min_angle=0.0,
                                 max_angle=180.0, inverted=spec.inverted, min_pulse_us=spec.min_pulse_us,
                                 max_pulse_us=spec.max_pulse_us)
        self.driver = driver
        self.say = say
        self.ask = ask
        self.hardware = hardware
        self.step = max(MIN_STEP_DEG, min(MAX_STEP_DEG, float(step)))
        self.angle = float(spec.neutral)
        self.energised = False
        self.marks: dict[str, float] = {}
        self.positive_toward: str = spec.positive_toward

    # -- output ------------------------------------------------------------
    def _write(self) -> None:
        pulse = self.mapping.angle_to_pulse(self.angle)
        pulse = min(ABS_MAX_PULSE_US, max(ABS_MIN_PULSE_US, pulse))
        self.driver.set_pulse_us(self.spec.channel, pulse)
        self.energised = True
        self.say(f"  {self.name}: {self.angle:6.1f} deg  pulse {pulse:7.1f} us  "
                 f"marks {self._marks_text()}")

    def _marks_text(self) -> str:
        if not self.marks:
            return "-"
        return " ".join(f"{k}={v:.1f}" for k, v in sorted(self.marks.items()))

    def move_by(self, delta: float) -> None:
        delta = max(-MAX_STEP_DEG, min(MAX_STEP_DEG, delta))
        self.angle = max(0.0, min(180.0, self.angle + delta))
        self._write()

    def energise_at_neutral(self) -> None:
        self.angle = float(self.spec.neutral)
        self._write()

    def walk_to(self, target: float) -> None:
        import time

        while abs(target - self.angle) > 1e-6:
            delta = max(-self.step, min(self.step, target - self.angle))
            self.move_by(delta)
            if self.hardware:
                time.sleep(0.15)  # ~13 deg/s at a 2 degree step

    def off(self) -> None:
        self.driver.off(self.spec.channel)
        self.energised = False
        self.say(f"  {self.name}: de-energised (limp)")

    # -- direction ---------------------------------------------------------
    def direction_check(self) -> None:
        if self.name == "head":  # the PAN axis
            question = "Step UP (+) turns the head toward the robot's LEFT? [y/n] "
        elif self.name == "head_tilt":
            question = "Step UP (+) tilts the head UP (LittleBot looks up)? [y/n] "
        else:
            question = f"Step UP (+) RAISES the {self.name.replace('_', ' ')}? [y/n] "
        answer = self.ask(question).strip().lower()
        if answer.startswith("y"):
            self.positive_toward = "max"
        elif answer.startswith("n"):
            self.positive_toward = "min"
        else:
            self.say("  no change")
            return
        self.say(f"  positive_toward = {self.positive_toward}")

    # -- result ------------------------------------------------------------
    def result(self) -> dict[str, Any] | None:
        lo = self.marks.get("min_angle", None)
        hi = self.marks.get("max_angle", None)
        mid = self.marks.get("neutral", None)
        if lo is None or hi is None or mid is None:
            self.say("  mark min ([), neutral (n) and max (]) first")
            return None
        if not lo < mid < hi:
            self.say(f"  need min < neutral < max (got {lo:.1f} < {mid:.1f} < {hi:.1f}); re-mark")
            return None
        if hi - lo < 10:
            self.say("  range under 10 degrees - that is probably a mistake; re-mark")
            return None
        return {"channel": self.spec.channel, "neutral": mid, "min_angle": lo, "max_angle": hi,
                "inverted": bool(self.spec.inverted), "positive_toward": self.positive_toward,
                "min_pulse_us": int(self.spec.min_pulse_us), "max_pulse_us": int(self.spec.max_pulse_us),
                "max_speed_dps": float(self.spec.max_speed_dps), "max_accel_dps2": float(self.spec.max_accel_dps2)}

    def run(self) -> dict[str, Any] | None:
        """Interactive loop. Returns the joint's calibration, or None when skipped."""
        s = self.spec
        self.say(f"\n--- {self.name} (channel {s.channel}) - configured neutral {s.neutral:.1f}, "
                 f"limits {s.min_angle:.1f}..{s.max_angle:.1f}, inverted={s.inverted}")
        if self.hardware:
            self.say("  The servo will now be energised at its configured NEUTRAL. If it was elsewhere it")
            self.say("  will move there at full servo speed. Hands clear, horn detached for the first run.")
        if self.ask("  Press Enter to energise (q to quit) > ").strip().lower() == "q":
            raise Abort()
        self.energise_at_neutral()
        self.say(HELP)
        try:
            while True:
                cmd = self.ask(f"[{self.name} {self.angle:.1f} step {self.step:g}] > ").strip().lower()
                if cmd in ("+", "-"):
                    self.move_by(self.step if cmd == "+" else -self.step)
                elif cmd in ("++", "--"):
                    self.move_by(MAX_STEP_DEG if cmd == "++" else -MAX_STEP_DEG)
                elif cmd in ("1", "2", "3", "4", "5"):
                    self.step = float(cmd)
                    self.say(f"  step = {self.step:g} deg")
                elif cmd == "n":
                    self.marks["neutral"] = self.angle
                    self.say(f"  neutral = {self.angle:.1f}")
                elif cmd in ("[", "min"):
                    self.marks["min_angle"] = self.angle
                    self.say(f"  min = {self.angle:.1f}")
                elif cmd in ("]", "max"):
                    self.marks["max_angle"] = self.angle
                    self.say(f"  max = {self.angle:.1f}")
                elif cmd == "d":
                    self.direction_check()
                elif cmd == "c":
                    self.walk_to(self.marks.get("neutral", s.neutral))
                elif cmd == "o":
                    self.off()
                elif cmd == "r":
                    self._write()
                elif cmd == "s":
                    res = self.result()
                    if res is not None:
                        return res
                elif cmd == "k":
                    self.say(f"  {self.name} skipped (not calibrated)")
                    return None
                elif cmd == "q":
                    raise Abort()
                elif cmd in ("?", "h", "help", ""):
                    self.say(HELP)
                else:
                    self.say(f"  unknown command {cmd!r} (? for help)")
        finally:
            try:
                self.driver.off(self.spec.channel)
            except Exception as e:  # noqa: BLE001
                self.say(f"  WARNING: could not de-energise {self.name}: {e} - cut servo power now")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Calibrate LittleBot's servos one at a time (simulated by default).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--simulate", action="store_true", help="use the emulated PCA9685 (default)")
    g.add_argument("--hardware", action="store_true", help="drive the real PCA9685 (needs the risk flag too)")
    p.add_argument("--i-understand-the-risks", dest="risks", action="store_true",
                   help="required with --hardware after reading docs/MOTION_SAFETY.md")
    p.add_argument("--joint", action="append", help="calibrate only this joint (repeatable)")
    p.add_argument("--output", help="calibration file to write (default: motion.calibration_file)")
    p.add_argument("--mode", default="pi", choices=["simulation", "pi", "offline"], help="config mode (default pi)")
    p.add_argument("--config", help="extra config YAML to layer on top")
    p.add_argument("--step", type=float, default=DEFAULT_STEP_DEG, help="initial step size, 1-5 degrees")
    return p


def main(argv: Sequence[str] | None = None, *, input_fn: Callable[[str], str] = input,
         output: Callable[[str], None] = print, config: Any = None) -> int:
    args = build_parser().parse_args(argv)
    say = output

    def ask(prompt: str) -> str:
        try:
            return input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            raise Abort() from None

    from littlebot.core.config import load_config
    from littlebot.motion.joints import (
        CalibrationError, MotionConfigError, build_joint_specs, load_calibration, resolve_path, save_calibration,
    )

    say(SAFETY_CHECKLIST)
    hardware = bool(args.hardware)
    if hardware and not args.risks:
        say("Refusing to drive hardware: add --i-understand-the-risks after reading docs/MOTION_SAFETY.md.")
        return 2
    if not 1 <= args.step <= 5:
        say("--step must be between 1 and 5 degrees")
        return 2
    cfg = config or load_config(mode=args.mode, config_file=args.config)
    mcfg = cfg.motion
    if hardware and not mcfg.physical_enabled:
        say("Refusing to drive hardware: motion.physical_enabled is false. Set it in config/local.yaml only "
            "after the wiring/power checks in docs/MOTION_SAFETY.md have passed (keep backend: mock until "
            "calibration is done).")
        return 2
    try:
        specs = build_joint_specs(mcfg)
    except MotionConfigError as e:
        say(f"Motion config invalid: {e}")
        return 2
    names = args.joint or list(specs)
    unknown = [n for n in names if n not in specs]
    if unknown:
        say(f"Unknown joint(s): {', '.join(unknown)}. Configured: {', '.join(specs)}")
        return 2

    driver: Any = None
    results: dict[str, dict[str, Any]] = {}
    try:
        if hardware:
            if ask('Type "yes" if EVERY checklist item above is true > ').strip().lower() != "yes":
                say("Not confirmed - nothing was energised.")
                return 2
            from littlebot.motion.drivers import DriverError, PCA9685Driver

            try:
                driver = PCA9685Driver(bus_number=mcfg.i2c_bus, address=mcfg.i2c_address,
                                       frequency_hz=mcfg.pwm_frequency)
            except DriverError as e:
                say(f"PCA9685 not available: {e}")
                return 3
            say(f"PCA9685 ready: {driver.describe()} - all outputs off.")
        else:
            from littlebot.motion.drivers import MockPCA9685

            driver = MockPCA9685(frequency_hz=mcfg.pwm_frequency, address=mcfg.i2c_address)
            say("SIMULATION: driving an emulated PCA9685 - no hardware is touched.")
        for name in names:
            session = JointCalibration(name, specs[name], driver, say=say, ask=ask, step=args.step,
                                       hardware=hardware)
            res = session.run()
            if res is not None:
                results[name] = res
    except Abort:
        say("\nAborted - outputs off, nothing saved.")
        return 130
    finally:
        if driver is not None:
            try:
                driver.all_off()
                driver.close()
            except Exception as e:  # noqa: BLE001
                say(f"WARNING: could not switch outputs off: {e} - cut servo power now")

    if not results:
        say("No joint calibrated - nothing written.")
        return 1
    out_path = resolve_path(args.output or mcfg.calibration_file)
    merged: dict[str, dict[str, Any]] = {}
    if out_path.is_file():
        try:
            merged = dict(load_calibration(out_path, known_joints=specs).servos)
        except CalibrationError as e:
            say(f"Existing {out_path} is invalid ({e}); it will be replaced.")
    merged.update(results)
    missing = [n for n in specs if n not in merged]
    say("\nResult:")
    for name, values in merged.items():
        say(f"  {name}: min {values['min_angle']:.1f}  neutral {values['neutral']:.1f}  max {values['max_angle']:.1f}"
            f"  positive_toward {values.get('positive_toward', 'max')}")
    if missing:
        say(f"NOTE: not yet calibrated: {', '.join(missing)} - physical motion stays disabled until every joint is.")
    try:
        if ask(f"Write {out_path}? [y/N] > ").strip().lower() not in ("y", "yes"):
            say("Not written.")
            return 1
    except Abort:
        say("Not written.")
        return 130
    try:
        save_calibration(out_path, merged, pwm_frequency=mcfg.pwm_frequency, i2c_address=mcfg.i2c_address)
        build_joint_specs(mcfg, load_calibration(out_path, known_joints=specs))
    except (CalibrationError, MotionConfigError) as e:
        say(f"Calibration rejected: {e}")
        return 1
    say(f"Wrote {out_path}. Next: run scripts/servo_test_pattern.py, then follow the verification checklist "
        "in docs/MOTION_SAFETY.md before setting motion.backend: pca9685.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
