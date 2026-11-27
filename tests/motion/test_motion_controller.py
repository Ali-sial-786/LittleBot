"""MotionController end-to-end: scheduling, gestures, cancel, STOP_ALL, e-stop, leases, gating, faults."""

from __future__ import annotations

import asyncio
import sys
import threading

import pytest

from littlebot.core.arbiter import Priority, Resource
from littlebot.core.clock import Clock, ManualClock
from littlebot.core.events import Topics, collect
from littlebot.core.interfaces import MotionAPI
from littlebot.core.service import Service, ServiceStatus
from littlebot.motion import create_service
from littlebot.motion.controller import ARMED_KEY, MotionController, decide_backend
from littlebot.motion.drivers import DriverError, MockSMBus, PCA9685Chip, PCA9685Driver

from motion_testkit import DEFAULT_JOINTS, record_writes, run_until, write_calibration

STATUS_KEYS = {"backend", "physical_enabled", "armed", "estop", "moving", "gesture", "joints", "simulation_only",
               "reason"}
JOINT_KEYS = {"angle", "target", "min", "max", "neutral", "channel"}


def angles_by_channel(controller: MotionController, writes):
    specs = {s.channel: s for s in controller.specs.values()}
    out: dict[int, list[tuple[float, float]]] = {}
    for t, ch, pulse in writes:
        out.setdefault(ch, []).append((t, specs[ch].pulse_to_angle(pulse)))
    return out, specs


def assert_speed_limits(controller: MotionController, writes, factor: float = 1.0) -> None:
    """Between any two consecutive writes, |delta angle| <= max_speed * dt (+5% rate-limiter margin)."""
    by_ch, specs = angles_by_channel(controller, writes)
    for ch, series in by_ch.items():
        vmax = specs[ch].speed_limit * factor
        for (t0, a0), (t1, a1) in zip(series, series[1:]):
            dt = max(t1 - t0, 1e-9)
            assert abs(a1 - a0) <= vmax * dt * 1.05 + 1e-3, (specs[ch].name, t0, a0, t1, a1)


# ----------------------------------------------------------------- contract
async def test_create_service_implements_contract(make_ctx, smbus2_blocker):
    ctx = make_ctx()
    svc = create_service(ctx)
    assert isinstance(svc, MotionController) and isinstance(svc, MotionAPI) and isinstance(svc, Service)
    await svc.start()
    try:
        st = svc.status()
        assert STATUS_KEYS <= set(st)
        assert st["backend"] == "mock" and st["simulation_only"] is True and st["physical_enabled"] is False
        assert "simulation only" in st["reason"]
        assert set(st["joints"]) == DEFAULT_JOINTS
        for j in st["joints"].values():
            assert JOINT_KEYS <= set(j)
        assert set(ctx.motion.gestures() if ctx.motion else []) <= set(svc.gestures())
        assert svc.health()["status"] == "running"
        assert svc.idle and svc.ticks == 0  # nothing moves on its own after start
        assert st["moving"] is False and st["joints"]["head"]["energised"] is False
    finally:
        await svc.stop()
    assert "smbus2" not in sys.modules and smbus2_blocker.attempts == 0


# ----------------------------------------------------------------- gestures end-to-end
async def test_run_gesture_end_to_end(make_controller, make_ctx):
    ctx = make_ctx()
    events = collect(ctx.bus, ["motion.*"])
    c = await make_controller(ctx)
    writes = record_writes(c)
    assert await c.run_gesture("wave") is True
    topics = [e.topic for e in events if e.topic != Topics.MOTION_POSITIONS]
    assert topics == [Topics.MOTION_GESTURE_STARTED, Topics.MOTION_GESTURE_FINISHED]
    finished = [e for e in events if e.topic == Topics.MOTION_GESTURE_FINISHED][0]
    assert finished.data == {"gesture": "wave", "cancelled": False}
    positions = [e for e in events if e.topic == Topics.MOTION_POSITIONS]
    moving = [e for e in positions if e.data["moving"]]
    assert len(moving) >= 20  # ~10 Hz over a multi-second gesture
    assert positions[-1].data["moving"] is False
    assert set(positions[-1].data["joints"]) == DEFAULT_JOINTS
    # ~10 Hz: consecutive publishes at least 0.1 s apart in simulated time
    assert c.status()["gesture"] is None and c.status()["moving"] is False
    st = c.status()["joints"]
    assert st["right_arm"]["angle"] == pytest.approx(160.0) and st["head"]["angle"] == pytest.approx(90.0)
    assert st["left_arm"]["energised"] is False  # wave never moves the left arm
    assert_speed_limits(c, writes)
    # outputs: the emulated chip outputs the neutral pulses at the end
    assert c.driver.pulse_us(2) == pytest.approx(c.specs["right_arm"].angle_to_pulse(160), abs=5)


async def test_every_gesture_runs_within_limits(make_controller, caplog):
    c = await make_controller()
    writes = record_writes(c)
    with caplog.at_level("DEBUG", logger="littlebot.motion"):
        for name in c.gestures():
            assert await c.run_gesture(name) is True, name
    assert "rate limiter engaged" not in caplog.text  # the planner alone keeps within limits
    assert_speed_limits(c, writes)
    by_ch, specs = angles_by_channel(c, writes)
    for ch, series in by_ch.items():
        for _t, a in series:
            assert specs[ch].min_angle - 0.01 <= a <= specs[ch].max_angle + 0.01
    assert await c.run_gesture("no_such_gesture") is False


async def test_first_move_eases_from_assumed_neutral(make_controller):
    c = await make_controller(settle_seconds=0.5)
    writes = record_writes(c)
    t_start = c._clock.now()  # noqa: SLF001
    assert await c.move_joint("head", 120) is True
    head = [(t, p) for t, ch, p in writes if ch == 0]
    neutral_pulse = c.specs["head"].angle_to_pulse(90)
    assert head[0][1] == pytest.approx(neutral_pulse)  # energised exactly at neutral first
    second_t = head[1][0]
    assert second_t - t_start >= 0.5  # held at neutral for the settle time before moving
    assert head[1][1] - neutral_pulse < 5  # then a tiny first step, never a jump
    assert c.status()["joints"]["head"]["angle"] == pytest.approx(120)


async def test_out_of_range_commands_are_clamped(make_controller):
    c = await make_controller()
    writes = record_writes(c)
    assert await c.move_joint("head", 500) is True
    assert c.status()["joints"]["head"]["angle"] == pytest.approx(130)
    assert await c.move_joint("head", -40) is True
    assert c.status()["joints"]["head"]["angle"] == pytest.approx(50)
    lo, hi = c.specs["head"].angle_to_pulse(50), c.specs["head"].angle_to_pulse(130)
    assert all(lo - 0.01 <= p <= hi + 0.01 for _t, ch, p in writes if ch == 0)
    assert await c.move_joint("tail", 10) is False
    assert await c.move_joint("head", float("nan")) is False
    assert await c.move_joint("head", 100, speed_dps=0) is False
    assert await c.move_joint("head", 100, speed_dps=float("inf")) is False


async def test_speed_cap_and_global_speed_scale(make_controller):
    c = await make_controller(settle_seconds=0.0)
    assert await c.move_joint("head", 90.5) is True  # energise
    t0 = c._clock.now()  # noqa: SLF001
    assert await c.move_joint("head", 120, speed_dps=10) is True
    assert c._clock.now() - t0 >= 1.875 * 29.5 / 10 - 0.05  # noqa: SLF001
    slow = await make_controller(settle_seconds=0.0, speed_scale=0.5)
    writes = record_writes(slow)
    assert await slow.move_joint("left_arm", 140) is True
    assert_speed_limits(slow, writes, factor=0.5)


async def test_concurrent_moves_on_different_joints_run_in_parallel(make_controller):
    c = await make_controller()
    t0 = c._clock.now()  # noqa: SLF001
    a, b = await asyncio.gather(c.move_joint("head", 110), c.move_joint("left_arm", 60))
    assert a is True and b is True
    st = c.status()["joints"]
    assert st["head"]["angle"] == pytest.approx(110) and st["left_arm"]["angle"] == pytest.approx(60)
    # in parallel: total time is the longer of the two, not the sum
    assert c._clock.now() - t0 < 2.5  # noqa: SLF001


async def test_new_command_supersedes_overlapping_one(make_controller, make_ctx):
    ctx = make_ctx()
    events = collect(ctx.bus, [Topics.MOTION_GESTURE_FINISHED])
    c = await make_controller(ctx)
    first = asyncio.create_task(c.run_gesture("look_left"))
    await run_until(lambda: c.status()["gesture"] == "look_left" and c.ticks > 40)
    assert await c.move_joint("head", 70) is True
    assert await first is False
    assert events[-1].data == {"gesture": "look_left", "cancelled": True}
    assert c.status()["joints"]["head"]["angle"] == pytest.approx(70)


# ----------------------------------------------------------------- cancel
async def test_cancel_mid_gesture_stops_smoothly_where_it_is(make_controller, make_ctx):
    ctx = make_ctx()
    events = collect(ctx.bus, [Topics.MOTION_GESTURE_FINISHED])
    c = await make_controller(ctx, settle_seconds=0.0)
    writes = record_writes(c)
    task = asyncio.create_task(c.run_gesture("raise_arms"))
    await run_until(lambda: c._joints["left_arm"].angle > 60 and abs(c._joints["left_arm"].velocity) > 10)  # noqa: SLF001
    angle_at_cancel = c.status()["joints"]["left_arm"]["angle"]
    c.cancel()
    assert await task is False
    assert events[-1].data == {"gesture": "raise_arms", "cancelled": True}
    await run_until(lambda: not c.status()["moving"])
    final = c.status()["joints"]["left_arm"]["angle"]
    assert final > angle_at_cancel  # decelerated forward, no reversal or snap
    assert final < c.specs["left_arm"].resolve(0.8) - 1  # stopped before the gesture target
    assert final > c.specs["left_arm"].neutral + 5  # and did not return to neutral
    assert_speed_limits(c, writes)
    n = len(writes)
    await run_until(lambda: c.idle)
    assert len(writes) == n  # nothing more is written once stopped
    assert await c.run_gesture("neutral") is True  # cancel is not a latch


async def test_caller_task_cancellation_cancels_motion(make_controller):
    c = await make_controller()
    task = asyncio.create_task(c.run_gesture("dance"))
    await run_until(lambda: c.status()["gesture"] == "dance" and c.ticks > 50)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await run_until(lambda: not c.status()["moving"])
    assert c.status()["gesture"] is None


# ----------------------------------------------------------------- emergency stop
async def test_emergency_stop_halts_immediately_latches_and_resets(make_controller, make_ctx):
    ctx = make_ctx()
    events = collect(ctx.bus, [Topics.MOTION_ESTOP, Topics.MOTION_GESTURE_FINISHED])
    c = await make_controller(ctx)
    writes = record_writes(c)
    task = asyncio.create_task(c.run_gesture("celebrate"))
    await run_until(lambda: c.driver.chip.energised_channels() and c.ticks > 60)
    calls = c.driver.all_off_calls
    c.emergency_stop("test")
    # synchronous: outputs are already off before we yield to the loop
    assert c.driver.chip.energised_channels() == []
    assert c.driver.all_off_calls == calls + 1
    assert events[0].topic == Topics.MOTION_ESTOP and events[0].data == {"active": True, "source": "test"}
    n = len(writes)
    assert await task is False
    for _ in range(200):
        await asyncio.sleep(0)
    assert len(writes) == n and c.driver.chip.energised_channels() == []  # nothing re-energised
    st = c.status()
    assert st["estop"] is True and st["estop_source"] == "test" and st["moving"] is False
    assert "emergency stop" in st["reason"]
    assert all(not j["known"] and not j["energised"] for j in st["joints"].values())
    # latched: everything refused
    assert await c.run_gesture("wave") is False
    assert await c.move_joint("head", 100) is False
    assert await c.neutral() is False
    c.emergency_stop("again")  # idempotent, still off
    assert len([e for e in events if e.topic == Topics.MOTION_ESTOP]) == 1
    # reset
    assert c.reset_emergency_stop() is True
    assert events[-1].data == {"active": False, "source": "reset"}
    assert c.status()["estop"] is False
    writes.clear()
    assert await c.move_joint("left_arm", 40) is True
    first_left = [p for _t, ch, p in writes if ch == 1][0]
    assert first_left == pytest.approx(c.specs["left_arm"].angle_to_pulse(c.specs["left_arm"].neutral))


async def test_emergency_stop_from_another_thread(make_controller):
    c = await make_controller()
    task = asyncio.create_task(c.run_gesture("raise_arms"))
    await run_until(lambda: c.ticks > 40)
    seen: dict[str, object] = {}

    def worker() -> None:
        c.emergency_stop("dashboard-thread")
        seen["energised_after"] = list(c.driver.chip.energised_channels())
        seen["reset_while_busy"] = c.reset_emergency_stop()  # loop has not processed the halt yet

    th = threading.Thread(target=worker)
    th.start()
    th.join()  # blocks the loop: the cleanup is still queued
    assert seen["energised_after"] == []
    assert seen["reset_while_busy"] is False
    assert await task is False
    assert c.reset_emergency_stop() is True


async def test_shutdown_path_estop_then_stop(make_controller, make_ctx):
    ctx = make_ctx()
    c = await make_controller(ctx)
    task = asyncio.create_task(c.run_gesture("wave"))
    await run_until(lambda: c.ticks > 40)
    c.emergency_stop("shutdown")  # what LittleBot._stop_async does first
    await c.stop()
    await c.stop()  # idempotent
    assert await task is False
    chip = c.driver.chip
    assert chip.energised_channels() == [] and chip.sleeping and c.driver.bus.closed
    assert c.health()["status"] == ServiceStatus.STOPPED.value
    assert await c.run_gesture("wave") is False
    c.emergency_stop("late")  # must not raise after close
    c.cancel()
    assert c._task is None  # noqa: SLF001


# ----------------------------------------------------------------- STOP_ALL
async def test_stop_all_cancels_then_returns_slowly_to_neutral_without_latch(make_controller, make_ctx):
    ctx = make_ctx()
    events = collect(ctx.bus, [Topics.MOTION_GESTURE_FINISHED, Topics.MOTION_ESTOP])
    c = await make_controller(ctx, settle_seconds=0.0)
    task = asyncio.create_task(c.run_gesture("raise_arms"))
    await run_until(lambda: c.status()["joints"]["left_arm"]["angle"] > 80)
    writes = record_writes(c)
    ctx.bus.publish(Topics.STOP_ALL, {"source": "parent"})
    c.cancel()  # LittleBot.stop_all calls cancel() right after publishing STOP_ALL
    ctx.arbiter.preempt_all(by="parent")  # ... and then revokes every lease
    assert await task is False
    assert events[0].data == {"gesture": "raise_arms", "cancelled": True}
    await run_until(lambda: not c.status()["moving"])
    st = c.status()
    assert st["estop"] is False
    for name in ("left_arm", "right_arm"):
        assert st["joints"][name]["angle"] == pytest.approx(c.specs[name].neutral)
    assert st["joints"]["head"]["energised"] is False  # untouched joints stay de-energised
    assert_speed_limits(c, writes, factor=max(c.cfg.homing_speed_factor, 0.6))
    assert not [e for e in events if e.topic == Topics.MOTION_ESTOP]
    assert await c.run_gesture("look_left") is True  # not latched


async def test_stop_all_can_be_configured_to_just_stop(make_controller, make_ctx):
    ctx = make_ctx(stop_all_returns_to_neutral=False, settle_seconds=0.0)
    c = await make_controller(ctx)
    task = asyncio.create_task(c.run_gesture("raise_arms"))
    await run_until(lambda: c.status()["joints"]["left_arm"]["angle"] > 80)
    ctx.bus.publish(Topics.STOP_ALL, {"source": "parent"})
    assert await task is False
    await run_until(lambda: not c.status()["moving"])
    assert c.status()["joints"]["left_arm"]["angle"] > 80


async def test_new_command_supersedes_stop_all_homing(make_controller, make_ctx):
    ctx = make_ctx(settle_seconds=0.0)
    c = await make_controller(ctx)
    assert await c.move_joint("head", 125) is True
    ctx.bus.publish(Topics.STOP_ALL, {"source": "parent"})
    await run_until(lambda: c.status()["moving"] and c.status()["joints"]["head"]["angle"] < 124)
    assert await c.move_joint("head", 60) is True
    assert c.status()["joints"]["head"]["angle"] == pytest.approx(60)


# ----------------------------------------------------------------- leases
async def test_gesture_takes_and_releases_motion_lease(make_controller, make_ctx):
    ctx = make_ctx()
    c = await make_controller(ctx)
    task = asyncio.create_task(c.run_gesture("look_left"))
    await run_until(lambda: c.ticks > 5)
    holder = ctx.arbiter.holder(Resource.MOTION)
    assert holder is not None and holder.owner == "motion" and holder.priority == Priority.ACTIVITY
    assert await task is True
    assert ctx.arbiter.is_free(Resource.MOTION)


async def test_refused_when_motion_held_by_higher_priority(make_controller, make_ctx):
    ctx = make_ctx()
    events = collect(ctx.bus, ["motion.gesture_*"])
    c = await make_controller(ctx)
    lease = ctx.arbiter.acquire("parent-dance", [Resource.MOTION], Priority.PARENT)
    assert await c.run_gesture("wave") is False
    assert await c.move_joint("head", 100) is False
    assert events == [] and c.ticks == 0
    assert lease.active  # never preempted by motion
    assert await c.run_gesture("look_left", owner="parent-dance") is True  # the holder itself may use it
    assert await c.run_gesture("look_right", priority=Priority.PARENT) is True
    assert lease.active


async def test_runs_on_behalf_of_activity_without_preempting_it(make_controller, make_ctx):
    ctx = make_ctx()
    preempted = []
    c = await make_controller(ctx)
    lease = ctx.arbiter.acquire("activity:peekaboo", [Resource.FACE, Resource.MOTION], Priority.ACTIVITY,
                                on_preempt=lambda l, by: preempted.append(by))
    assert await c.run_gesture("raise_arms") is True
    assert lease.active and preempted == []
    assert ctx.arbiter.holder(Resource.FACE) is lease
    # the activity ends (lease released) mid-gesture -> its motion stops
    task = asyncio.create_task(c.run_gesture("wave"))
    await run_until(lambda: c.ticks > 600)
    lease.release()
    assert await task is False


async def test_own_lease_preempted_by_activity_cancels_gesture(make_controller, make_ctx):
    ctx = make_ctx()
    c = await make_controller(ctx)
    task = asyncio.create_task(c.run_gesture("dance"))
    await run_until(lambda: c.ticks > 40)
    ctx.arbiter.acquire("activity:songs", [Resource.MOTION, Resource.AUDIO_OUT], Priority.ACTIVITY)
    assert await task is False
    await run_until(lambda: not c.status()["moving"])
    # preempt_all (parent STOP) also cancels
    task = asyncio.create_task(c.run_gesture("look_left", owner="activity:songs"))
    await run_until(lambda: c.status()["gesture"] == "look_left")
    ctx.arbiter.preempt_all(by="parent")
    assert await task is False


# ----------------------------------------------------------------- physical gating (two keys)
class FactorySpy:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls = 0
        self.fail = fail
        self.chip = PCA9685Chip()
        self.bus = MockSMBus({0x40: self.chip})

    def __call__(self, cfg):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return PCA9685Driver(bus=self.bus, address=cfg.i2c_address, frequency_hz=cfg.pwm_frequency)


async def test_real_driver_refused_without_physical_enabled(make_controller, tmp_path, smbus2_blocker):
    cal = write_calibration(tmp_path / "cal.yaml")
    spy = FactorySpy()
    c = await make_controller(backend="pca9685", physical_enabled=False, calibration_file=str(cal),
                              driver_factory=spy)
    assert spy.calls == 0 and c.physical is False
    st = c.status()
    assert st["simulation_only"] and "physical_enabled is false" in st["reason"]
    assert st["calibration"]["loaded"] is True  # simulation still uses calibrated limits
    assert st["joints"]["head"]["neutral"] == 92
    assert await c.run_gesture("look_left") is True  # simulated
    assert spy.chip.energised_channels() == []
    assert smbus2_blocker.attempts == 0 and "smbus2" not in sys.modules


@pytest.mark.parametrize("calibration", ["missing", "invalid", "partial", "unsafe"])
async def test_real_driver_refused_without_valid_calibration(make_controller, tmp_path, calibration):
    path = tmp_path / "cal.yaml"
    if calibration == "invalid":
        path.write_text("servos: {head: {neutral: oops}}", encoding="utf-8")
    elif calibration == "partial":
        write_calibration(path, {"servos": {"head": {"neutral": 90, "min_angle": 50, "max_angle": 130}}})
    elif calibration == "unsafe":
        write_calibration(path, {"servos": {n: {"neutral": 90, "min_angle": 100, "max_angle": 130}
                                            for n in ("head", "left_arm", "right_arm")}})
    spy = FactorySpy()
    c = await make_controller(backend="pca9685", physical_enabled=True, calibration_file=str(path),
                              driver_factory=spy)
    assert spy.calls == 0 and c.physical is False
    st = c.status()
    assert st["simulation_only"] is True and st["backend"] == "mock"
    assert "calibration" in st["reason"]
    assert await c.run_gesture("look_left") is True  # simulation keeps working


def test_decide_backend_is_pure(tmp_path, smbus2_blocker):
    from littlebot.core.config import MotionConfig

    cal = write_calibration(tmp_path / "cal.yaml")
    assert decide_backend(MotionConfig()).use_physical is False
    assert decide_backend(MotionConfig(backend="pca9685", physical_enabled=True,
                                       calibration_file=str(cal))).use_physical is True
    assert smbus2_blocker.attempts == 0


async def test_physical_outputs_stay_off_until_armed(make_controller, make_ctx, tmp_path):
    cal = write_calibration(tmp_path / "cal.yaml")
    ctx = make_ctx(backend="pca9685", physical_enabled=True, calibration_file=str(cal), settle_seconds=0.0)
    spy = FactorySpy()
    c = await make_controller(ctx, driver_factory=spy)
    assert spy.calls == 1 and c.physical is True
    st = c.status()
    assert st["backend"] == "pca9685" and st["simulation_only"] is False and st["armed"] is False
    assert "NOT armed" in st["reason"]
    assert spy.chip.energised_channels() == [] and not spy.chip.sleeping and spy.chip.prescale == 121
    # key 2 missing: refused, nothing energised
    assert await c.run_gesture("look_left") is False
    assert await c.neutral() is False
    assert spy.chip.energised_channels() == []
    # parent arms (never persisted)
    ctx.settings.set(ARMED_KEY, True, source="dashboard")
    assert c.status()["armed"] is True
    assert await c.run_gesture("look_left") is True
    head = c.specs["head"]
    assert head.neutral == 92  # calibrated limits in force
    assert spy.chip.output_pulse_us(0) == pytest.approx(head.angle_to_pulse(92), abs=5)
    assert spy.chip.energised_channels() == [0]
    # disarm: outputs off synchronously, motion stops, joints forgotten
    task = asyncio.create_task(c.run_gesture("raise_arms"))
    await run_until(lambda: len(spy.chip.energised_channels()) >= 2)
    ctx.settings.set(ARMED_KEY, False, source="dashboard")
    assert spy.chip.energised_channels() == []
    assert await task is False
    assert await c.run_gesture("wave") is False
    assert all(not j["known"] for j in c.status()["joints"].values())


async def test_driver_construction_failure_falls_back_to_simulation(make_controller, tmp_path):
    cal = write_calibration(tmp_path / "cal.yaml")
    spy = FactorySpy(fail=DriverError("no I2C device answering at 0x40"))
    c = await make_controller(backend="pca9685", physical_enabled=True, calibration_file=str(cal),
                              driver_factory=spy)
    assert spy.calls == 1 and c.physical is False
    st = c.status()
    assert st["simulation_only"] and "PCA9685 unavailable" in st["reason"]
    assert c.health()["status"] == ServiceStatus.DEGRADED.value
    assert await c.run_gesture("look_left") is True


async def test_default_factory_without_smbus2_falls_back(make_controller, tmp_path, smbus2_blocker):
    cal = write_calibration(tmp_path / "cal.yaml")
    c = await make_controller(backend="pca9685", physical_enabled=True, calibration_file=str(cal))
    assert c.physical is False and "smbus2" in c.status()["reason"]
    assert smbus2_blocker.attempts == 1  # only attempted because all gates were open


# ----------------------------------------------------------------- driver faults
async def test_i2c_error_latches_estop_and_never_crashes(make_controller, make_ctx):
    ctx = make_ctx()
    events = collect(ctx.bus, [Topics.MOTION_ESTOP, Topics.SYSTEM_ERROR])
    c = await make_controller(ctx)
    task = asyncio.create_task(c.run_gesture("wave"))
    await run_until(lambda: c.ticks > 60)
    c.driver.bus.fail_always = True
    assert await task is False
    st = c.status()
    assert st["estop"] is True and st["estop_source"] == "driver_error"
    assert st["error"] and "driver" in st["error"]
    assert c.health()["status"] == ServiceStatus.DEGRADED.value
    topics = [e.topic for e in events]
    assert Topics.SYSTEM_ERROR in topics and Topics.MOTION_ESTOP in topics
    assert [e for e in events if e.topic == Topics.SYSTEM_ERROR][0].data["module"] == "motion"
    assert c._task is not None and not c._task.done()  # noqa: SLF001 - scheduler alive
    assert await c.run_gesture("wave") is False
    assert c.reset_emergency_stop() is False  # bus still failing
    c.driver.bus.fail_always = False
    assert c.reset_emergency_stop() is True
    assert c.status()["error"] is None
    assert await c.run_gesture("look_left") is True


async def test_i2c_error_on_physical_driver_runs_off_loop_and_latches(make_controller, make_ctx, tmp_path):
    cal = write_calibration(tmp_path / "cal.yaml")
    ctx = make_ctx(backend="pca9685", physical_enabled=True, calibration_file=str(cal))
    spy = FactorySpy()
    c = await make_controller(ctx, driver_factory=spy)
    ctx.settings.set(ARMED_KEY, True)
    task = asyncio.create_task(c.run_gesture("raise_arms"))
    await run_until(lambda: len(spy.chip.energised_channels()) >= 2)
    spy.bus.fail_next(1)
    assert await task is False
    assert c.status()["estop_source"] == "driver_error"


# ----------------------------------------------------------------- scheduler idle / real time
async def test_scheduler_idles_when_nothing_moves(make_controller):
    c = await make_controller()
    assert await c.move_joint("head", 100) is True
    ticks = c.ticks
    await asyncio.sleep(0.15)  # real time: no ticks may happen
    assert c.ticks == ticks and c.idle is True
    assert c._task is not None and not c._task.done()  # noqa: SLF001
    assert await c.move_joint("head", 95) is True  # wakes up again
    assert c.ticks > ticks


async def test_real_clock_runs_at_update_rate(make_controller):
    c = await make_controller(clock=Clock(1.0), update_hz=50, settle_seconds=0.0, min_move_seconds=0.25)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await c.move_joint("head", 92) is True
    elapsed = loop.time() - t0
    assert 0.2 <= elapsed < 2.0
    assert 5 <= c.ticks <= 60


async def test_release_when_idle(make_controller):
    c = await make_controller(release_when_idle_seconds=0.05, settle_seconds=0.0)
    assert await c.move_joint("left_arm", 60) is True
    await asyncio.sleep(0.2)
    assert c.driver.chip.energised_channels() == []
    j = c.status()["joints"]["left_arm"]
    assert j["energised"] is False and j["known"] is True
    writes = record_writes(c)
    assert await c.move_joint("left_arm", 70) is True
    # re-energised where it was left, not at neutral
    assert c.specs["left_arm"].pulse_to_angle(writes[0][2]) == pytest.approx(60, abs=0.1)


async def test_stall_pauses_motion_instead_of_jumping(make_controller):
    clock = ManualClock()
    c = await make_controller(clock=clock, settle_seconds=0.0)
    writes = record_writes(c)
    task = asyncio.create_task(c.move_joint("left_arm", 140))
    await run_until(lambda: c.ticks > 20)
    clock.advance(2.0)  # the loop "froze" for two seconds
    assert await task is True
    by_ch, specs = angles_by_channel(c, writes)
    deltas = [abs(b[1] - a[1]) for a, b in zip(by_ch[1], by_ch[1][1:])]
    assert max(deltas) <= specs[1].speed_limit / 50 * 1.05 + 1e-3


async def test_invalid_config_disables_motion(make_controller):
    c = await make_controller(update_hz=1000)
    assert c.health()["status"] == ServiceStatus.FAILED.value
    st = c.status()
    assert "invalid configuration" in st["reason"] and st["error"]
    assert await c.run_gesture("wave") is False
    assert await c.move_joint("head", 100) is False
    assert c.ticks == 0


async def test_restart_after_stop_uses_a_fresh_driver(make_controller):
    c = await make_controller()
    assert await c.move_joint("head", 100) is True
    await c.stop()
    old = c.driver
    await c.start()
    assert c.driver is not old and c.driver.chip.energised_channels() == []
    assert await c.move_joint("head", 80) is True
    assert c.status()["joints"]["head"]["angle"] == pytest.approx(80)


async def test_status_is_thread_safe_during_motion(make_controller):
    c = await make_controller()
    errors: list[BaseException] = []
    stop = threading.Event()

    def poll() -> None:
        try:
            while not stop.is_set():
                st = c.status()
                assert set(st["joints"]) == DEFAULT_JOINTS
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    th = threading.Thread(target=poll)
    th.start()
    try:
        for name in ("wave", "dance", "celebrate"):
            assert await c.run_gesture(name) is True
    finally:
        stop.set()
        th.join()
    assert errors == []


async def test_gesture_interrupting_a_gesture_brings_its_other_joints_home(make_controller, make_ctx):
    """QA: Look Left during a wave left the right arm raised. The interrupted gesture's other
    joints now return to neutral as part of the new gesture."""
    ctx = make_ctx()
    c = await make_controller(ctx)
    wave = asyncio.create_task(c.run_gesture("wave"))
    await run_until(lambda: c._joints["right_arm"].angle < c.specs["right_arm"].neutral - 30)  # noqa: SLF001
    assert await c.run_gesture("look_left") is True
    assert await wave is False
    await run_until(lambda: not c.status()["moving"])
    joints = c.status()["joints"]
    assert joints["right_arm"]["angle"] == pytest.approx(c.specs["right_arm"].neutral, abs=0.5)
    assert joints["head"]["angle"] == pytest.approx(c.specs["head"].neutral, abs=0.5)
