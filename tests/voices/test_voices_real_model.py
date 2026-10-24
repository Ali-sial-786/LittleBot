"""The real speaker model (sherpa-onnx + 3D-Speaker ERes2Net), when installed and downloaded; skipped otherwise.

Get it with: .venv/bin/python -m pip install -e '.[voices]' and .venv/bin/python scripts/download_models.py voices
This only checks that the model loads and gives sensible voiceprints quickly; telling real people apart was
measured by hand (docs/VOICES.md).
"""

from __future__ import annotations

import time

import numpy as np
import pytest

pytest.importorskip("sherpa_onnx")

from littlebot.core.paths import PROJECT_ROOT  # noqa: E402
from littlebot.voices.engine import EngineUnavailable, SpeakerEngine  # noqa: E402
from littlebot.voices.service import MODEL  # noqa: E402

MODEL_PATH = PROJECT_ROOT / MODEL
needs_model = pytest.mark.skipif(not MODEL_PATH.is_file(), reason="voice model not downloaded "
                                                                  "(scripts/download_models.py voices)")


def babble(seed: int, seconds: float = 2.0) -> np.ndarray:
    """Speech-like sound: a buzzing voice (pitch ~ 110-220 Hz) through moving vowel-like resonances."""
    rng = np.random.default_rng(seed)
    n = int(seconds * 16000)
    t = np.arange(n) / 16000
    pitch = 110 + 110 * rng.random() + 10 * np.sin(2 * np.pi * 3 * t)
    buzz = np.sign(np.sin(2 * np.pi * np.cumsum(pitch) / 16000))
    out = np.zeros(n)
    for f in rng.uniform(300, 3000, 3):
        out += np.sin(2 * np.pi * f * t) * buzz * (0.5 + 0.5 * np.sin(2 * np.pi * rng.uniform(2, 5) * t))
    return (0.1 * out / np.abs(out).max()).astype(np.float32)


@needs_model
def test_the_real_model_loads_and_gives_unit_voiceprints_quickly():
    engine = SpeakerEngine(MODEL_PATH, threads=2)
    assert engine.dim == 192 and engine.name == MODEL_PATH.name
    a = engine.embed(babble(1))
    assert a.shape == (192,) and np.linalg.norm(a) == pytest.approx(1.0, abs=1e-4)
    assert float(np.dot(a, engine.embed(babble(1)))) == pytest.approx(1.0, abs=1e-4)  # the same sound: the same
    t0 = time.perf_counter()
    for seed in range(5):
        engine.embed(babble(seed, 1.5))
    per = (time.perf_counter() - t0) / 5
    assert per < 1.0, f"{per * 1000:.0f} ms for 1.5 s of sound"  # ~50 ms on an x86 dev machine


def test_a_file_that_isnt_a_model_is_load_failed(tmp_path):
    bad = tmp_path / "broken.onnx"
    bad.write_bytes(b"not a model" * 100)
    with pytest.raises(EngineUnavailable) as e:
        SpeakerEngine(bad)
    assert e.value.reason == "load_failed"
    with pytest.raises(EngineUnavailable) as e:
        SpeakerEngine(tmp_path / "missing.onnx")
    assert e.value.reason == "no_model"
