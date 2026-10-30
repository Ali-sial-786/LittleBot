"""Wheels phase: the drive base protocol, the simulated pose, limits, watchdog, e-stop and the gate."""

from __future__ import annotations

import asyncio
import math

import pytest

from littlebot.core.clock import ManualClock
from littlebot.core.config import DriveConfig, MotionConfig
from littlebot.core.events import EventBus, Topics, collect
from littlebot.motion.drive import (
    DRIVE_TOPIC, CommandWatchdog, Drive, DriveBase, MockDrive, MotorHatWheels, SimulatedWheels, create_drive,
    validate_drive_config, wheel_speeds,
)

from motion_testkit import run_until


class LoggedWheels(SimulatedWheels):
    """Records (time, linear, angular) for every command."""

    def __init__(self, clock: ManualClock, *, physical: bool = False) -> None:
        super().__init__()
        self.clock = clock
        self.physical = physical
        self.log: list[tuple[float, float, float]] = []

    def set_velocity(self, linear_mps: float, angular_dps: float) -> None:
        super().set_velocity(linear_mps, angular_dps)
        self.log.append((self.clock.now(), linear_mps, angular_dps))


def make(enabled: bool = True, **kw):
    clock = kw.pop("clock", None) or ManualClock()
    bus = kw.pop("bus", None)
    cfg = DriveConfig(enabled=enabled, **{k: v for k, v in kw.items() if k in DriveConfig.model_fields})
    extra = {k: v for k, v in kw.items() if k not in DriveConfig.model_fields}
    return MockDrive(cfg, clock=clock, bus=bus, **extra), clock


# ----------------------------------------------------------------- refusal + protocol
async def test_disabled_by_default_and_refuses_everything():
    assert MotionConfig().drive.enabled is False
    d, _ = make(enabled=False)
    assert isinstance(d, DriveBase)
    assert await d.forward(0.5) is False
    assert await d.turn(90) is False
    assert await d.drive(0.1, 0.0, 1.0) is False
    st = d.status()
    assert st["enabled"] is False and "disabled" in st["reason"] and st["moving"] is False
    assert st["pose"] == {"x": 0.0, "y": 0.0, "heading": 0.0} and d.ticks == 0


async def test_invalid_config_refuses():
    d, _ = make(max_linear_mps=5.0)
    assert validate_drive_config(d.cfg) and await d.forward(0.1) is False
    assert "invalid" in d.status()["reason"]


async def test_bad_arguments_refused():
    d, _ = make()
    assert await d.forward(float("inf")) is False
    assert await d.turn("left") is False  # type: ignore[arg-type]
    assert await d.drive(0.1, 0.0, -1.0) is False
    assert await d.forward(0.0) is True and d.ticks == 0  # nothing to do


# ----------------------------------------------------------------- pose integration
async def test_forward_and_turn_integrate_the_pose():
    d, clock = make()
    assert await d.forward(0.5) is True
    p = d.pose
    assert p.x == pytest.approx(0.5, abs=1e-3) and p.y == pytest.approx(0.0, abs=1e-9) and p.heading == 0
    assert await d.turn(90) is True
    assert d.pose.heading == pytest.approx(90, abs=0.1)
    assert await d.forward(0.2) is True
    assert d.pose.x == pytest.approx(0.5, abs=2e-3) and d.pose.y == pytest.approx(0.2, abs=2e-3)
    assert await d.forward(-0.2) is True  # backwards
    assert d.pose.y == pytest.approx(0.0, abs=2e-3)
    await run_until(lambda: not d.status()["moving"])
    assert d.wheels.linear == 0 and d.wheels.angular == 0


async def test_square_returns_home_and_heading_wraps():
    d, _ = make(cruise_angular_dps=60.0)
    for _ in range(4):
        assert await d.forward(0.3) is True
        assert await d.turn(90) is True
    p = d.pose
    assert p.x == pytest.approx(0, abs=0.01) and p.y == pytest.approx(0, abs=0.01)
    assert p.heading == pytest.approx(0, abs=0.5)  # 4 x 90 degrees wrapped back to 0
    assert -180 <= p.heading < 180
    assert await d.turn(-270) is True
    assert d.pose.heading == pytest.approx(90, abs=0.5)  # -270 == +90
    d.reset_pose()
    assert d.pose.x == 0 and d.pose.heading == 0


async def test_timed_drive_runs_for_its_duration_then_brakes():
    d, clock = make()
    t0 = clock.now()
    assert await d.drive(0.1, 0.0, 2.0) is True
    assert clock.now() - t0 == pytest.approx(2.0, abs=0.06)
    await run_until(lambda: not d.status()["moving"])
    # ~2 s at 0.1 m/s minus the ramp up, plus the braking distance
    assert 0.17 < d.pose.x < 0.21


# ----------------------------------------------------------------- limits
async def test_speeds_clamped_and_ramped_within_acceleration_limits():
    clock = ManualClock()
    wheels = LoggedWheels(clock)
    cfg = DriveConfig(enabled=True, max_linear_mps=0.15, max_angular_dps=60, max_linear_accel_mps2=0.3,
                      max_angular_accel_dps2=180)
    d = Drive(cfg, wheels=wheels, clock=clock)
    assert await d.drive(5.0, 500.0, 1.5) is True  # asks far too much
    await run_until(lambda: not d.status()["moving"])
    assert max(abs(v) for _t, v, _w in wheels.log) <= 0.15 + 1e-9
    assert max(abs(w) for _t, _v, w in wheels.log) <= 60 + 1e-9
    pts = [(0.0, 0.0, 0.0), *wheels.log]
    for (t0, v0, w0), (t1, v1, w1) in zip(pts, pts[1:]):
        dt = t1 - t0
        assert abs(v1 - v0) <= 0.3 * dt + 1e-6
        assert abs(w1 - w0) <= 180 * dt + 1e-6
    assert d.status()["limits"]["max_linear_mps"] == 0.15


async def test_no_command_runs_longer_than_the_maximum():
    d, clock = make(max_duration_seconds=3.0)
    t0 = clock.now()
    assert await d.drive(0.05, 0.0, 60.0) is True
    assert clock.now() - t0 == pytest.approx(3.0, abs=0.06)


# ----------------------------------------------------------------- watchdog
async def test_open_ended_drive_stops_by_itself_without_new_commands():
    d, clock = make()
    t0 = clock.now()
    assert await d.drive(0.1, 0.0) is False  # nobody re-sent it
    assert clock.now() - t0 == pytest.approx(1.0, abs=0.06)
    assert "watchdog" in d.status()["last"]
    await run_until(lambda: not d.status()["moving"])
    assert d.wheels.linear == 0


async def test_re_sending_keeps_an_open_ended_drive_going():
    d, clock = make()
    first = asyncio.create_task(d.drive(0.1, 0.0))
    t0 = clock.now()
    tasks = [first]
    while clock.now() - t0 < 2.5:  # teleop: the same command every ~0.5 s
        mark = clock.now()
        await run_until(lambda: clock.now() - mark >= 0.5)
        tasks.append(asyncio.create_task(d.drive(0.1, 0.0)))
    assert d.status()["moving"] is True and d.status()["command"] == "velocity"
    assert all([await t for t in tasks[:-1]])  # replaced by the next one: fine
    assert await tasks[-1] is False  # the last one: the watchdog stopped it
    assert d.pose.x > 0.2


async def test_a_stalled_control_loop_stops_the_wheels():
    d, clock = make()
    task = asyncio.create_task(d.drive(0.1, 0.0, 5.0))
    await run_until(lambda: d.status()["linear"] > 0.05)
    clock.advance(2.0)  # the loop could not run for 2 s
    assert await task is False
    assert "stalled" in d.status()["last"] and d.status()["linear"] == 0


def test_command_watchdog_for_real_drivers():
    t = [0.0]
    stops: list[float] = []
    wd = CommandWatchdog(1.0, lambda: stops.append(t[0]), clock=lambda: t[0])
    assert wd.check() is False  # not armed yet
    wd.kick()
    t[0] = 0.9
    assert wd.check() is False
    wd.kick()
    t[0] = 1.8
    assert wd.check() is False
    t[0] = 2.0
    assert wd.check() is True and stops == [2.0]
    assert wd.check() is False  # once
    wd.kick()
    wd.disarm()
    t[0] = 9.0
    assert wd.check() is False and wd.expired == 1


# ----------------------------------------------------------------- stop + e-stop + gate
async def test_stop_brakes_smoothly():
    clock = ManualClock()
    wheels = LoggedWheels(clock)
    d = Drive(DriveConfig(enabled=True), wheels=wheels, clock=clock)
    task = asyncio.create_task(d.forward(1.0))
    await run_until(lambda: d.status()["linear"] >= 0.1 - 1e-9)
    d.stop()
    assert await task is False
    await run_until(lambda: not d.status()["moving"])
    tail = [v for _t, v, _w in wheels.log[-6:]]
    assert tail == sorted(tail, reverse=True) and tail[-1] > 0  # ramped down, not cut
    assert d.pose.x < 1.0


async def test_emergency_stop_cuts_latches_and_resets():
    d, clock = make()
    task = asyncio.create_task(d.forward(1.0))
    await run_until(lambda: d.status()["linear"] > 0.05)
    d.emergency_stop("test")
    assert d.wheels.linear == 0 and d.status()["linear"] == 0  # at once, no ramp
    assert await task is False
    st = d.status()
    assert st["estop"] is True and "emergency stop" in st["reason"]
    assert await d.turn(10) is False
    assert d.reset_emergency_stop() is True
    assert await d.turn(10) is True


async def test_gate_and_physical_wheels_need_arming():
    clock = ManualClock()
    why: list[str | None] = ["motion service stopped"]
    d = Drive(DriveConfig(enabled=True), wheels=SimulatedWheels(), clock=clock, gate=lambda: why[0])
    assert await d.forward(0.1) is False and d.refusal() == "motion service stopped"
    why[0] = None
    assert await d.forward(0.1) is True  # simulated wheels: no arming needed (nothing can move)

    armed = [False]
    real = LoggedWheels(clock, physical=True)
    d = Drive(DriveConfig(enabled=True), wheels=real, clock=clock, armed=lambda: armed[0], physical_enabled=False)
    assert await d.forward(0.1) is False and "physical_enabled" in d.refusal()
    d = Drive(DriveConfig(enabled=True), wheels=real, clock=clock, armed=lambda: armed[0], physical_enabled=True)
    assert await d.forward(0.1) is False and "not armed" in d.refusal()
    armed[0] = True
    assert await d.forward(0.1) is True and real.log


async def test_driver_fault_latches_the_estop():
    class Broken(SimulatedWheels):
        def set_velocity(self, linear_mps: float, angular_dps: float) -> None:
            raise OSError("I2C gone")

    d = Drive(DriveConfig(enabled=True), wheels=Broken(), clock=ManualClock())
    assert await d.forward(0.3) is False
    st = d.status()
    assert st["estop"] is True and "I2C gone" in st["last"] and st["error"] == "I2C gone"
    assert d.reset_emergency_stop() is True and d.status()["error"] is None


# ----------------------------------------------------------------- events + factory + kinematics
async def test_publishes_motion_drive_events():
    bus = EventBus()
    events = collect(bus, [DRIVE_TOPIC])
    d, _ = make(bus=bus)
    assert await d.forward(0.3) is True
    await run_until(lambda: not d.status()["moving"])
    assert DRIVE_TOPIC == "motion.drive" and len(events) >= 5
    last = events[-1].data
    assert last["moving"] is False and last["x"] == pytest.approx(0.3, abs=2e-3) and last["simulated"] is True
    assert {"x", "y", "heading", "linear", "angular", "command", "estop"} <= set(last)
    times = [e.ts for e in events]
    assert times == sorted(times)


def test_create_drive_never_drives_real_motors_by_accident():
    d = create_drive(DriveConfig())
    assert isinstance(d, MockDrive) and d.status()["physical"] is False
    d = create_drive(DriveConfig(backend="motor_hat"), physical_enabled=False)
    assert isinstance(d, MockDrive) and "physical_enabled" in d.status()["note"]
    d = create_drive(DriveConfig(backend="motor_hat", enabled=True), physical_enabled=True)
    assert isinstance(d, MockDrive) and "not written yet" in d.status()["reason"]  # placeholder: simulation
    with pytest.raises(Exception, match="not written yet"):
        MotorHatWheels(DriveConfig())


def test_differential_kinematics():
    assert wheel_speeds(0.1, 0.0, 0.12) == (0.1, 0.1)
    left, right = wheel_speeds(0.0, 90.0, 0.12)
    assert left == pytest.approx(-math.pi / 2 * 0.06) and right == pytest.approx(math.pi / 2 * 0.06)


# ----------------------------------------------------------------- inside the motion controller
async def test_controller_owns_the_wheels_estop_and_stop_all_cover_them(make_controller, make_ctx):
    ctx = make_ctx(drive={"enabled": True})
    c = await make_controller(ctx)
    assert c.status()["drive"]["enabled"] is True and c.status()["drive"]["physical"] is False
    task = asyncio.create_task(c.drive.forward(1.0))
    await run_until(lambda: c.drive.status()["linear"] > 0.05)
    c.emergency_stop("test")
    assert c.drive.status()["linear"] == 0 and await task is False
    assert await c.drive.turn(10) is False
    assert c.reset_emergency_stop() is True
    assert await c.drive.turn(10) is True
    task = asyncio.create_task(c.drive.forward(1.0))
    await run_until(lambda: c.drive.status()["linear"] > 0.05)
    ctx.bus.publish(Topics.STOP_ALL, {"source": "test"})
    assert await task is False
    await run_until(lambda: not c.drive.status()["moving"])


async def test_controller_drive_disabled_by_default_and_closed_on_stop(make_controller):
    c = await make_controller()
    assert await c.drive.forward(0.1) is False
    await c.stop()
    assert "closed" in c.drive.status()["reason"]
    await c.start()  # a restart gets fresh wheels
    assert "disabled" in c.drive.status()["reason"]
