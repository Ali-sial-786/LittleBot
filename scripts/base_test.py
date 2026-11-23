#!/usr/bin/env python3
"""LittleBot v5 base: talk to the RP2040 on the deck over the Pi's UART (GPIO14/15, pins 8 and 10).

Needs ``dtparam=uart0=on`` in /boot/firmware/config.txt (then reboot): the port is /dev/ttyAMA0.
Stop LittleBot first if it's running, so only one program uses the port.

    python3 scripts/base_test.py ping                 # is the RP2040 there?
    python3 scripts/base_test.py battery              # the 6 x AA pack's voltage
    python3 scripts/base_test.py center               # all 3 servos to 90 (fit the pinion and the arms like this)
    python3 scripts/base_test.py servo 1 60           # servo 0 head, 1 left arm, 2 right arm, to 60 degrees
    python3 scripts/base_test.py off                  # servos limp
    python3 scripts/base_test.py encoders             # live encoder counts (turn the wheels by hand); Ctrl-C ends
    python3 scripts/base_test.py wheels               # guided: each wheel spins slowly; set which way is forwards
    python3 scripts/base_test.py calibrate            # guided: turn the wheels by hand, saves the counts per turn
    python3 scripts/base_test.py update [main.py]     # send a new firmware/base_controller/main.py and restart it
    python3 scripts/base_test.py send "D 20 20"       # any command line; prints the replies

The robot must stand on a box with its wheels off the table for ``wheels``.
"""

from __future__ import annotations

import argparse
import os
import select
import sys
import time
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from littlebot.power.monitor import _make_raw  # noqa: E402 - the same raw 115200 set-up as the battery monitor

FIRMWARE = REPO_ROOT / "firmware" / "base_controller" / "main.py"


class Link:
    def __init__(self, port: str) -> None:
        try:
            self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            sys.exit(f"can't open {port}: {e}\n(is dtparam=uart0=on in /boot/firmware/config.txt, and did you reboot? "
                     "Are you in the dialout group?)")
        if os.isatty(self.fd):
            _make_raw(self.fd)
        self.buf = b""

    def send(self, line: str | bytes) -> None:
        data = line if isinstance(line, bytes) else line.encode() + b"\n"
        view = memoryview(data)
        while view:
            _, w, _ = select.select([], [self.fd], [], 2.0)
            if not w:
                raise TimeoutError("the UART won't take more bytes")
            n = os.write(self.fd, view)
            view = view[n:]

    def lines(self, timeout: float):
        """Lines received within timeout seconds (yields as they come)."""
        end = time.monotonic() + timeout
        while True:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                yield line.decode("utf-8", "replace").strip()
            left = end - time.monotonic()
            if left <= 0:
                return
            r, _, _ = select.select([self.fd], [], [], left)
            if r:
                try:
                    self.buf += os.read(self.fd, 4096)
                except BlockingIOError:
                    pass

    def ask(self, line: str, starts: str, timeout: float = 1.0) -> str | None:
        """Send a command; the first reply line starting with ``starts`` (or None)."""
        self.send(line)
        for reply in self.lines(timeout):
            if reply.startswith(starts) or reply.startswith("ERR"):
                return reply
        return None


def need(reply: str | None, what: str) -> str:
    if reply is None:
        sys.exit(f"no answer to {what}: check the 3 UART wires (RP2040 GP0 -> Pi pin 10, GP1 -> Pi pin 8, GND -> pin 6), "
                 "that the base's switch is on and the RP2040's LED is lit")
    if reply.startswith("ERR"):
        sys.exit(reply)
    return reply


def counts(link: Link) -> tuple[int, int]:
    _, l, r = need(link.ask("E", "E "), "E").split()
    return int(l), int(r)


def spin(link: Link, l: float, r: float, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        link.send(f"D {l} {r}")
        for _ in link.lines(0.1):
            pass
    link.send("STOP")
    for _ in link.lines(1.0):        # the ramp down
        pass


def yes(prompt: str) -> bool:
    return input(prompt + " [y/n] ").strip().lower().startswith("y")


def cmd_wheels(link: Link) -> None:
    print("The robot must stand on a box with both wheels off the table.")
    input("Press Enter when it does (Ctrl-C to stop) ")
    cfg = need(link.ask("GET", "CFG "), "GET")
    for side, i in (("left", 0), ("right", 1)):
        before = counts(link)
        print(f"Spinning the {side} wheel slowly for 2 seconds...")
        spin(link, 25 if i == 0 else 0, 25 if i == 1 else 0, 2.0)
        moved = counts(link)[i] - before[i]
        flip_motor = not yes(f"Did the {side} wheel turn the way it turns when the robot drives FORWARDS?")
        key = f"minv_{side[0]}"
        if flip_motor:
            now = 1 - int(f'"{key}": 1' in cfg)
            need(link.ask(f"SET {key} {now}", "OK"), "SET")
            moved = -moved
            print(f"  ok: that motor now turns the other way ({key} = {now})")
        if moved == 0:
            print(f"  the {side} encoder didn't count: check its yellow, green, blue and black wires")
            continue
        if moved < 0:
            key = f"einv_{side[0]}"
            now = 1 - int(f'"{key}": 1' in cfg)
            need(link.ask(f"SET {key} {now}", "OK"), "SET")
            print(f"  ok: the {side} encoder now counts up going forwards ({key} = {now})")
        else:
            print(f"  the {side} encoder counts up going forwards: good ({abs(moved)} counts)")
    print(need(link.ask("SAVE", "OK"), "SAVE"))


def cmd_calibrate(link: Link) -> None:
    turns = 5
    result = []
    for side, i in (("left", 0), ("right", 1)):
        input(f"Mark the {side} wheel (a bit of tape), then press Enter ")
        a = counts(link)[i]
        input(f"Turn the {side} wheel FORWARDS exactly {turns} whole turns by hand, then press Enter ")
        b = counts(link)[i]
        cpr = abs(b - a) / turns
        print(f"  {side}: {b - a} counts = {cpr:.1f} per turn")
        result.append(cpr)
    if min(result) < 10:
        sys.exit("too few counts: check the encoders' wires (and run `wheels` first)")
    cpr = round(sum(result) / 2)
    print(need(link.ask(f"SET cpr {cpr}", "OK"), "SET"))
    print(need(link.ask("SAVE", "OK"), "SAVE"))


def cmd_update(link: Link, path: Path) -> None:
    data = path.read_bytes()
    reply = need(link.ask(f"PUT {len(data)} {zlib.crc32(data)}", "READY"), "PUT")
    print(reply, "- sending", path)
    for i in range(0, len(data), 256):
        link.send(data[i:i + 256])
        time.sleep(0.03)
    for line in link.lines(20.0):
        if line.startswith(("OK put", "ERR")):
            print(line)
            if line.startswith("ERR"):
                sys.exit(1)
            break
    else:
        sys.exit("no answer after the file: nothing was changed")
    link.send("RESET")
    for line in link.lines(5.0):
        if line.startswith("HELLO"):
            print(line)
            break
    print(need(link.ask("PING", "PONG"), "PING"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Talk to the LittleBot v5 base's RP2040 over the Pi's UART.")
    p.add_argument("--port", default="/dev/ttyAMA0")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("ping", "battery", "center", "off", "encoders", "wheels", "calibrate"):
        sub.add_parser(name)
    s = sub.add_parser("servo")
    s.add_argument("n", type=int, choices=(0, 1, 2))
    s.add_argument("deg", type=float)
    u = sub.add_parser("update")
    u.add_argument("file", nargs="?", type=Path, default=FIRMWARE)
    r = sub.add_parser("send")
    r.add_argument("line")
    a = p.parse_args(argv)

    link = Link(a.port)
    if a.cmd == "ping":
        print(need(link.ask("PING", "PONG"), "PING"))
    elif a.cmd == "battery":
        reply = need(link.ask("B", "B "), "B")
        print("the pack: " + ("no reading: check the 22k and 10k resistors and the wire from Wago A (on a YD-RP2040, its VREF jumper must be bridged)" if reply.endswith("null")
                              else reply[2:] + " V"))
    elif a.cmd == "center":
        print(need(link.ask("CENTER", "OK"), "CENTER"), "- the 3 servos are at 90 degrees and stay there")
    elif a.cmd == "off":
        print(need(link.ask("OFF", "OK"), "OFF"))
    elif a.cmd == "servo":
        link.send(f"S {a.n} {a.deg}")
        print(need(link.ask("PING", "PONG"), "PING") and f"servo {a.n} -> {a.deg} degrees")
    elif a.cmd == "encoders":
        need(link.ask("T 10", "OK"), "T")
        print("turn the wheels by hand; Ctrl-C ends")
        try:
            for line in link.lines(1e9):
                if line.startswith("T "):
                    _, ms, l, r, v = line.split()
                    print(f"\rleft {int(l):7d}   right {int(r):7d}   pack {v} V   ", end="", flush=True)
        except KeyboardInterrupt:
            print()
        finally:
            link.send("T 0")
    elif a.cmd == "wheels":
        try:
            cmd_wheels(link)
        finally:
            link.send("STOP")
    elif a.cmd == "calibrate":
        cmd_calibrate(link)
    elif a.cmd == "update":
        cmd_update(link, a.file)
    elif a.cmd == "send":
        link.send(a.line)
        for line in link.lines(1.0):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
