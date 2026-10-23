#!/usr/bin/env python3
"""Make a recording LittleBot's giggle (any video or audio file: mp4, m4a, mp3, wav, ...).

    .venv/bin/python scripts/set_giggle.py ~/giggle.mp4                    # whole sound, silence trimmed
    .venv/bin/python scripts/set_giggle.py ~/giggle.mp4 --start 1.2 --end 3.5   # just that part (seconds)
    .venv/bin/python scripts/set_giggle.py --reset                         # back to the built-in giggles

The sound is taken from the file, trimmed of silence at both ends, faded in/out, levelled to
LittleBot's other sounds and saved as data/sounds/voice/giggle_1.wav (data/ is never touched by
updates). It replaces every giggle: tapping the face, games and activities. Restart LittleBot after:
systemctl --user restart littlebot
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

RATE = 22050
MAX_SECONDS = 6.0


def decode(path: Path) -> np.ndarray:
    """The file's sound as mono float32 at RATE (PyAV, which comes with the live-voice extra)."""
    try:
        import av
    except ImportError:
        raise SystemExit("PyAV is missing: .venv/bin/python -m pip install -e '.[live]'") from None
    with av.open(str(path)) as c:
        stream = next((s for s in c.streams if s.type == "audio"), None)
        if stream is None:
            raise SystemExit(f"{path} has no sound in it.")
        rs = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        parts = [f.to_ndarray().reshape(-1) for frame in c.decode(stream) for f in rs.resample(frame)]
    if not parts:
        raise SystemExit(f"{path} has no sound in it.")
    return np.concatenate(parts).astype(np.float32) / 32768.0


def tidy(x: np.ndarray, start: float | None, end: float | None, peak: float = 0.6) -> np.ndarray:
    if start is not None or end is not None:
        x = x[int((start or 0) * RATE): int(end * RATE) if end is not None else None]
    win = max(1, RATE // 50)
    env = np.sqrt(np.convolve(x.astype(np.float64) ** 2, np.ones(win) / win, mode="same"))
    loud = np.nonzero(env > max(0.003, 0.05 * float(env.max(initial=0))))[0]
    if not len(loud):
        raise SystemExit("That part is silent.")
    pad = int(0.05 * RATE)
    y = x[max(0, loud[0] - pad): loud[-1] + pad].astype(np.float64)
    if len(y) > MAX_SECONDS * RATE:
        print(f"Note: the sound is {len(y) / RATE:.1f} s long; keeping the first {MAX_SECONDS:g} s "
              "(use --start/--end to pick the part you want).")
        y = y[: int(MAX_SECONDS * RATE)]
    fade = min(len(y) // 4, int(0.03 * RATE))
    if fade:
        y[:fade] *= np.linspace(0, 1, fade)
        y[-fade:] *= np.linspace(1, 0, fade)
    return (y / max(1e-6, float(np.abs(y).max())) * peak).astype(np.float32)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", type=Path, help="video or audio file with the giggle")
    ap.add_argument("--start", type=float, default=None, help="start of the giggle in the file (seconds)")
    ap.add_argument("--end", type=float, default=None, help="end of the giggle in the file (seconds)")
    ap.add_argument("--reset", action="store_true", help="remove your giggle and use the built-in ones again")
    ap.add_argument("--out", type=Path, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    from littlebot.audio import dsp
    from littlebot.core.config import load_config

    out_dir = args.out or load_config(mode="pi").data_path / "sounds" / "voice"
    if args.reset:
        for old in out_dir.glob("giggle_*.wav") if out_dir.is_dir() else []:
            old.unlink()
        print("Back to the built-in giggles. Restart LittleBot: systemctl --user restart littlebot")
        return 0
    if args.file is None or not args.file.expanduser().is_file():
        ap.error("give the path of the video or audio file")
    clip = tidy(decode(args.file.expanduser()), args.start, args.end)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("giggle_*.wav"):
        old.unlink()
    path = out_dir / "giggle_1.wav"
    dsp.write_wav(path, clip, RATE)
    print(f"Saved {path} ({len(clip) / RATE:.1f} s). Restart LittleBot: systemctl --user restart littlebot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
