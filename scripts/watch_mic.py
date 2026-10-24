#!/usr/bin/env python3
"""Watch whether PipeWire's echo canceller passes your voice through (scripts/setup_echo_cancel.sh).

Every few seconds it records the same 2 s three ways at once with ``pw-record``: the USB microphone
itself, the echo-cancelled microphone LittleBot listens to (``littlebot_ec_mic``) and what the
echo-cancelled speaker plays (``littlebot_ec_speaker``). It prints a line when someone was loud at
the USB microphone, LittleBot wasn't playing anything, and yet the echo-cancelled microphone was
nearly silent: the canceller muted them, and to LittleBot the microphone looks dead. The recordings
go to a temporary folder and are deleted at once; only the loudness numbers are printed.

    python3 scripts/watch_mic.py                                    # until Ctrl-C
    nohup python3 scripts/watch_mic.py >> ~/mic_watch.log 2>&1 &    # in the background (stop: pkill -f watch_mic.py)
    python3 scripts/watch_mic.py --all                              # a line for every check, not only problems

Standard library only (no venv needed). Needs PipeWire's pw-record and pw-cli.
"""

from __future__ import annotations

import argparse
import array
import contextlib
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

EC_MIC = "littlebot_ec_mic"
EC_SPEAKER = "littlebot_ec_speaker"
USB_MIC_HINT = "alsa_input.usb"
LOUD = 2000  # peak (of 32767) at the USB microphone: someone talking near it
MUTED_SHARE = 0.1  # the echo-cancelled microphone below this share of the USB microphone's peak: muted
SPEAKER_QUIET = 500  # the speaker's peak below this: LittleBot wasn't playing (so it isn't its own echo)


def muted(ec: int, raw: int, speaker: int) -> bool:
    """The canceller muted someone: loud at the USB microphone, nearly silent after it, nothing playing."""
    return raw >= LOUD and ec < raw * MUTED_SHARE and speaker < SPEAKER_QUIET


def peak(path: Path) -> int:
    """The loudest sample of a 16-bit WAV (0 for a missing, empty or broken one)."""
    try:
        with wave.open(str(path)) as w:
            a = array.array("h", w.readframes(w.getnframes()))
    except (OSError, wave.Error, EOFError):
        return 0
    return max(map(abs, a), default=0)


def usb_mic() -> str | None:
    """The USB microphone's PipeWire node name."""
    try:
        out = subprocess.run(["pw-cli", "ls", "Node"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if "node.name" in line and USB_MIC_HINT in line:
            return line.split("=", 1)[1].strip().strip('"')
    return None


def record(target: str, seconds: float, path: Path, *, sink: bool = False) -> subprocess.Popen[bytes]:
    cmd = ["pw-record", "--format", "s16", "--channels", "1", "--target", target]
    if sink:
        cmd += ["-P", "stream.capture.sink=true"]  # what the speaker plays, not a microphone
    return subprocess.Popen(["timeout", str(seconds), *cmd, str(path)], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def check(raw_mic: str, seconds: float) -> tuple[int, int, int]:
    with tempfile.TemporaryDirectory(prefix="littlebot-mic-") as tmp:
        files = [Path(tmp) / f for f in ("ec.wav", "usb.wav", "speaker.wav")]
        procs = [record(EC_MIC, seconds, files[0]), record(raw_mic, seconds, files[1]),
                 record(EC_SPEAKER, seconds, files[2], sink=True)]
        for p in procs:
            p.wait(timeout=seconds + 5)
        return peak(files[0]), peak(files[1]), peak(files[2])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seconds", type=float, default=2.0, help="each recording (default 2)")
    ap.add_argument("--every", type=float, default=5.0, help="start a check this often (default 5 s)")
    ap.add_argument("--all", action="store_true", help="a line for every check")
    args = ap.parse_args()
    if not shutil.which("pw-record"):
        print("pw-record not found (PipeWire's tools)", file=sys.stderr)
        return 2
    raw_mic = usb_mic()
    if raw_mic is None:
        print("no USB microphone in PipeWire (pw-cli ls Node)", file=sys.stderr)
        return 2
    print(f"{time.strftime('%H:%M:%S')} watching {raw_mic} -> {EC_MIC} (a line when your voice doesn't get "
          f"through{'; every check with --all' if args.all else ''})", flush=True)
    checks = problems = 0
    with contextlib.suppress(KeyboardInterrupt):
        while True:
            started = time.monotonic()
            ec, raw, spk = check(raw_mic, args.seconds)
            checks += 1
            bad = muted(ec, raw, spk)
            problems += bad
            if bad or args.all:
                what = "MUTED: your voice didn't get through the echo canceller" if bad else "ok"
                print(f"{time.strftime('%H:%M:%S')} usb mic {raw:5d}  echo-cancelled {ec:5d}  speaker {spk:5d}  "
                      f"{what}", flush=True)
            time.sleep(max(0.0, args.every - (time.monotonic() - started)))
    print(f"{checks} checks, {problems} muted", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
