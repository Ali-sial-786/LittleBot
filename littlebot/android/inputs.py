"""When the robot's touch screen (or a mouse or keyboard plugged into it) was last used, whichever window it went to.

Android's window gets the touches meant for it, so LittleBot's face never hears them. While Android runs, this
reads the Linux input devices directly (``/dev/input/event*``; the desktop user is in the ``input`` group) and keeps
only the time of the last event: never which key, where or what. Which devices: from ``/proc/bus/input/devices``,
those with a touch (``BTN_TOUCH``), a mouse (relative axes and ``BTN_LEFT``) or letter keys; not the power button,
the HDMI remote control (CEC) or the like.

``InputWatch(on_input).start()`` / ``.stop()``: ``on_input()`` is called from its thread at most every
``every`` seconds while something is being used. Not Linux, or no such device: it does nothing.
"""

from __future__ import annotations

import contextlib
import logging
import os
import select
import struct
import threading
import time
from collections.abc import Callable

log = logging.getLogger("littlebot.android")

DEVICES = "/proc/bus/input/devices"
EV_KEY, EV_REL, EV_ABS = 1, 2, 3
BTN_LEFT, BTN_TOUCH = 0x110, 0x14A
KEY_Q, KEY_A, KEY_Z, KEY_SPACE = 16, 30, 44, 57
RESCAN_S = 30.0  # a mouse plugged in later is found within this long
READ_BYTES = 4096
LONG_BITS = struct.calcsize("P") * 8  # a kernel ``long`` (64 on the Pi)


def _bits(text: str, width: int = LONG_BITS) -> set[int]:
    """A ``B:`` line's bitmap ("120013" or "1 0 ff00"): the words are ``long``s, most significant first."""
    words = text.split()
    out: set[int] = set()
    for i, word in enumerate(reversed(words)):
        try:
            value = int(word, 16)
        except ValueError:
            continue
        bit = 0
        while value:
            if value & 1:
                out.add(i * width + bit)
            value >>= 1
            bit += 1
    return out


def used_devices(text: str) -> list[str]:
    """The event devices (``event3`` ...) in ``/proc/bus/input/devices`` text that a person uses by hand."""
    found: list[str] = []
    for block in text.split("\n\n"):
        handlers: list[str] = []
        caps: dict[str, set[int]] = {}
        for line in block.splitlines():
            if line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1].split()
            elif line.startswith("B: ") and "=" in line:
                key, value = line[3:].split("=", 1)
                caps[key.strip()] = _bits(value)
        ev, keys = caps.get("EV", set()), caps.get("KEY", set())
        touch = EV_ABS in ev and BTN_TOUCH in keys
        mouse = EV_REL in ev and BTN_LEFT in keys
        keyboard = EV_KEY in ev and {KEY_Q, KEY_A, KEY_Z, KEY_SPACE} <= keys
        if touch or mouse or keyboard:
            found += [h for h in handlers if h.startswith("event") and h[5:].isdigit()]
    return found


def _read_devices(path: str = DEVICES) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return used_devices(fh.read())
    except OSError:
        return []


class InputWatch:
    """See the module docstring. ``devices`` (tests) replaces reading ``/proc/bus/input/devices``."""

    def __init__(self, on_input: Callable[[], None], *, every: float = 5.0,
                 devices: Callable[[], list[str]] | None = None, dev_dir: str = "/dev/input",
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._on_input = on_input
        self._every = float(every)
        self._devices = devices or _read_devices
        self._dir = dev_dir
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_input: float | None = None
        self.watching: list[str] = []

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not hasattr(select, "poll"):  # not Linux (the tests on a laptop): nothing to read
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="android-inputs", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _open(self, fds: dict[int, str], poller: "select.poll") -> None:
        want = set(self._devices())
        for fd, name in list(fds.items()):  # gone (unplugged)
            if name not in want:
                self._close(fd, fds, poller)
        have = set(fds.values())
        for name in sorted(want - have):
            try:
                fd = os.open(os.path.join(self._dir, name), os.O_RDONLY | os.O_NONBLOCK)
            except OSError as e:
                log.debug("android: can't read %s (%s)", name, e)
                continue
            fds[fd] = name
            poller.register(fd, select.POLLIN)
        names = sorted(fds.values())
        if names != self.watching:
            self.watching = names
            log.debug("android: watching the input devices %s", ", ".join(names) or "(none)")

    @staticmethod
    def _close(fd: int, fds: dict[int, str], poller: "select.poll") -> None:
        with contextlib.suppress(KeyError, ValueError, OSError):
            poller.unregister(fd)
        with contextlib.suppress(OSError):
            os.close(fd)
        fds.pop(fd, None)

    def _run(self) -> None:
        poller = select.poll()
        fds: dict[int, str] = {}
        told = 0.0
        scanned = -RESCAN_S
        try:
            while not self._stop.is_set():
                now = self._clock()
                if now - scanned >= RESCAN_S:
                    self._open(fds, poller)
                    scanned = now
                if not fds:
                    self._stop.wait(1.0)
                    continue
                got = False
                for fd, mask in poller.poll(500):
                    if mask & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                        self._close(fd, fds, poller)
                        continue
                    try:
                        while os.read(fd, READ_BYTES):  # thrown away: only the time counts
                            got = True
                    except BlockingIOError:
                        pass
                    except OSError:
                        self._close(fd, fds, poller)
                if got:
                    now = self._clock()
                    self.last_input = now
                    if now - told >= self._every:
                        told = now
                        try:
                            self._on_input()
                        except Exception:  # noqa: BLE001 - the watch never dies of its listener
                            log.debug("android: the input listener failed", exc_info=True)
        finally:
            for fd in list(fds):
                self._close(fd, fds, poller)
            self.watching = []


__all__ = ["InputWatch", "used_devices"]
