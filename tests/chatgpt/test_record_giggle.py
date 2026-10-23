"""scripts/record_giggle.py against a local WebRTC peer standing in for OpenAI."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import httpx
import numpy as np
import pytest

pytest.importorskip("aiortc")

from chatgpt_helpers import FakeOpenAI, signed_in_auth  # noqa: E402

from littlebot.testing import make_test_config  # noqa: E402

pytestmark = pytest.mark.timeout(60)

spec = importlib.util.spec_from_file_location("record_giggle", Path(__file__).resolve().parents[2] / "scripts" / "record_giggle.py")
rg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rg)


async def test_a_take_records_only_the_voice_and_asks_for_a_giggle(tmp_path):
    server = FakeOpenAI(reject_voices=("bubbie",))
    cfg = make_test_config().chatgpt

    async def finish_when_asked():
        await server.channel_ready.wait()
        while not any(m.get("type") == "session.context.append" for m in server.received):
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.6)  # the fake "voice" (a tone) plays meanwhile
        server.send({"type": "turn.done", "turn": {"role": "assistant", "transcript": "That tickles!"}})

    helper = asyncio.ensure_future(finish_when_asked())
    try:
        audio, said, voice = await rg.record_take(signed_in_auth(tmp_path), cfg, transport=httpx.MockTransport(server),
                                                  max_seconds=8)
    finally:
        helper.cancel()
        await server.close()
    assert said == "That tickles!" and voice == "sol"  # bubbie refused -> fallback
    ask = next(m for m in server.received if m.get("type") == "session.context.append")
    assert "giggle" in ask["content"][0]["text"] and "That tickles" in ask["content"][0]["text"]
    body = server.requests[-1]["body"]["session"]
    assert "never the spoken words 'he he'" in body["instructions"]
    clip = rg.tidy(audio)
    assert len(clip) / rg.OUT_RATE > 0.4 and 0.5 < float(np.abs(clip).max()) <= 0.56


async def test_like_mode_plays_the_giggle_to_the_voice_then_keeps_only_its_copy(tmp_path):
    server = FakeOpenAI()
    cfg = make_test_config().chatgpt
    sr = rg.RATE
    t = np.arange(int(0.6 * sr)) / sr
    like = (np.sin(2 * np.pi * 300 * t) * 16000).astype(np.int16)  # the "giggle" to copy

    def mic_loud() -> int:
        return sum(1 for f in server.heard if np.abs(f).max() > 4000)

    async def copy_after_hearing_it():
        await server.channel_ready.wait()
        server.send({"type": "turn.done", "turn": {"role": "assistant", "transcript": "Okay!"}})  # too early: ignored
        while mic_loud() < 20:  # most of the 0.6 s giggle (30 frames of 20 ms) came through the mic
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.6)
        server.send({"type": "turn.done", "turn": {"role": "assistant", "transcript": ""}})

    helper = asyncio.ensure_future(copy_after_hearing_it())
    try:
        audio, said, voice = await rg.record_take(signed_in_auth(tmp_path), cfg, transport=httpx.MockTransport(server),
                                                  max_seconds=8, like=like, say="That tickles!")
    finally:
        helper.cancel()
        await server.close()
    assert said == "" and voice == "bubbie"
    body = server.requests[-1]["body"]["session"]
    assert "copy it out loud as exactly as you can" in body["instructions"]
    assert "then say the words 'That tickles!'" in body["instructions"]
    ask = next(m for m in server.received if m.get("type") == "session.context.append")
    assert "copy it exactly and then say 'That tickles!'" in ask["content"][0]["text"]
    assert mic_loud() >= 25
    assert len(rg.tidy(audio)) / rg.OUT_RATE > 0.4  # the voice's sound after the giggle was kept


def test_load_like_trims_and_levels_any_file(tmp_path, monkeypatch):
    sr = rg.set_giggle.RATE
    x = np.concatenate([np.zeros(sr), 0.2 * np.sin(np.arange(sr) / 3), np.zeros(sr)]).astype(np.float32)
    monkeypatch.setattr(rg.set_giggle, "decode", lambda path: x)
    pcm = rg.load_like(tmp_path / "giggle.mp4")
    assert pcm.dtype == np.int16 and 1.0 <= len(pcm) / rg.RATE < 1.2
    assert 19000 <= int(np.abs(pcm).max()) <= 20000


def test_tidy_trims_silence():
    sr = rg.OUT_RATE
    x = np.concatenate([np.zeros(sr), 0.3 * np.sin(np.arange(sr) / 5), np.zeros(sr)]).astype(np.float32)
    y = rg.tidy(x)
    assert 1.0 <= len(y) / sr < 1.2


async def test_a_skipped_take_is_saved_aside_and_the_kept_ones_installed(tmp_path, monkeypatch):
    import argparse

    import littlebot.core.config as config_mod

    cfg = make_test_config(data_dir=str(tmp_path))
    signed_in_auth(tmp_path)
    monkeypatch.setattr(config_mod, "load_config", lambda **kw: cfg)
    tone = (0.3 * np.sin(np.arange(rg.OUT_RATE) / 5)).astype(np.float32)
    calls = iter([True, False])

    async def fake_take(auth, chatgpt_cfg, **kw):
        return tone, "", "bubbie"

    monkeypatch.setattr(rg, "record_take", fake_take)
    monkeypatch.setattr(rg, "ask_keep", lambda clip: next(calls))
    args = argparse.Namespace(takes=2, dry_run=False, like=None, start=None, end=None, say=None, ask=True)
    assert await rg.main_async(args) == 0
    assert [p.name for p in (tmp_path / "sounds" / "voice").glob("giggle_*.wav")] == ["giggle_1.wav"]
    skipped = list((tmp_path / "giggle_takes").glob("skipped_*_take2.wav"))
    assert len(skipped) == 1 and skipped[0].stat().st_size > 1000
