"""LittleBot's own trained wake word model next to Vosk (littlebot/audio/oww.py, wake.oww_mode): the feature steps
with fake onnx sessions, shadow mode changing nothing, confirm mode, switching itself off, no words in what it
logs, and scripts/wake_shadow_report.py. No onnxruntime, no model files."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from test_wakeword import _listening, rig, until  # noqa: F401

from littlebot.audio import oww
from littlebot.audio.oww import WakeModelCheck, WakeScorer, call_outcome

ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------ the feature steps (fake sessions)
class FakeSessions:
    """Stand-ins for the three onnx models, recording the shapes and values they are given."""

    def __init__(self, mel_value: float = 8.0, score: float = 0.25) -> None:
        self.mel_value, self.score = mel_value, score
        self.mel_in: list[tuple[int, ...]] = []
        self.embed_in: list[np.ndarray] = []
        self.classify_in: list[np.ndarray] = []

    def melspec(self, x: np.ndarray) -> list[np.ndarray]:
        assert x.dtype == np.float32 and x.ndim == 2 and x.shape[0] == 1
        self.mel_in.append(x.shape)
        frames = math.ceil(x.shape[1] / 160 - 3)  # the real model: 8 frames for 1760 samples
        return [np.full((1, 1, frames, 32), self.mel_value, dtype=np.float32)]

    def embed(self, x: np.ndarray) -> list[np.ndarray]:
        assert x.dtype == np.float32 and x.shape[1:] == (76, 32, 1)
        self.embed_in.append(x)
        return [np.ones((x.shape[0], 1, 1, 96), dtype=np.float32)]

    def classify(self, x: np.ndarray) -> list[np.ndarray]:
        self.classify_in.append(x)
        return [np.array([[self.score]], dtype=np.float32)]

    def scorer(self) -> WakeScorer:
        return WakeScorer(self.melspec, self.embed, self.classify)


def test_the_feature_steps_match_openwakewords_shapes():
    f = FakeSessions()
    s = f.scorer()
    # at the start (and each reset): 4 s of quiet noise -> 397 mel frames -> 41 windows of 76, 8 apart
    assert f.mel_in == [(1, 64000)] and f.embed_in[0].shape == (41, 76, 32, 1)
    f.mel_in.clear(), f.embed_in.clear()
    assert s.process(np.zeros(1000, np.int16)) == []  # less than 80 ms: waits for more
    scores = s.process(np.zeros(1600, np.int16))  # 2600 samples: two whole 80 ms chunks
    assert len(scores) == 2 and scores == [0.0, 0.0]  # the first 5 scores are 0, as in openWakeWord
    # the first chunk alone (nothing before it yet), then each chunk with the 480 samples before it
    assert f.mel_in == [(1, 1280), (1, 1760)]
    assert [e.shape for e in f.embed_in] == [(1, 76, 32, 1)] * 2
    assert all(c.shape == (1, 16, 96) and c.dtype == np.float32 for c in f.classify_in)
    # mel / 10 + 2: the model's 8.0 is 2.8 in the window given to the embedding model
    assert np.allclose(f.embed_in[-1][0, -8:, :, 0], 2.8)
    assert np.allclose(f.embed_in[0][0, :70, :, 0], 1.0)  # openWakeWord's mel buffer starts as ones
    more = s.process(np.zeros(1280 * 6 - 2600 + 2560, np.int16))
    assert more[:3] == [0.0, 0.0, 0.0] and more[3:] == [0.25] * (len(more) - 3)


def test_sound_in_any_block_size_scores_the_same():
    rng = np.random.default_rng(3)
    audio = rng.integers(-3000, 3000, 16000 * 3).astype(np.int16)

    def run(block: int) -> list[float]:
        f = FakeSessions()
        f.melspec = lambda x: [np.full((1, 1, math.ceil(x.shape[1] / 160 - 3), 32), float(np.abs(x).mean()) / 100,
                                       dtype=np.float32)]
        f.classify = lambda x: [np.array([[float(x.mean())]], dtype=np.float32)]
        s = f.scorer()
        out: list[float] = []
        for i in range(0, len(audio), block):
            out += s.process(audio[i:i + block])
        return out

    assert run(1280) == run(1600) == run(500)


def test_how_a_chat_went():
    assert call_outcome(None) == "unknown"
    assert call_outcome({"connected": False}) == "not_connected"
    assert call_outcome({"connected": True, "heard_user": False, "turns": 0}) == "no_words"
    assert call_outcome({"connected": True, "heard_user": True, "turns": 1, "ended_reason": "asked to stop"}) \
        == "dismissed"
    assert call_outcome({"connected": True, "heard_user": True, "turns": 3, "ended_reason": "no follow-up"}) \
        == "request"
    assert call_outcome({"connected": True, "heard_user": True}) == "request"  # GPT-Live: no turns


# ------------------------------------------------------------------ the check, with a fake scorer
class LevelScorer:
    """A WakeScorer stand-in: every 80 ms chunk scores ``level``."""

    def __init__(self, level: float = 0.0) -> None:
        self.level = level
        self._rest = 0
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1
        self._rest = 0

    def process(self, pcm: np.ndarray) -> list[float]:
        n = self._rest + len(pcm)
        self._rest = n % 1280
        return [self.level] * (n // 1280)


def _journal(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


async def _wait(pred, timeout: float = 8.0) -> None:
    await until(pred, timeout)


async def _feed(check: WakeModelCheck, blocks: int) -> None:
    """Hand over ``blocks`` x 0.1 s and wait until it is scored (the fake scorer's level is read when it is)."""
    for _ in range(blocks):
        check.feed(np.zeros(1600, np.int16))
    await _wait(lambda: check._done >= check._fed)


async def test_a_vosk_wake_is_measured_and_a_peak_with_no_wake_is_logged(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(oww, "CLAIM_SECONDS", 0.2)
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    sc = LevelScorer(0.8)
    j = tmp_path / "shadow.jsonl"
    check = WakeModelCheck(tmp_path, journal=j, load=lambda _p: sc)
    check.start()
    try:
        await _wait(lambda: check.ready)
        assert "wake model: ready in" in caplog.text
        await _feed(check, 10)
        wid = check.vosk_wake(0.83, early=True)
        assert check.score_now(1.0) == pytest.approx(0.8)
        await _feed(check, 8)  # the sound just after the wake (the carry)
        await _wait(lambda: any(r["kind"] == "vosk_wake" for r in _journal(j)))
        rec = next(r for r in _journal(j) if r["kind"] == "vosk_wake")
        assert rec["id"] == wid and rec["vosk_conf"] == 0.83 and rec["model_score"] == pytest.approx(0.8)
        assert rec["early"] is True and rec["blocked"] is False and rec["mode"] == "shadow"
        assert "wake model: Vosk woke it (word confidence 0.83, early); the model's score: 0.80 at the wake" \
            in caplog.text
        # the model's peak there was that wake, not a model-only peak
        sc.level = 0.0
        check.reset()
        await _feed(check, 5)
        # a peak of its own, Vosk heard a wake phrase but wasn't sure
        check.vosk_unsure()
        sc.level = 0.66
        await _feed(check, 4)
        sc.level = 0.1
        await _feed(check, 3)
        await _wait(lambda: any(r["kind"] == "model_only" for r in _journal(j)))
        peaks = [r for r in _journal(j) if r["kind"] == "model_only"]
        assert len(peaks) == 1 and peaks[0]["model_score"] == pytest.approx(0.66) and peaks[0]["vosk_unsure"]
        assert "wake model: score 0.66 with no Vosk wake (Vosk heard a wake phrase but wasn't sure enough)" \
            in caplog.text
        check.outcome(wid, "request", turns=2, seconds=31.4)
        out = [r for r in _journal(j) if r["kind"] == "outcome"]
        assert out == [{"t": out[0]["t"], "kind": "outcome", "id": wid, "outcome": "request", "turns": 2,
                        "seconds": 31.4}]
        assert check.status()["vosk_wakes"] == 1 and check.status()["model_only"] == 1
    finally:
        check.stop()


async def test_missing_files_switch_it_off_with_one_warning(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    check = WakeModelCheck(tmp_path)  # the real loader: no model files here
    check.start()
    await _wait(lambda: check.failed is not None)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "wake model: switched off (could not load it" in warnings[0].getMessage()
    assert "hey_little_bot.onnx" in warnings[0].getMessage()
    check.feed(np.zeros(1600, np.int16))  # all of it does nothing now
    assert check.score_now(0.1) is None and check.vosk_wake(0.9) is None
    check.stop()


# ------------------------------------------------------------------ in the wake word
async def _rig_with_model(rig, tmp_path: Path, level: float, mode: str, **wake: Any) -> Any:
    r = await rig(start_wake=False, wake={"oww_mode": mode, "oww_threshold": 0.5, **wake})
    sc = LevelScorer(level)
    j = tmp_path / "shadow.jsonl"
    r.scorer, r.journal = sc, j
    r.wake._check_factory = lambda: WakeModelCheck(tmp_path, journal=j, mode=mode, load=lambda _p: sc)
    await r.wake.start()
    await until(lambda: r.wake._oww is not None and r.wake._oww.ready, what="the model check ready")
    await _listening(r)
    return r


@pytest.mark.parametrize("level", [0.0, 0.95])
async def test_shadow_mode_changes_nothing_about_waking(rig, tmp_path, caplog, level):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    r = await _rig_with_model(rig, tmp_path, level, "shadow")
    await asyncio.sleep(0.3)  # some sound scored
    r.rec.say("hey little bot what is the weather like", conf=0.72)
    await until(lambda: r.live.calls, what="live voice started")
    assert r.live.calls == [True] and r.wake.triggers == 1
    await until(lambda: any(x["kind"] == "vosk_wake" for x in _journal(r.journal)), what="the wake measured")
    rec = next(x for x in _journal(r.journal) if x["kind"] == "vosk_wake")
    assert rec["vosk_conf"] == 0.72 and rec["model_score"] == pytest.approx(level) and rec["mode"] == "shadow"
    await r.live.end()  # the chat ends: how it went goes to the journal
    await until(lambda: any(x["kind"] == "outcome" for x in _journal(r.journal)), what="the outcome")
    out = next(x for x in _journal(r.journal) if x["kind"] == "outcome")
    assert out["id"] == rec["id"] and out["outcome"] == "request"
    assert "(shadow: it changes nothing)" in caplog.text
    assert r.wake.status()["wake_model"]["vosk_wakes"] == 1
    # never what was said: not in the journal, not in the model's log lines
    model_lines = [x.getMessage() for x in caplog.records if x.getMessage().startswith("wake model")]
    for text in [r.journal.read_text(encoding="utf-8"), *model_lines]:
        for word in ("weather", "what", "little", "hey"):
            assert word not in text


async def test_confirm_mode_blocks_a_low_score(rig, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    r = await _rig_with_model(rig, tmp_path, 0.2, "confirm")
    await asyncio.sleep(0.3)
    r.rec.say("hey little bot", conf=0.9)
    await until(lambda: "not waking" in caplog.text, what="the blocked line")
    await asyncio.sleep(0.3)
    assert r.live.calls == [] and r.wake.triggers == 0
    assert ("wake word: Vosk heard 'hey little bot' (word confidence 0.90) but the wake model's score 0.20 is below "
            "wake.oww_threshold 0.50: not waking") in caplog.text
    await until(lambda: any(x["kind"] == "vosk_wake" for x in _journal(r.journal)), what="the blocked wake noted")
    assert next(x for x in _journal(r.journal) if x["kind"] == "vosk_wake")["blocked"] is True
    assert r.wake.status()["listening"]  # still listening
    r.scorer.level = 0.9  # ...and a wake the model agrees with goes through
    await asyncio.sleep(0.3)
    r.rec.say("hey little bot", conf=0.9)
    await until(lambda: r.live.calls, what="live voice started")


async def test_confirm_mode_lets_a_high_score_through(rig, tmp_path):
    r = await _rig_with_model(rig, tmp_path, 0.8, "confirm")
    await asyncio.sleep(0.3)
    r.rec.say("hey lil bot")
    await until(lambda: r.live.calls, what="live voice started")
    assert r.wake.triggers == 1


async def test_rescue_mode_wakes_on_an_unsure_phrase_the_model_is_sure_of(rig, tmp_path, caplog):
    # "hey little bot" heard as "hey bot" at 0.55 while the model scored 0.79 (the robot, 2026-11-02)
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    r = await _rig_with_model(rig, tmp_path, 0.9, "rescue")
    await asyncio.sleep(0.3)
    r.rec.say("hey bot", conf=0.55)  # below wake.short_min_confidence, nobody in front of the camera
    await until(lambda: r.live.calls, what="live voice started")
    assert r.wake.rescued == 1 and r.wake.triggers == 1
    assert ("wake word: Vosk wasn't sure of 'hey bot', but the wake model's score is 0.90 "
            "(wake.oww_rescue_threshold 0.70): waking") in caplog.text


async def test_rescue_mode_needs_the_model_to_be_sure_too(rig, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    r = await _rig_with_model(rig, tmp_path, 0.3, "rescue")
    await asyncio.sleep(0.3)
    r.rec.say("hey bot", conf=0.55)
    await until(lambda: "heard 'hey bot' but not sure enough" in caplog.text, what="the near miss")
    await asyncio.sleep(0.3)
    assert r.live.calls == [] and r.wake.rescued == 0
    r.rec.say("hey little bot", conf=0.9)  # a sure Vosk wake needs nothing from the model (as in shadow)
    await until(lambda: r.live.calls, what="live voice started")


async def test_primary_mode_the_model_wakes_it_on_its_own(rig, tmp_path, caplog):
    # the owner's choice (2026-11-02): the trained model first at 0.45, Vosk as the backup
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    r = await _rig_with_model(rig, tmp_path, 0.3, "primary")
    await asyncio.sleep(0.4)
    assert r.live.calls == []  # under 0.45: nothing
    r.scorer.level = 0.6  # "hey little bot" that Vosk didn't make out at all
    await until(lambda: r.live.calls, what="live voice started")
    assert r.wake.triggers == 1 and r.live.carries[0].phrase == "hey little bot"
    assert "wake model: it woke LittleBot (score 0.60, wake.oww_primary_threshold 0.45)" in caplog.text
    assert "wake word heard (phrase=hey little bot, before the sentence ended)" in caplog.text
    await until(lambda: any(x["kind"] == "model_wake" for x in _journal(r.journal)), what="the wake noted")
    wake = next(x for x in _journal(r.journal) if x["kind"] == "model_wake")
    await r.live.end()
    await until(lambda: any(x["kind"] == "outcome" for x in _journal(r.journal)), what="the outcome")
    assert next(x for x in _journal(r.journal) if x["kind"] == "outcome")["id"] == wake["id"]
    assert not any(x["kind"] == "model_only" for x in _journal(r.journal))  # its own wake, not a stray peak


async def test_primary_mode_vosk_is_the_backup(rig, tmp_path):
    r = await _rig_with_model(rig, tmp_path, 0.1, "primary")
    await asyncio.sleep(0.3)
    r.rec.say("hey little bot", conf=0.9)
    await until(lambda: r.live.calls, what="live voice started")
    assert r.wake.triggers == 1


async def test_a_broken_model_switches_off_and_waking_goes_on(rig, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    r = await _rig_with_model(rig, tmp_path, 0.9, "confirm")

    def boom(_pcm: np.ndarray) -> list[float]:
        raise RuntimeError("bad model")

    r.scorer.process = boom
    await until(lambda: "wake model: switched off (scoring failed: RuntimeError: bad model)" in caplog.text,
                what="switched off")
    r.rec.say("hey little bot")
    await until(lambda: r.live.calls, what="live voice started anyway")
    assert sum(1 for x in caplog.records if x.levelno == logging.WARNING and "wake model" in x.getMessage()) == 1
    assert r.wake.status()["wake_model"] is None


async def test_missing_model_files_leave_waking_as_before(rig, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.wake")
    r = await rig(start_wake=False, wake={"oww_mode": "confirm", "oww_model_dir": str(tmp_path / "nothing")})
    await r.wake.start()
    await until(lambda: "wake model: switched off" in caplog.text, what="switched off")
    await _listening(r)
    r.rec.say("hey little bot")
    await until(lambda: r.live.calls, what="live voice started")


async def test_off_by_default(rig):
    r = await rig()
    await _listening(r)
    assert r.ctx.config.wake.oww_mode == "off" and r.wake._oww is None
    assert r.wake.status()["wake_model"] is None


# ------------------------------------------------------------------ the report
def _report_module():
    spec = importlib.util.spec_from_file_location("wake_shadow_report", ROOT / "scripts" / "wake_shadow_report.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_the_report_on_a_sample_file(tmp_path, capsys):
    rows: list[dict[str, Any]] = []
    # 20 real requests the model scored high, 4 false wakes it scored low, one false wake it scored high
    for i in range(20):
        rows += [{"t": f"2026-11-02T10:{i:02d}:00", "kind": "vosk_wake", "id": i, "vosk_conf": 0.8,
                  "model_score": 0.6 + i / 100, "model_score_after": 0.9, "early": True, "video": False,
                  "mode": "shadow", "blocked": False},
                 {"t": f"2026-11-02T10:{i:02d}:30", "kind": "outcome", "id": i, "outcome": "request", "turns": 2}]
    for i, s in enumerate((0.05, 0.1, 0.2, 0.9)):
        rows += [{"t": f"2026-11-02T11:0{i}:00", "kind": "vosk_wake", "id": 100 + i, "vosk_conf": 0.4,
                  "model_score": s, "model_score_after": s, "mode": "shadow", "blocked": False},
                 {"t": f"2026-11-02T11:0{i}:20", "kind": "outcome", "id": 100 + i,
                  "outcome": "no_words" if i % 2 == 0 else "dismissed"}]
    rows += [{"t": "2026-11-02T12:00:00", "kind": "model_only", "model_score": 0.7, "vosk_unsure": True},
             {"t": "2026-11-02T12:30:00", "kind": "model_only", "model_score": 0.55, "vosk_unsure": False},
             {"t": "2026-11-02T13:00:00", "kind": "listened", "sound_s": 7200.0, "vosk_wakes": 24, "model_only": 2}]
    path = tmp_path / "shadow.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n", encoding="utf-8")
    mod = _report_module()
    assert mod.main([str(path)]) == 0
    text = capsys.readouterr().out
    assert "the model listened for 2.0 hours" in text
    assert "Vosk woke LittleBot 24 times." in text
    assert "after 20: a real request" in text and "after 2: nobody said anything" in text
    assert "after 2: dismissed at once" in text
    assert "at 0.5: 21 of 24 (88%)" in text
    assert "The model's own peaks with no Vosk wake: 2, 1.0 an hour; at 1 of them Vosk heard a wake phrase" in text
    assert "0.5: keeps 20 of 20 real requests, drops 3 of 4 false wakes" in text
    assert "Best: 0.25. It keeps all 20 real requests and drops 3 of 4 false wakes." in text
    assert "wait for at least" not in text
    # --since leaves the earlier ones out; a missing file says so
    assert mod.main([str(path), "--since", "2026-11-02T12"]) == 0
    assert "Vosk woke LittleBot 0 times." in capsys.readouterr().out
    assert mod.main([str(tmp_path / "none.jsonl")]) == 1
