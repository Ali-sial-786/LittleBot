#!/usr/bin/env python3
"""Slow servo sweep within the configured / calibrated limits. Simulated by default.

    python scripts/servo_test_pattern.py                       # emulated PCA9685, real-time
    python scripts/servo_test_pattern.py --fast                # simulation without waiting
    python scripts/servo_test_pattern.py --hardware --i-understand-the-risks --joint head

For each joint: energise at neutral, settle, then neutral -> min -> max -> neutral using
the same minimum-jerk planner, speed/acceleration limits and hard clamps as the robot,
at a reduced speed (``--speed-factor``, default 0.3, max 0.5).

Hardware needs --hardware AND --i-understand-the-risks AND motion.physical_enabled AND a
valid calibration covering every joint AND typing "yes". Ctrl-C switches all outputs off.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAX_SPEED_FACTOR = 0.5
SETTLE_SECONDS = 0.5


def sweep_joint(spec: Any, driver: Any, *, speed_factor: float, update_hz: float, cycles: int,
                sleep: Callable[[float], None], say: Callable[[str], None], print_every: float = 0.25) -> int:
    """Run the sweep for one joint. Returns the number of pulse writes."""
    from littlebot.motion.interpolation import MinJerkSegment, plan_duration

    dt = 1.0 / update_hz
    writes = 0
    angle = spec.neutral
    driver.set_pulse_us(spec.channel, spec.angle_to_pulse(angle))  # energise at neutral first
    writes += 1
    say(f"{spec.name}: energised at neutral {angle:.1f} deg, settling {SETTLE_SECONDS}s")
    sleep(SETTLE_SECONDS)
    vmax = spec.speed_limit * speed_factor
    targets = [spec.min_angle, spec.max_angle] * cycles + [spec.neutral]
    for target in targets:
        target = spec.clamp(target)
        duration = plan_duration(target - angle, vmax, spec.accel_limit, 0.25)
        seg = MinJerkSegment(start=angle, end=target, t0=0.0, duration=duration)
        steps = max(1, int(round(duration / dt)))
        last_print = -1.0
        for i in range(1, steps + 1):
            t = min(i * dt, duration)
            angle = spec.clamp(seg.position(t))
            pulse = spec.angle_to_pulse(angle)
            driver.set_pulse_us(spec.channel, pulse)
            writes += 1
            if t - last_print >= print_every or i == steps:
                last_print = t
                frac = (angle - spec.min_angle) / (spec.max_angle - spec.min_angle)
                bar = "#" * int(round(frac * 30))
                say(f"  {spec.name:9s} {angle:6.1f} deg {pulse:7.1f} us |{bar:<30s}|")
            sleep(dt)
        angle = target
    driver.off(spec.channel)
    say(f"{spec.name}: done, de-energised")
    return writes


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Slow sweep of LittleBot's servos within their limits.")
    p.add_argument("--hardware", action="store_true", help="drive the real PCA9685 (needs the risk flag too)")
    p.add_argument("--i-understand-the-risks", dest="risks", action="store_true")
    p.add_argument("--joint", action="append", help="only this joint (repeatable)")
    p.add_argument("--cycles", type=int, default=1, help="min->max sweeps per joint (1-5)")
    p.add_argument("--speed-factor", type=float, default=0.3, help=f"fraction of max speed (<= {MAX_SPEED_FACTOR})")
    p.add_argument("--fast", action="store_true", help="simulation only: do not wait in real time")
    p.add_argument("--mode", default="pi", choices=["simulation", "pi", "offline"])
    p.add_argument("--config", help="extra config YAML to layer on top")
    return p


def main(argv: Sequence[str] | None = None, *, input_fn: Callable[[str], str] = input,
         output: Callable[[str], None] = print, config: Any = None) -> int:
    args = build_parser().parse_args(argv)
    say = output
    from littlebot.core.config import load_config
    from littlebot.motion.controller import decide_backend
    from littlebot.motion.joints import MotionConfigError, build_joint_specs

    if args.hardware and not args.risks:
        say("Refusing to drive hardware: add --i-understand-the-risks after reading docs/MOTION_SAFETY.md.")
        return 2
    if args.hardware and args.fast:
        say("--fast is for simulation only.")
        return 2
    if not 0 < args.speed_factor <= MAX_SPEED_FACTOR:
        say(f"--speed-factor must be in (0, {MAX_SPEED_FACTOR}]")
        return 2
    if not 1 <= args.cycles <= 5:
        say("--cycles must be 1..5")
        return 2
    cfg = config or load_config(mode=args.mode, config_file=args.config)
    mcfg = cfg.motion
    calibration = None
    if args.hardware:
        # Same gate as the robot itself, except backend may still be 'mock' while testing.
        decision = decide_backend(mcfg.model_copy(update={"backend": "pca9685"}))
        if not decision.use_physical:
            say(f"Refusing to drive hardware: {decision.reason.replace('simulation only: ', '')}")
            return 2
        calibration = decision.calibration
    else:
        decision = decide_backend(mcfg)
        calibration = decision.calibration  # simulate with calibrated limits when available
    try:
        specs = build_joint_specs(mcfg, calibration)
    except MotionConfigError as e:
        say(f"Motion config invalid: {e}")
        return 2
    names = args.joint or list(specs)
    unknown = [n for n in names if n not in specs]
    if unknown:
        say(f"Unknown joint(s): {', '.join(unknown)}")
        return 2

    driver: Any = None
    try:
        if args.hardware:
            say("Hands clear of every pinch point; child not present; LittleBot itself stopped; "
                "be ready to cut servo power.")
            if input_fn('Type "yes" to start the sweep > ').strip().lower() != "yes":
                say("Not confirmed - nothing was energised.")
                return 2
            from littlebot.motion.drivers import DriverError, PCA9685Driver

            try:
                driver = PCA9685Driver(bus_number=mcfg.i2c_bus, address=mcfg.i2c_address,
                                       frequency_hz=mcfg.pwm_frequency)
            except DriverError as e:
                say(f"PCA9685 not available: {e}")
                return 3
        else:
            from littlebot.motion.drivers import MockPCA9685

            driver = MockPCA9685(frequency_hz=mcfg.pwm_frequency, address=mcfg.i2c_address)
            say("SIMULATION (emulated PCA9685)" + (" - fast" if args.fast else ""))
        sleep = (lambda _s: None) if args.fast else time.sleep
        total = 0
        for name in names:
            total += sweep_joint(specs[name], driver, speed_factor=args.speed_factor, update_hz=mcfg.update_hz,
                                 cycles=args.cycles, sleep=sleep, say=say)
        say(f"Sweep complete ({total} pulse writes).")
        return 0
    except (KeyboardInterrupt, EOFError):
        say("\nInterrupted - outputs off.")
        return 130
    finally:
        if driver is not None:
            try:
                driver.all_off()
                driver.close()
            except Exception as e:  # noqa: BLE001
                say(f"WARNING: could not switch outputs off: {e} - cut servo power now")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
