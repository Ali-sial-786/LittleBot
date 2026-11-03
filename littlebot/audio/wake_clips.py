"""The family's own "hey little bot" clips, for the next wake word training (docs/WAKE_TRAINING.md).

A grown-up records themselves on a phone or PC saying the phrase over and over, with or without breaks, and uploads
the file on the dashboard (``/api/wake/clips``). It is decoded (any format PyAV reads: m4a, mp3, wav, ogg, webm ...)
to 16 kHz mono, and Vosk, held to the wake phrases, marks each word. Every "hey little bot" is cut out, a little
before "hey" to a little after "bot" (never into the next one), and kept as its own WAV in
``data/wake_clips/<name>/``. Nothing else of the recording is kept, and nothing leaves the robot.

Run together, a free Vosk heard "the little not a little not" in 20 phrases and found none; held to the phrases it
found all 20 (espeak, on the robot, 2026-11-03).
"""

from __future__ import annotations

import io
import json
import re
import shutil
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

RATE = 16000
GRAMMAR = ["hey little bot", "hey lil bot", "[unk]"]
MAX_SECONDS = 15 * 60  # one recording at most this long (a few minutes is plenty)
PAD_S = 0.25  # kept before "hey" and after "bot"
MIN_CONF = 0.35  # a phrase with a word Vosk was less sure of than this is left out (a laugh, a cough)
MAX_GAP_S = 0.8  # "hey" ... "little" ... "bot" further apart than this are not one phrase
MAX_NAMES = 12
_NAME = re.compile(r"[^a-z0-9]+")


class ClipError(ValueError):
    """The recording can't be used (the message says why, for the page)."""


def clips_dir(config: Any) -> Path:
    return Path(config.data_path) / "wake_clips"


def slug(name: str) -> str:
    s = _NAME.sub("-", str(name or "").strip().lower()).strip("-")[:32]
    if not s:
        raise ClipError("Give the name of who is speaking.")
    return s


def decode(data: bytes) -> np.ndarray:
    """``data`` (an audio file) as 16 kHz mono int16."""
    import av  # noqa: PLC0415

    try:
        container = av.open(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001 - PyAV raises its own errors for every bad file
        raise ClipError("That file isn't a recording LittleBot can read.") from e
    parts: list[np.ndarray] = []
    total = 0
    try:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ClipError("That file has no sound in it.")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                a = out.to_ndarray().reshape(-1).astype(np.int16)
                parts.append(a)
                total += len(a)
                if total > MAX_SECONDS * RATE:
                    raise ClipError(f"That recording is longer than {MAX_SECONDS // 60} minutes: record a shorter one.")
        for out in resampler.resample(None):
            parts.append(out.to_ndarray().reshape(-1).astype(np.int16))
    except ClipError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ClipError("That recording couldn't be read to the end.") from e
    finally:
        container.close()
    pcm = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
    if len(pcm) < RATE:
        raise ClipError("That recording is too short.")
    return pcm


def find_phrases(pcm: np.ndarray, model: Any) -> list[tuple[float, float]]:
    """(start, end) seconds of every "hey little bot" Vosk marks in ``pcm``."""
    import vosk  # noqa: PLC0415 - optional dependency

    rec = vosk.KaldiRecognizer(model, float(RATE), json.dumps(GRAMMAR))
    rec.SetWords(True)
    words: list[dict[str, Any]] = []
    raw = pcm.astype(np.int16).tobytes()
    step = RATE  # half a second of 16-bit samples
    for i in range(0, len(raw), step):
        if rec.AcceptWaveform(raw[i:i + step]):
            words += json.loads(rec.Result() or "{}").get("result", [])
    words += json.loads(rec.FinalResult() or "{}").get("result", [])
    found = []
    for i in range(len(words) - 2):
        hey, little, bot = words[i:i + 3]
        if (hey.get("word"), little.get("word"), bot.get("word")) not in (("hey", "little", "bot"), ("hey", "lil", "bot")):
            continue
        if min(float(w.get("conf", 1.0)) for w in (hey, little, bot)) < MIN_CONF:
            continue
        if float(little["start"]) - float(hey["end"]) > MAX_GAP_S or float(bot["start"]) - float(little["end"]) > MAX_GAP_S:
            continue
        found.append((float(hey["start"]), float(bot["end"])))
    return found


def cut(pcm: np.ndarray, phrases: list[tuple[float, float]]) -> list[np.ndarray]:
    """Each phrase with PAD_S around it, never past halfway to its neighbours."""
    clips = []
    for n, (start, end) in enumerate(phrases):
        lo = max(0.0, start - PAD_S)
        hi = min(len(pcm) / RATE, end + PAD_S)
        if n > 0:
            lo = max(lo, (phrases[n - 1][1] + start) / 2)
        if n + 1 < len(phrases):
            hi = min(hi, (end + phrases[n + 1][0]) / 2)
        clips.append(pcm[int(lo * RATE):int(hi * RATE)])
    return clips


def _write(path: Path, pcm: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.astype(np.int16).tobytes())


def add_recording(config: Any, name: str, data: bytes, model: Any, *, now: float | None = None) -> dict[str, Any]:
    """Cut ``data`` (one recording of ``name``) into clips and keep them. {name, found, seconds, clips (all of theirs)}."""
    who = slug(name)
    root = clips_dir(config)
    if not (root / who).is_dir() and len(summary(config)) >= MAX_NAMES:
        raise ClipError(f"Clips are kept for {MAX_NAMES} people at most: delete someone's first.")
    pcm = decode(data)
    clips = cut(pcm, find_phrases(pcm, model))
    if not clips:
        raise ClipError("No “hey little bot” was heard in that recording. Say it clearly, close to the phone.")
    folder = root / who
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    for n, clip in enumerate(clips, 1):
        _write(folder / f"{stamp}-{n:03d}.wav", clip)
    return {"name": who, "found": len(clips), "seconds": round(len(pcm) / RATE, 1),
            "clips": len(list(folder.glob("*.wav")))}


def summary(config: Any) -> list[dict[str, Any]]:
    """[{name, clips, recordings}] for everyone who has clips, by name."""
    root = clips_dir(config)
    if not root.is_dir():
        return []
    out = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        files = list(folder.glob("*.wav"))
        if files:
            out.append({"name": folder.name, "clips": len(files),
                        "recordings": len({f.name.rsplit("-", 1)[0] for f in files})})
    return out


def delete(config: Any, name: str) -> bool:
    """Every clip of ``name``. False: there were none."""
    folder = clips_dir(config) / slug(name)
    if not folder.is_dir():
        return False
    shutil.rmtree(folder)
    return True


__all__ = ["ClipError", "add_recording", "clips_dir", "cut", "decode", "delete", "find_phrases", "slug", "summary"]
