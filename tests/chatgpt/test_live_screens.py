"""Live voice and the screen's info cards: the screen director in a real call (a local WebRTC peer standing in
for OpenAI), card news as notes, the call's start / end on the bus, and a hand-off's card going through it."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

pytest.importorskip("aiortc")

from chatgpt_helpers import FakeLiveAudio, FakeOpenAI, signed_in_auth  # noqa: E402

from littlebot.brain.providers.base import BaseProvider, ProviderReply  # noqa: E402
from littlebot.chatgpt.live import (  # noqa: E402
    CALL_ENDED,
    CALL_STARTED,
    SCREEN_INSTRUCTIONS,
    LiveVoiceService,
)
from littlebot.screens import base  # noqa: E402
from littlebot.screens.base import CardType  # noqa: E402

pytestmark = pytest.mark.timeout(60)


class LendingAudio:
    async def suspend(self, owner: str, wait: float = 1.5) -> None:
        pass

    def resume(self, owner: str) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {"volume": 0.5}


class Provider(BaseProvider):
    """The text model: JSON for the screen director, else runs ``script`` (tool calls) and answers ``text``."""

    name = "chatgpt"
    is_cloud = True
    model = "fake"
    supports_tools = True

    def __init__(self, decision: dict[str, Any] | None = None, script: list[tuple[str, dict[str, Any]]] | None = None,
                 text: str = "It's sunny tomorrow.") -> None:
        self.decision = decision or {"do": "none"}
        self.script, self.text = script or [], text
        self.decisions: list[dict[str, Any]] = []
        self.hand_offs: list[dict[str, Any]] = []
        self.results: list[str] = []

    async def reply(self, messages: list[dict[str, str]], *, system: str, max_tokens: int, timeout: float,
                    tools: list[dict[str, Any]] | None = None, run_tool: Any = None, max_tool_rounds: int = 6,
                    effort: str | None = None) -> ProviderReply:
        if "screen director" in system:
            self.decisions.append({"content": messages[-1]["content"], "effort": effort})
            return ProviderReply(text=json.dumps(self.decision), model="fake")
        self.hand_offs.append({"tools": [t.get("name") for t in tools or []]})
        for name, args in self.script:
            self.results.append(await run_tool(name, json.dumps(args)))
        return ProviderReply(text=self.text, model="fake")


class FakeScreens:
    def __init__(self) -> None:
        self.shown: list[tuple[dict[str, Any], str]] = []
        self.card: str | None = None

    @property
    def active(self) -> bool:
        return self.card is not None

    @property
    def current(self) -> dict[str, Any] | None:
        return {"kind": self.card} if self.card else None

    def describe(self) -> str | None:
        return f"the {self.card} card" if self.card else None

    async def show(self, spec: dict[str, Any], *, source: str = "voice") -> dict[str, Any]:
        self.shown.append((spec, source))
        self.card = spec["card"]
        return {"ok": True, "showing": f"the {self.card} card"}

    def command(self, action: str, args: dict[str, Any]) -> bool:
        return True

    def close(self, reason: str) -> bool:
        self.card = None
        return True


async def _build(spec: dict[str, Any], ctx: Any, host: Any) -> None:
    return None


async def wait_for(cond: Any, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


@pytest.fixture
def make_live(ctx, tmp_path, monkeypatch):
    """make_live(provider, screens=True) -> a connected call (each test closes it with ``close``)."""
    import littlebot.screens.cards  # noqa: F401, PLC0415 - the real cards first, so this weather card replaces theirs

    monkeypatch.setitem(base._TYPES, "weather", CardType(
        "weather", "The weather.", {"place": {"type": "string"}, "days": {"type": "integer"}}, _build,
        actions={"day": "offset"}))

    async def _make(provider: BaseProvider, *, screens: bool = True) -> tuple[LiveVoiceService, FakeOpenAI]:
        ctx.privacy.set(cloud_enabled=True)
        ctx.audio = LendingAudio()
        if screens:
            ctx.screens = FakeScreens()
        server, audio = FakeOpenAI(), FakeLiveAudio()
        svc = LiveVoiceService(ctx, auth=signed_in_auth(tmp_path), transport=httpx.MockTransport(server),
                               audio_factory=lambda: audio, provider=provider)
        await svc.start()
        result = await svc.start_session(parent_present=True)
        assert result["ok"], result
        await wait_for(lambda: server.channel_ready.is_set() and svc.status().get("phase") == "listening")
        return svc, server

    return _make


async def close(svc: LiveVoiceService, server: FakeOpenAI) -> None:
    await svc.stop()
    await server.close()


def hear(server: FakeOpenAI, text: str) -> None:
    server.send({"type": "turn.done", "turn": {"role": "user", "transcript": text}})


def say(server: FakeOpenAI, text: str) -> None:
    server.send({"type": "turn.done", "turn": {"role": "assistant", "transcript": text}})


def notes(server: FakeOpenAI) -> list[str]:
    return [m["content"][0]["text"] for m in server.received if m.get("type") == "session.context.append"]


def delegate(server: FakeOpenAI, did: str, text: str) -> None:
    server.send({"type": "delegation.created", "item": {"id": did, "type": "delegation", "target": "client",
                                                        "content": [{"type": "input_text", "text": text}]}})


async def test_a_question_puts_a_card_up_while_the_voice_talks(make_live, ctx):
    events: list[tuple[str, dict[str, Any]]] = []
    ctx.bus.subscribe("live.*", lambda e: events.append((e.topic, dict(e.data))))
    provider = Provider({"do": "show", "spec": {"card": "weather", "days": 2}})
    svc, server = await make_live(provider)
    try:
        assert (CALL_STARTED, {}) in events
        assert SCREEN_INSTRUCTIONS.strip() in server.requests[0]["body"]["session"]["instructions"]
        say(server, "Hello there!")
        hear(server, "What's the weather like tomorrow?")
        await wait_for(lambda: ctx.screens.shown)
        assert ctx.screens.shown == [({"card": "weather", "days": 2}, "voice")]
        await wait_for(lambda: any(n.startswith("On your screen now: the weather card.") for n in notes(server)))
        content = provider.decisions[0]["content"]
        assert "LittleBot: Hello there!" in content and "They just said: What's the weather like tomorrow?\n" in content
        assert provider.decisions[0]["effort"] == "none"
        hear(server, "ba ba ba")  # the baby: nothing is asked
        await asyncio.sleep(0.2)
        assert len(provider.decisions) == 1
    finally:
        await close(svc, server)
    assert events[-1][0] == CALL_ENDED and events[-1][1]["reason"] == "service stop"


async def test_card_news_reaches_the_voice_as_a_note(make_live, ctx):
    svc, server = await make_live(Provider())
    try:
        ctx.bus.publish("screen.news", {"text": "The egg timer is done."}, source="screens")
        await wait_for(lambda: "The egg timer is done. Tell them now, in a few words." in notes(server))
    finally:
        await close(svc, server)


async def test_without_a_screen_service_nothing_changes(make_live, ctx):
    provider = Provider({"do": "show", "spec": {"card": "weather"}})
    svc, server = await make_live(provider, screens=False)
    try:
        assert "info cards" not in server.requests[0]["body"]["session"]["instructions"]
        hear(server, "What's the weather like tomorrow?")
        await asyncio.sleep(0.3)
        assert provider.decisions == []
    finally:
        await close(svc, server)


async def test_cards_switched_off_means_no_director(make_live, ctx):
    try:
        ctx.settings.set("screens.enabled", False)
    except KeyError:  # not registered in this checkout (the screen service registers it)
        ctx.settings._values["screens.enabled"] = False
    provider = Provider({"do": "show", "spec": {"card": "weather"}})
    svc, server = await make_live(provider)
    try:
        hear(server, "What's the weather like tomorrow?")
        await asyncio.sleep(0.3)
        assert provider.decisions == [] and ctx.screens.shown == []
        assert "info cards" not in server.requests[0]["body"]["session"]["instructions"]
    finally:
        await close(svc, server)


async def test_worrying_words_get_no_card(make_live, ctx):
    provider = Provider({"do": "show", "spec": {"card": "weather"}})
    svc, server = await make_live(provider)
    try:
        hear(server, "My baby swallowed a battery, what should I do?")
        await asyncio.sleep(0.3)
        assert provider.decisions == [] and ctx.screens.shown == []
    finally:
        await close(svc, server)


async def test_a_hand_offs_card_goes_through_the_director_once(make_live, ctx):
    provider = Provider({"do": "show", "spec": {"card": "weather"}},
                        script=[("show_on_screen", {"card": "weather"})])
    svc, server = await make_live(provider)
    try:
        hear(server, "What's the weather like tomorrow?")
        await wait_for(lambda: ctx.screens.shown)
        delegate(server, "w1", "What's the weather tomorrow?")
        await wait_for(lambda: provider.results)
        assert "show_on_screen" in provider.hand_offs[0]["tools"]
        assert provider.results[0].startswith("ok: on the screen now: the weather card")
        assert len(ctx.screens.shown) == 1  # up already: not shown twice
        await wait_for(lambda: any(m.get("type") == "delegation.context.append" for m in server.received))
    finally:
        await close(svc, server)
