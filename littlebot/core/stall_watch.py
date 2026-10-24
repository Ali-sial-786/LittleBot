"""Notices when LittleBot stops responding, and logs why (its own thread; reads only).

Nearly everything LittleBot does (live voice, the dashboard, activities, the wake word's hand-over)
runs on one asyncio loop (littlebot/core/runtime.py). If something blocks that loop, the robot
seems to stop responding. Every second this watcher asks the loop to answer:

* no answer within STUCK_AFTER_S: a WARNING "the main loop has been stuck", naming the code it is
  busy in (file, line and function only: never data), repeated every STILL_STUCK_EVERY_S, and one
  when it answers again, with how long it was stuck;
* the watcher's own one-second sleep took much longer: the whole Pi was frozen (out of memory and
  swapping, or a power dip), a WARNING saying so;
* memory low, the Pi swapping, too hot or under-voltage: a WARNING when it starts (and an INFO when
  it is fine again), and an INFO "health" line every HEALTH_EVERY_S to compare with.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

PING_EVERY_S = 1.0
STUCK_AFTER_S = 2.0
STILL_STUCK_EVERY_S = 30.0
SLOW_S = 1.0  # an answer later than this (but not stuck) is logged too
FROZEN_AFTER_S = 5.0  # the watcher's own 1 s sleep took this long: the whole Pi stood still
SAMPLE_EVERY_S = 30.0
HEALTH_EVERY_S = 300.0
LOW_MEMORY_MB = 150
MEMORY_OK_MB = 250
SWAP_GROWTH_MB = 64  # swap use grew this much in one sample: the Pi is swapping
HOT_C = 80.0
COOL_C = 75.0
STACK_FRAMES = 6


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                out[key] = int(parts[0]) // 1024  # MB
    except OSError:
        pass
    return out


def _temp_c() -> float | None:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def _throttled() -> int | None:
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
        return int(out.strip().split("=")[-1], 16)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _own_usage() -> tuple[int | None, float | None]:
    """This process: resident memory (MB) and CPU seconds so far."""
    rss = None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        t = os.times()
        cpu: float | None = t.user + t.system
    except OSError:
        cpu = None
    return rss, cpu


def where(thread: threading.Thread | None) -> str:
    """The last few calls ``thread`` is in, innermost last ("live.py:812 _instructions <- ..."); code only."""
    if thread is None or thread.ident is None:
        return "unknown"
    frame = sys._current_frames().get(thread.ident)  # noqa: SLF001 - the standard way to see another thread
    if frame is None:
        return "unknown"
    steps = traceback.extract_stack(frame)[-STACK_FRAMES:]
    return " <- ".join(f"{Path(s.filename).name}:{s.lineno} {s.name}" for s in reversed(steps))


class StallWatch:
    def __init__(self, runtime: Any, *, sample: Callable[[], dict[str, Any]] | None = None,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.runtime = runtime
        self._sample = sample or self._read_system
        self._now = now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stalls = 0  # times the loop was stuck (for tests and status)
        self.worst_ms = 0  # slowest answer since the last health line
        self._low_memory = self._hot = self._swapping = False
        self._throttle_seen = 0
        self._last_swap: int | None = None
        self._cpu_at: tuple[float, float] | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="littlebot-stall-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    # ------------------------------------------------------------ the loop
    def _run(self) -> None:
        next_sample = self._now() + 5.0
        next_health = self._now() + 60.0  # a first line soon after starting, then every HEALTH_EVERY_S
        while not self._stop.is_set():
            self.ping()
            before = self._now()
            if self._stop.wait(PING_EVERY_S):
                return
            slept = self._now() - before
            if slept >= FROZEN_AFTER_S:
                log.warning("LittleBot as a whole stood still for %.0f s (the Pi itself was stuck: out of memory "
                            "and swapping, or a power dip)", slept)
            now = self._now()
            if now >= next_sample:
                next_sample = now + SAMPLE_EVERY_S
                self._check_system(health=now >= next_health)
                if now >= next_health:
                    next_health = now + HEALTH_EVERY_S

    def ping(self) -> float | None:
        """Ask the loop to answer; wait for it (logging while it is stuck). Seconds it took, None if no loop."""
        try:
            loop = self.runtime.loop
        except RuntimeError:
            return None
        if loop.is_closed():
            return None
        answered = threading.Event()
        t0 = self._now()
        try:
            loop.call_soon_threadsafe(answered.set)
        except RuntimeError:  # closed meanwhile
            return None
        if not answered.wait(STUCK_AFTER_S):
            self.stalls += 1
            thread = getattr(self.runtime, "_thread", None)
            log.warning("the main loop has been stuck for %.0f s; it is busy in: %s", self._now() - t0, where(thread))
            while not answered.wait(STILL_STUCK_EVERY_S):
                if self._stop.is_set():
                    return None
                log.warning("the main loop is still stuck (%.0f s so far); busy in: %s", self._now() - t0,
                            where(thread))
            took = self._now() - t0
            log.warning("the main loop answered again after %.1f s stuck", took)
        else:
            took = self._now() - t0
            if took >= SLOW_S:
                log.info("the main loop was slow to answer: %d ms", int(took * 1000))
        self.worst_ms = max(self.worst_ms, int(took * 1000))
        return took

    # ------------------------------------------------------------ the Pi
    def _read_system(self) -> dict[str, Any]:
        mem = _meminfo()
        rss, cpu = _own_usage()
        try:
            load = os.getloadavg()[0]
        except OSError:
            load = None
        return {"available_mb": mem.get("MemAvailable"), "swap_used_mb": (mem["SwapTotal"] - mem["SwapFree"])
                if "SwapTotal" in mem and "SwapFree" in mem else None, "temp_c": _temp_c(),
                "throttled": _throttled(), "load": load, "rss_mb": rss, "cpu_s": cpu}

    def _check_system(self, *, health: bool = False) -> None:
        try:
            s = self._sample()
        except Exception:  # noqa: BLE001 - watching must never break anything
            log.debug("stall watch: reading the system failed", exc_info=True)
            return
        avail, swap, temp = s.get("available_mb"), s.get("swap_used_mb"), s.get("temp_c")
        if self._cpu_at is None and s.get("cpu_s") is not None:
            self._cpu_at = (self._now(), s["cpu_s"])  # the first health line's CPU share counts from here
        if avail is not None:
            if avail < LOW_MEMORY_MB and not self._low_memory:
                self._low_memory = True
                log.warning("memory is low: %d MB free (LittleBot uses %s MB); the Pi may start to swap and stall",
                            avail, s.get("rss_mb"))
            elif avail >= MEMORY_OK_MB and self._low_memory:
                self._low_memory = False
                log.info("memory is fine again: %d MB free", avail)
        if swap is not None:
            if self._last_swap is not None and swap - self._last_swap >= SWAP_GROWTH_MB:
                if not self._swapping:
                    log.warning("the Pi is swapping: %d MB of swap in use (+%d MB in %.0f s); everything slows down",
                                swap, swap - self._last_swap, SAMPLE_EVERY_S)
                self._swapping = True
            elif self._last_swap is not None and swap <= self._last_swap:
                self._swapping = False
            self._last_swap = swap
        if temp is not None:
            if temp >= HOT_C and not self._hot:
                self._hot = True
                log.warning("the Pi is hot: %.0f °C; it slows itself down to cool", temp)
            elif temp <= COOL_C and self._hot:
                self._hot = False
                log.info("the Pi has cooled to %.0f °C", temp)
        throttled = s.get("throttled")
        if throttled:
            new = throttled & ~self._throttle_seen & 0xF000F
            if new:
                self._throttle_seen |= throttled
                log.warning("the Pi reports %s (vcgencmd get_throttled = %#x)", _throttle_words(throttled), throttled)
        if health:
            cpu = s.get("cpu_s")
            pct = None
            now = self._now()
            if cpu is not None and self._cpu_at is not None and now > self._cpu_at[0]:
                pct = 100.0 * (cpu - self._cpu_at[1]) / (now - self._cpu_at[0])
            if cpu is not None:
                self._cpu_at = (now, cpu)
            load = s.get("load")
            log.info("health: %s MB free, %s MB swap in use, %s, load %s, LittleBot %s MB and %s CPU, main loop at "
                     "worst %d ms", avail if avail is not None else "?", swap if swap is not None else "?",
                     f"{temp:.0f} °C" if temp is not None else "temperature ?",
                     f"{load:.2f}" if load is not None else "?", s.get("rss_mb") or "?",
                     f"{pct:.0f}%" if pct is not None else "?", self.worst_ms)
            self.worst_ms = 0


def _throttle_words(value: int) -> str:
    bits = {0: "under-voltage now", 1: "the CPU limited now", 2: "throttled now", 3: "the soft temperature limit now",
            16: "under-voltage since boot", 17: "the CPU limited since boot", 18: "throttling since boot",
            19: "the soft temperature limit since boot"}
    return ", ".join(text for bit, text in bits.items() if value & (1 << bit)) or "a power or heat problem"
