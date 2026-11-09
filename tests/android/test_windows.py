"""littlebot/android/windows.py: the desktop's windows from a fake compositor speaking Wayland's wire format."""

from __future__ import annotations

import os
import socket
import struct
import threading

import pytest

from littlebot.android import windows as W
from littlebot.android.service import where_android_is

FACE, DROID, GONE = 0xFF000000, 0xFF000001, 0xFF000002


def ev(obj: int, opcode: int, args: bytes = b"") -> bytes:
    return W.message(obj, opcode, args)


def u(*values: int) -> bytes:
    return struct.pack(f"={len(values)}I", *values)


def s(text: str) -> bytes:
    return W._string(text)


def states(*values: int) -> bytes:
    raw = u(*values) if values else b""
    return u(len(raw)) + raw


def compositor(*, manager: bool = True, error: bool = False) -> bytes:
    """Everything a compositor would answer, in order (the client reads it as it goes)."""
    out = ev(W.REGISTRY, W.REGISTRY_GLOBAL, u(1) + s("wl_compositor") + u(6))
    if manager:
        out += ev(W.REGISTRY, W.REGISTRY_GLOBAL, u(7) + s(W.MANAGER) + u(3))
    if error:
        return out + ev(W.DISPLAY, W.DISPLAY_ERROR, u(W.DISPLAY, 1) + s("invalid object"))
    out += ev(W.SYNC_1, W.CALLBACK_DONE, u(0))
    if not manager:
        return out
    out += ev(W.MANAGER_ID, W.MANAGER_TOPLEVEL, u(FACE))
    out += ev(FACE, W.H_TITLE, s("LittleBot")) + ev(FACE, W.H_APP_ID, s("__main__.py"))
    out += ev(FACE, W.H_STATE, states(W.STATE_FULLSCREEN)) + ev(FACE, W.H_DONE)
    out += ev(W.MANAGER_ID, W.MANAGER_TOPLEVEL, u(DROID))
    out += ev(DROID, W.H_TITLE, s("Waydroid")) + ev(DROID, W.H_APP_ID, s("Waydroid"))
    out += ev(DROID, W.H_STATE, states(W.STATE_ACTIVATED, W.STATE_MAXIMIZED)) + ev(DROID, W.H_DONE)
    out += ev(W.MANAGER_ID, W.MANAGER_TOPLEVEL, u(GONE))
    out += ev(GONE, W.H_APP_ID, s("closing")) + ev(GONE, W.H_CLOSED)
    out += ev(W.DISPLAY, 1, u(W.SYNC_1))  # delete_id: ignored
    return out + ev(W.SYNC_2, W.CALLBACK_DONE, u(0))


def ask(answer: bytes) -> tuple[list[W.Window] | None, bytes]:
    """query() against a fake compositor; returns the windows and what the client sent."""
    client, server = socket.socketpair()
    sent = bytearray()

    def serve() -> None:
        server.sendall(answer)
        server.settimeout(2.0)
        try:
            while True:
                got = server.recv(4096)
                if not got:
                    return
                sent.extend(got)
        except OSError:
            return

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    client.settimeout(2.0)
    try:
        got = W.query(client)
    finally:
        client.close()
        t.join(2.0)
        server.close()
    return got, bytes(sent)


def test_the_windows_and_which_is_in_front() -> None:
    got, _ = ask(compositor())
    assert got == [W.Window(app_id="__main__.py", title="LittleBot", fullscreen=True),
                   W.Window(app_id="Waydroid", title="Waydroid", active=True, maximized=True)]


def test_the_client_binds_the_toplevel_manager() -> None:
    _, sent = ask(compositor())
    msgs = []
    at = 0
    while at < len(sent):
        obj, word = struct.unpack_from("=II", sent, at)
        msgs.append((obj, word & 0xFFFF, sent[at + 8:at + (word >> 16)]))
        at += word >> 16
    assert [(o, op) for o, op, _ in msgs] == [(W.DISPLAY, W.DISPLAY_GET_REGISTRY), (W.DISPLAY, W.DISPLAY_SYNC),
                                              (W.REGISTRY, W.REGISTRY_BIND), (W.DISPLAY, W.DISPLAY_SYNC)]
    bind = msgs[2][2]
    assert bind == u(7) + s(W.MANAGER) + u(3, W.MANAGER_ID)


def test_a_compositor_without_the_manager() -> None:
    got, _ = ask(compositor(manager=False))
    assert got is None


def test_the_compositors_error_is_raised() -> None:
    with pytest.raises(W.WaylandError, match="invalid object"):
        ask(compositor(error=True))


def test_no_socket_means_it_cant_be_asked(tmp_path) -> None:  # noqa: ANN001
    assert W.list_windows(str(tmp_path / "wayland-9")) is None


def test_the_socket_path() -> None:
    assert W.socket_path({"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-1"}) == \
        os.path.join("/run/user/1000", "wayland-1")
    assert W.socket_path({"XDG_RUNTIME_DIR": "/run/user/1000"}) == os.path.join("/run/user/1000", "wayland-0")
    assert W.socket_path({"WAYLAND_DISPLAY": "/tmp/w"}) == "/tmp/w"


def win(app_id: str, *, active: bool = False, minimized: bool = False, title: str = "") -> W.Window:
    return W.Window(app_id=app_id, title=title, active=active, minimized=minimized)


@pytest.mark.parametrize(("windows", "where"), [
    (None, None),
    ([], "none"),
    ([win("__main__.py", active=True)], "none"),
    ([win("__main__.py"), win("Waydroid", active=True)], "front"),
    ([win("__main__.py"), win("waydroid.com.glazero.android", active=True)], "front"),  # multi-window mode
    ([win("__main__.py", active=True), win("Waydroid")], "behind"),
    ([win("__main__.py"), win("Waydroid", active=True, minimized=True)], "behind"),
    ([win("__main__.py"), win("Waydroid")], "front"),  # nothing has the focus: it shows
    ([win("x", title="Waydroid", active=True)], "front"),  # matched by its title too
])
def test_where_android_is(windows: list[W.Window] | None, where: str | None) -> None:
    assert where_android_is(windows) == where
