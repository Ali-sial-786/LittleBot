"""The 2S Li-ion percent curve, smoothing (never jumping around while charging) and the time-left estimate."""

from __future__ import annotations

import pytest

from littlebot.power.battery import CELL_CURVE, BatteryGauge, TimeLeft, percent_for_cell, percent_for_pack
from littlebot.power.monitor import cell_for_percent
from littlebot.power.protocol import Reading


def reading(cell: float | None, *, cell2: float | None = None, charging: bool | None = False,
            charged: bool | None = False, t: float | None = 25.0) -> Reading:
    c2 = cell if cell2 is None else cell2
    v = None if cell is None else round(cell + c2, 3)
    return Reading(voltage=v, cell1=cell, temperature_c=t, charging=charging, charged=charged, uptime_s=0)


def test_curve_is_a_monotonic_table_from_empty_to_full():
    volts = [v for v, _ in CELL_CURVE]
    pcts = [p for _, p in CELL_CURVE]
    assert volts == sorted(volts) and pcts == sorted(pcts) and pcts[0] == 0 and pcts[-1] == 100
    assert percent_for_cell(4.25) == 100 and percent_for_cell(3.0) == 0
    assert percent_for_cell(3.84) == pytest.approx(50)
    assert percent_for_cell(3.745) == pytest.approx(23.75)  # linear between 3.73 (20) and 3.75 (25)
    assert percent_for_pack(8.4) == 100 and percent_for_pack(7.68) == pytest.approx(50)
    for p in (0, 7, 20, 33, 50, 81, 100):
        assert percent_for_cell(cell_for_percent(p)) == pytest.approx(p, abs=0.01)


def test_the_weakest_cell_decides():
    g = BatteryGauge()
    g.update(reading(3.95, cell2=3.73), 0.0)  # 70 % and 20 %
    assert g.percent == pytest.approx(20, abs=0.5) and g.min_cell == pytest.approx(3.73)
    g2 = BatteryGauge()  # no midpoint tap: half the pack
    g2.update(Reading(voltage=7.68, cell1=None, temperature_c=None, charging=None, charged=None, uptime_s=None), 0.0)
    assert g2.percent == pytest.approx(50) and g2.cell_volts == ()


def test_discharging_only_goes_down_and_smoothly():
    g = BatteryGauge(settle_s=0)
    t = 0.0
    g.update(reading(cell_for_percent(60)), t)
    seen = [g.percent]
    # load spikes (the voltage dips and recovers) and noise: the percentage never goes up
    for i in range(600):
        t += 1.0
        dip = 0.08 if i % 37 == 0 else 0.0
        g.update(reading(cell_for_percent(60 - i * 0.01) - dip + (0.004 if i % 2 else -0.004)), t)
        seen.append(g.percent)
    assert all(b <= a + 1e-9 for a, b in zip(seen, seen[1:]))
    assert seen[-1] == pytest.approx(54, abs=1.5)
    # a sudden big drop is followed at most max_fall_per_min
    before = g.percent
    g.update(reading(cell_for_percent(20)), t + 60)
    assert before - g.percent <= g.max_fall_per_min + 1e-6


def test_charging_never_jumps_around():
    g = BatteryGauge(settle_s=0)
    t = 0.0
    g.update(reading(cell_for_percent(30)), t)
    start = g.percent
    seen = []
    # plugged in: the voltage jumps up by the charge current, wobbles as the charger regulates
    for i in range(1200):
        t += 1.0
        rest = cell_for_percent(min(100, 30 + i * 0.04))
        g.update(reading(rest + 0.1 + (0.03 if i % 5 == 0 else 0.0) - (0.05 if i % 50 == 0 else 0.0), charging=True), t)
        seen.append(g.percent)
    assert all(b >= a - 1e-9 for a, b in zip(seen, seen[1:]))  # never down while charging
    per_min = [(seen[i + 60] - seen[i]) for i in range(0, len(seen) - 60, 60)]
    assert max(per_min) <= g.max_rise_per_min + 1e-6  # never faster than max_rise_per_min
    assert start < seen[-1] < 90
    # the charger says full: heads for 100 slowly, and unplugging doesn't drop it at once
    for _ in range(60 * 30):
        t += 1.0
        g.update(reading(4.2, charging=False, charged=True), t)
    assert g.percent == pytest.approx(100)
    t += 1.0
    g.update(reading(4.12), t)  # unplugged: the voltage relaxes under load
    assert g.percent > 99


def test_settling_follows_the_first_minutes_both_ways():
    g = BatteryGauge(settle_s=120)
    g.update(reading(cell_for_percent(30)), 0.0)  # the first reading came during a load spike
    for t in range(1, 60):
        g.update(reading(cell_for_percent(60)), float(t))
    assert g.percent > 50  # corrected upwards while settling
    for t in range(200, 400):
        g.update(reading(cell_for_percent(80)), float(t))
    assert g.percent < 62  # after settling it never goes up while discharging


def test_unknown_voltage_keeps_the_last_percent_and_a_gap_restarts_smoothing():
    g = BatteryGauge(settle_s=0)
    g.update(reading(cell_for_percent(50)), 0.0)
    assert g.update(reading(None), 1.0) == pytest.approx(50, abs=0.5)
    g.update(reading(cell_for_percent(45)), 500.0)  # the monitor was away for 8 min
    assert g.voltage == pytest.approx(2 * cell_for_percent(45))
    g.reset()
    assert g.percent is None and g.voltage is None


def test_time_left_needs_a_steady_trend():
    tl = TimeLeft()
    for i in range(0, 200, 10):
        tl.add(float(i), 50 - i / 60 * 0.5, charging=False)
    assert tl.minutes(48.0) is None  # not enough history yet
    tl.clear()
    for i in range(0, 660, 10):  # 11 min at 0.5 %/min
        tl.add(float(i), 50 - i / 60 * 0.5, charging=False)
    assert tl.slope_per_min() == pytest.approx(-0.5, abs=0.01)
    assert tl.minutes(44.5) == 89
    tl2 = TimeLeft()
    for i in range(0, 660, 10):  # flat: no estimate (not draining measurably)
        tl2.add(float(i), 50.0, charging=False)
    assert tl2.minutes(50.0) is None
    tl3 = TimeLeft()
    for i in range(0, 660, 10):  # scattered: no estimate
        tl3.add(float(i), 50 + (8 if (i // 10) % 3 == 0 else -8) - i / 600, charging=False)
    assert tl3.minutes(50.0) is None
    # charging: the minutes to full; switching to discharging starts over
    tl4 = TimeLeft()
    for i in range(0, 660, 10):
        tl4.add(float(i), 50 + i / 60, charging=True)
    assert tl4.minutes(60.0) == 40
    tl4.add(700.0, 61.0, charging=False)
    assert tl4.minutes(61.0) is None
