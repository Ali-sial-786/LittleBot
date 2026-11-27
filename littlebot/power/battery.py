"""From voltages to a battery percentage that a parent can trust (pure: no threads, no clock of its own).

The pack is 2S1P 21700 Li-ion (7.2 V nominal, 8.4 V full). Its state of charge is read from the cell
voltage with ``CELL_CURVE``, a typical Li-ion discharge curve under the robot's light load (~1 A, a few
tens of mV of sag per cell). The weakest cell decides (the BMS cuts off when one cell is empty); without
the midpoint tap it is half the pack voltage.

Voltage alone jumps around: speaker peaks, Chromium starting, the charger switching on and off. So
``BatteryGauge``:

* smooths the voltages (exponential, ``tau_s``);
* for the first ``settle_s`` after it starts it follows the smoothed voltage both ways (a first reading
  taken during a load spike would otherwise stick);
* after that, while discharging, the percentage only ever goes **down**, at most ``max_fall_per_min``;
* while charging, it only ever goes **up**, at most ``max_rise_per_min``, from the voltage minus what the
  charging current adds (``CHARGE_RISE_PER_CELL``); "charged" heads for 100 %. So it never jumps around
  while charging, and unplugging (the voltage settles back) doesn't make it drop.

``TimeLeft`` estimates the minutes left from the percentage's slope over the last ``window_s`` (10 min),
and only once it has enough of it and the slope is steady.
"""

from __future__ import annotations

import collections
import math
from dataclasses import dataclass, field

from littlebot.power.protocol import Reading

CELLS = 2
# (volts per cell, percent) under light load; linear in between, 0 below / 100 above.
CELL_CURVE: tuple[tuple[float, float], ...] = (
    (3.30, 0), (3.50, 3), (3.60, 6), (3.65, 9), (3.69, 12), (3.71, 15), (3.73, 20), (3.75, 25), (3.77, 30),
    (3.79, 35), (3.80, 40), (3.82, 45), (3.84, 50), (3.86, 55), (3.88, 60), (3.91, 65), (3.95, 70), (3.98, 75),
    (4.02, 80), (4.06, 85), (4.10, 90), (4.14, 95), (4.18, 100),
)
CHARGE_RISE_PER_CELL = 0.10  # V a cell reads above its resting voltage while the charger pushes current
GAP_S = 60.0  # readings further apart than this restart the voltage smoothing (the monitor was away)


def percent_for_cell(volts: float) -> float:
    """State of charge (0..100) for one cell's voltage, from ``CELL_CURVE``."""
    curve = CELL_CURVE
    if volts <= curve[0][0]:
        return 0.0
    if volts >= curve[-1][0]:
        return 100.0
    for (v0, p0), (v1, p1) in zip(curve, curve[1:]):
        if v0 <= volts <= v1:
            return p0 + (p1 - p0) * (volts - v0) / (v1 - v0)
    return 0.0  # pragma: no cover - the curve is sorted


def percent_for_pack(volts: float, cells: int = CELLS) -> float:
    return percent_for_cell(volts / max(1, cells))


def _ema(old: float | None, new: float | None, alpha: float) -> float | None:
    if new is None:
        return old
    if old is None:
        return new
    return old + alpha * (new - old)


@dataclass
class BatteryGauge:
    """Smoothed voltages and the percentage (see the module docstring). Feed ``update(reading, now)``."""

    tau_s: float = 30.0
    max_fall_per_min: float = 1.0  # the robot uses ~0.3 %/min (about 7 W from 36 Wh)
    max_rise_per_min: float = 1.5  # a 2 A charger adds ~0.7 %/min
    settle_s: float = 120.0
    cells: int = CELLS
    voltage: float | None = None  # smoothed pack volts
    cell_volts: tuple[float, ...] = ()  # smoothed cell volts (both or none)
    percent: float | None = None  # smoothed state of charge 0..100
    charging: bool = False
    charged: bool = False
    _at: float | None = None
    _started: float | None = None
    _cells: list[float | None] = field(default_factory=list)

    @property
    def plugged_in(self) -> bool:
        return self.charging or self.charged

    @property
    def min_cell(self) -> float | None:
        """The weakest cell's smoothed voltage (half the pack without the midpoint tap)."""
        if self.cell_volts:
            return min(self.cell_volts)
        return None if self.voltage is None else self.voltage / max(1, self.cells)

    def update(self, reading: Reading, now: float) -> float | None:
        """Take one reading; returns the percentage (None while the voltage is unknown)."""
        dt = None if self._at is None else max(0.0, now - self._at)
        restart = dt is None or dt > GAP_S
        alpha = 1.0 if restart else 1.0 - math.exp(-(dt or 0.0) / max(0.1, self.tau_s))
        self._at = now
        self.charging = reading.charging is True
        self.charged = reading.charged is True
        self.voltage = _ema(None if restart else self.voltage, reading.voltage, alpha)
        cells = reading.cells
        if cells:
            old = [None] * len(cells) if restart or len(self._cells) != len(cells) else self._cells
            self._cells = [_ema(o, c, alpha) for o, c in zip(old, cells)]
            self.cell_volts = tuple(round(c, 4) for c in self._cells if c is not None)
        elif restart:
            self._cells, self.cell_volts = [], ()
        if reading.voltage is None and restart:
            self.voltage = None
        weakest = self.min_cell
        if weakest is None:
            return self.percent
        if self.charged:
            target = 100.0
        elif reading.percent is not None:  # the UPS board's own gauge counts the charge: trust it over volts
            target = max(0.0, min(100.0, float(reading.percent)))
        elif self.charging:
            target = percent_for_cell(weakest - CHARGE_RISE_PER_CELL)
        else:
            target = percent_for_cell(weakest)
        if self.percent is None:
            self.percent, self._started = target, now
            return self.percent
        minutes = (dt or 0.0) / 60.0
        if self._started is not None and now - self._started < self.settle_s:
            self.percent = target  # settling: the smoothed voltage, either way
        elif self.plugged_in:  # never down while charging, and slowly up
            if target > self.percent:
                self.percent = min(target, self.percent + self.max_rise_per_min * minutes)
        elif target < self.percent:  # never up while discharging, and smoothly down
            self.percent = max(target, self.percent - self.max_fall_per_min * minutes)
        self.percent = max(0.0, min(100.0, self.percent))
        return self.percent

    def reset(self) -> None:
        self.voltage, self.cell_volts, self.percent, self._at, self._cells = None, (), None, None, []
        self._started = None
        self.charging = self.charged = False


@dataclass
class TimeLeft:
    """Minutes left (discharging) or to full (charging) from the percentage's slope over ``window_s``."""

    window_s: float = 600.0
    min_span_s: float = 300.0  # at least this much history first
    min_samples: int = 20
    min_slope_per_min: float = 0.03  # % per minute: flatter than this is "not draining" (no estimate)
    min_r2: float = 0.6  # the points must lie on a line (a steady load)
    max_minutes: float = 24 * 60
    _samples: collections.deque[tuple[float, float]] = field(default_factory=lambda: collections.deque(maxlen=2000))
    _mode: str | None = None

    def clear(self) -> None:
        self._samples.clear()

    def add(self, now: float, percent: float | None, charging: bool) -> None:
        mode = "charging" if charging else "discharging"
        if mode != self._mode:  # a new trend: the old slope means nothing now
            self._mode = mode
            self._samples.clear()
        if percent is None:
            return
        self._samples.append((now, float(percent)))
        while self._samples and now - self._samples[0][0] > self.window_s:
            self._samples.popleft()

    def slope_per_min(self) -> float | None:
        """Least-squares slope in % per minute, or None while there's too little or too scattered data."""
        pts = list(self._samples)
        if len(pts) < self.min_samples or pts[-1][0] - pts[0][0] < self.min_span_s:
            return None
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        mp = sum(p for _, p in pts) / n
        stt = sum((t - mt) ** 2 for t, _ in pts)
        spp = sum((p - mp) ** 2 for _, p in pts)
        if stt <= 0:
            return None
        stp = sum((t - mt) * (p - mp) for t, p in pts)
        slope = stp / stt * 60.0
        if spp > 0 and stp * stp / (stt * spp) < self.min_r2:
            return None
        return slope

    def minutes(self, percent: float | None) -> int | None:
        """Minutes until empty (discharging) or full (charging), or None when there's no steady trend."""
        slope = self.slope_per_min()
        if slope is None or percent is None or abs(slope) < self.min_slope_per_min:
            return None
        if self._mode == "charging":
            if slope <= 0:
                return None
            left = (100.0 - percent) / slope
        else:
            if slope >= 0:
                return None
            left = percent / -slope
        if not math.isfinite(left) or left < 0:
            return None
        return int(round(min(left, self.max_minutes)))
