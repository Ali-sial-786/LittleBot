"""Viseme (mouth shape) estimation from the sound being played: formants, hiss, hums, silence."""

from __future__ import annotations

import collections
import math
import time

import numpy as np
import pytest

from littlebot.audio import dsp

# Textbook formants (Hz, bandwidth Hz) for adult and child voices.
VOWELS = {
    "ah": [(750, 90), (1200, 110), (2500, 160)],
    "ee": [(280, 60), (2300, 100), (3000, 160)],
    "eh": [(550, 80), (1850, 100), (2500, 160)],
    "oo": [(320, 70), (800, 90), (2300, 160)],
    "oh": [(500, 80), (850, 90), (2400, 160)],
    "ah_child": [(1000, 100), (1400, 120), (2900, 160)],
    "ee_child": [(370, 70), (3000, 120), (3600, 160)],
    "oo_child": [(430, 80), (1150, 100), (3200, 160)],
}
EXPECTED = {"ah": "ah", "ee": "ee", "eh": "ee", "oo": "oo", "oh": "oo", "ah_child": "ah", "ee_child": "ee",
            "oo_child": "oo"}


def _resonate(x: np.ndarray, freq: float, bw: float, sr: int) -> np.ndarray:
    r = math.exp(-math.pi * bw / sr)
    c, r2 = 2 * r * math.cos(2 * math.pi * freq / sr), r * r
    y = np.zeros_like(x)
    y1 = y2 = 0.0
    for i, v in enumerate(x.tolist()):
        y0 = v + c * y1 - r2 * y2
        y[i], y2, y1 = y0, y1, y0
    return y


def vowel(formants, f0: float = 140.0, sr: int = 16000, seconds: float = 0.4, amp: float = 0.4,
          natural: bool = False) -> np.ndarray:
    """Cascade formant synthesis: a pulse train through resonators (``natural``: darker, like a real voice)."""
    n = int(sr * seconds)
    y = np.zeros(n)
    y[np.arange(0, n, sr / f0).astype(int)] = 1.0
    if natural:  # glottal source roll-off, ~-6 dB/octave above 300 Hz
        k, acc = 1 - 2 * math.pi * 300 / sr, 0.0
        for i, v in enumerate(y.tolist()):
            acc = v + k * acc
            y[i] = acc
    for f, bw in formants:
        y = _resonate(y, f, bw, sr)
    y -= y.mean()
    return (y / np.abs(y).max() * amp).astype(np.float32)


def _majority(shapes, trim: int = 3) -> str:
    return collections.Counter(shapes[trim:-trim] if len(shapes) > 2 * trim + 2 else shapes).most_common(1)[0][0]


def test_viseme_names_match_the_face():
    from littlebot.face.mouth import VISEMES

    assert dsp.VISEMES == VISEMES


def test_silence_and_near_silence_are_closed():
    for x in (np.zeros(16000, np.float32), np.random.default_rng(0).normal(0, 2e-4, 16000).astype(np.float32)):
        levels, shapes = dsp.viseme_track(x, 16000)
        assert len(shapes) == 50 and set(shapes) == {"closed"} and not levels.any()
    est = dsp.VisemeEstimator(16000)
    assert est.process(np.zeros(320, np.float32)) == (0.0, "closed")


@pytest.mark.parametrize("name", list(VOWELS))
@pytest.mark.parametrize("f0", [120, 220])
@pytest.mark.parametrize("natural", [False, True], ids=["bright", "natural"])
def test_vowels_get_their_mouth_shape(name, f0, natural):
    x = vowel(VOWELS[name], f0, natural=natural)
    levels, shapes = dsp.viseme_track(x, 16000)
    assert _majority(shapes) == EXPECTED[name], shapes
    assert levels[5:-5].min() > 0.5  # a steady vowel is loud throughout


@pytest.mark.parametrize("sr", [8000, 22050, 44100, 48000])
def test_any_sample_rate(sr):
    for name in ("ah", "ee", "oo"):
        _, shapes = dsp.viseme_track(vowel(VOWELS[name], 160, sr=sr), sr)
        assert _majority(shapes) == EXPECTED[name], (sr, name)


def test_lpc_finds_the_formants():
    for name in ("ah", "ee", "oo", "eh"):
        x = vowel(VOWELS[name], 130, sr=8000, natural=True)
        frames = x[: len(x) // 160 * 160].reshape(-1, 160)
        f1, f2, _ = dsp.lpc_formants(frames[5:-5], 8000)
        (t1, _), (t2, _) = VOWELS[name][:2]
        assert abs(float(np.median(f1)) - t1) < 0.2 * t1 + 40, name
        assert abs(float(np.median(f2)) - t2) < 0.15 * t2, name


def test_hissing_shows_the_teeth():
    rng = np.random.default_rng(1)
    for sr in (16000, 48000):
        noise = rng.normal(0, 0.2, sr).astype(np.float32)
        hiss = noise - np.convolve(noise, np.ones(4) / 4, mode="same").astype(np.float32)  # high-passed: "sss"
        _, shapes = dsp.viseme_track(hiss, sr)
        assert _majority(shapes) == "ee"


def test_a_hum_between_vowels_closes_the_lips():
    a = vowel(VOWELS["ah"], 130)
    hum = vowel([(250, 60), (1000, 400)], 130) * 0.3  # quiet, low, dull: "mmm"
    levels, shapes = dsp.viseme_track(np.concatenate([a, hum, a]), 16000)
    n = len(shapes) // 3
    assert _majority(shapes[:n]) == "ah" and _majority(shapes[2 * n:]) == "ah"
    assert _majority(shapes[n:2 * n]) == "closed"
    assert levels[n + 3:2 * n - 3].max() <= 0.25  # closed lips also for listeners that only read the level


def test_level_and_shape_do_not_depend_on_volume():
    x = np.concatenate([vowel(VOWELS["ah"], 150), np.zeros(3200, np.float32), vowel(VOWELS["ee"], 150)])
    quiet = dsp.viseme_track(x * 0.05, 16000)
    loud = dsp.viseme_track(x, 16000)
    assert quiet[1] == loud[1]
    assert np.allclose(quiet[0], loud[0], atol=0.02)


def test_track_timing():
    x = np.concatenate([np.zeros(8000, np.float32), vowel(VOWELS["ah"], 150, seconds=0.5), np.zeros(8000, np.float32)])
    levels, shapes = dsp.viseme_track(x, 16000)
    assert len(levels) == len(shapes) == 75  # one frame per 20 ms
    first = shapes.index("ah")
    assert 24 <= first <= 27  # the mouth opens when the sound starts (0.5 s), not before
    assert shapes[:24] == ("closed",) * 24 and shapes[-20:] == ("closed",) * 20
    assert dsp.viseme_track(np.zeros(0, np.float32), 16000)[1] == ()
    assert len(dsp.viseme_track(vowel(VOWELS["ah"], seconds=0.01), 16000)[1]) == 1


def test_streaming_matches_the_precomputed_track():
    sr = 48000
    parts = [vowel(VOWELS[n], 170, sr=sr, seconds=0.3, natural=True) for n in ("ah", "ee", "oo", "ah")]
    x = np.concatenate(parts)
    _, track = dsp.viseme_track(x, sr)
    est = dsp.VisemeEstimator(sr)
    hop = sr // 50
    stream = [est.process(x[i:i + hop])[1] for i in range(0, len(x), hop)]
    steady = [i for i in range(len(track)) if i % 15 not in (0, 1, 14)]  # skip each vowel's edges
    agree = sum(stream[i] == track[i] for i in steady) / len(steady)
    assert agree > 0.9
    assert [_majority(stream[k * 15:(k + 1) * 15]) for k in range(4)] == ["ah", "ee", "oo", "ah"]


def test_estimator_levels_follow_the_voice_not_the_volume():
    sr = 16000
    est = dsp.VisemeEstimator(sr)
    loud = vowel(VOWELS["ah"], 150, seconds=0.2, amp=0.05)  # a quiet voice still opens the mouth fully
    levels = [est.process(loud[i:i + 320])[0] for i in range(0, len(loud), 320)]
    assert max(levels) > 0.9
    assert est.process(np.zeros(320, np.float32)) == (0.0, "closed")  # silence closes at once
    before = (est.level, est.shape)
    assert est.process(np.ones(8, np.float32)) == before  # too short to judge: unchanged
    est.reset()
    assert est.ref == est.floor and est.shape == "closed"


def test_odd_block_sizes():
    for sr, block in ((22050, 1024), (44100, 882), (48000, 960), (16000, 256)):
        est = dsp.VisemeEstimator(sr)
        x = vowel(VOWELS["oo"], 150, sr=sr, seconds=0.3)
        shapes = [est.process(x[i:i + block])[1] for i in range(0, len(x) - block, block)]
        assert _majority(shapes, trim=1) == "oo", (sr, block, shapes)


def test_cheap_enough_for_the_pi():
    sr = 48000
    x = vowel(VOWELS["ah"], 150, sr=sr, seconds=1.0).astype(np.float64)
    est = dsp.VisemeEstimator(sr)
    t0 = time.perf_counter()
    for i in range(0, len(x), 960):
        est.process(x[i:i + 960])
    per_block = (time.perf_counter() - t0) / 50
    assert per_block < 0.004  # far below the 20 ms each block lasts (typically ~0.3 ms)
    t0 = time.perf_counter()
    dsp.viseme_track(np.tile(x.astype(np.float32), 5), sr)
    assert time.perf_counter() - t0 < 0.5  # 5 s of speech (typically ~20 ms)
