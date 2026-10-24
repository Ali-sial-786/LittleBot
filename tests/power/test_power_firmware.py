"""The battery monitor's MicroPython firmware: its maths and line format, imported in CPython (no ``machine``)."""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

from littlebot.power.protocol import parse_line

FIRMWARE = Path(__file__).resolve().parents[2] / "firmware" / "battery_monitor" / "main.py"


@pytest.fixture(scope="module")
def fw():
    spec = importlib.util.spec_from_file_location("battery_monitor_main", FIRMWARE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # must not need the machine module, nor start run()
    return mod


def test_the_line_is_exactly_the_agreed_format(fw):
    line = fw.line(7.84, 3.92, 24.6, False, False, 123)
    assert line == '{"dev":"littlebot-battery","fw":1,"v":7.84,"cell1":3.92,"t":24.6,"chg":false,"full":false,"up":123}'
    assert json.loads(line)["dev"] == "littlebot-battery"
    r = parse_line(line)
    assert r is not None and r.voltage == 7.84 and r.cell1 == 3.92 and r.cell2 == pytest.approx(3.92)


def test_nothing_measured_is_null(fw):
    line = fw.line(None, None, None, None, None, 0)
    assert line == '{"dev":"littlebot-battery","fw":1,"v":null,"cell1":null,"t":null,"chg":null,"full":null,"up":0}'
    r = parse_line(line)
    assert r is not None and (r.voltage, r.cell1, r.temperature_c, r.charging, r.charged) == (None,) * 5


def test_divider_ratios_match_the_pin_plan(fw):
    assert fw.pack_volts(2.0842) == pytest.approx(8.4, abs=0.01)  # 100 k / 33 k
    assert fw.cell_volts(1.3429) == pytest.approx(4.2, abs=0.01)  # 100 k / 47 k
    assert fw.adc_volts(65535) == pytest.approx(3.3)
    assert fw.adc_volts(0) == 0


def test_ntc_maths(fw):
    assert fw.ntc_celsius(65535 // 2) == pytest.approx(25.0, abs=0.1)  # 10 k NTC = 10 k pull-up at 25 °C
    # B = 3950: R(50 °C) ~ 3.6 k, R(0 °C) ~ 33.6 k (pull-up 10 k to 3.3 V, NTC to GND)
    r50 = 10000 * math.exp(3950 * (1 / 323.15 - 1 / 298.15))
    assert fw.ntc_celsius(65535 * r50 / (r50 + 10000)) == pytest.approx(50.0, abs=0.2)
    r0 = 10000 * math.exp(3950 * (1 / 273.15 - 1 / 298.15))
    assert fw.ntc_celsius(65535 * r0 / (r0 + 10000)) == pytest.approx(0.0, abs=0.2)
    assert fw.ntc_celsius(65535) is None  # open: no NTC (pulled up to the rail)
    assert fw.ntc_celsius(0) is None  # shorted / no pull-up
    assert fw.ntc_celsius(None) is None


def _ticks(volts_at_pin, n=20, jitter=0.0):
    return [volts_at_pin + (jitter if i % 2 else -jitter) for i in range(n)]


def test_judge_good_readings(fw):
    pack = _ticks(7.84 / fw.PACK_RATIO, jitter=0.002)
    cell = _ticks(3.92 / fw.CELL_RATIO, jitter=0.002)
    ntc = _ticks(65535 / 2)
    v, c1, t = fw.judge(pack, cell, ntc)
    assert v == pytest.approx(7.84, abs=0.01) and c1 == pytest.approx(3.92, abs=0.01) and t == pytest.approx(25, abs=0.1)


def test_judge_unconnected_pins_are_null(fw):
    # a floating pin that the probe caught, even though it reads a plausible steady voltage
    pack = _ticks(7.5 / fw.PACK_RATIO)
    v, c1, t = fw.judge(pack, _ticks(3.7 / fw.CELL_RATIO), _ticks(30000), floats=(True, True, True))
    assert (v, c1, t) == (None, None, None)
    # out of range: nothing on the pack divider reads ~0 V, a pin pulled high reads far too much
    assert fw.judge(_ticks(0.01), _ticks(0.0), _ticks(65535))[:3] == (None, None, None)
    assert fw.judge(_ticks(3.3), _ticks(3.3), _ticks(0))[:2] == (None, None)
    # wandering (a floating pin drifting) is null too
    v, _, _ = fw.judge([1.0 + 0.05 * i for i in range(20)], [], [])
    assert v is None


def test_cell1_must_fit_the_pack(fw):
    pack = _ticks(7.8 / fw.PACK_RATIO)
    cell = _ticks(4.4 / fw.CELL_RATIO)  # cell 2 would be 3.4 V: plausible, kept
    assert fw.judge(pack, cell, [])[1] == pytest.approx(4.4, abs=0.01)
    pack = _ticks(6.0 / fw.PACK_RATIO)
    cell = _ticks(4.2 / fw.CELL_RATIO)  # cell 2 would be 1.8 V: nonsense
    v, c1, _ = fw.judge(pack, cell, [])
    assert v == pytest.approx(6.0, abs=0.01) and c1 is None


def test_floating_probe(fw):
    assert fw.floating(0.0, 3.3)  # follows the pulls rail to rail: nothing connected
    assert not fw.floating(1.4, 2.5)  # a divider (Thevenin ~25 k) only moves a little
    assert not fw.floating(0.9, 0.95)


def test_charger_status_pins(fw):
    # driven low with either pull: asserted; driven high: not asserted; following the pulls: unknown until seen
    assert fw.status_pin(True, True, False) == (True, True)
    assert fw.status_pin(False, False, False) == (False, True)
    assert fw.status_pin(True, False, False) == (None, False)  # floating (low with the pull-down, high with the
    # pull-up), never seen driven: null
    assert fw.status_pin(True, False, True) == (False, True)  # floating after being seen: open-drain off
