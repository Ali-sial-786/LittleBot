"""The Waveshare UPS HAT (E) as the battery monitor: its own fuel gauge, read over I2C (smbus2, the "pi" extra).

The HAT sits under a Raspberry Pi 5 on pogo pins and answers on I2C bus 1 at 0x2D (Waveshare's register
manual, https://docs.waveshare.com/UPS-HAT-E-Tutorials-Register). Every value is little-endian:

* ``0x02`` charging status: bit 7 charging, bit 5 USB-C power present, bits 2..0 the charger's state (5 = full);
* ``0x20`` the pack's voltage (mV), ``0x22`` its current (mA, signed: + charging), ``0x24`` the percentage,
  ``0x28`` the minutes left; ``0x30``..``0x37`` each cell's voltage (mV; an empty slot reads about 0).

``UpsHatMonitor`` polls those once a second on a daemon thread and hands each ``Reading`` to the same
callbacks as ``SerialMonitor``: the gauge then smooths the HAT's percentage like any other, and the weakest
cell still decides the critical check. Nothing is ever written to the HAT (register 0x01 would cut the power).
``probe()`` says whether the HAT is there (``power.monitor: auto`` uses it before the serial ports).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from littlebot.power.monitor import ReadingCallback, SourceStatus, StatusCallback
from littlebot.power.protocol import Reading

log = logging.getLogger("littlebot.power.ups_hat")

BUS = 1
ADDRESS = 0x2D
REG_ID, ID = 0x00, 0x0A
REG_STATUS = 0x02
REG_BATTERY = 0x20  # voltage, current, percent, mAh left, minutes left, minutes to full: 6 x 16 bits
REG_CELLS = 0x30  # 4 x 16 bits
CHARGING, VBUS = 0x80, 0x20
STATE_FULL = 0b101
MIN_CELL_V = 1.0  # below this a cell slot is empty


def _u16(data: list[int], i: int) -> int:
    return data[i] | (data[i + 1] << 8)


def _s16(data: list[int], i: int) -> int:
    v = _u16(data, i)
    return v - 0x10000 if v & 0x8000 else v


def decode(status: int, battery: list[int], cells: list[int]) -> Reading:
    """A ``Reading`` from the raw registers (pure, for tests)."""
    volts = _u16(battery, 0) / 1000.0
    percent = _u16(battery, 4)
    cell_volts = tuple(round(v, 3) for v in (_u16(cells, i) / 1000.0 for i in range(0, 8, 2)) if v >= MIN_CELL_V)
    full = (status & 0b111) == STATE_FULL
    charging = bool(status & CHARGING) and not full
    return Reading(voltage=round(volts, 3) if volts > 0 else None, cell1=None, temperature_c=None,
                   charging=charging, charged=full or (bool(status & VBUS) and not charging and percent >= 99),
                   uptime_s=None, cell_volts=cell_volts, percent=float(percent) if 0 <= percent <= 100 else None)


class Bus:
    """smbus2 on /dev/i2c-N, opened per use so an unplugged HAT never leaves a stale handle."""

    def __init__(self, bus: int = BUS, address: int = ADDRESS) -> None:
        self.bus, self.address = bus, address

    def read(self) -> tuple[int, list[int], list[int]]:
        from smbus2 import SMBus  # noqa: PLC0415 - only on the Pi ("pi" extra)

        with SMBus(self.bus) as b:
            status = b.read_byte_data(self.address, REG_STATUS)
            battery = b.read_i2c_block_data(self.address, REG_BATTERY, 12)
            cells = b.read_i2c_block_data(self.address, REG_CELLS, 8)
        return status, battery, cells

    def ident(self) -> int:
        from smbus2 import SMBus  # noqa: PLC0415

        with SMBus(self.bus) as b:
            return b.read_byte_data(self.address, REG_ID)


def probe(bus: int = BUS, address: int = ADDRESS, *, reader: Any = None) -> bool:
    """True when a UPS HAT (E) answers (its ID register reads 0x0A). Never raises."""
    try:
        return (reader or Bus(bus, address)).ident() == ID
    except Exception:  # noqa: BLE001 - no smbus2, no /dev/i2c-1, nothing at the address
        return False


class UpsHatMonitor:
    """Reads the HAT every ``interval_s``; ``stale_s`` of failed reads: disconnected, then retried every ``scan_s``."""

    kind = "ups_hat_e"

    def __init__(self, *, bus: int = BUS, address: int = ADDRESS, interval_s: float = 1.0, stale_s: float = 5.0,
                 scan_s: float = 10.0, reader: Any = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.reader = reader or Bus(bus, address)
        self.where = f"i2c-{bus} 0x{address:02x}"
        self.interval_s, self.stale_s, self.scan_s = interval_s, stale_s, scan_s
        self.clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_reading: ReadingCallback | None = None
        self._on_status: StatusCallback | None = None
        self._lock = threading.Lock()
        self._st = SourceStatus(self.kind)
        self._last_at: float | None = None

    def start(self, on_reading: ReadingCallback, on_status: StatusCallback | None = None) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._on_reading, self._on_status = on_reading, on_status
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="battery-ups-hat", daemon=True)
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
            if self._last_at is not None:
                st["last_line_age_s"] = round(max(0.0, self.clock() - self._last_at), 1)
            return st

    def poll(self) -> Reading | None:
        """One read: the reading (delivered), or None when the HAT didn't answer."""
        try:
            reading = decode(*self.reader.read())
        except Exception as e:  # noqa: BLE001 - an I2C error is "not answering", never fatal
            with self._lock:
                self._st.bad_lines += 1
            log.debug("UPS HAT: read failed (%s)", e)
            return None
        now = self.clock()
        if not self._st.connected:
            with self._lock:
                self._st.connected, self._st.port, self._st.detail = True, self.where, "connected"
                self._st.connects += 1
            log.info("battery: UPS HAT (E) answering on %s", self.where)
            self._status_cb(True, self.where)
        with self._lock:
            self._st.lines += 1
            self._last_at = now
        cb = self._on_reading
        if cb is not None:
            try:
                cb(reading)
            except Exception:  # noqa: BLE001
                log.exception("battery reading handler failed")
        return reading

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll()
            if self._st.connected and self._last_at is not None and self.clock() - self._last_at > self.stale_s:
                with self._lock:
                    self._st.connected, self._st.detail = False, f"no readings for {self.stale_s:.0f} s"
                log.warning("battery: UPS HAT on %s stopped answering; trying again", self.where)
                self._status_cb(False, None)
            self._stop.wait(self.interval_s if self._st.connected else self.scan_s)

    def _status_cb(self, connected: bool, port: str | None) -> None:
        cb = self._on_status
        if cb is not None:
            try:
                cb(connected, port)
            except Exception:  # noqa: BLE001
                log.exception("battery monitor status handler failed")


__all__ = ["ADDRESS", "BUS", "UpsHatMonitor", "decode", "probe"]
