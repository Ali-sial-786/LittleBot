#!/usr/bin/env python3
"""Calibrate LittleBot's touch screen so a tap lands where you touch (MPI3508, ADS7846 touch).

    .venv/bin/python scripts/calibrate_touch.py            # crosses on the screen, then saves the result
    .venv/bin/python scripts/calibrate_touch.py --reset    # remove the calibration again

LittleBot is stopped while the crosses are shown and started again afterwards. Touch the centre
of each cross (a fingernail works best on this screen) and hold it for a moment. The script reads
the screen's raw touch readings, works out how they map to the screen (swapped, mirrored, shifted
or stretched directions are all covered) and saves that as a libinput calibration matrix in
/etc/udev/rules.d/99-littlebot-touch.rules (it asks for your password for sudo). Reboot afterwards:
the desktop only reads the calibration when it starts.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import queue
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO, Iterator, Sequence

import numpy as np

RULE_PATH = Path("/etc/udev/rules.d/99-littlebot-touch.rules")
TOUCH_NAMES = ("ads7846", "xpt2046", "touchscreen")
# Crosses away from the edges (resistive screens are least accurate at the very edge); the last
# one, in the middle, only checks the result.
TARGETS: tuple[tuple[float, float], ...] = ((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9), (0.5, 0.5))
MAX_CHECK_ERROR = 0.06  # the middle cross may be off by this much of the screen
MIN_SAMPLES = 4  # readings in one press (~10 ms apart); fewer is a bounce, not a press
PRESS_GAP_S = 0.4  # a press this soon after the last one is the same touch bouncing

EVENT = struct.Struct("llHHi")  # struct input_event on 64-bit Linux
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT, BTN_TOUCH, ABS_X, ABS_Y = 0, 0x14A, 0, 1
EVIOCGABS = 0x80184540  # _IOR('E', 0x40 + axis, struct input_absinfo)


def find_touch_device(devices_text: str) -> tuple[str, str] | None:
    """(name, "eventN") of the touch screen in /proc/bus/input/devices, or None."""
    name = None
    for line in devices_text.splitlines() + [""]:
        if line.startswith("N: Name="):
            name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("H: Handlers=") and name and any(t in name.lower() for t in TOUCH_NAMES):
            event = next((h for h in line.split("=", 1)[1].split() if h.startswith("event")), None)
            if event:
                return name, event
        elif not line.strip():
            name = None
    return None


def abs_range(fd: int, axis: int) -> tuple[int, int]:
    """The (minimum, maximum) the driver reports for ``axis``; libinput scales by these too."""
    info = struct.unpack("6i", fcntl.ioctl(fd, EVIOCGABS + axis, bytes(24)))
    lo, hi = info[1], info[2]
    return (lo, hi) if hi > lo else (0, 4095)


def presses(stream: BinaryIO) -> Iterator[list[tuple[int, int]]]:
    """Each touch (finger down to finger up) as its list of raw (x, y) readings."""
    x = y = None
    down = False
    samples: list[tuple[int, int]] = []
    while True:
        data = stream.read(EVENT.size)
        if not data or len(data) < EVENT.size:
            return
        _s, _us, etype, code, value = EVENT.unpack(data)
        if etype == EV_ABS and code == ABS_X:
            x = value
        elif etype == EV_ABS and code == ABS_Y:
            y = value
        elif etype == EV_KEY and code == BTN_TOUCH:
            if value:
                down, samples = True, []
            elif down:
                down = False
                yield samples
        elif etype == EV_SYN and code == SYN_REPORT and down and x is not None and y is not None:
            samples.append((x, y))


def press_point(samples: Sequence[tuple[int, int]]) -> tuple[float, float] | None:
    """Where one press was: the median of its steady middle (the first and last readings wobble)."""
    if len(samples) < MIN_SAMPLES:
        return None
    cut = len(samples) // 5
    mid = np.asarray(samples[cut:len(samples) - cut] or samples, dtype=np.float64)
    return float(np.median(mid[:, 0])), float(np.median(mid[:, 1]))


def normalise(point: tuple[float, float], xr: tuple[int, int], yr: tuple[int, int]) -> tuple[float, float]:
    return (point[0] - xr[0]) / (xr[1] - xr[0]), (point[1] - yr[0]) / (yr[1] - yr[0])


def fit_matrix(touched: Sequence[tuple[float, float]], targets: Sequence[tuple[float, float]]) -> list[float]:
    """libinput's calibration matrix [a b c d e f]: screen x = a*tx + b*ty + c, y = d*tx + e*ty + f,
    with touch and screen both scaled to 0..1."""
    a = np.array([[tx, ty, 1.0] for tx, ty in touched])
    if np.linalg.matrix_rank(a) < 3:
        raise ValueError("the touches were all in the same place")
    row_x = np.linalg.lstsq(a, np.array([t[0] for t in targets]), rcond=None)[0]
    row_y = np.linalg.lstsq(a, np.array([t[1] for t in targets]), rcond=None)[0]
    return [float(v) for v in (*row_x, *row_y)]


def apply(matrix: Sequence[float], p: tuple[float, float]) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return a * p[0] + b * p[1] + c, d * p[0] + e * p[1] + f


def calibrate(points: Sequence[tuple[float, float]], xr: tuple[int, int],
              yr: tuple[int, int]) -> tuple[list[float], float]:
    """The matrix from the four corner crosses, and how far off it puts the middle one (0..1)."""
    touched = [normalise(p, xr, yr) for p in points]
    matrix = fit_matrix(touched[:4], TARGETS[:4])
    cx, cy = apply(matrix, touched[4])
    return matrix, max(abs(cx - TARGETS[4][0]), abs(cy - TARGETS[4][1]))


def rule_text(name: str, matrix: Sequence[float]) -> str:
    values = " ".join(f"{v:.5f}" for v in matrix)
    return (f"# LittleBot touch calibration (scripts/calibrate_touch.py, {time.strftime('%Y-%m-%d %H:%M')}).\n"
            "# Delete this file and reboot to undo.\n"
            f'SUBSYSTEM=="input", KERNEL=="event*", ATTRS{{name}}=="{name}", '
            f'ENV{{LIBINPUT_CALIBRATION_MATRIX}}="{values}"\n')


def _desktop_env() -> None:
    """Let SDL reach the desktop from an SSH session, like scripts/systemd/littlebot-launch.sh."""
    run_dir = os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if not os.environ.get("WAYLAND_DISPLAY"):
        socks = sorted(Path(run_dir).glob("wayland-[0-9]*"))
        socks = [s for s in socks if not s.name.endswith(".lock")]
        if socks:
            os.environ["WAYLAND_DISPLAY"] = socks[0].name
        elif not os.environ.get("DISPLAY") and Path("/tmp/.X11-unix/X0").exists():
            os.environ["DISPLAY"] = ":0"


def _littlebot(action: str) -> bool:
    try:
        if action == "active":
            out = subprocess.run(["systemctl", "--user", "is-active", "littlebot"], capture_output=True, text=True)
            return out.stdout.strip() == "active"
        return subprocess.run(["systemctl", "--user", action, "littlebot"], check=False).returncode == 0
    except FileNotFoundError:
        return False


def _write_rule(text: str) -> bool:
    print(f"Saving {RULE_PATH} (sudo may ask for your password)...")
    ok = subprocess.run(["sudo", "tee", str(RULE_PATH)], input=text, text=True, stdout=subprocess.DEVNULL).returncode == 0
    if ok:
        subprocess.run(["sudo", "udevadm", "control", "--reload"], check=False)
    return ok


def collect(reader: "queue.Queue[tuple[float, list[tuple[int, int]]]]") -> list[tuple[float, float]] | None:
    """Show each cross and wait for a press on it; None if Esc was pressed or the window closed."""
    import pygame as pg

    pg.init()
    screen = pg.display.set_mode((0, 0), pg.FULLSCREEN)
    pg.mouse.set_visible(False)
    w, h = screen.get_size()
    while not reader.empty():  # touches from before the crosses were shown don't count
        reader.get_nowait()
    big, small = pg.font.Font(None, max(24, h // 12)), pg.font.Font(None, max(18, h // 18))
    points: list[tuple[float, float]] = []
    last_press = 0.0
    clock = pg.time.Clock()
    try:
        while len(points) < len(TARGETS):
            for ev in pg.event.get():
                if ev.type == pg.QUIT or (ev.type == pg.KEYDOWN and ev.key == pg.K_ESCAPE):
                    return None
            try:
                at, samples = reader.get_nowait()
            except queue.Empty:
                pass
            else:
                point = press_point(samples)
                if point is not None and at - last_press > PRESS_GAP_S:
                    points.append(point)
                    last_press = at
            screen.fill((255, 214, 228))
            if len(points) < len(TARGETS):
                tx, ty = TARGETS[len(points)]
                cx, cy, r = int(tx * w), int(ty * h), max(12, min(w, h) // 14)
                pg.draw.circle(screen, (60, 40, 70), (cx, cy), r, 3)
                pg.draw.line(screen, (60, 40, 70), (cx - 2 * r, cy), (cx + 2 * r, cy), 3)
                pg.draw.line(screen, (60, 40, 70), (cx, cy - 2 * r), (cx, cy + 2 * r), 3)
                msg = big.render(f"Touch the centre of the cross ({len(points) + 1} of {len(TARGETS)})", True,
                                 (60, 40, 70))
                screen.blit(msg, msg.get_rect(center=(w // 2, h // 2 + (h // 5 if ty < 0.7 else -h // 5))))
                hint = small.render("Hold it for a moment. Esc on a keyboard stops.", True, (110, 80, 120))
                screen.blit(hint, hint.get_rect(center=(w // 2, h // 2 + (h // 5 if ty < 0.7 else -h // 5) + h // 12)))
            pg.display.flip()
            clock.tick(30)
        screen.fill((255, 214, 228))
        done = big.render("Thank you!", True, (60, 40, 70))
        screen.blit(done, done.get_rect(center=(w // 2, h // 2)))
        pg.display.flip()
        time.sleep(1.0)
        return points
    finally:
        pg.quit()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="remove the calibration (then reboot)")
    ap.add_argument("--dry-run", action="store_true", help="only print the calibration, don't save it")
    args = ap.parse_args(argv)

    if args.reset:
        if not RULE_PATH.exists():
            print("There is no touch calibration to remove.")
            return 0
        subprocess.run(["sudo", "rm", "-f", str(RULE_PATH)], check=False)
        subprocess.run(["sudo", "udevadm", "control", "--reload"], check=False)
        print("Calibration removed. Reboot to use the screen's own settings again: sudo reboot")
        return 0

    found = find_touch_device(Path("/proc/bus/input/devices").read_text(errors="replace"))
    if found is None:
        print("No touch screen found. Set up the touch layer first: docs/DISPLAY_SETUP.md, section 5.")
        return 1
    name, event = found
    try:
        fd = os.open(f"/dev/input/{event}", os.O_RDONLY)
    except PermissionError:
        print(f"Can't read /dev/input/{event}. Run: sudo usermod -aG input $USER, log out and in, and try again.")
        return 1
    xr, yr = abs_range(fd, ABS_X), abs_range(fd, ABS_Y)
    print(f"Touch screen: {name} (/dev/input/{event}, x {xr[0]}-{xr[1]}, y {yr[0]}-{yr[1]})")

    reader: queue.Queue[tuple[float, list[tuple[int, int]]]] = queue.Queue()

    def read() -> None:
        with os.fdopen(fd, "rb", buffering=0) as stream:
            for samples in presses(stream):
                reader.put((time.monotonic(), samples))

    threading.Thread(target=read, name="touch-reader", daemon=True).start()
    was_running = _littlebot("active")
    if was_running:
        print("Stopping LittleBot while the crosses are shown...")
        _littlebot("stop")
    try:
        _desktop_env()
        points = collect(reader)
    finally:
        if was_running:
            print("Starting LittleBot again.")
            _littlebot("start")
    if points is None:
        print("Stopped; nothing was changed.")
        return 1

    for (tx, ty), raw in zip(TARGETS, points):
        print(f"   cross at {tx:.0%} across, {ty:.0%} down: raw x {raw[0]:.0f}, y {raw[1]:.0f}")
    try:
        matrix, error = calibrate(points, xr, yr)
    except ValueError as e:
        print(f"That didn't work: {e}. Try again, touching each cross firmly.")
        return 1
    print(f"Check: the middle cross would land {error:.1%} of the screen away from where you touched.")
    if error > MAX_CHECK_ERROR:
        print("That is too far off (a touch probably slipped). Nothing was changed; please run it again.")
        return 1
    text = rule_text(name, matrix)
    if args.dry_run:
        print(text, end="")
        return 0
    if not _write_rule(text):
        print("Couldn't save the calibration. Here it is to save by hand:\n" + text)
        return 1
    print("Saved. Reboot so the screen uses it:  sudo reboot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
