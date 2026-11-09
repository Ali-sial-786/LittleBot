"""look_at(x, y): view coordinates -> head pan / tilt, and the eyes-lead-head-follows timing."""

from __future__ import annotations

import asyncio
import threading

import pytest

from littlebot.core.arbiter import Priority, Resource
from littlebot.core.clock import ManualClock
from littlebot.core.config import LookConfig, MotionConfig
from littlebot.motion.joints import HEAD_PAN, HEAD_TILT, build_joint_specs
from littlebot.motion.look import MIN_TURN_DEG, LookPlanner, LookSettings

from motion_testkit import run_until


def specs():
    return build_joint_specs(MotionConfig())


def planner(**kw) -> LookPlanner:
    return LookPlanner(LookSettings.from_config(LookConfig(**kw)))


def follow(p: LookPlanner, x, y, t, s, **kw):
    return p.update(x, y, t, pan=s.get(HEAD_PAN), tilt=s.get(HEAD_TILT), **kw)


# ----------------------------------------------------------------- mapping
def test_camera_on_head_turns_by_the_offset_times_gain():
    s = specs()
    p = planner()
    assert follow(p, 0.8, 0.3, 0.0, s) is None  # first sighting: the eyes take it
    targets = follow(p, 0.8, 0.3, 0.5, s)
    # x 0.8: 0.3 of a 70 degree view to the robot's LEFT (+ = toward max for the pan), gain 0.8
    assert targets[HEAD_PAN] == pytest.approx(90 + 0.3 * 70 * 0.8)
    # y 0.3 = above the centre: 0.2 of a 45 degree view UP
    assert targets[HEAD_TILT] == pytest.approx(90 + 0.2 * 45 * 0.8)


def test_relative_to_where_the_head_points_now():
    s = specs()
    p = planner()
    follow(p, 0.2, 0.5, 0.0, s, pan_angle=100.0)
    targets = follow(p, 0.2, 0.5, 0.5, s, pan_angle=100.0)
    assert targets == {HEAD_PAN: pytest.approx(100 - 0.3 * 70 * 0.8)}  # tilt already centred: not moved


def test_mirrored_joint_and_absolute_camera():
    s = specs()
    s[HEAD_PAN] = s[HEAD_PAN].__class__(**{**s[HEAD_PAN].to_dict(), "positive_toward": "min"})
    p = planner(camera_on_head=False, gain=1.0)
    follow(p, 0.8, 0.5, 0.0, s, pan_angle=90.0)
    targets = follow(p, 0.8, 0.5, 0.5, s, pan_angle=90.0)
    assert targets[HEAD_PAN] == pytest.approx(90 - 0.3 * 70)  # robot's left is toward min on this mounting
    # absolute: the same target from a turned head goes to the same place
    p = planner(camera_on_head=False, gain=1.0)
    follow(p, 0.8, 0.5, 0.0, s, pan_angle=120.0)
    assert follow(p, 0.8, 0.5, 0.5, s, pan_angle=120.0)[HEAD_PAN] == pytest.approx(90 - 0.3 * 70)


def test_targets_stay_inside_the_soft_limits_and_a_limit_does_not_retrigger():
    s = specs()
    p = planner(gain=1.0, settle_seconds=0.0, fov_h_deg=120.0)  # 60 degrees: beyond the pan's soft limit
    follow(p, 1.0, 0.0, 0.0, s)
    targets = follow(p, 1.0, 0.0, 0.5, s)
    assert targets[HEAD_PAN] == s[HEAD_PAN].soft_max and targets[HEAD_TILT] == s[HEAD_TILT].soft_max
    # already at the soft limits: a target still beyond them is not worth another move
    a = {"pan_angle": s[HEAD_PAN].soft_max, "tilt_angle": s[HEAD_TILT].soft_max}
    for t in (1.0, 1.5, 2.0, 3.0):
        assert follow(p, 1.0, 0.0, t, s, **a) is None


def test_settings_from_bad_config_are_clamped():
    st = LookSettings.from_config(LookConfig(fov_h_deg=-5, gain=7, follow_offset=float("nan"), speed=0))
    assert st.fov_h_deg == 10 and st.gain == 1.0 and st.follow_offset == 0.15 and st.speed == 0.05


# ----------------------------------------------------------------- eyes lead, head follows
def test_small_shifts_never_move_the_head():
    s = specs()
    p = planner()
    for i in range(50):  # 5 s of a target 0.1 off-centre
        assert follow(p, 0.6, 0.45, i * 0.1, s) is None
    assert p.follows == 0


def test_head_follows_only_after_the_target_stays_off_centre():
    s = specs()
    p = planner()  # 0.15 / 0.4 s
    assert follow(p, 0.75, 0.5, 10.0, s) is None
    assert follow(p, 0.75, 0.5, 10.2, s) is None
    assert follow(p, 0.75, 0.5, 10.39, s) is None
    assert follow(p, 0.75, 0.5, 10.41, s) is not None
    assert p.follows == 1


def test_a_glance_back_to_the_centre_restarts_the_wait():
    s = specs()
    p = planner()
    follow(p, 0.8, 0.5, 0.0, s)
    follow(p, 0.8, 0.5, 0.3, s)
    assert follow(p, 0.52, 0.5, 0.35, s) is None  # back near the centre: timer reset
    assert follow(p, 0.8, 0.5, 0.5, s) is None
    assert follow(p, 0.8, 0.5, 0.85, s) is None
    assert follow(p, 0.8, 0.5, 0.91, s) is not None


def test_nothing_is_judged_while_the_head_moves_or_settles():
    s = specs()
    p = planner(settle_seconds=0.6)
    follow(p, 0.8, 0.5, 0.0, s)
    assert follow(p, 0.8, 0.5, 0.5, s) is not None
    assert follow(p, 0.8, 0.5, 0.6, s) is None  # settling
    for t in (0.7, 1.0, 1.4):
        assert follow(p, 0.8, 0.5, t, s, busy=True) is None
    assert follow(p, 0.8, 0.5, 1.9, s) is None  # settle runs from the end of the move (1.4 + 0.6)
    assert follow(p, 0.8, 0.5, 2.05, s) is None  # off-centre again: start the 0.4 s wait
    assert follow(p, 0.8, 0.5, 2.5, s) is not None


def test_no_head_joints_means_no_plan():
    p = planner()
    assert p.update(0.9, 0.9, 0.0, pan=None, tilt=None) is None
    assert MIN_TURN_DEG > 0


# ----------------------------------------------------------------- the controller
async def test_controller_look_at_turns_the_head_after_the_dwell(make_controller):
    clock = ManualClock()
    c = await make_controller(clock=clock, settle_seconds=0.0)
    assert c.look_at(0.85, 0.5) is True
    clock.advance(0.2)
    assert c.look_at(0.85, 0.5) is True
    await asyncio.sleep(0)
    assert c.status()["moving"] is False  # the eyes lead: the head waits
    clock.advance(0.3)
    assert c.look_at(0.85, 0.5) is True
    await run_until(lambda: c.status()["moving"])
    await run_until(lambda: not c.status()["moving"])
    st = c.status()
    assert st["joints"][HEAD_PAN]["angle"] == pytest.approx(90 + 0.35 * 70 * 0.8, abs=0.05)
    assert st["joints"][HEAD_TILT]["angle"] == pytest.approx(90, abs=0.05)
    assert st["look"]["follows"] == 1 and st["gesture"] is None


async def test_look_at_refusals(make_controller, make_ctx):
    ctx = make_ctx()
    c = await make_controller(ctx)
    assert c.look_at(float("nan"), 0.5) is False
    assert c.look_at("left", 0.5) is False  # type: ignore[arg-type]
    lease = ctx.arbiter.acquire("activity", [Resource.MOTION], Priority.ACTIVITY)
    assert c.look_at(0.9, 0.5) is False  # an activity holds the motors
    assert c.look_at(0.9, 0.5, owner="activity") is True  # ... unless it asks itself
    lease.release()
    c.emergency_stop("test")
    assert c.look_at(0.9, 0.5) is False
    assert c.reset_emergency_stop() is True
    assert c.look_at(0.9, 0.5) is True


async def test_look_at_without_head_joints(make_controller):
    arms = {k: v.model_dump() for k, v in MotionConfig().servos.items() if k in ("left_arm", "right_arm")}
    c = await make_controller(servos=arms)
    assert c.look_at(0.9, 0.5) is False


async def test_look_never_interrupts_a_gesture_and_gives_way_to_one(make_controller):
    clock = ManualClock()
    c = await make_controller(clock=clock, settle_seconds=0.0)
    nod = asyncio.create_task(c.run_gesture("nod", priority=Priority.AMBIENT))
    await run_until(lambda: c.status()["gesture"] == "nod")
    for _ in range(10):  # a target far off-centre for 2 s while the head nods
        c.look_at(0.95, 0.5, priority=Priority.PARENT)
        clock.advance(0.2)
        await asyncio.sleep(0)
    assert await nod is True  # the nod finished undisturbed
    assert c.status()["look"]["follows"] == 0
    # a look in progress is superseded by any gesture, whatever its priority
    for _ in range(4):
        c.look_at(0.95, 0.5, priority=Priority.PARENT)
        clock.advance(0.3)
    await run_until(lambda: c.status()["moving"])
    assert await c.run_gesture("nod_small", priority=Priority.AMBIENT) is True


async def test_look_at_is_thread_safe(make_controller):
    c = await make_controller()
    results: list[bool] = []
    th = threading.Thread(target=lambda: results.extend(c.look_at(0.7, 0.4) for _ in range(50)))
    th.start()
    while th.is_alive():
        await asyncio.sleep(0)
    th.join()
    assert results and all(results)
