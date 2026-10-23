"""scripts/set_giggle.py: any video/audio file becomes LittleBot's giggle."""

from __future__ import annotations

import fractions
import importlib.util
from pathlib import Path

import numpy as np
import pytest

av = pytest.importorskip("av")

spec = importlib.util.spec_from_file_location("set_giggle", Path(__file__).resolve().parents[2] / "scripts" / "set_giggle.py")
sg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sg)


def make_mp4(path: Path, sr: int = 48000) -> None:
    """1 s silence, 1.5 s of "giggle" (a pulsing tone), 1 s silence, as AAC in an MP4."""
    t = np.arange(int(1.5 * sr)) / sr
    giggle = 0.4 * np.sin(2 * np.pi * 500 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 8 * t))
    x = np.concatenate([np.zeros(sr), giggle, np.zeros(sr)]).astype(np.float32)
    with av.open(str(path), "w") as out:
        st = out.add_stream("aac", rate=sr)
        st.layout = "mono"
        for i in range(0, len(x), 1024):
            chunk = x[i:i + 1024]
            frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="flt", layout="mono")
            frame.sample_rate, frame.pts, frame.time_base = sr, i, fractions.Fraction(1, sr)
            for pkt in st.encode(frame):
                out.mux(pkt)
        for pkt in st.encode(None):
            out.mux(pkt)


def test_a_video_file_becomes_the_giggle(tmp_path):
    src = tmp_path / "giggle.mp4"
    make_mp4(src)
    out = tmp_path / "voice"
    assert sg.main([str(src), "--out", str(out)]) == 0
    from littlebot.audio import dsp

    y, sr = dsp.read_wav(out / "giggle_1.wav")
    assert sr == sg.RATE and 1.4 <= len(y) / sr <= 1.8  # the silence around it is gone
    assert 0.5 < float(np.abs(y).max()) <= 0.61


def test_start_end_and_reset(tmp_path):
    src = tmp_path / "giggle.mp4"
    make_mp4(src)
    out = tmp_path / "voice"
    assert sg.main([str(src), "--start", "1.2", "--end", "2.0", "--out", str(out)]) == 0
    from littlebot.audio import dsp

    y, sr = dsp.read_wav(out / "giggle_1.wav")
    assert len(y) / sr <= 0.85
    assert sg.main(["--reset", "--out", str(out)]) == 0
    assert not list(out.glob("giggle_*.wav"))
