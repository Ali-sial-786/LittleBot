"""The family's own "hey little bot" clips: a recording decoded, each phrase found and cut, kept per person."""

from __future__ import annotations

import io
import json
import sys
import types
import wave
from typing import Any

import numpy as np
import pytest

from littlebot.audio import wake_clips
from littlebot.testing import make_test_context


def _wav(seconds: float, rate: int = 44100, channels: int = 2) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    tone = (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.repeat(tone, channels).tobytes())
    return buf.getvalue()


def _word(word: str, start: float, end: float, conf: float = 1.0) -> dict[str, Any]:
    return {"word": word, "start": start, "end": end, "conf": conf}


@pytest.fixture
def fake_vosk(monkeypatch):
    """A Vosk whose recogniser gives back the words a test set (and records the grammar it was held to)."""
    seen: dict[str, Any] = {}

    class Recognizer:
        def __init__(self, model: Any, rate: float, grammar: str) -> None:
            seen["grammar"] = json.loads(grammar)
            self.calls = 0

        def SetWords(self, on: bool) -> None:  # noqa: N802 - Vosk's own name
            pass

        def AcceptWaveform(self, data: bytes) -> bool:  # noqa: N802
            self.calls += 1
            return self.calls == 1

        def Result(self) -> str:  # noqa: N802
            return json.dumps({"result": seen["words"][:3]})

        def FinalResult(self) -> str:  # noqa: N802
            return json.dumps({"result": seen["words"][3:]})

    monkeypatch.setitem(sys.modules, "vosk", types.SimpleNamespace(KaldiRecognizer=Recognizer))
    return seen


def test_a_phone_recording_is_decoded_to_16k_mono() -> None:
    pcm = wake_clips.decode(_wav(2.0))
    assert pcm.dtype == np.int16 and abs(len(pcm) - 2 * wake_clips.RATE) < 400


def test_what_isnt_a_recording_is_refused() -> None:
    with pytest.raises(wake_clips.ClipError, match="isn't a recording"):
        wake_clips.decode(b"not audio at all" * 100)
    with pytest.raises(wake_clips.ClipError, match="too short"):
        wake_clips.decode(_wav(0.3))
    with pytest.raises(wake_clips.ClipError, match="name"):
        wake_clips.slug("  !!  ")


def test_every_phrase_is_found_even_run_together(fake_vosk) -> None:
    fake_vosk["words"] = [_word("hey", 0.1, 0.3), _word("little", 0.3, 0.6), _word("bot", 0.6, 0.9),
                          _word("hey", 0.9, 1.1), _word("lil", 1.1, 1.3), _word("bot", 1.3, 1.6),
                          _word("hey", 2.0, 2.2), _word("little", 2.2, 2.5), _word("bot", 2.5, 2.8, conf=0.2),
                          _word("hey", 3.0, 3.2), _word("bot", 3.3, 3.5)]
    found = wake_clips.find_phrases(np.zeros(4 * wake_clips.RATE, dtype=np.int16), object())
    assert found == [(0.1, 0.9), (0.9, 1.6)]  # not the unsure one, nor "hey bot"
    assert fake_vosk["grammar"] == wake_clips.GRAMMAR


def test_clips_never_reach_into_their_neighbours() -> None:
    pcm = np.arange(4 * wake_clips.RATE, dtype=np.int64).astype(np.int16)
    a, b = wake_clips.cut(pcm, [(0.1, 0.9), (0.9, 1.6)])
    assert len(a) == int(0.9 * wake_clips.RATE)  # from 0 (not before the start) to halfway (0.9)
    assert len(b) == int((1.6 + wake_clips.PAD_S - 0.9) * wake_clips.RATE)


def test_clips_are_kept_per_person_and_deleted(tmp_path, monkeypatch) -> None:
    ctx = make_test_context(data_dir=str(tmp_path))
    monkeypatch.setattr(wake_clips, "find_phrases", lambda pcm, model: [(0.2, 0.8), (1.0, 1.6), (1.8, 2.4)])
    added = wake_clips.add_recording(ctx.config, "Ali", _wav(3.0), object(), now=0)
    assert added == {"name": "ali", "found": 3, "seconds": 3.0, "clips": 3}
    files = sorted((tmp_path / "wake_clips" / "ali").glob("*.wav"))
    assert len(files) == 3
    with wave.open(str(files[0])) as w:
        assert (w.getnchannels(), w.getframerate()) == (1, wake_clips.RATE)
    wake_clips.add_recording(ctx.config, "ali", _wav(3.0), object(), now=100)
    assert wake_clips.summary(ctx.config) == [{"name": "ali", "clips": 6, "recordings": 2}]
    monkeypatch.setattr(wake_clips, "find_phrases", lambda pcm, model: [])
    with pytest.raises(wake_clips.ClipError, match="No “hey little bot”"):
        wake_clips.add_recording(ctx.config, "Sara", _wav(3.0), object())
    assert wake_clips.delete(ctx.config, "Ali") and wake_clips.summary(ctx.config) == []
    assert not wake_clips.delete(ctx.config, "Ali")
