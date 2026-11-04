"""littlebot/android/inputs.py: which input devices count as someone using the robot, and the watch itself."""

from __future__ import annotations

import os
import select
import threading
import time

import pytest

from littlebot.android.inputs import InputWatch, _bits, used_devices

# The robot's own /proc/bus/input/devices (2026-11-04): HDMI remote controls (CEC), HDMI jacks, the USB speaker's
# buttons, the power button and the screen's touch panel. Only the touch panel is someone using the robot.
ROBOT = """\
I: Bus=001e Vendor=0000 Product=0000 Version=0001
N: Name="vc4-hdmi-0"
H: Handlers=kbd event0
B: PROP=20
B: EV=100017
B: KEY=ffffc000000000 3ff 0 400000320fc200 40830c900000000 0 210300 49d2c040ec00 1e378000000000 8010000010000000
B: REL=3
B: MSC=10

I: Bus=0000 Vendor=0000 Product=0000 Version=0000
N: Name="vc4-hdmi-0 HDMI Jack"
H: Handlers=event1
B: PROP=0
B: EV=21
B: SW=40

I: Bus=0003 Vendor=4c4a Product=4155 Version=0100
N: Name="Jieli Technology UACDemoV1.0"
H: Handlers=kbd event4
B: PROP=0
B: EV=1b
B: KEY=1000000018000 7800000000 8e000000000000 0
B: ABS=100000000
B: MSC=10

I: Bus=0019 Vendor=0001 Product=0001 Version=0100
N: Name="pwr_button"
H: Handlers=kbd event5
B: PROP=0
B: EV=3
B: KEY=10000000000000 0

I: Bus=001c Vendor=0000 Product=1ea6 Version=0000
N: Name="ADS7846 Touchscreen"
H: Handlers=mouse0 event6
B: PROP=0
B: EV=b
B: KEY=400 0 0 0 0 0
B: ABS=1000003
"""

MOUSE_AND_KEYBOARD = """\
N: Name="USB Optical Mouse"
H: Handlers=mouse1 event7
B: EV=17
B: KEY=1f0000 0 0 0 0
B: REL=903

N: Name="USB Keyboard"
H: Handlers=sysrq kbd leds event8
B: EV=120013
B: KEY=1000000000007 ff9f207ac14057ff febeffdfffefffff fffffffffffffffe
"""


def test_only_the_touch_panel_on_the_robot() -> None:
    assert used_devices(ROBOT) == ["event6"]


def test_a_mouse_and_a_keyboard_count_too() -> None:
    assert used_devices(ROBOT + "\n" + MOUSE_AND_KEYBOARD) == ["event6", "event7", "event8"]


def test_bits() -> None:
    assert _bits("400 0 0 0 0 0", 64) == {330}
    assert _bits("b", 64) == {0, 1, 3}
    assert _bits("1 0", 32) == {32}
    assert _bits("", 64) == set()


@pytest.mark.skipif(not hasattr(select, "poll") or not hasattr(os, "mkfifo"), reason="Linux only (poll, FIFOs)")
def test_the_watch_tells_when_something_is_used(tmp_path) -> None:  # noqa: ANN001
    fifo = tmp_path / "event6"
    os.mkfifo(fifo)
    writer = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)  # keeps the FIFO open for the watch
    told = threading.Event()
    watch = InputWatch(told.set, every=0.0, devices=lambda: ["event6"], dev_dir=str(tmp_path))
    watch.start()
    try:
        deadline = time.monotonic() + 2.0
        while watch.watching != ["event6"] and time.monotonic() < deadline:
            time.sleep(0.01)
        os.write(writer, b"\0" * 24)  # one input_event
        assert told.wait(2.0)
        assert watch.last_input is not None
    finally:
        watch.stop()
        os.close(writer)
    assert not watch.running


def test_no_devices_does_nothing() -> None:
    watch = InputWatch(lambda: None, devices=lambda: [])
    watch.start()
    watch.stop()
    assert watch.last_input is None
