"""Sound effects, melody synthesis and the sound bank."""

from __future__ import annotations

import math
import wave

import numpy as np
import pytest

from littlebot.audio import dsp, sfx
from littlebot.core.interfaces import SOUND_NAMES
from littlebot.core.paths import SOUNDS_DIR

SR = sfx.SAMPLE_RATE


@pytest.fixture(scope="module")
def sounds() -> dict[str, np.ndarray]:
    return sfx.synthesize_all()


# ------------------------------------------------------------------ effects
def test_every_sound_name_is_synthesised(sounds):
    assert set(sounds) == set(SOUND_NAMES)
    for name, x in sounds.items():
        assert x.dtype == np.float32 and x.ndim == 1, name
        assert 0.05 <= len(x) / SR <= 2.0, f"{name}: {len(x) / SR:.2f}s"


def test_levels_are_gentle_and_never_clip(sounds):
    for name, x in sounds.items():
        pk = dsp.peak(x)
        assert pk <= sfx.TARGET_PEAK + 1e-3, f"{name} peak {dsp.dbfs(pk):.1f} dBFS"  # about -6 dBFS max
        assert pk >= 0.25, f"{name} too quiet ({dsp.dbfs(pk):.1f} dBFS)"
        assert dsp.rms(x) <= sfx.MAX_RMS + 1e-3, f"{name} too loud on average"
        assert np.all(np.abs(dsp.to_int16(x)) < 32767), f"{name} clips"


def test_no_startling_transients(sounds):
    for name, x in sounds.items():
        pk = dsp.peak(x)
        onset = float(np.max(np.abs(x[: int(0.002 * SR)])))
        assert onset <= 0.35 * pk, f"{name}: harsh onset ({onset / pk:.2f} of peak within 2 ms)"
        assert abs(float(x[-1])) < 0.01, f"{name}: does not fade out"
        assert abs(float(x[0])) < 1e-3, f"{name}: starts abruptly"


def test_soft_spectrum(sounds):
    for name, x in sounds.items():
        spec = np.abs(np.fft.rfft(x)) ** 2
        freqs = np.fft.rfftfreq(len(x), 1.0 / SR)
        hf = float(spec[freqs > 6000].sum() / spec.sum())
        assert hf < 0.02, f"{name}: {hf:.1%} of energy above 6 kHz"


def test_synthesis_is_deterministic():
    assert np.array_equal(sfx.synthesize("sparkle"), sfx.synthesize("sparkle"))
    assert np.array_equal(sfx.synthesize("whoosh"), sfx.synthesize("whoosh"))


def test_unknown_sound_name_raises():
    with pytest.raises(KeyError):
        sfx.synthesize("explosion")


def test_committed_wavs_exist_and_match(sounds):
    for name in SOUND_NAMES:
        path = SOUNDS_DIR / f"{name}.wav"
        assert path.is_file(), f"missing {path}; run scripts/generate_sounds.py"
        with wave.open(str(path), "rb") as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, SR)
        assert path.stat().st_size < 160_000, f"{name}.wav is too big"
        data, rate = dsp.read_wav(path)
        ref = dsp.as_mono_float(dsp.to_int16(sounds[name]))
        assert rate == SR and len(data) == len(ref)
        assert float(np.max(np.abs(data - ref))) <= 4 / 32768, f"{name}.wav is outdated; run scripts/generate_sounds.py"


# ------------------------------------------------------------------ notes / melodies
@pytest.mark.parametrize("note,midi", [
    ("C4", 60), ("A4", 69), ("F#4", 66), ("Bb3", 58), ("c#5", 73), ("Eb4", 63), ("B3", 59),
    ("C", 60), ("G♯4", 68), ("D♭5", 73), (" e5 ", 76), ("Cb4", 59), ("B#3", 60),
])
def test_note_parsing(note, midi):
    assert sfx.note_to_midi(note) == midi


@pytest.mark.parametrize("rest", ["R", "r", "rest", "REST", "-", "_"])
def test_rests(rest):
    assert sfx.note_to_midi(rest) is None
    assert sfx.parse_note(rest) is None


def test_note_frequencies():
    assert sfx.parse_note("A4") == pytest.approx(440.0)
    assert sfx.parse_note("A5") == pytest.approx(880.0)
    assert sfx.parse_note("C4") == pytest.approx(261.63, abs=0.01)


@pytest.mark.parametrize("bad", ["H4", "", "C##4", "X", "C10", "4C", "C4.5", "Cx4", "do", None, 5, "C-1"])
def test_invalid_notes_raise(bad):
    with pytest.raises(ValueError):
        sfx.note_to_midi(bad)


def test_render_melody_length_rests_and_level():
    x = sfx.render_melody([("C4", 1), ("R", 1), ("G4", 2)], tempo_bpm=120)
    expected = 4 * 0.5 + 0.22 + 0.05
    assert len(x) / SR == pytest.approx(expected, abs=0.02)
    assert dsp.peak(x) <= sfx.TARGET_PEAK + 1e-3
    # the rest (0.5 .. 1.0 s) is much quieter than the notes (only the release tail rings into it)
    rest = x[int(0.75 * SR): int(1.0 * SR)]
    note = x[int(0.05 * SR): int(0.3 * SR)]
    assert dsp.rms(rest) < 0.1 * dsp.rms(note)
    first = x[: int(0.002 * SR)]
    assert float(np.max(np.abs(first))) < 0.35 * dsp.peak(x)  # soft attack


def test_render_melody_accepts_lists_and_bare_strings():
    a = sfx.render_melody([["E4", 1], ["F#4", 0.5]], tempo_bpm=100)
    b = sfx.render_melody(["E4", ("F#4", 0.5)], tempo_bpm=100)
    assert np.allclose(a, b)


def test_rest_only_melody_is_silent():
    x = sfx.render_melody([("R", 2)], tempo_bpm=60)
    assert len(x) > SR and dsp.peak(x) == 0.0


@pytest.mark.parametrize("notes,tempo", [
    ([("H4", 1)], 100),
    ([("C4", 0)], 100),
    ([("C4", -1)], 100),
    ([("C4", "long")], 100),
    ([("C4", math.inf)], 100),
    ([("C4", 1, 2)], 100),
    ([], 100),
    ([("C4", 1)], 0),
    ([("C4", 1)], 1000),
    ([("C4", 1)], "fast"),
    ([("C8", 1)], 100),  # too shrill
    ([("C1", 1)], 100),  # too low for small speakers
    ([("C4", 16)] * 10, 60),  # 160 s: too long
])
def test_render_melody_rejects_invalid(notes, tempo):
    with pytest.raises(ValueError):
        sfx.render_melody(notes, tempo_bpm=tempo)


# ------------------------------------------------------------------ sound bank
def test_sound_bank_loads_committed_files():
    bank = sfx.SoundBank()
    samples, rate = bank.get("chime")
    assert rate == SR and len(samples) > SR
    assert bank.cached("chime") is not None


def test_sound_bank_synthesises_missing_builtins(tmp_path):
    bank = sfx.SoundBank(tmp_path)
    samples, rate = bank.get("pop")
    assert rate == SR and dsp.peak(samples) > 0.25
    assert not list(tmp_path.iterdir())  # nothing written at runtime


def test_sound_bank_custom_files_and_path_safety(tmp_path):
    (tmp_path / "songs").mkdir()
    dsp.write_wav(tmp_path / "songs" / "lullaby.wav", np.zeros(1000, np.float32) + 0.1, 16000)
    (tmp_path.parent / "secret.wav").write_bytes(dsp.encode_wav(np.zeros(10, np.float32), 8000))
    bank = sfx.SoundBank(tmp_path)
    assert bank.get("songs/lullaby")[1] == 16000
    assert bank.get("songs/lullaby.wav")[1] == 16000
    assert bank.get("../secret.wav") is None
    assert bank.get(str(tmp_path.parent / "secret.wav")) is None
    assert bank.get("nope") is None
    assert bank.get("") is None


def test_recorded_voice_clips_replace_a_built_in(tmp_path):
    import numpy as np

    from littlebot.audio import dsp
    from littlebot.audio.sfx import SoundBank

    (tmp_path / "voice").mkdir()
    for i, level in ((1, 0.2), (2, 0.4)):
        dsp.write_wav(tmp_path / "voice" / f"giggle_{i}.wav", np.full(2205, level, np.float32), 22050)
    bank = SoundBank(tmp_path)
    assert bank.voice_variants("giggle") == ["voice/giggle_1.wav", "voice/giggle_2.wav"]
    levels = {round(float(bank.get("giggle")[0].max()), 1) for _ in range(40)}
    assert levels == {0.2, 0.4}  # one of the clips, picked at random
    assert bank.voice_variants("chime") == [] and bank.get("chime") is not None  # others stay synthesised


def test_the_parents_own_recordings_win_over_shipped_clips(tmp_path):
    import numpy as np

    from littlebot.audio import dsp
    from littlebot.audio.sfx import SoundBank

    shipped, mine = tmp_path / "assets", tmp_path / "data"
    (shipped / "voice").mkdir(parents=True)
    dsp.write_wav(shipped / "voice" / "giggle_1.wav", np.full(2205, 0.2, np.float32), 22050)
    assert SoundBank(shipped, user_dir=mine).voice_variants("giggle") == ["voice/giggle_1.wav"]
    (mine / "voice").mkdir(parents=True)
    dsp.write_wav(mine / "voice" / "giggle_1.wav", np.full(2205, 0.4, np.float32), 22050)
    bank = SoundBank(shipped, user_dir=mine)
    assert round(float(bank.get("giggle")[0].max()), 1) == 0.4
