"""Giving memory back to the system after something big is let go (a card's drawings, a model unloaded).

Python and SDL free memory into the C library's heap, which keeps it for the next allocation instead of
returning it: the process stays as big as it ever was, and Chromium or a video has less room (the Pi swaps).
``give_back()`` collects Python's garbage and asks glibc to hand free heap pages back (``malloc_trim``):
measured on the cards, 33 MB of drawings freed took the process from 92 to 90 MB alone and to 59 MB with it.
A no-op where there is no glibc (macOS, musl).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import logging
import sys
import threading

log = logging.getLogger("littlebot.memory")

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


__all__ = ["give_back", "give_back_soon"]
