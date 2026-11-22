#!/usr/bin/env python3
"""Narration for the wiring video: each step's "say" through Piper TTS, then the timeline the page follows.

    PIPER=/path/to/piper VOICE=/path/to/en_US-lessac-medium.onnx python3 tts.py build
writes build/audio/sNN.wav, build/timeline.json ([{t0, dur, speech}] per step) and build/narration.wav (one track,
each step's speech starting LEAD s into its step). A step lasts its speech + TAIL s (at least MIN_STEP s).
Unchanged lines are not spoken again (build/audio/sNN.txt keeps the text each wav was made from).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEAD, TAIL, MIN_STEP = 0.6, 1.1, 4.0
RATE = 22050


def speak(text: str, out: Path) -> None:
    piper = os.environ.get("PIPER", "piper")
    voice = os.environ["VOICE"]
    subprocess.run([piper, "-m", voice, "-f", str(out), "--length-scale", os.environ.get("LENGTH", "1.06"),
                    "--sentence-silence", "0.35"], input=text.encode(), check=True, capture_output=True)


def read_wav(p: Path) -> tuple[bytes, int, int]:
    with wave.open(str(p)) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, p
        return w.readframes(w.getnframes()), w.getframerate(), w.getnframes()


def main(build: Path) -> None:
    steps = json.loads((HERE / "steps.json").read_text())
    audio = build / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    timeline, track, t = [], bytearray(), 0.0
    for i, s in enumerate(steps):
        wav, txt = audio / f"s{i:02d}.wav", audio / f"s{i:02d}.txt"
        if not (wav.exists() and txt.exists() and txt.read_text() == s["say"]):
            speak(s["say"], wav)
            txt.write_text(s["say"])
        frames, rate, n = read_wav(wav)
        assert rate == RATE, (wav, rate)
        speech = n / rate
        dur = max(MIN_STEP, LEAD + speech + TAIL)
        timeline.append({"t0": round(t, 3), "dur": round(dur, 3), "speech": round(speech, 3)})
        start = int(round((t + LEAD) * RATE))
        track.extend(b"\0\0" * (start - len(track) // 2))
        track.extend(frames)
        t += dur
    track.extend(b"\0\0" * (int(round(t * RATE)) - len(track) // 2))
    (build / "timeline.json").write_text(json.dumps(timeline))
    with wave.open(str(build / "narration.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(track))
    print(f"{len(steps)} steps, {t:.1f} s ({t / 60:.1f} min)")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "build").resolve())
