"""Minimum-jerk planning: speed/acceleration limits, smoothness, braking."""

from __future__ import annotations

import pytest

from littlebot.motion.interpolation import (
    BrakeSegment, MinJerkSegment, brake_duration, limit_step, make_brake, minimum_jerk, minimum_jerk_rate,
    plan_duration, sample, smoothstep,
)


def numeric_derivative(values: list[float], dt: float) -> list[float]:
    return [(b - a) / dt for a, b in zip(values, values[1:])]


def test_easing_curves_endpoints_and_monotonic():
    for f in (smoothstep, minimum_jerk):
        assert f(0) == 0 and f(1) == 1 and f(-1) == 0 and f(2) == 1
        assert f(0.5) == pytest.approx(0.5)
        xs = [i / 100 for i in range(101)]
        ys = [f(x) for x in xs]
        assert all(b >= a for a, b in zip(ys, ys[1:]))
    assert minimum_jerk_rate(0) == 0 and minimum_jerk_rate(1) == 0
    assert minimum_jerk_rate(0.5) == pytest.approx(1.875)


@pytest.mark.parametrize("distance,vmax,amax", [(100, 90, 200), (15, 90, 200), (5, 60, 200), (140, 30, 50),
                                                (-80, 60, 400)])
def test_planned_minimum_jerk_respects_speed_and_acceleration(distance, vmax, amax):
    duration = plan_duration(distance, vmax, amax)
    seg = MinJerkSegment(start=10.0, end=10.0 + distance, t0=0.0, duration=duration)
    dt = 0.001
    pos = sample(seg, dt)
    vel = numeric_derivative(pos, dt)
    acc = numeric_derivative(vel, dt)
    assert max(abs(v) for v in vel) <= vmax * 1.001
    assert max(abs(a) for a in acc) <= amax * 1.01
    assert pos[0] == pytest.approx(10.0) and pos[-1] == pytest.approx(10.0 + distance)
    assert seg.peak_velocity <= vmax + 1e-9


def test_speed_limit_holds_per_tick_at_50hz():
    vmax = 60.0
    seg = MinJerkSegment(start=50.0, end=130.0, t0=0.0, duration=plan_duration(80.0, vmax, 200.0))
    dt = 1 / 50
    pos = sample(seg, dt)
    assert max(abs(b - a) for a, b in zip(pos, pos[1:])) <= vmax * dt + 1e-9


def test_motion_is_smooth_no_jumps():
    seg = MinJerkSegment(start=20.0, end=150.0, t0=1.0, duration=plan_duration(130, 90, 200))
    dt = 0.02
    pos = sample(seg, dt)
    vel = numeric_derivative(pos, dt)
    # starts and ends at rest, velocity changes gradually (bounded jerk-ish second differences)
    assert abs(vel[0]) < 1.0 and abs(vel[-1]) < 1.5
    dv = [abs(b - a) for a, b in zip(vel, vel[1:])]
    assert max(dv) <= 200 * dt * 1.05
    assert seg.velocity(seg.t0) == 0 and seg.velocity(seg.end_time) == 0
    assert seg.position(0.0) == 20.0 and seg.position(99.0) == 150.0


def test_plan_duration_edge_cases():
    assert plan_duration(0.0, 90, 200) == 0.0
    assert plan_duration(0.0, 90, 200, min_duration=0.3) == 0.3
    assert plan_duration(1.0, 90, 200, min_duration=2.0) == 2.0
    with pytest.raises(ValueError):
        plan_duration(10, 0, 200)


def test_brake_decelerates_within_limit_to_rest():
    amax = 200.0
    brake = make_brake(position=80.0, velocity=60.0, t0=0.0, max_accel=amax)
    assert isinstance(brake, BrakeSegment)
    assert brake.duration == pytest.approx(brake_duration(60.0, amax))
    dt = 0.001
    pos = sample(brake, dt)
    vel = numeric_derivative(pos, dt)
    acc = numeric_derivative(vel, dt)
    assert vel[0] == pytest.approx(60.0, rel=0.01)
    assert abs(vel[-1]) < 0.1
    assert max(abs(a) for a in acc) <= amax * 1.01
    assert pos[-1] == pytest.approx(brake.end) == pytest.approx(80.0 + 60.0 * brake.duration / 2)
    assert all(b >= a - 1e-12 for a, b in zip(pos, pos[1:]))  # never reverses


def test_limit_step():
    assert limit_step(10.0, 20.0, 1.5) == 11.5
    assert limit_step(10.0, 0.0, 1.5) == 8.5
    assert limit_step(10.0, 10.4, 1.5) == 10.4
