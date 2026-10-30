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


async def test_memories_lists_and_reminders_from_the_phone(authed, dctx) -> None:
    from littlebot.skills.hub import SkillHub

    dctx.skills = SkillHub(dctx)
    mem, lists, rem = dctx.skills.get("memory"), dctx.skills.get("lists"), dctx.skills.get("reminders")
    assert "ok" in await mem.run("remember", {"fact": "Ali takes his coffee black"})
    assert "ok" in await lists.run("add_to_list", {"items": ["milk", "eggs"], "list": "shopping"})
    assert "ok" in await rem.run("set_reminder", {"text": "call mom", "when": "in 2 hours"})
    r = await authed.get("/api/assistant/data")
    body = r.json()
    assert body["memories"][0]["text"].lower().endswith("coffee black")
    shopping = next(x for x in body["lists"] if x["name"] == "shopping")
    assert [i["item"] for i in shopping["items"]] == ["milk", "eggs"]
    assert body["reminders"][0]["text"] == "call mom"
    r = await authed.post("/api/assistant/lists/tick", json={"list": "shopping", "id": shopping["items"][0]["id"]})
    assert r.json()["ok"] is True
    r = await authed.post("/api/assistant/memories/forget", json={"id": body["memories"][0]["id"]})
    assert r.status_code == 200
    r = await authed.post("/api/assistant/reminders/cancel", json={"id": body["reminders"][0]["id"]})
    assert r.status_code == 200
    body = (await authed.get("/api/assistant/data")).json()
    assert body["memories"] == [] and body["reminders"] == []
    assert [i["item"] for x in body["lists"] for i in x["items"]] == ["eggs"]
