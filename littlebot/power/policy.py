"""When the battery needs someone: low, critical, hot, cold (pure: every call takes ``now``, no clock of its own).

``PowerPolicy.update(snapshot, now)`` returns the alarms that start *now*, each once, with hysteresis:

* ``low``       - percent <= ``low_percent`` for ``low_sustain_s``, not plugged in. Again only after charging
                  or climbing ``rearm_percent`` above it.
* ``critical``  - percent <= ``critical_percent`` or a cell below ``critical_cell_v`` (under load), held for
                  ``critical_sustain_s`` so a load spike doesn't count, and **never while plugged in**: plugging
                  in resets the timer. Again only after being plugged in.
* ``hot``       - pack at or above ``hot_c`` for ``hot_sustain_s``; again after cooling below ``hot_c - 5``.
* ``too_hot``   - pack at or above ``shutdown_c`` for ``hot_sustain_s`` (a clean shutdown follows).
* ``cold``      - charging below ``cold_charge_c`` (Li-ion must not charge below 0 °C) for ``hot_sustain_s``.

A reading that doesn't have the value (None) doesn't count either way for its alarm's timer.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Thresholds:
    low_percent: float = 20.0
    critical_percent: float = 7.0
    critical_cell_v: float = 3.3
    low_sustain_s: float = 5.0
    critical_sustain_s: float = 20.0
    rearm_percent: float = 5.0
    hot_c: float = 50.0
    shutdown_c: float = 60.0
    hot_sustain_s: float = 10.0
    hot_rearm_c: float = 5.0
    cold_charge_c: float = 0.0


@dataclass(frozen=True)
class Snapshot:
    """What the policy looks at: the smoothed percentage, the raw weakest cell and temperature, the charger."""

    percent: float | None
    min_cell: float | None
    temperature_c: float | None
    charging: bool
    plugged_in: bool


class Sustain:
    """True once a condition has held without a break for ``seconds``."""

    def __init__(self) -> None:
        self.since: float | None = None

    def check(self, condition: bool | None, now: float, seconds: float) -> bool:
        if not condition:
            self.since = None
            return False
        if self.since is None:
            self.since = now
        return now - self.since >= seconds

    def held_for(self, now: float) -> float:
        return 0.0 if self.since is None else max(0.0, now - self.since)


@dataclass
class PowerPolicy:
    limits: Thresholds = field(default_factory=Thresholds)
    low: bool = False  # latched alarms (what the dashboard and the voice see)
    critical: bool = False
    hot: bool = False
    too_hot: bool = False
    cold: bool = False
    critical_reason: str = ""

    def __post_init__(self) -> None:
        self._low = Sustain()
        self._critical = Sustain()
        self._hot = Sustain()
        self._too_hot = Sustain()
        self._cold = Sustain()

    def update(self, snap: Snapshot, now: float) -> list[str]:
        lim = self.limits
        fired: list[str] = []
        pct, plugged = snap.percent, snap.plugged_in

        # --- low
        if plugged or (pct is not None and pct >= lim.low_percent + lim.rearm_percent):
            self.low = False
        is_low = pct is not None and pct <= lim.low_percent and not plugged
        if self._low.check(is_low, now, lim.low_sustain_s) and not self.low:
            self.low = True
            fired.append("low")

        # --- critical (never while plugged in)
        if plugged:
            self.critical, self.critical_reason = False, ""
        empty = pct is not None and pct <= lim.critical_percent
        weak = snap.min_cell is not None and snap.min_cell < lim.critical_cell_v
        if self._critical.check((empty or weak) and not plugged, now, lim.critical_sustain_s) and not self.critical:
            self.critical = True
            self.critical_reason = "a cell is below %.2f V" % lim.critical_cell_v if weak else \
                "the battery is at %d%%" % round(pct or 0)
            fired.append("critical")

        # --- heat
        t = snap.temperature_c
        if t is not None and t < lim.hot_c - lim.hot_rearm_c:
            self.hot = False
        if self._hot.check(t is not None and t >= lim.hot_c, now, lim.hot_sustain_s) and not self.hot:
            self.hot = True
            fired.append("hot")
        if self._too_hot.check(t is not None and t >= lim.shutdown_c, now, lim.hot_sustain_s) and not self.too_hot:
            self.too_hot = True
            fired.append("too_hot")
        if t is not None and t < lim.shutdown_c - lim.hot_rearm_c:
            self.too_hot = False

        # --- charging in the cold
        if not snap.charging or (t is not None and t > lim.cold_charge_c + 2.0):
            self.cold = False
        freezing = snap.charging and t is not None and t < lim.cold_charge_c
        if self._cold.check(freezing, now, lim.hot_sustain_s) and not self.cold:
            self.cold = True
            fired.append("cold")
        return fired

    def flags(self) -> list[str]:
        return [name for name in ("low", "critical", "hot", "too_hot", "cold") if getattr(self, name)]
