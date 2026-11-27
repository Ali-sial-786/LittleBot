"""The battery monitor's line format (firmware/battery_monitor/main.py), parsed defensively.

One JSON object per line, once a second::

    {"dev":"littlebot-battery","fw":1,"v":7.84,"cell1":3.92,"t":24.6,"chg":false,"full":false,"up":123}

``parse_line`` returns a ``Reading`` or None for anything else: garbage, a half line (the port was
opened mid-line), another device, a MicroPython banner. Values that are missing, of the wrong type or
physically impossible become None (unknown) rather than failing the whole line.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

DEVICE = "littlebot-battery"
MARKER = b'"dev":"littlebot-battery"'  # a line from the monitor has this (port discovery looks for it)
MAX_LINE = 512  # longer is not ours (bytes)

# Sanity limits on the robot side too (the firmware already sends null outside its own ranges).
PACK_LIMITS = (0.0, 12.0)
CELL_LIMITS = (0.0, 5.0)
TEMP_LIMITS = (-40.0, 125.0)


@dataclass(frozen=True)
class Reading:
    """One line from the monitor. None = the monitor couldn't measure it (not connected)."""

    voltage: float | None  # pack, volts
    cell1: float | None  # bottom cell, volts
    temperature_c: float | None
    charging: bool | None
    charged: bool | None
    uptime_s: int | None
    fw: int | None = None
    # A UPS board with its own fuel gauge (littlebot/power/ups_hat.py): every cell measured, and its percentage.
    cell_volts: tuple[float, ...] = ()
    percent: float | None = None

    @property
    def cell2(self) -> float | None:
        if self.voltage is None or self.cell1 is None:
            return None
        return round(self.voltage - self.cell1, 3)

    @property
    def cells(self) -> tuple[float, ...]:
        """The cell voltages that are known (both, or none; every cell from a UPS board)."""
        if self.cell_volts:
            return self.cell_volts
        c2 = self.cell2
        if self.cell1 is None or c2 is None:
            return ()
        return (self.cell1, c2)


def _number(value: Any, limits: tuple[float, float]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    if not math.isfinite(v) or not limits[0] <= v <= limits[1]:
        return None
    return v


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def parse_line(line: bytes | str) -> Reading | None:
    """A ``Reading`` from one line, or None when it isn't a (whole) line from the battery monitor."""
    if isinstance(line, bytes):
        if len(line) > MAX_LINE:
            return None
        try:
            text = line.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
    else:
        text = str(line)
        if len(text) > MAX_LINE:
            return None
    text = text.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("dev") != DEVICE:
        return None
    up = data.get("up")
    fw = data.get("fw")
    voltage = _number(data.get("v"), PACK_LIMITS)
    cell1 = _number(data.get("cell1"), CELL_LIMITS)
    if cell1 is not None and (voltage is None or not CELL_LIMITS[0] <= voltage - cell1 <= CELL_LIMITS[1]):
        cell1 = None  # a cell we can't pair with the pack (cell 2 would be nonsense)
    return Reading(
        voltage=voltage,
        cell1=cell1,
        temperature_c=_number(data.get("t"), TEMP_LIMITS),
        charging=_flag(data.get("chg")),
        charged=_flag(data.get("full")),
        uptime_s=int(up) if isinstance(up, int) and not isinstance(up, bool) and up >= 0 else None,
        fw=int(fw) if isinstance(fw, int) and not isinstance(fw, bool) else None,
    )


class LineSplitter:
    """Bytes from the port -> whole lines. A partial first line (opened mid-line) and over-long junk are dropped."""

    def __init__(self, max_line: int = MAX_LINE) -> None:
        self.max_line = max_line
        self._buf = bytearray()
        self._overflow = False

    def feed(self, data: bytes) -> list[bytes]:
        out: list[bytes] = []
        self._buf += data
        while True:
            i = self._buf.find(b"\n")
            if i < 0:
                break
            line = bytes(self._buf[:i]).rstrip(b"\r")
            del self._buf[:i + 1]
            if self._overflow:
                self._overflow = False  # the tail of a line that was too long
                continue
            out.append(line)
        if len(self._buf) > self.max_line:
            self._buf.clear()
            self._overflow = True
        return out
