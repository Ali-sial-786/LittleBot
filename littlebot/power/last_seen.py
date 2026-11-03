"""LittleBot's last sign of life, so a sudden power cut can be told from a clean stop afterwards.

The robot switched off when it was picked up or bumped, the UPS HAT (E) full, six times in a day (2026-11-02):
nothing in the log before each one, and the Pi keeps no journal across a restart, so a cut looked just like a
quiet room. The power service writes ``data/power_last.json`` every LAST_EVERY_S (the time, the battery's percent,
the pack's and each cell's volts, the charger) and ``"clean": true`` when it stops. At the next start, a file
without that, written before the Pi itself started, means its power was cut: a warning with the last reading.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("littlebot.power")

LAST_EVERY_S = 60.0  # "cut at about" to the minute is enough, and the SD card isn't written every few seconds
BOOT_SLACK_S = 5.0  # the clock and the uptime disagree by a little


def boot_time(uptime_file: Path = Path("/proc/uptime"), now: Callable[[], float] = time.time) -> float | None:
    """When the computer started (wall clock), or None where there's no /proc/uptime (Windows)."""
    try:
        return now() - float(uptime_file.read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        return None


class LastSeen:
    def __init__(self, path: Path | None, *, now: Callable[[], float] = time.time,
                 booted: Callable[[], float | None] = boot_time) -> None:
        self.path = path
        self._now = now
        self._booted = booted
        self._written_at = -1e18
        self.cut: dict[str, Any] | None = None  # the last sign of life before a power cut (check())

    @classmethod
    def for_context(cls, ctx: Any) -> LastSeen:
        """data/power_last.json on the Raspberry Pi (mode ``pi``: a desktop's restarts aren't power cuts);
        ``ctx.extras["power_last_file"]`` instead: the tests."""
        given = (getattr(ctx, "extras", None) or {}).get("power_last_file")
        if given:
            return cls(Path(given))
        config = getattr(ctx, "config", None)
        mode = getattr(getattr(config, "mode", None), "value", getattr(config, "mode", None))
        data = getattr(config, "data_path", None)
        return cls(Path(data) / "power_last.json" if data and mode == "pi" else None)

    def check(self) -> dict[str, Any] | None:
        """At start: the last sign of life when the run before ended with the power cut (logged), else None."""
        if self.path is None:
            return None
        try:
            last = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(last, dict) or last.get("clean") or not isinstance(last.get("at"), (int, float)):
            return None
        booted = self._booted()
        at = float(last["at"])
        when = dt.datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S")
        if booted is None or at > booted + BOOT_SLACK_S:
            log.info("power: LittleBot stopped without a clean stop at about %s, but the computer kept running "
                     "(a crash, or it was killed)", when)
            return None
        self.cut = last
        log.warning("power: the power was cut at about %s (LittleBot was running, there was no shutdown, and the "
                    "computer restarted after); the last reading: %s", when, describe(last))
        return last

    def note(self, reading: Any, percent: int | None, *, force: bool = False) -> None:
        """Every LAST_EVERY_S (or ``force``): the newest reading, as the last sign of life."""
        now = self._now()
        if self.path is None or (not force and now - self._written_at < LAST_EVERY_S):
            return
        self._written_at = now
        rec: dict[str, Any] = {"at": round(now, 1)}
        if reading is not None:
            rec.update(percent=percent, volts=reading.voltage, cells=list(reading.cells),
                       charging=reading.charging, charged=reading.charged)
        self._write(rec)

    def clean(self) -> None:
        """A clean stop: the next start knows it wasn't a power cut."""
        if self.path is not None:
            self._write({"at": round(self._now(), 1), "clean": True})

    def _write(self, rec: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rec), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            log.debug("power: the last sign of life couldn't be written", exc_info=True)


def describe(last: dict[str, Any]) -> str:
    """'on the battery, 100%, pack 16.42 V, cells 4.11/4.10/4.11/4.10 V' (what is known)."""
    if "volts" not in last and "percent" not in last:
        return "no battery reading"
    plugged = last.get("charging") or last.get("charged")
    parts = ["charger plugged in" if plugged else "on the battery"]
    if last.get("percent") is not None:
        parts.append(f"{last['percent']}%")
    if last.get("volts") is not None:
        parts.append(f"pack {float(last['volts']):.2f} V")
    cells = [float(c) for c in last.get("cells") or []]
    if cells:
        parts.append("cells " + "/".join(f"{c:.2f}" for c in cells) + " V")
    return ", ".join(parts)
