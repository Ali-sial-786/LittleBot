"""scripts/calibrate_touch.py: raw touch readings -> libinput calibration matrix."""

from __future__ import annotations

import importlib.util
import io
import queue
import threading
import time
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "calibrate_touch", Path(__file__).resolve().parents[2] / "scripts" / "calibrate_touch.py")
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

DEVICES = """I: Bus=0019 Vendor=0001 Product=0001 Version=0100
N: Name="gpio_ir_recv"
H: Handlers=kbd event0

I: Bus=0000 Vendor=0000 Product=0000 Version=0000
N: Name="ADS7846 Touchscreen"
P: Phys=spi0.1/input0
H: Handlers=mouse0 event3
B: EV=b
"""
RANGE = (200, 3900)


def raw_for(screen, to_raw):
    """The raw reading a perfect touch at ``screen`` (0..1) gives on a screen wired as ``to_raw``."""
    nx, ny = to_raw(*screen)
    span = RANGE[1] - RANGE[0]
    return RANGE[0] + nx * span, RANGE[0] + ny * span


WIRINGS = {
    "straight": lambda x, y: (x, y),
    "swapped": lambda x, y: (y, x),
    "mirrored": lambda x, y: (1 - x, y),
    "upside down": lambda x, y: (1 - x, 1 - y),
    "rotated": lambda x, y: (y, 1 - x),
    "squashed": lambda x, y: (0.1 + 0.8 * x, 0.05 + 0.85 * y),
}


def test_finds_the_touch_screen():
    assert ct.find_touch_device(DEVICES) == ("ADS7846 Touchscreen", "event3")
    assert ct.find_touch_device(DEVICES.split("\n\n")[0]) is None


def _event(etype, code, value):
    return ct.EVENT.pack(0, 0, etype, code, value)


def test_reads_each_press_as_its_readings():
    data = b"".join([
        _event(ct.EV_ABS, ct.ABS_X, 100), _event(ct.EV_ABS, ct.ABS_Y, 200), _event(ct.EV_KEY, ct.BTN_TOUCH, 1),
        _event(ct.EV_SYN, ct.SYN_REPORT, 0),
        _event(ct.EV_ABS, ct.ABS_X, 110), _event(ct.EV_SYN, ct.SYN_REPORT, 0),
        _event(ct.EV_KEY, ct.BTN_TOUCH, 0), _event(ct.EV_SYN, ct.SYN_REPORT, 0),
        _event(ct.EV_SYN, ct.SYN_REPORT, 0),  # no finger: not part of a press
        _event(ct.EV_KEY, ct.BTN_TOUCH, 1), _event(ct.EV_ABS, ct.ABS_Y, 300), _event(ct.EV_SYN, ct.SYN_REPORT, 0),
        _event(ct.EV_KEY, ct.BTN_TOUCH, 0),
    ])
    assert list(ct.presses(io.BytesIO(data))) == [[(100, 200), (110, 200)], [(110, 300)]]


def test_a_press_is_its_steady_middle_and_a_bounce_is_no_press():
    wobbly = [(0, 0)] + [(1000, 2000)] * 8 + [(4000, 4000)]
    assert ct.press_point(wobbly) == (1000.0, 2000.0)
    assert ct.press_point([(5, 5)] * (ct.MIN_SAMPLES - 1)) is None


@pytest.mark.parametrize("wiring", WIRINGS)
def test_any_wiring_is_calibrated_exactly(wiring):
    to_raw = WIRINGS[wiring]
    points = [raw_for(t, to_raw) for t in ct.TARGETS]
    matrix, error = ct.calibrate(points, RANGE, RANGE)
    assert error < 1e-9
    for target in [(0.0, 0.0), (1.0, 1.0), (0.3, 0.7)]:  # every other spot lands right too
        nx, ny = ct.normalise(raw_for(target, to_raw), RANGE, RANGE)
        assert ct.apply(matrix, (nx, ny)) == pytest.approx(target, abs=1e-9)


def test_a_slipped_touch_shows_in_the_check():
    points = [raw_for(t, WIRINGS["straight"]) for t in ct.TARGETS]
    points[4] = (points[4][0] + 400, points[4][1])  # the middle touch slipped ~11% to the side
    _matrix, error = ct.calibrate(points, RANGE, RANGE)
    assert error > ct.MAX_CHECK_ERROR


def test_touches_in_one_place_are_refused():
    with pytest.raises(ValueError):
        ct.calibrate([(1000, 1000)] * 5, RANGE, RANGE)


def test_the_rule_matches_the_screen_by_name():
    text = ct.rule_text("ADS7846 Touchscreen", [0, -1, 1, 1, 0, 0])
    assert ('ATTRS{name}=="ADS7846 Touchscreen", '
            'ENV{LIBINPUT_CALIBRATION_MATRIX}="0.00000 -1.00000 1.00000 1.00000 0.00000 0.00000"') in text


def test_the_crosses_collect_one_press_each(monkeypatch):
    pytest.importorskip("pygame")
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
    reader: queue.Queue = queue.Queue()
    reader.put((0.0, [(1, 1)] * 6))  # touched before the crosses appeared: ignored
    steady = [(500, 600)] * 6

    def touch_each_cross():
        time.sleep(0.3)
        for i in range(len(ct.TARGETS)):
            reader.put((10.0 + i, steady))
            reader.put((10.1 + i, steady))  # the same touch bouncing: ignored
            time.sleep(0.1)

    threading.Thread(target=touch_each_cross, daemon=True).start()
    points = ct.collect(reader)
    assert points == [(500.0, 600.0)] * len(ct.TARGETS)
