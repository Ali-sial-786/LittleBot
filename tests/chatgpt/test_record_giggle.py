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


def test_tidy_trims_silence():
    sr = rg.OUT_RATE
    x = np.concatenate([np.zeros(sr), 0.3 * np.sin(np.arange(sr) / 5), np.zeros(sr)]).astype(np.float32)
    y = rg.tidy(x)
    assert 1.0 <= len(y) / sr < 1.2
