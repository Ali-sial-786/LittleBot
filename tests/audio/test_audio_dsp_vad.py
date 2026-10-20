"""Signal helpers (WAV, resampling, envelope), text cleaning and the energy VAD."""

from __future__ import annotations

import struct

import numpy as np
import pytest

from littlebot.audio import dsp
from littlebot.audio.tts import clamp_rate, clean_text, estimate_seconds
from littlebot.audio.vad import EnergyVAD, VADState


def _tone(freq: float, secs: float, sr: int, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(secs * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _dominant(x: np.ndarray, sr: int) -> float:
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1.0 / sr)[int(np.argmax(spec))])


# ------------------------------------------------------------------ WAV
def test_wav_roundtrip_in_memory():
    x = _tone(440, 0.2, 22050)
    data = dsp.encode_wav(x, 22050)
    y, sr = dsp.decode_wav(data)
    assert sr == 22050 and len(y) == len(x)
    assert np.max(np.abs(x - y)) < 1e-4


def test_decode_streaming_header_stereo_and_formats():
    pcm = (np.array([[1000, -1000], [2000, -2000], [3000, -3000]], dtype="<i2")).tobytes()
    fmt = struct.pack("<HHIIHH", 1, 2, 16000, 64000, 4, 16)
    data = b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
    data += b"data" + struct.pack("<I", 0xFFFFFFFF) + pcm  # espeak-ng / HTTP style unknown sizes
    y, sr = dsp.decode_wav(data)
    assert sr == 16000 and len(y) == 3 and np.allclose(y, 0.0, atol=1e-6)  # L/R cancel out in the mono mix
    # float32 WAV
    f = np.array([0.5, -0.25], dtype="<f4").tobytes()
    fmt = struct.pack("<HHIIHH", 3, 1, 8000, 32000, 4, 32)
    data = b"RIFF" + struct.pack("<I", 36 + len(f)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
    data += b"data" + struct.pack("<I", len(f)) + f
    y, sr = dsp.decode_wav(data)
    assert sr == 8000 and np.allclose(y, [0.5, -0.25])
    with pytest.raises(ValueError):
        dsp.decode_wav(b"not a wav file at all")


# ------------------------------------------------------------------ resampling
@pytest.mark.parametrize("src,dst", [(22050, 48000), (22050, 44100), (24000, 22050), (48000, 16000)])
def test_resample_keeps_pitch_and_duration(src, dst):
    x = _tone(440, 0.5, src)
    y = dsp.resample(x, src, dst)
    assert len(y) == pytest.approx(len(x) * dst / src, abs=1)
    assert _dominant(y, dst) == pytest.approx(440, abs=5)
    assert dsp.peak(y) == pytest.approx(0.5, abs=0.05)
    z = dsp.resample_linear(x, src, dst)
    assert _dominant(z, dst) == pytest.approx(440, abs=5)


@pytest.mark.parametrize("src", [48000, 44100, 16000])
def test_stream_resampler_is_continuous(src):
    x = _tone(300, 1.0, src, amp=0.3)
    rs = dsp.StreamResampler(src, 16000)
    out = np.concatenate([rs.process(x[i:i + 1536]) for i in range(0, len(x), 1536)])
    assert len(out) == pytest.approx(16000, abs=4)
    assert _dominant(out, 16000) == pytest.approx(300, abs=3)
    # no clicks at chunk boundaries: a smooth 300 Hz tone never jumps more than its slope allows
    assert float(np.max(np.abs(np.diff(out[200:])))) < 2 * np.pi * 300 / 16000 * 0.3 * 1.3


def test_level_envelope():
    sr = 22050
    assert not np.any(dsp.level_envelope(np.zeros(sr, np.float32), sr))
    x = np.concatenate([_tone(200, 0.5, sr), np.zeros(sr // 2, np.float32), _tone(200, 0.5, sr, 0.1)])
    lv = dsp.level_envelope(x, sr, hz=16)
    assert len(lv) == pytest.approx(1.5 * 16, abs=1)
    assert lv.min() >= 0.0 and lv.max() <= 1.0
    assert lv[3] > 0.9 and lv[12] == 0.0 and 0.0 < lv[-4] < lv[3]


def test_fade_and_prepare_speech():
    x = np.ones(2205, np.float32) * 0.9
    y = dsp.fade(x, 22050, 0.01, 0.01)
    assert y[0] == 0.0 and abs(y[-1]) < 1e-6 and y[1100] == pytest.approx(0.9)
    s = dsp.prepare_speech(_tone(200, 0.5, 22050, 0.95), 22050, target_peak=0.6)
    assert dsp.peak(s) == pytest.approx(0.6, abs=0.01) and s[0] == 0.0


# ------------------------------------------------------------------ text
def test_clean_text():
    assert clean_text("  Hello\n\tthere  🙂🐶 friend! ") == "Hello there friend!"
    assert clean_text("bell\x07 ring​") == "bell ring"
    assert clean_text(None) == ""
    long = " ".join(["word"] * 200)
    out = clean_text(long, max_chars=50)
    assert len(out) <= 51 and out.endswith(".")


def test_rate_clamping_and_estimates():
    assert clamp_rate(5) == 1.6 and clamp_rate(0.1) == 0.5 and clamp_rate(None, 0.9) == 0.9
    assert clamp_rate("x", 1.0) == 1.0 and clamp_rate(float("nan"), 0.8) == 0.8
    assert estimate_seconds("one two three four five", 1.0) == pytest.approx(2.0)
    assert estimate_seconds("hi", 1.0) == 0.5


# ------------------------------------------------------------------ VAD
SR = 16000


def _feed(vad: EnergyVAD, x: np.ndarray, chunk: int = 512) -> VADState:
    state = vad.state
    for i in range(0, len(x), chunk):
        state = vad.process(x[i:i + chunk])
        if state == VADState.DONE:
            break
    return state


def _noise(secs: float, level: float = 0.002) -> np.ndarray:
    return (np.random.default_rng(3).standard_normal(int(secs * SR)) * level).astype(np.float32)


def test_vad_ignores_quiet_room():
    vad = EnergyVAD(SR, threshold=0.015, silence_seconds=0.5)
    assert _feed(vad, _noise(3.0)) == VADState.WAITING
    assert not vad.started and len(vad.audio()) == 0


def test_vad_captures_utterance_until_silence():
    vad = EnergyVAD(SR, threshold=0.015, silence_seconds=0.6, pre_roll_seconds=0.3)
    x = np.concatenate([_noise(0.5), _tone(220, 1.0, SR, 0.2), _noise(1.0)])
    assert _feed(vad, x) == VADState.DONE and vad.reason == "silence"
    audio = vad.audio()
    assert 1.0 <= len(audio) / SR <= 1.0 + 0.35 + 0.3 + 0.05  # speech + pre-roll + trimmed tail
    vad.discard()
    assert len(vad.audio()) == 0


def test_vad_caps_long_recordings():
    vad = EnergyVAD(SR, threshold=0.015, silence_seconds=0.5, max_record_seconds=1.0)
    assert _feed(vad, _tone(220, 3.0, SR, 0.2)) == VADState.DONE
    assert vad.reason == "max_length" and len(vad.audio()) / SR == pytest.approx(1.0, abs=0.1)


def test_vad_discards_short_bumps():
    vad = EnergyVAD(SR, threshold=0.015, silence_seconds=0.4, min_speech_seconds=0.2)
    click = np.concatenate([_tone(1000, 0.1, SR, 0.5), _noise(0.8)])
    assert _feed(vad, click) == VADState.WAITING  # treated as a bang, not speech


def test_vad_adapts_to_background_noise():
    vad = EnergyVAD(SR, threshold=0.005, silence_seconds=0.4, noise_factor=3.0)
    _feed(vad, _noise(2.0, level=0.004))
    assert vad.effective_threshold > 0.01  # louder room -> higher bar
    assert not vad.started
