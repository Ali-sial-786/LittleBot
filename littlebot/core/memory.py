"""Giving memory back to the system after something big is let go (a card's drawings, a model unloaded).

Python and SDL free memory into the C library's heap, which keeps it for the next allocation instead of
returning it: the process stays as big as it ever was, and Chromium or a video has less room (the Pi swaps).
``give_back()`` collects Python's garbage and asks glibc to hand free heap pages back (``malloc_trim``):
measured on the cards, 33 MB of drawings freed took the process from 92 to 90 MB alone and to 59 MB with it.
A no-op where there is no glibc (macOS, musl).

``roomy(setting)``: whether this board has the memory to keep more loaded (``system.memory`` in the config:
auto = at least ``ROOMY_MB`` of RAM, i.e. a 4 GB or bigger Pi 5; small = always save, as on a 2 GB board;
roomy = always keep). What a roomy robot keeps: docs/PERFORMANCE.md "Memory profile".
"""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
import gc
import logging
import sys
import threading

log = logging.getLogger("littlebot.memory")

ROOMY_MB = 3000  # a 4 GB Pi 5 reports ~3.9 GB of RAM, a 2 GB one ~2.0 GB

_LOCK = threading.Lock()
_TRIM: object = None  # the C function, False once known to be missing


def _trim_fn() -> object:
    global _TRIM
    if _TRIM is None:
        _TRIM = False
        if sys.platform.startswith("linux"):
            try:
                libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
                _TRIM = getattr(libc, "malloc_trim", False)
            except OSError:
                pass
    return _TRIM


def give_back() -> None:
    """Collect garbage and return free heap memory to the system (a few ms). Never raises."""
    with _LOCK:
        try:
            gc.collect()
            trim = _trim_fn()
            if trim:
                trim(0)  # type: ignore[operator]
        except Exception:  # noqa: BLE001 - only ever an optimisation
            log.debug("giving memory back failed", exc_info=True)


def give_back_soon() -> None:
    """``give_back`` on a thread of its own (it takes ~25 ms: not on the event loop)."""
    threading.Thread(target=give_back, name="give-back-memory", daemon=True).start()


@functools.lru_cache(maxsize=1)
def total_mb() -> int | None:
    """The board's RAM in MB (``MemTotal``), None where /proc/meminfo isn't there (macOS, Windows)."""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def roomy(setting: object = "auto") -> bool:
    """True when LittleBot may keep more in memory (``system.memory``: "auto", "small" or "roomy"). A computer
    whose RAM can't be read (not Linux) counts as roomy: it isn't a 2 GB Pi."""
    if setting == "small":
        return False
    if setting == "roomy":
        return True
    mb = total_mb()
    return mb is None or mb >= ROOMY_MB


def roomy_for(ctx: object) -> bool:
    """``roomy`` with the robot's config (``ctx.config.system.memory``); auto when there is none."""
    system = getattr(getattr(ctx, "config", None), "system", None)
    return roomy(getattr(system, "memory", "auto"))


__all__ = ["ROOMY_MB", "give_back", "give_back_soon", "roomy", "roomy_for", "total_mb"]
