"""Finding the battery monitor on a serial port, reading it in raw mode, and reconnecting when it is unplugged."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import pytest

from littlebot.power.monitor import SerialMonitor, SimulatedMonitor, TtyPort, candidate_ports, cell_for_percent
from littlebot.power.battery import percent_for_cell
from littlebot.power.protocol import Reading

LINE = b'{"dev":"littlebot-battery","fw":1,"v":7.84,"cell1":3.92,"t":24.6,"chg":false,"full":false,"up":%d}\n'


def wait_until(cond: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


# ---------------------------------------------------------------- discovery
def _fake_dev(tmp_path):
    """/dev/serial/by-id links and /dev/ttyACM* nodes with a sysfs tree giving each one a USB vendor id."""
    dev = tmp_path / "dev"
    byid = dev / "serial" / "by-id"
    byid.mkdir(parents=True)
    sysfs = tmp_path / "sys" / "class" / "tty"
    for name, vendor in (("ttyACM0", "2e8a"), ("ttyACM1", "2341"), ("ttyACM2", None)):
        (dev / name).write_text("")
        usb = tmp_path / "sys" / "devices" / name / "usb1"
        iface = usb / "1-1:1.0"
        iface.mkdir(parents=True)
        if vendor:
            (usb / "idVendor").write_text(vendor + "\n")
        (sysfs / name).mkdir(parents=True)
        os.symlink(iface, sysfs / name / "device")
    os.symlink(dev / "ttyACM0", byid / "usb-MicroPython_Board_in_FS_mode_e6614c311b7e6a35-if00")
    os.symlink(dev / "ttyACM1", byid / "usb-Arduino__www.arduino.cc__0043_7563-if00")
    return dev, str(sysfs)


def test_candidate_ports_pick_rp2040_boards_once(tmp_path):
    dev, sysfs = _fake_dev(tmp_path)
    globs = [str(dev / "serial" / "by-id" / "*"), str(dev / "ttyACM*")]
    ports = candidate_ports(globs, sysfs=sysfs)
    # the MicroPython board by its by-id name (once, though ttyACM0 is the same device); not the Arduino
    assert ports == [str(dev / "serial" / "by-id" / "usb-MicroPython_Board_in_FS_mode_e6614c311b7e6a35-if00")]
    # with only the ttyACM glob, the RP2040 is still found by its USB vendor id
    assert candidate_ports([str(dev / "ttyACM*")], sysfs=sysfs) == [str(dev / "ttyACM0")]
    # probe_any_port: every serial device, each once
    assert len(candidate_ports(globs, match_any=True, sysfs=sysfs)) == 3
    assert candidate_ports([str(tmp_path / "nothing" / "*")], sysfs=sysfs) == []


# ---------------------------------------------------------------- a fake port
class FakePort:
    """Scripted reads: bytes chunks, or an exception (an unplugged USB device raises OSError)."""

    def __init__(self, script: list[Any], *, then: Any = b"") -> None:
        self.script = list(script)
        self.then = then
        self.closed = False
        self.reads = 0

    def read(self, timeout: float) -> bytes:
        self.reads += 1
        if self.closed:
            raise OSError(9, "closed")
        item = self.script.pop(0) if self.script else self.then
        if isinstance(item, BaseException):
            raise item
        if item == b"":
            time.sleep(min(timeout, 0.01))
        return item

    def close(self) -> None:
        self.closed = True


class Ports:
    """The opener: path -> a new FakePort each time it is opened (from a per-path factory)."""

    def __init__(self, factories: dict[str, Any]) -> None:
        self.factories = factories
        self.opened: list[str] = []
        self.ports: list[FakePort] = []

    def __call__(self, path: str) -> FakePort:
        self.opened.append(path)
        make = self.factories[path]
        if isinstance(make, BaseException):
            raise make
        port = make()
        self.ports.append(port)
        return port


def test_find_accepts_only_a_port_that_says_littlebot_battery():
    ports = Ports({
        "/dev/ttyACM0": lambda: FakePort([b"hello from an arduino\n"] * 3),
        "/dev/ttyACM1": lambda: FakePort([b"39,\"cell1\":3.9}\n" + LINE % 1]),  # a partial line first
    })
    mon = SerialMonitor(opener=ports, lister=lambda: ["/dev/ttyACM0", "/dev/ttyACM1"], probe_s=0.2)
    found = mon.find()
    assert found is not None
    port, path, first = found
    assert path == "/dev/ttyACM1" and first.voltage == 7.84 and first.uptime_s == 1
    assert ports.ports[0].closed and not port.closed
    # the port that isn't the monitor is not opened again for a while
    found[0].close()
    ports.opened.clear()
    mon.find()
    assert ports.opened == ["/dev/ttyACM1"]


def test_find_with_nothing_plugged_in_is_quiet():
    mon = SerialMonitor(opener=Ports({}), lister=lambda: [])
    assert mon.find() is None
    assert mon.status()["connected"] is False and mon.status()["detail"] == "not found"
    perm = SerialMonitor(opener=Ports({"/dev/ttyACM0": PermissionError(13, "Permission denied")}),
                         lister=lambda: ["/dev/ttyACM0"])
    assert perm.find() is None  # can't open it: skipped, no crash


def test_reads_garbage_tolerantly_and_reconnects_after_unplugging():
    readings: list[Reading] = []
    statuses: list[tuple[bool, Any]] = []
    plugs = {"n": 0}

    def plug() -> FakePort:
        plugs["n"] += 1
        if plugs["n"] == 1:  # the first time: readings, garbage, then unplugged
            return FakePort([LINE % 1, b"\x00\xffgarbage\n", LINE % 2, b'{"dev":"littlebot-batt', OSError(5, "EIO")])
        return FakePort([LINE % 10], then=LINE % 11)  # plugged back in: keeps sending

    ports = Ports({"/dev/ttyACM0": plug})
    mon = SerialMonitor(opener=ports, lister=lambda: ["/dev/ttyACM0"], probe_s=0.5, scan_s=0.05)
    mon.start(readings.append, lambda c, p: statuses.append((c, p)))
    try:
        wait_until(lambda: any(r.uptime_s == 11 for r in readings), timeout=5)
    finally:
        mon.stop()
    ups = [r.uptime_s for r in readings]
    assert ups[:2] == [1, 2] and 10 in ups and 11 in ups
    assert statuses[:3] == [(True, "/dev/ttyACM0"), (False, None), (True, "/dev/ttyACM0")]
    st = mon.status()
    assert st["bad_lines"] >= 1 and st["connects"] >= 2
    assert all(p.closed for p in ports.ports)
    assert not mon.running


def test_a_monitor_that_goes_quiet_is_reconnected():
    statuses: list[bool] = []
    mon = SerialMonitor(opener=Ports({"/dev/x": lambda: FakePort([LINE % 1], then=b"")}), lister=lambda: ["/dev/x"],
                        stale_s=0.3, scan_s=0.05)
    mon.start(lambda r: None, lambda c, p: statuses.append(c))
    try:
        wait_until(lambda: statuses.count(True) >= 2, timeout=6)
    finally:
        mon.stop()
    assert statuses[:2] == [True, False]


def test_a_fixed_port_is_retried_even_after_failing():
    calls = {"n": 0}

    def flaky() -> FakePort:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(2, "No such file")
        return FakePort([LINE % 3])

    ports = Ports({"/dev/battery": flaky})
    mon = SerialMonitor(port="/dev/battery", opener=ports, lister=lambda: [], probe_s=0.3)
    assert mon.find() is None
    found = mon.find()
    assert found is not None and found[1] == "/dev/battery"


# ---------------------------------------------------------------- a real tty (pty) in raw mode
def test_tty_port_reads_a_real_pseudo_terminal_in_raw_mode():
    termios = pytest.importorskip("termios")
    master, slave = os.openpty()
    path = os.ttyname(slave)
    try:
        port = TtyPort(path)
        try:
            attrs = termios.tcgetattr(port.fd)
            assert not attrs[3] & termios.ICANON and not attrs[3] & termios.ECHO  # raw: no line editing, no echo
            os.write(master, b"\r\nMicroPython banner\r\n" + LINE.replace(b"\n", b"\r\n") % 7)
            got = b""
            deadline = time.monotonic() + 2
            while b"littlebot-battery" not in got and time.monotonic() < deadline:
                got += port.read(0.1)
            assert b'"up":7}\r\n' in got  # raw: CR LF arrive untouched (no ICRNL / echo)
        finally:
            port.close()
        # through the monitor's own find(): it opens the pty itself and the line arrives
        mon = SerialMonitor(port=path, probe_s=2.0)
        writer = threading.Timer(0.2, lambda: os.write(master, LINE % 8))
        writer.start()
        found = mon.find()
        writer.join()
        assert found is not None and found[2].uptime_s == 8
        found[0].close()
    finally:
        os.close(master)
        os.close(slave)


# ---------------------------------------------------------------- the simulated pack
def test_simulated_pack_drains_charges_and_reports_like_the_monitor():
    now = [0.0]
    sim = SimulatedMonitor(percent=50, drain_per_hour=60, charge_per_hour=120, interval_s=0, clock=lambda: now[0])
    r = sim.reading()
    assert r.cells and percent_for_cell(min(r.cells)) == pytest.approx(50, abs=2)
    now[0] = 600  # 10 min at 60 %/h
    assert sim.reading() and sim.percent == pytest.approx(40)
    sim.set(charging=True)
    now[0] = 900
    r = sim.reading()
    assert r.charging is True and r.charged is False and sim.percent == pytest.approx(50)
    sim.set(percent=100)
    r = sim.reading()
    assert r.charged is True and r.charging is False
    sim.set(temperature_c=None)
    assert sim.reading().temperature_c is None
    got: list[Reading] = []
    sim.start(got.append)
    sim.emit()
    sim.stop()
    assert len(got) == 1 and sim.status()["kind"] == "simulated"
    assert cell_for_percent(0) == pytest.approx(3.30) and cell_for_percent(100) == pytest.approx(4.18)
