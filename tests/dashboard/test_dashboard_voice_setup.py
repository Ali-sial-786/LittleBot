"""The Assistant page's voices and models (/api/assistant/setup): the choices over the config file's, used at once
or after the conversation, back to the file; Cartesia's voice library searched and a voice heard; keys never shown."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import numpy as np
import pytest
from pydantic import SecretStr

from littlebot.assistant.agent import Brain
from littlebot.assistant.service import AssistantService
from littlebot.assistant.tts import cartesia as cartesia_mod
from littlebot.core.settings import CONFIG_OVERRIDES
from littlebot.testing import FakeCallAudio, FakeSTT, FakeTTS

from dashboard_fakes import FAKE_ANTHROPIC_KEY

CARTESIA_KEY = "fake-cartesia-key-VOICE-SETUP-5d1e"
VOICE = "a0e99841-438c-4a64-b679-ae501e7d6091"


class Closable(FakeTTS):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def _service(dctx: Any) -> AssistantService:
    svc = AssistantService(dctx, audio_factory=FakeCallAudio, stt_factory=lambda: FakeSTT(), tts_factory=FakeTTS,
                           brain=Brain(dctx, provider=object()))
    dctx.assistant = svc
    await svc.start()
    return svc


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
async def svc(dctx):
    s = await _service(dctx)
    yield s
    await s.stop()


async def test_a_choice_goes_over_the_config_file_and_back(authed, dctx, svc) -> None:
    a = dctx.config.assistant
    d = (await authed.get("/api/assistant/setup")).json()
    assert d["choices"]["stt"] == {"value": ["deepgram", "openai", "vosk"], "file": ["deepgram", "openai", "vosk"],
                                   "changed": False}
    assert d["choices"]["brain"]["value"] is None and d["running"] is True  # the tests' config has no Claude
    old_tts = svc._tts = Closable()

    r = await authed.put("/api/assistant/setup", json={"listen": "assemblyai", "speak": "elevenlabs"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["choices"]["stt"]["value"] == ["assemblyai", "deepgram", "openai", "vosk"] and d["choices"]["stt"]["changed"]
    assert a.stt == ["assemblyai", "deepgram", "openai", "vosk"]  # in the config: every reader sees it
    assert a.tts == ["elevenlabs", "cartesia", "openai", "local"]
    await settle()
    assert svc._tts is None and old_tts.closed  # made again from the new choices for the next question

    d = (await authed.put("/api/assistant/setup", json={"listen": "deepgram"})).json()
    assert d["choices"]["stt"] == {"value": ["deepgram", "openai", "vosk"], "file": ["deepgram", "openai", "vosk"],
                                   "changed": False}  # the file's own: not kept, so the file decides again
    assert not dctx.settings.is_stored("assistant.stt")


async def test_compare_brain_voice_then_everything_back(authed, dctx, svc) -> None:
    r = await authed.put("/api/assistant/setup", json={
        "compare": ["cartesia", "assemblyai", "cartesia"], "log_words": True, "brain": "claude-sonnet-5-5",
        "deep_model": "off", "cartesia_voice": VOICE, "pipeline": "cascade"})
    assert r.status_code == 200, r.text
    c = dctx.config
    assert c.assistant.stt_shadow == ["cartesia", "assemblyai"] and c.assistant.log_words is True
    assert c.claude.model == "claude-sonnet-5-5" and c.assistant.deep_model is None
    assert c.assistant.cartesia_voice == VOICE and c.assistant.pipeline == "cascade"
    assert svc.status()["choices"]["stt_shadow"] == ["cartesia", "assemblyai"]

    d = (await authed.post("/api/assistant/setup/reset")).json()
    assert not any(x["changed"] for x in d["choices"].values())
    assert not any(dctx.settings.is_stored(k) for k in CONFIG_OVERRIDES)
    assert c.assistant.stt_shadow == [] and c.claude.model is None
    assert c.assistant.deep_model == "claude-sonnet-5-5" and c.assistant.pipeline == "auto"


async def test_bad_choices_are_refused(authed, dctx, svc) -> None:
    for body in ({}, {"listen": "siri"}, {"compare": ["openai"]}, {"cartesia_voice": "not a voice!"},
                 {"brain": "gpt-9"}, {"deep_model": "claude-haiku-5-5"}, {"speak": "deepgram"}, {"made_up": 1}):
        r = await authed.put("/api/assistant/setup", json=body)
        assert r.status_code == 422, body
    assert dctx.config.assistant.stt == ["deepgram", "openai", "vosk"]
    with pytest.raises(ValueError):  # the same check as the config file's, for any other setter
        dctx.settings.set("assistant.tts", ["siri"])


async def test_a_change_waits_for_the_conversation_to_end(authed, dctx, svc) -> None:
    dctx.privacy.set(cloud_enabled=True)
    assert (await authed.post("/api/assistant/session/start")).status_code == 200
    old_tts = svc._tts = Closable()
    d = (await authed.put("/api/assistant/setup", json={"brain": "claude-opus-5-5"})).json()
    assert d["live"]["waiting"] is True
    await settle()
    assert svc._tts is old_tts and not old_tts.closed  # not in the middle of the conversation
    await authed.post("/api/assistant/session/stop")
    await settle()
    assert svc._tts is None and old_tts.closed
    assert (await authed.get("/api/assistant/setup")).json()["live"]["waiting"] is False


class _Cartesia:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="{}")
        if request.url.path == "/voices":
            return httpx.Response(200, json={"has_more": True, "next_page": VOICE, "data": [
                {"id": VOICE, "name": "Tessa", "tagline": "Warm companion", "description": "Soft and kind.",
                 "gender": "feminine", "is_owner": False, "accents": [{"accent": "American", "locale": "en-US"}]}]})
        return httpx.Response(200, json={"id": VOICE, "name": "Tessa", "tagline": "Warm companion"})


async def test_voices_are_searched_with_the_robots_key(authed, dctx, svc, monkeypatch) -> None:
    service = _Cartesia()
    monkeypatch.setattr(cartesia_mod, "voices_transport", httpx.MockTransport(service))
    monkeypatch.setattr("littlebot.dashboard.routes.voice_setup_routes._voices", {})
    assert (await authed.get("/api/assistant/setup/voices")).status_code == 409  # no key yet: nothing asked
    dctx.config.cartesia_api_key = SecretStr(CARTESIA_KEY)
    dctx.privacy.set(cloud_enabled=False)
    assert (await authed.get("/api/assistant/setup/voices")).status_code == 403
    dctx.privacy.set(cloud_enabled=True)
    assert not service.requests

    r = await authed.get("/api/assistant/setup/voices", params={"q": "warm", "gender": "feminine"})
    assert r.status_code == 200, r.text
    assert r.json() == {"next": VOICE, "voices": [{"id": VOICE, "name": "Tessa", "tagline": "Warm companion",
                                                   "description": "Soft and kind.", "gender": "feminine",
                                                   "accent": "American", "language": "", "mine": False}]}
    req = service.requests[-1]
    assert req.headers["authorization"] == f"Bearer {CARTESIA_KEY}" and req.headers["cartesia-version"]
    assert dict(req.url.params) == {"limit": "30", "q": "warm", "gender": "feminine", "language": "en"}
    assert CARTESIA_KEY not in r.text

    dctx.settings.set("assistant.cartesia_voice", VOICE)
    assert (await authed.get("/api/assistant/setup/voice")).json()["name"] == "Tessa"  # remembered from the search
    assert (await authed.get("/api/assistant/setup")).json()["voice"]["name"] == "Tessa"
    assert (await authed.get("/api/assistant/setup/voices", params={"after": "../etc"})).status_code == 422
    service.status = 401
    r = await authed.get("/api/assistant/setup/voices")
    assert r.status_code == 502 and "refused" in r.json()["detail"]
    all_text = (await authed.get("/api/assistant/setup")).text
    assert CARTESIA_KEY not in all_text and FAKE_ANTHROPIC_KEY not in all_text


async def test_hear_a_voice_on_littlebot(authed, dctx, svc, monkeypatch) -> None:
    asked: list[tuple[str, str]] = []

    class Voice:
        sample_rate = 48000

        def __init__(self, voice: str) -> None:
            self.voice = voice

        async def stream(self, text: str):
            asked.append((self.voice, text))
            yield np.zeros(480, dtype=np.float32)

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr("littlebot.assistant.tts.cartesia_tts", lambda ctx, voice=None, **kw: Voice(voice))
    dctx.config.cartesia_api_key = SecretStr(CARTESIA_KEY)
    dctx.privacy.set(cloud_enabled=True)
    r = await authed.post("/api/assistant/setup/voices/try", json={"id": VOICE})
    assert r.status_code == 200, r.text
    assert asked == [(VOICE, "Hi, I'm LittleBot. This is how I would sound, answering your questions.")]
    assert dctx.audio.spoken[-1].startswith("Hi, I'm LittleBot")
    assert dctx.config.assistant.cartesia_voice != VOICE  # only heard, not chosen
    assert (await authed.post("/api/assistant/setup/voices/try", json={"id": "x"})).status_code == 422

    assert (await authed.post("/api/assistant/session/start")).status_code == 200
    r = await authed.post("/api/assistant/setup/voices/try", json={"id": VOICE})
    assert r.status_code == 409 and "conversation" in r.json()["detail"]


def test_the_page_lists_the_panel() -> None:
    from pathlib import Path

    js = Path(__file__).parents[2] / "littlebot" / "dashboard" / "static" / "js" / "pages"
    assert "voiceSetupPanel(app)" in (js / "assistant.js").read_text(encoding="utf-8")
    panel = (js / "voice_setup_panel.js").read_text(encoding="utf-8")
    assert "/assistant/setup" in panel and json.dumps("/assistant/setup/voices/try")[1:-1] in panel
