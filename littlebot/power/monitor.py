"""Where readings come from: the RP2040 battery monitor on a USB serial port, or a simulated one.

``SerialMonitor`` (standard library only: termios raw mode, no pyserial) runs one daemon thread:

1. **Find** the monitor: the configured ``port``, else the ``globs`` (``/dev/serial/by-id/*``,
   ``/dev/ttyACM*``) narrowed to boards that look like an RP2040 running MicroPython (the by-id name, or
   USB vendor 2e8a in sysfs) so other serial devices are left alone. Each candidate is opened read-only
   and accepted once a line with ``"dev":"littlebot-battery"`` arrives within ``probe_s``; a port that
   isn't the monitor isn't tried again for ``reject_s``. Nothing found: quietly try again in ``scan_s``.
2. **Read** whole lines, parse them (``protocol.parse_line``) and hand each ``Reading`` to the callback.
3. **Reconnect**: unplugged (read error), or no valid line for ``stale_s``: close, report disconnected,
   go back to 1 (a second later, then every ``scan_s``).

Nothing is ever written to the port. ``SimulatedMonitor`` makes readings from a pretend pack (desktop
preview, ``scripts/check_battery.py --simulate``, tests): it drains slowly and can be set from the
dashboard's simulator.
"""

from __future__ import annotations

import errno
import glob
import logging
import os
import re
import select
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from littlebot.power.battery import CELL_CURVE
from littlebot.power.protocol import MARKER, LineSplitter, Reading, parse_line

log = logging.getLogger("littlebot.power.monitor")

DEFAULT_GLOBS = ("/dev/serial/by-id/*", "/dev/ttyACM*")
RP2040_VENDOR = "2e8a"  # Raspberry Pi (RP2040 boards running MicroPython)
BY_ID_HINT = re.compile(r"micropython|rp2040|raspberry|pico|waveshare|2e8a", re.I)
SYSFS_TTY = "/sys/class/tty"

ReadingCallback = Callable[[Reading], None]
StatusCallback = Callable[[bool, "str | None"], None]


class Port(Protocol):
    def read(self, timeout: float) -> bytes: ...  # b"" on timeout; raises OSError when the device is gone
    def close(self) -> None: ...


class TtyPort:
    """A serial tty opened read-only in raw mode (no echo, no line editing, no modem control)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            if os.isatty(self.fd):
                _make_raw(self.fd)
        except Exception:
            os.close(self.fd)
            raise

    def read(self, timeout: float) -> bytes:
        ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not ready:
            return b""
        try:
            data = os.read(self.fd, 1024)
        except BlockingIOError:
            return b""
        if not data:  # readable but empty: the device went away (hang-up)
            raise OSError(errno.EIO, "serial device closed")
        return data

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


def _make_raw(fd: int) -> None:
    """cfmakeraw() + 115200 baud + ignore modem lines, and drop anything already buffered."""
    import termios  # noqa: PLC0415 - POSIX only

    attrs = termios.tcgetattr(fd)
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
    iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP | termios.INLCR | termios.IGNCR
               | termios.ICRNL | termios.IXON | termios.IXOFF)
    oflag &= ~termios.OPOST
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    cflag &= ~(termios.CSIZE | termios.PARENB)
    cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
    speed = getattr(termios, "B115200", ispeed)
    cc = list(cc)
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed, speed, cc])
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
    except termios.error:
        pass


def _usb_vendor(real_path: str, sysfs: str = SYSFS_TTY) -> str | None:
    """The USB vendor id of a tty (``ttyACM0`` -> its USB device's ``idVendor``), or None."""
    dev = os.path.join(sysfs, os.path.basename(real_path), "device")
    try:
        node = os.path.realpath(dev)
    except OSError:
        return None
    for _ in range(3):  # the interface's parent is the USB device
        try:
            with open(os.path.join(node, "idVendor"), encoding="ascii") as fh:
                return fh.read().strip().lower()
        except OSError:
            node = os.path.dirname(node)
    return None


def candidate_ports(globs: tuple[str, ...] | list[str] = DEFAULT_GLOBS, *, match_any: bool = False,
                    sysfs: str = SYSFS_TTY) -> list[str]:
    """Ports that may be the battery monitor, each device once (by-id links first)."""
    out: list[str] = []
    seen: set[str] = set()
    for pattern in globs:
        for path in sorted(glob.glob(pattern)):
            try:
                real = os.path.realpath(path)
            except OSError:
                continue
            if real in seen:
                continue
            if not match_any and not (BY_ID_HINT.search(os.path.basename(path))
                                      or _usb_vendor(real, sysfs) == RP2040_VENDOR):
                continue
            seen.add(real)
            out.append(path)
    return out


@dataclass
class SourceStatus:
    kind: str
    connected: bool = False
    port: str | None = None
    detail: str = "not found"
    lines: int = 0
    bad_lines: int = 0
    connects: int = 0
    last_line_age_s: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "connected": self.connected, "port": self.port, "detail": self.detail,
                "lines": self.lines, "bad_lines": self.bad_lines, "connects": self.connects,
                "last_line_age_s": self.last_line_age_s}


class SerialMonitor:
    """The real monitor on a serial port: find, read, reconnect (see the module docstring)."""

    kind = "serial"

    def __init__(self, *, port: str | None = None, globs: tuple[str, ...] | list[str] = DEFAULT_GLOBS,
                 match_any: bool = False, scan_s: float = 10.0, probe_s: float = 3.0, stale_s: float = 5.0,
                 reject_s: float = 300.0, opener: Callable[[str], Port] = TtyPort,
                 lister: Callable[[], list[str]] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.port = port
        self.globs = tuple(globs)
        self.match_any = match_any
        self.scan_s = scan_s
        self.probe_s = probe_s
        self.stale_s = stale_s
        self.reject_s = reject_s
        self.opener = opener
        self.lister = lister or (lambda: candidate_ports(self.globs, match_any=self.match_any))
        self.clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_reading: ReadingCallback | None = None
        self._on_status: StatusCallback | None = None
        self._rejected: dict[str, float] = {}
        self._lock = threading.Lock()
        self._st = SourceStatus(self.kind)
        self._last_line_at: float | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self, on_reading: ReadingCallback, on_status: StatusCallback | None = None) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._on_reading, self._on_status = on_reading, on_status
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="battery-monitor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            st = self._st.as_dict()
            if self._last_line_at is not None:
                st["last_line_age_s"] = round(max(0.0, self.clock() - self._last_line_at), 1)
            return st

    # ------------------------------------------------------------------ the thread
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                found = self.find()
            except Exception:  # noqa: BLE001 - scanning must never kill the thread
                log.debug("battery monitor scan failed", exc_info=True)
                found = None
            if found is None:
                self._stop.wait(self.scan_s)
                continue
            port, path, first = found
            try:
                self._connected(path)
                self._deliver(first)
                why = self.read_until_lost(port)
            finally:
                port.close()
            self._disconnected(why)
            self._stop.wait(1.0)

    def find(self) -> tuple[Port, str, Reading] | None:
        """Open the first candidate that sends a monitor line within ``probe_s``: (port, path, first reading)."""
        now = self.clock()
        self._rejected = {p: t for p, t in self._rejected.items() if now - t < self.reject_s}
        paths = [self.port] if self.port else [p for p in self.lister() if p not in self._rejected]
        for path in paths:
            if self._stop.is_set():
                return None
            try:
                port = self.opener(path)
            except OSError as e:
                log.debug("battery monitor: can't open %s (%s)", path, e.strerror or e)
                if not self.port:
                    self._rejected[path] = now
                continue
            reading = self._probe(port)
            if reading is not None:
                return port, path, reading
            port.close()
            if not self.port:
                log.debug("battery monitor: %s isn't the monitor", path)
                self._rejected[path] = now
        return None

    def _probe(self, port: Port) -> Reading | None:
        split = LineSplitter()
        deadline = self.clock() + self.probe_s
        while not self._stop.is_set():
            left = deadline - self.clock()
            if left <= 0:
                return None
            try:
                data = port.read(min(0.5, left))
            except OSError:
                return None
            for raw in split.feed(data):
                if MARKER in raw.replace(b" ", b""):
                    reading = parse_line(raw)
                    if reading is not None:
                        return reading
        return None

    def read_until_lost(self, port: Port, on_reading: ReadingCallback | None = None) -> str:
        """Deliver readings until the port fails, goes quiet for ``stale_s`` or stop(); returns why it ended.
        ``on_reading`` (scripts/check_battery.py) replaces the callback given to start()."""
        if on_reading is not None:
            self._on_reading = on_reading
        split = LineSplitter()
        last_good = self.clock()
        while not self._stop.is_set():
            try:
                data = port.read(0.5)
            except OSError as e:
                return f"unplugged ({e.strerror or e})"
            for raw in split.feed(data):
                reading = parse_line(raw)
                if reading is None:
                    with self._lock:
                        self._st.bad_lines += 1
                    continue
                last_good = self.clock()
                self._deliver(reading)
            if self.clock() - last_good > self.stale_s:
                return f"no readings for {self.stale_s:.0f} s"
        return "stopped"

    # ------------------------------------------------------------------ callbacks
    def _deliver(self, reading: Reading) -> None:
        with self._lock:
            self._st.lines += 1
            self._last_line_at = self.clock()
        cb = self._on_reading
        if cb is not None:
            try:
                cb(reading)
            except Exception:  # noqa: BLE001
                log.exception("battery reading handler failed")

    def _connected(self, path: str) -> None:
        with self._lock:
            self._st.connected, self._st.port, self._st.detail = True, path, "connected"
            self._st.connects += 1
        log.info("battery monitor connected on %s", path)
        self._status_cb(True, path)

    def _disconnected(self, why: str) -> None:
        with self._lock:
            port = self._st.port
            self._st.connected, self._st.detail = False, why
        if why != "stopped":
            log.warning("battery monitor on %s lost: %s; looking for it again", port, why)
        self._status_cb(False, None)

    def _status_cb(self, connected: bool, port: str | None) -> None:
        cb = self._on_status
        if cb is not None:
            try:
                cb(connected, port)
            except Exception:  # noqa: BLE001
                log.exception("battery monitor status handler failed")


# ====================================================================== simulated
def cell_for_percent(percent: float) -> float:
    """Inverse of ``battery.percent_for_cell``: a cell voltage for a state of charge."""
    p = max(0.0, min(100.0, float(percent)))
    for (v0, p0), (v1, p1) in zip(CELL_CURVE, CELL_CURVE[1:]):
        if p0 <= p <= p1:
            return v0 + (v1 - v0) * ((p - p0) / (p1 - p0) if p1 > p0 else 0.0)
    return CELL_CURVE[-1][0]


class SimulatedMonitor:
    """A pretend pack: drains ``drain_per_hour`` % an hour, charges ``charge_per_hour`` while plugged in."""

    kind = "simulated"

    def __init__(self, *, percent: float = 80.0, charging: bool = False, temperature_c: float | None = 24.0,
                 drain_per_hour: float = 20.0, charge_per_hour: float = 40.0, interval_s: float = 1.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.percent = float(percent)
        self.charging = bool(charging)
        self.temperature_c = temperature_c
        self.drain_per_hour = drain_per_hour
        self.charge_per_hour = charge_per_hour
        self.interval_s = interval_s
        self.clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_reading: ReadingCallback | None = None
        self._at = clock()
        self._started = clock()
        self.lines = 0

    def start(self, on_reading: ReadingCallback, on_status: StatusCallback | None = None) -> None:
        self._on_reading = on_reading
        if on_status is not None:
            on_status(True, "simulated")
        if self.interval_s > 0 and (self._thread is None or not self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="battery-sim", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)
        self._thread = None

    def set(self, *, percent: float | None = None, charging: bool | None = None,
            temperature_c: float | None | str = "keep") -> None:
        with self._lock:
            self._advance()
            if percent is not None:
                self.percent = max(0.0, min(100.0, float(percent)))
            if charging is not None:
                self.charging = bool(charging)
            if temperature_c != "keep":
                self.temperature_c = None if temperature_c is None else float(temperature_c)  # type: ignore[arg-type]

    def _advance(self) -> None:
        now = self.clock()
        hours = max(0.0, now - self._at) / 3600.0
        self._at = now
        if self.charging:
            self.percent = min(100.0, self.percent + self.charge_per_hour * hours)
        else:
            self.percent = max(0.0, self.percent - self.drain_per_hour * hours)

    def reading(self) -> Reading:
        """The pack now, as the monitor would report it."""
        with self._lock:
            self._advance()
            full = self.charging and self.percent >= 99.9
            cell = cell_for_percent(self.percent) + (0.1 if self.charging and not full else 0.0)
            return Reading(voltage=round(2 * cell, 3), cell1=round(cell, 3), temperature_c=self.temperature_c,
                           charging=self.charging and not full, charged=full,
                           uptime_s=int(self.clock() - self._started), fw=1)

    def emit(self) -> Reading:
        r = self.reading()
        self.lines += 1
        cb = self._on_reading
        if cb is not None:
            cb(r)
        return r

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.emit()
            except Exception:  # noqa: BLE001
                log.exception("simulated battery failed")

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"kind": self.kind, "connected": True, "port": "simulated", "detail": "simulated battery",
                    "lines": self.lines, "bad_lines": 0, "connects": 1, "last_line_age_s": None,
                    "simulated": {"percent": round(self.percent, 1), "charging": self.charging,
                                  "temperature_c": self.temperature_c}}
