"""Low / critical / hot / cold with a fake clock: sustain timers, hysteresis, never critical while charging."""

from __future__ import annotations

from littlebot.power.policy import PowerPolicy, Snapshot, Thresholds


def snap(pct: float | None = 50, cell: float | None = 3.8, t: float | None = 25, charging: bool = False,
         plugged: bool | None = None) -> Snapshot:
    return Snapshot(percent=pct, min_cell=cell, temperature_c=t, charging=charging,
                    plugged_in=charging if plugged is None else plugged)


def run(policy: PowerPolicy, s: Snapshot, start: float, seconds: float, step: float = 1.0) -> list[str]:
    fired: list[str] = []
    t = start
    while t <= start + seconds + 1e-9:
        fired += policy.update(s, t)
        t += step
    return fired


def test_low_fires_once_after_its_sustain_and_rearms_after_charging():
    p = PowerPolicy(Thresholds(low_sustain_s=5))
    assert run(p, snap(25), 0, 10) == []
    assert run(p, snap(20), 11, 3) == []  # not long enough yet
    assert run(p, snap(19), 15, 10) == ["low"]  # 5 s at or below 20 %
    assert run(p, snap(18), 26, 60) == []  # once
    assert p.low and p.flags() == ["low"]
    run(p, snap(19, charging=True), 90, 5)  # plugged in: cleared
    assert not p.low
    assert run(p, snap(19), 100, 10) == ["low"]  # unplugged while still low: again


def test_low_rearms_only_well_above_the_threshold():
    p = PowerPolicy(Thresholds(low_sustain_s=0))
    assert run(p, snap(20), 0, 1) == ["low"]
    assert run(p, snap(22), 2, 5) == [] and p.low  # a wobble just above: still low, no repeat
    run(p, snap(26), 10, 1)
    assert not p.low


def test_critical_needs_twenty_seconds_so_a_load_spike_does_not_count():
    p = PowerPolicy(Thresholds(critical_sustain_s=20, low_sustain_s=0))
    # a cell sags below 3.3 V under a load spike for 5 s, then recovers: nothing
    fired = run(p, snap(30, cell=3.25), 0, 5) + run(p, snap(30, cell=3.5), 6, 5) + run(p, snap(30, cell=3.2), 12, 15)
    assert "critical" not in fired
    # held: after 20 s
    fired = run(p, snap(30, cell=3.2), 28, 30)
    assert fired == ["critical"] and "below 3.30 V" in p.critical_reason
    assert run(p, snap(30, cell=3.2), 60, 30) == []  # once


def test_critical_by_percent_and_its_exact_timing():
    p = PowerPolicy(Thresholds(critical_sustain_s=20, low_sustain_s=0))
    assert run(p, snap(7, cell=3.6), 100, 19) == ["low"]  # 7 % for 19 s: low only
    assert p.update(snap(7, cell=3.6), 120.0) == ["critical"]  # exactly 20 s after it began
    assert p.critical_reason == "the battery is at 7%"


def test_never_critical_while_charging():
    p = PowerPolicy(Thresholds(critical_sustain_s=20))
    assert "critical" not in run(p, snap(3, cell=3.1, charging=True), 0, 120)
    assert "critical" not in run(p, snap(3, cell=3.1, charging=False, plugged=True), 121, 60)  # charged / standby
    # unplugged: the 20 s start from now (the time plugged in doesn't count)
    assert "critical" not in run(p, snap(3, cell=3.1), 200, 15)
    # plugged in for a moment mid-way resets the timer
    run(p, snap(3, cell=3.1, charging=True), 216, 1)
    assert "critical" not in run(p, snap(3, cell=3.1), 218, 19)
    assert "critical" in run(p, snap(3, cell=3.1), 238, 2)
    run(p, snap(3, cell=3.1, charging=True), 300, 1)
    assert not p.critical  # charging clears it


def test_unknown_values_never_trigger():
    p = PowerPolicy(Thresholds(critical_sustain_s=0, low_sustain_s=0, hot_sustain_s=0))
    assert run(p, snap(None, cell=None, t=None), 0, 60) == []
    assert p.flags() == []


def test_hot_too_hot_and_cooling_down():
    p = PowerPolicy(Thresholds(hot_sustain_s=10))
    assert run(p, snap(t=49.9), 0, 30) == []
    assert run(p, snap(t=52), 31, 5) == []  # not for long enough
    assert run(p, snap(t=52), 37, 10) == ["hot"]
    assert run(p, snap(t=52), 48, 30) == []
    assert run(p, snap(t=61), 80, 9) == []
    assert run(p, snap(t=61), 90, 2) == ["too_hot"]
    assert p.flags() == ["hot", "too_hot"]
    run(p, snap(t=40), 100, 1)
    assert p.flags() == []
    assert run(p, snap(t=51), 110, 10) == ["hot"]  # cooled down below 45 °C first: again


def test_charging_in_the_cold():
    p = PowerPolicy(Thresholds(hot_sustain_s=10))
    assert run(p, snap(t=-3), 0, 30) == []  # cold but not charging: fine
    assert run(p, snap(t=-3, charging=True), 31, 12) == ["cold"]
    assert run(p, snap(t=-3, charging=True), 45, 30) == []
    run(p, snap(t=-3), 80, 1)  # unplugged: cleared
    assert not p.cold
    assert run(p, snap(t=1, charging=True), 90, 30) == []  # above 0 °C: fine
