"""The dashboard's Assistant page API: status without keys, typed chat, intercom, notes, sessions."""

from __future__ import annotations

from typing import Any

from littlebot.assistant.service import AssistantService
from littlebot.brain.providers.base import ProviderReply
from littlebot.skills.base import ANNOUNCE
from littlebot.testing import FakeCallAudio, FakeSTT, FakeTTS

from dashboard_fakes import FAKE_ANTHROPIC_KEY


class OneLine:
    name, model = "claude", "fake"

    async def reply(self, messages: Any, *, system: str, max_tokens: int, timeout: float, on_delta: Any = None,
                    **kw: Any) -> ProviderReply:
        on_delta("Hello from the brain.")
        return ProviderReply(text="Hello from the brain.")


def _install(dctx: Any) -> AssistantService:
    from littlebot.assistant.agent import Brain

    svc = AssistantService(dctx, audio_factory=FakeCallAudio, stt_factory=lambda: FakeSTT(),
                           tts_factory=FakeTTS, brain=Brain(dctx, provider=OneLine()))
    dctx.assistant = svc
    return svc


async def test_status_never_shows_keys(authed, dctx) -> None:
    _install(dctx)
    r = await authed.get("/api/assistant")
    assert r.status_code == 200
    body = r.json()
    assert body["assistant"]["pipeline"] == "live" and body["assistant"]["keys"]["anthropic"] is True
    assert FAKE_ANTHROPIC_KEY not in r.text


async def test_chat_needs_cloud_then_answers(authed, dctx) -> None:
    _install(dctx)
    dctx.privacy.set(cloud_enabled=False)
    r = await authed.post("/api/assistant/chat", json={"text": "hi"})
    assert r.status_code == 403
    dctx.privacy.set(cloud_enabled=True)
    r = await authed.post("/api/assistant/chat", json={"text": "hi"})
    assert r.status_code == 200 and r.json()["reply"] == "Hello from the brain."


async def test_intercom_and_note(authed, dctx) -> None:
    _install(dctx)
    said: list[str] = []
    dctx.bus.subscribe(ANNOUNCE, lambda e: said.append(e.get("text")))
    r = await authed.post("/api/assistant/say", json={"text": "Dinner is ready"})
    assert r.status_code == 200 and said == ["Dinner is ready"]
    r = await authed.post("/api/assistant/note", json={"text": "Back in 5"})
    assert r.status_code in (200, 409, 503)


async def test_session_start_and_stop(authed, dctx) -> None:
    _install(dctx)
    dctx.privacy.set(cloud_enabled=True)
    r = await authed.post("/api/assistant/session/start")
    assert r.status_code == 200 and r.json()["active"] is True
    r = await authed.post("/api/assistant/session/stop")
    assert r.status_code == 200
