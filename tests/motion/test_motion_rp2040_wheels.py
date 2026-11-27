"""v5's wheels: RP2040Wheels talks to the base's firmware over a serial line (here a pty with a fake RP2040)."""

from __future__ import annotations

import os
import select
import threading
import time

import pytest

from littlebot.core.config import DriveConfig
from littlebot.motion.drive import DriveError, MockDrive, RP2040Wheels, create_drive


class FakeBase:
    """The RP2040's side of a pty: answers PING, records every other line."""

    def __init__(self, answer: bool = True) -> None:
        self.master, self.slave = os.openpty()
        os.set_blocking(self.slave, False)
        self.lines: list[str] = []
        self.answer = answer
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        buf = b""
        while not self._stop:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if not r:
                continue
            try:
                buf += os.read(self.master, 1024)
            except OSError:
                return
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode().strip()
                self.lines.append(text)
                if text == "PING" and self.answer:
                    os.write(self.master, b"PONG littlebot-base 1\n")
                elif text == "STOP":
                    os.write(self.master, b"OK\n")

    def wait_for(self, prefix: str, timeout: float = 2.0) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            hit = [ln for ln in self.lines if ln.startswith(prefix)]
            if hit:
                return hit[-1]
            time.sleep(0.01)
        raise AssertionError(f"no {prefix!r} line: {self.lines}")

    def close(self) -> None:
        self._stop = True
        self._t.join(timeout=1)
        os.close(self.master)


@pytest.fixture
def base():
    b = FakeBase()
    yield b
    b.close()


def wheels(base: FakeBase, **kw) -> RP2040Wheels:
    cfg = DriveConfig(enabled=True, backend="rp2040", wheel_base_m=0.185, **kw)
    return RP2040Wheels(cfg, opener=lambda _path: base.slave)


def test_opens_when_the_base_answers_and_stops_it_first(base):
    w = wheels(base)
    assert base.wait_for("PING") == "PING" and base.wait_for("STOP") == "STOP"
    assert w.physical is True and w.describe()["driver"] == "RP2040 base"


def test_a_silent_port_is_refused():
    b = FakeBase(answer=False)
    try:
        with pytest.raises(DriveError, match="no answer"):
            RP2040Wheels(DriveConfig(enabled=True, backend="rp2040"), opener=lambda _p: b.slave, probe_s=0.3)
    finally:
        b.close()


def test_velocity_becomes_wheel_percents(base):
    w = wheels(base)
    w.set_velocity(0.175, 0.0)                     # half the firmware's 350 mm/s on both wheels
    assert base.wait_for("D ") == "D 50 50"
    w.set_velocity(0.0, 90.0)                      # turning left on the spot: left wheel back, right forwards
    left, right = w.percents(0.0, 90.0)
    assert left < 0 < right and abs(left + right) < 1e-9
    assert base.wait_for("D -") == f"D {left:g} {right:g}"
    assert w.percents(1.0, 0.0) == (100.0, 100.0)  # clamped to the firmware's range
    w.stop()
    assert w.describe()["left_pct"] == 0.0


def test_close_stops_and_releases(base):
    w = wheels(base)
    base.lines.clear()
    w.close()
    assert base.wait_for("STOP") == "STOP"
    w.set_velocity(0.1, 0.0)  # closed: nothing written, nothing raised
    w.stop()


def test_create_drive_needs_physical_enabled_and_falls_back_without_a_base():
    cfg = DriveConfig(enabled=True, backend="rp2040", port="/nonexistent/tty")
    d = create_drive(cfg, physical_enabled=False)
    assert isinstance(d, MockDrive) and "physical_enabled" in d.status().get("note", "")
    d = create_drive(cfg, physical_enabled=True)
    assert isinstance(d, MockDrive) and "unavailable" in d.status().get("note", "")
