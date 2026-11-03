"""Dashboard: the family's own "hey little bot" clips (``/api/wake/clips``): upload, counts, delete."""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from littlebot.audio import wake_clips
from littlebot.dashboard.routes import wake_clip_routes


def _wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.sin(np.arange(int(seconds * 16000)) / 8) * 5000).astype(np.int16).tobytes())
    return buf.getvalue()


@pytest.fixture
def phrases(monkeypatch):
    monkeypatch.setattr(wake_clip_routes, "_model", lambda dash: object())
    monkeypatch.setattr(wake_clips, "find_phrases", lambda pcm, model: [(0.5, 1.2), (1.4, 2.1)])


async def test_a_recording_becomes_clips_and_can_be_deleted(authed, phrases):
    assert (await authed.get("/api/wake/clips")).json()["people"] == []
    big = _wav(5.0)  # 160 KB: more than any other dashboard request may send
    r = await authed.post("/api/wake/clips/upload?name=Ali", content=big, headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["added"]["found"] == 2 and d["people"] == [{"name": "ali", "clips": 2, "recordings": 1}]
    r = await authed.delete("/api/wake/clips/ali")
    assert r.status_code == 200 and r.json()["people"] == []
    assert (await authed.delete("/api/wake/clips/ali")).status_code == 404


async def test_a_bad_recording_says_why(authed, phrases):
    r = await authed.post("/api/wake/clips/upload?name=Ali", content=b"nope" * 50, headers={"Content-Type": "audio/wav"})
    assert r.status_code == 422 and "isn't a recording" in r.json()["detail"]


async def test_only_this_path_takes_a_big_body_and_only_logged_in(authed, client, phrases):
    big = _wav(5.0)
    r = await authed.post("/api/assistant/say", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    r = await client.post("/api/wake/clips/upload?name=Ali", content=_wav(2.0), headers={"Content-Type": "audio/wav"})
    assert r.status_code in (401, 403)
