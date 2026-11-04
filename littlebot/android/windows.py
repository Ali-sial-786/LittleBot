"""Which windows the robot's desktop has open and which one is in front, asked of the compositor directly.

``list_windows()`` returns ``[Window(app_id, title, active, minimized, fullscreen, maximized), ...]``, or None when
the desktop can't be asked: no Wayland socket, or a compositor without ``zwlr_foreign_toplevel_manager_v1`` (labwc,
the Raspberry Pi desktop, has it: its taskbar uses it). Nothing is installed for this: the few messages it takes
are written here.

Wayland's wire format: each message is the object id (uint32), then ``size << 16 | opcode`` (uint32, the size with
these 8 bytes), then the arguments, each a multiple of 4 bytes: uint / int / object / new_id as one uint32; a
string as a uint32 length (with its NUL) and the bytes, padded; an array as a uint32 length and the bytes, padded.
Native byte order. The steps: ``wl_display.get_registry`` and a ``sync`` (the registry lists the globals before the
sync's callback is done), ``wl_registry.bind`` of the toplevel manager and a second ``sync``: binding makes the
compositor announce every open window (``toplevel``, then its ``title``, ``app_id``, ``state`` and ``done``) before
that second callback. Then the connection is closed.

Run on the robot to see what the desktop has open: ``python3 -m littlebot.android.windows``.
"""

from __future__ import annotations

import contextlib
import os
import socket
import struct
import sys
import time
from dataclasses import dataclass

MANAGER = "zwlr_foreign_toplevel_manager_v1"
MANAGER_VERSION = 3
TIMEOUT_S = 2.0
MAX_MESSAGES = 20000  # a runaway compositor can't keep this loop busy
MAX_TEXT = 200

# object ids this client makes (the compositor's own, for the windows, start at 0xff000000)
DISPLAY, REGISTRY, SYNC_1, MANAGER_ID, SYNC_2 = 1, 2, 3, 4, 5
# opcodes
DISPLAY_SYNC, DISPLAY_GET_REGISTRY = 0, 1  # wl_display requests
DISPLAY_ERROR = 0  # wl_display event (delete_id, 1, is ignored)
REGISTRY_BIND = 0  # wl_registry request
REGISTRY_GLOBAL = 0  # wl_registry event
CALLBACK_DONE = 0  # wl_callback event
MANAGER_TOPLEVEL, MANAGER_FINISHED = 0, 1  # zwlr_foreign_toplevel_manager_v1 events
H_TITLE, H_APP_ID, H_STATE, H_DONE, H_CLOSED = 0, 1, 4, 5, 6  # zwlr_foreign_toplevel_handle_v1 events
STATE_MAXIMIZED, STATE_MINIMIZED, STATE_ACTIVATED, STATE_FULLSCREEN = 0, 1, 2, 3


class WaylandError(Exception):
    """The compositor sent an error, closed the connection or said something that doesn't parse."""


@dataclass(frozen=True)
class Window:
    app_id: str
    title: str
    active: bool = False
    minimized: bool = False
    fullscreen: bool = False
    maximized: bool = False


# ---------------------------------------------------------------- the wire format
def _string(text: str) -> bytes:
    raw = text.encode("utf-8") + b"\0"
    return struct.pack("=I", len(raw)) + raw + b"\0" * (-len(raw) % 4)


def message(obj: int, opcode: int, args: bytes = b"") -> bytes:
    return struct.pack("=II", obj, (8 + len(args)) << 16 | opcode) + args


def _read_string(body: bytes, at: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("=I", body, at)
    at += 4
    if n == 0:  # a null string
        return "", at
    raw = body[at:at + n]
    if len(raw) < n:
        raise WaylandError("a string runs past its message")
    return raw[:-1].decode("utf-8", "replace")[:MAX_TEXT], at + n + (-n % 4)


def _read_array(body: bytes, at: int) -> tuple[bytes, int]:
    (n,) = struct.unpack_from("=I", body, at)
    at += 4
    raw = body[at:at + n]
    if len(raw) < n:
        raise WaylandError("an array runs past its message")
    return raw, at + n + (-n % 4)


class _Reader:
    """Whole messages out of the socket: (object id, opcode, arguments)."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""

    def next(self) -> tuple[int, int, bytes]:
        while True:
            if len(self.buf) >= 8:
                obj, word = struct.unpack_from("=II", self.buf)
                size, opcode = word >> 16, word & 0xFFFF
                if size < 8:
                    raise WaylandError(f"a message of {size} bytes")
                if len(self.buf) >= size:
                    body, self.buf = self.buf[8:size], self.buf[size:]
                    return obj, opcode, body
            got = self.sock.recv(65536)
            if not got:
                raise WaylandError("the compositor closed the connection")
            self.buf += got


# ---------------------------------------------------------------- asking the desktop
def socket_path(env: dict[str, str] | None = None) -> str | None:
    """The compositor's socket (``$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY``; wayland-0 when unset), or None."""
    env = os.environ if env is None else env
    display = env.get("WAYLAND_DISPLAY") or "wayland-0"
    if display.startswith("/"):  # a full path (Wayland allows that)
        return display
    runtime = env.get("XDG_RUNTIME_DIR") or (f"/run/user/{os.getuid()}" if hasattr(os, "getuid") else "")
    return os.path.join(runtime, display) if runtime else None


def query(sock: socket.socket) -> list[Window] | None:
    """The windows, over a connected socket (tests hand it a fake compositor). None: no toplevel manager."""
    sock.sendall(message(DISPLAY, DISPLAY_GET_REGISTRY, struct.pack("=I", REGISTRY))
                 + message(DISPLAY, DISPLAY_SYNC, struct.pack("=I", SYNC_1)))
    reader = _Reader(sock)
    manager: tuple[int, int] | None = None  # (global name, version)
    handles: dict[int, dict[str, object]] = {}
    order: list[int] = []
    bound = False
    for _ in range(MAX_MESSAGES):
        obj, opcode, body = reader.next()
        if obj == DISPLAY and opcode == DISPLAY_ERROR:
            _, code = struct.unpack_from("=II", body)
            text, _ = _read_string(body, 8)
            raise WaylandError(f"the compositor's error {code}: {text}")
        if obj == REGISTRY and opcode == REGISTRY_GLOBAL:
            (name,) = struct.unpack_from("=I", body)
            interface, at = _read_string(body, 4)
            (version,) = struct.unpack_from("=I", body, at)
            if interface == MANAGER:
                manager = (name, version)
        elif obj == SYNC_1 and opcode == CALLBACK_DONE:
            if manager is None:
                return None
            name, version = manager
            sock.sendall(message(REGISTRY, REGISTRY_BIND, struct.pack("=I", name) + _string(MANAGER)
                                 + struct.pack("=II", min(version, MANAGER_VERSION), MANAGER_ID))
                         + message(DISPLAY, DISPLAY_SYNC, struct.pack("=I", SYNC_2)))
            bound = True
        elif obj == MANAGER_ID and opcode == MANAGER_TOPLEVEL:
            (handle,) = struct.unpack_from("=I", body)
            handles[handle] = {"app_id": "", "title": "", "states": set()}
            order.append(handle)
        elif obj in handles:
            h = handles[obj]
            if opcode == H_TITLE:
                h["title"] = _read_string(body, 0)[0]
            elif opcode == H_APP_ID:
                h["app_id"] = _read_string(body, 0)[0]
            elif opcode == H_STATE:
                raw, _ = _read_array(body, 0)
                h["states"] = set(struct.unpack(f"={len(raw) // 4}I", raw[:len(raw) // 4 * 4]))
            elif opcode == H_CLOSED:
                handles.pop(obj, None)
        elif obj == SYNC_2 and opcode == CALLBACK_DONE and bound:
            out = []
            for handle in order:
                h = handles.get(handle)
                if h is None:
                    continue
                states = h["states"]
                assert isinstance(states, set)
                out.append(Window(app_id=str(h["app_id"]), title=str(h["title"]),
                                  active=STATE_ACTIVATED in states, minimized=STATE_MINIMIZED in states,
                                  fullscreen=STATE_FULLSCREEN in states, maximized=STATE_MAXIMIZED in states))
            return out
    raise WaylandError("too many messages from the compositor")


def list_windows(path: str | None = None, *, timeout: float = TIMEOUT_S) -> list[Window] | None:
    """The desktop's windows, or None when it can't be asked (see the module docstring). Never raises."""
    path = path or socket_path()
    if not path or not hasattr(socket, "AF_UNIX") or not os.path.exists(path):
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            return query(sock)
    except (OSError, WaylandError, struct.error):
        return None


def main() -> int:  # pragma: no cover - a look at the robot's desktop
    t0 = time.monotonic()
    windows = list_windows()
    ms = (time.monotonic() - t0) * 1000
    if windows is None:
        print(f"the desktop can't be asked ({socket_path()}: no socket, or no {MANAGER})")
        return 1
    print(f"{len(windows)} window(s), asked in {ms:.1f} ms:")
    for w in windows:
        flags = [n for n, on in (("active", w.active), ("minimized", w.minimized), ("fullscreen", w.fullscreen),
                                 ("maximized", w.maximized)) if on]
        print(f"  app_id={w.app_id!r} title={w.title!r} {' '.join(flags)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    with contextlib.suppress(BrokenPipeError):
        sys.exit(main())
