#!/usr/bin/env python3
"""Generate LittleBot's built-in sound effects into assets/sounds/<name>.wav.

Every sound is synthesised procedurally (numpy, see littlebot/audio/sfx.py):
soft, child-friendly timbres, >= 6 ms attacks (no startling clicks), peaks
about -6 dBFS and capped loudness. Output is deterministic (fixed seeds), mono,
16-bit, 22050 Hz, so re-running produces byte-identical, commit-ready files.

    .venv/bin/python scripts/generate_sounds.py               # (re)write all sounds
    .venv/bin/python scripts/generate_sounds.py --only chime pop
    .venv/bin/python scripts/generate_sounds.py --check       # verify files exist and are up to date
    .venv/bin/python scripts/generate_sounds.py --out /tmp/sounds
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from littlebot.audio import dsp, sfx  # noqa: E402
from littlebot.core.interfaces import SOUND_NAMES  # noqa: E402
from littlebot.core.paths import SOUNDS_DIR  # noqa: E402


def _compare(path: Path, samples) -> str:
    """'ok' if the file matches the synthesis (a few LSBs of float noise across CPUs are fine)."""
    if not path.is_file():
        return "missing"
    try:
        existing, rate = dsp.read_wav(path)
    except (OSError, ValueError):
        return "unreadable"
    if rate != sfx.SAMPLE_RATE or len(existing) != len(samples):
        return "outdated"
    diff = float(abs(existing - dsp.as_mono_float(dsp.to_int16(samples))).max()) if len(samples) else 0.0
    return "ok" if diff <= 4 / 32768 else "outdated"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=SOUNDS_DIR, help=f"output folder (default {SOUNDS_DIR})")
    ap.add_argument("--only", nargs="+", metavar="NAME", help="only these sounds")
    ap.add_argument("--check", action="store_true", help="do not write; exit 1 if any file is missing or differs")
    args = ap.parse_args(argv)

    names = args.only or list(SOUND_NAMES)
    unknown = [n for n in names if n not in SOUND_NAMES]
    if unknown:
        print(f"unknown sound(s): {', '.join(unknown)}; known: {', '.join(SOUND_NAMES)}", file=sys.stderr)
        return 2
    out_dir: Path = args.out
    if not args.check:
        out_dir.mkdir(parents=True, exist_ok=True)

    problems = 0
    total = 0
    print(f"{'sound':10s} {'secs':>5s} {'peak dBFS':>9s} {'rms dBFS':>8s} {'bytes':>7s}  status")
    for name in names:
        samples = sfx.synthesize(name)
        data = dsp.encode_wav(samples, sfx.SAMPLE_RATE)
        path = out_dir / f"{name}.wav"
        if args.check:
            status = _compare(path, samples)
            problems += status != "ok"
        else:
            path.write_bytes(data)
            status = "written"
        total += len(data)
        print(f"{name:10s} {len(samples) / sfx.SAMPLE_RATE:5.2f} {dsp.dbfs(dsp.peak(samples)):9.1f} "
              f"{dsp.dbfs(dsp.rms(samples)):8.1f} {len(data):7d}  {status}")
    print(f"{len(names)} sound(s), {total / 1024:.0f} KiB, folder {out_dir}")
    if args.check and problems:
        print(f"FAIL: {problems} sound file(s) missing or outdated - run scripts/generate_sounds.py")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
