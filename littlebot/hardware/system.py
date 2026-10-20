"""CPU / memory / temperature monitoring (real via psutil, plus a mock)."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from littlebot.hardware.platform import describe_platform

try:  # psutil is a core dependency, but keep the monitor usable without it.
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]


def read_cpu_temperature() -> float | None:
    """Pi: /sys/class/thermal/thermal_zone0/temp (millidegrees). Desktop: psutil sensors if any."""
    zone = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int(zone.read_text().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        pass
    if psutil is not None and hasattr(psutil, "sensors_temperatures"):
        try:
            temps = psutil.sensors_temperatures() or {}
        except Exception:  # noqa: BLE001
            temps = {}
        for entries in temps.values():
            for e in entries:
                if e.current:
                    return round(float(e.current), 1)
    return None


_THROTTLE_GLOBS = (
    "/sys/devices/platform/soc/soc:firmware/get_throttled",
    "/sys/devices/platform/*/*firmware*/get_throttled",
    "/sys/devices/platform/*/*/*firmware*/get_throttled",
)
_THROTTLE_BITS = {
    0: "under-voltage now",
    1: "ARM frequency capped now",
    2: "throttled now",
    3: "soft temperature limit now",
    16: "under-voltage has occurred",
    17: "frequency capping has occurred",
    18: "throttling has occurred",
    19: "soft temperature limit has occurred",
}
_vcgencmd_cache: tuple[float, str | None] = (0.0, None)


def decode_throttled(value: int) -> list[str]:
    return [text for bit, text in _THROTTLE_BITS.items() if value & (1 << bit)]


def read_pi_throttled() -> str | None:
    """Pi firmware throttling flags as a hex string (e.g. '0x50000'), or None off-Pi.

    Reads the firmware sysfs node (its path differs between Pi models), falling back
    to ``vcgencmd get_throttled`` (cached for 30 s - it spawns a process).
    """
    global _vcgencmd_cache
    import glob

    for pattern in _THROTTLE_GLOBS:
        for path in glob.glob(pattern):
            try:
                return Path(path).read_text().strip()
            except OSError:
                continue
    now = time.monotonic()
    if now - _vcgencmd_cache[0] < 30:
        return _vcgencmd_cache[1]
    value: str | None = None
    try:
        import shutil
        import subprocess

        if shutil.which("vcgencmd"):
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
            if "=" in out:
                value = out.strip().split("=", 1)[1]
    except (OSError, subprocess.SubprocessError):
        value = None
    _vcgencmd_cache = (now, value)
    return value


class SystemMonitor:
    """Cheap snapshot of process/system health. Safe to call from any thread."""

    def __init__(self, min_interval: float = 1.0) -> None:
        self._started = time.monotonic()
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._cache: dict[str, Any] | None = None
        self._cache_t = 0.0
        self._proc = psutil.Process(os.getpid()) if psutil is not None else None
        if self._proc is not None:
            self._proc.cpu_percent(None)  # prime the counter
        self._platform = describe_platform()

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache_t < self._min_interval:
                return dict(self._cache)
            snap: dict[str, Any] = {
                "uptime_seconds": round(now - self._started, 1),
                "temperature_c": read_cpu_temperature(),
                "throttled": (throttled := read_pi_throttled()),
                "throttled_flags": _flags(throttled),
                "is_raspberry_pi": bool(self._platform["is_raspberry_pi"]),
                "platform": self._platform,
            }
            if psutil is not None and self._proc is not None:
                mem = psutil.virtual_memory()
                snap.update(
                    cpu_percent=round(self._proc.cpu_percent(None) / max(1, psutil.cpu_count() or 1), 1),
                    system_cpu_percent=psutil.cpu_percent(None),
                    memory_mb=round(self._proc.memory_info().rss / 1_048_576, 1),
                    memory_percent=round(self._proc.memory_percent(), 1),
                    system_memory_percent=mem.percent,
                    system_memory_total_mb=round(mem.total / 1_048_576),
                    threads=self._proc.num_threads(),
                )
            else:
                snap.update(cpu_percent=None, memory_mb=None, memory_percent=None, system_memory_percent=None)
            self._cache, self._cache_t = snap, now
            return dict(snap)


def _flags(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        return decode_throttled(int(value, 16))
    except ValueError:
        return []


class MockSystemMonitor:
    def __init__(self, **values: Any) -> None:
        self.values = {
            "cpu_percent": 12.0, "system_cpu_percent": 15.0, "memory_mb": 140.0, "memory_percent": 7.0,
            "system_memory_percent": 35.0, "temperature_c": 48.0, "is_raspberry_pi": False,
            "platform": {"system": "mock"}, "uptime_seconds": 0.0, "throttled": None,
        }
        self.values.update(values)

    def status(self) -> dict[str, Any]:
        return dict(self.values)
