"""Dashboard: "Record activity voices in the ChatGPT voice" (/api/voice-lines) on the Activities page."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chatgpt"))
from chatgpt_helpers import signed_in_auth  # noqa: E402

import littlebot.chatgpt.voice_record as vr  # noqa: E402
from littlebot.audio.voice_lines import VoiceLineStore  # noqa: E402


@pytest.fixture
def store(dctx, tmp_path):
    s = dctx.extras["voice_lines"] = VoiceLineStore(tmp_path / "voice_lines")
    dctx.settings.set("profile", {**dctx.settings.get("profile"), "nickname": "Amara", "age_months": 10})
    return s


@pytest.fixture
def fake_call(monkeypatch):
    """record_lines without a call: each line comes back right, slowly enough to watch the progress."""
    asked: list[list[str]] = []
    gate = asyncio.Event()

    async def fake(auth, cfg, lines, *, on_take=None, on_status=None, stop=None, **kw):
        asked.append(list(lines))
        on_status("recording")
        for i, line in enumerate(lines):
            if stop is not None and stop.is_set():
                break
            if i == 2:
                await gate.wait()  # hold here until the test lets it go on
            await on_take(i, len(lines), vr.Take(line, np.full(int(0.4 * vr.OUT_RATE), 0.3, np.float32), line,
                                                 "bubbie", 1.0, 1))
        return []

    monkeypatch.setattr(vr, "record_lines", fake)
    return asked, gate


async def wait_for(cond: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


async def test_status_says_what_is_needed_first(authed, dctx, store):
    r = await authed.get("/api/voice-lines")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] > 500 and d["recorded"] == 0 and d["missing"] == d["total"]
    assert d["named"] > 0 and d["nickname"] == "Amara" and d["share_name"] is False
    assert d["blocked"] == "cloud_off" and d["job"] is None
    assert 0 < d["minutes_without_name"] < d["minutes"] and d["for_age_missing"] > 100
    dctx.privacy.set(cloud_enabled=True)
    d = (await authed.get("/api/voice-lines")).json()
    assert d["blocked"] == "not_signed_in" and "AI page" in d["blocked_detail"]
    r = await authed.post("/api/voice-lines/record", json={})
    assert r.status_code == 403 and "Sign in with ChatGPT" in r.json()["detail"]


async def test_the_button_records_the_missing_lines_without_the_name_unless_asked(authed, dctx, store, fake_call,
                                                                                 tmp_path):
    asked, gate = fake_call
    dctx.privacy.set(cloud_enabled=True)
    dctx.extras["chatgpt_auth"] = signed_in_auth(tmp_path)
    store.save("Tap again, and I'll do my trick!", np.full(4000, 0.3, np.float32), vr.OUT_RATE, voice="sol")
    r = await authed.post("/api/voice-lines/record", json={"with_name": False})
    assert r.status_code == 200 and r.json()["started"] is True
    await wait_for(lambda: bool(asked))
    lines = asked[0]
    assert "Tap again, and I'll do my trick!" not in lines  # recorded already
    assert not any("Amara" in t for t in lines)  # the name is only sent with the parent's OK
    assert lines.index("Boing!") < lines.index("Let's count all the way to ten! Hold up your fingers.")  # her age first
    await wait_for(lambda: dctx.extras["voice_lines_job"].done == 2)
    d = (await authed.get("/api/voice-lines")).json()
    assert d["job"]["running"] is True and d["job"]["done"] == 2 and d["job"]["saved"] == 2
    assert d["job"]["current"] == lines[2] and d["recorded"] == 3
    r = await authed.post("/api/voice-lines/record", json={})
    assert r.status_code == 409  # one recording at a time
    r = await authed.post("/api/voice-lines/stop", json={})
    assert r.status_code == 200
    gate.set()
    await wait_for(lambda: not dctx.extras["voice_lines_job"].running)
    d = (await authed.get("/api/voice-lines")).json()
    assert d["job"]["phase"] == "stopped" and d["job"]["saved"] == 3 and d["recorded"] == 4
    # again, with the name this time: only what's missing, now including the lines with "Amara"
    r = await authed.post("/api/voice-lines/record", json={"with_name": True})
    await wait_for(lambda: len(asked) == 2)
    assert any("Amara" in t for t in asked[1]) and not set(asked[0][:3]) & set(asked[1])
    await wait_for(lambda: not dctx.extras["voice_lines_job"].running)


async def test_not_during_a_voice_chat(authed, dctx, store, fake_call, tmp_path):
    class Live:
        def status(self) -> dict[str, Any]:
            return {"active": True}

    dctx.privacy.set(cloud_enabled=True)
    dctx.extras["chatgpt_auth"] = signed_in_auth(tmp_path)
    dctx.live = Live()
    r = await authed.post("/api/voice-lines/record", json={})
    assert r.status_code == 409 and "voice chat" in r.json()["detail"] and fake_call[0] == []
