"""Live voice while a video is paused for the chat ("Hey LittleBot" while it played): the voice is told, and
"keep watching" / "stop the video" (the person's own words or the voice's hand-off) carry on with it after the
call or stop it. A local WebRTC peer stands in for OpenAI; the media controller is the real one."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("aiortc")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "media"))
from chatgpt_helpers import FakeLiveAudio, FakeOpenAI, signed_in_auth  # noqa: E402
from media_testkit import VID_A, FakeBackend  # type: ignore[import-not-found]  # noqa: E402

import littlebot.chatgpt.live as live_mod  # noqa: E402
from littlebot.brain.providers.base import BaseProvider, ProviderReply  # noqa: E402
from littlebot.chatgpt.live import LiveVoiceService, _video_request  # noqa: E402
from littlebot.core.state import RobotState  # noqa: E402
from littlebot.media.controller import MediaController  # noqa: E402

pytestmark = pytest.mark.timeout(60)


class LendingAudio:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def suspend(self, owner: str, wait: float = 1.5) -> None:
        self.calls.append(f"suspend:{owner}")

    def resume(self, owner: str) -> None:
        self.calls.append(f"resume:{owner}")

    def status(self) -> dict[str, Any]:
        return {"volume": 0.5}


class ToolProvider(BaseProvider):
    """The text model: runs scripted tool calls, then answers."""

    name = "chatgpt"
    is_cloud = True
    model = "fake"
    supports_tools = True

    def __init__(self, script: list[tuple[str, dict[str, Any]]] | None = None, text: str = "Okay!") -> None:
        self.script, self.text = script or [], text
        self.calls: list[dict[str, Any]] = []

    async def reply(self, messages: list[dict[str, str]], *, system: str, max_tokens: int, timeout: float,
                    tools: list[dict[str, Any]] | None = None, run_tool: Any = None, max_tool_rounds: int = 6,
                    images: Any = None) -> ProviderReply:
        self.calls.append({"messages": messages})
        for name, args in self.script:
            await run_tool(name, json.dumps(args))
        return ProviderReply(text=self.text, model="fake")


async def wait_for(cond: Any, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


def notes(server: FakeOpenAI) -> list[str]:
    return [m["content"][0]["text"] for m in server.received if m.get("type") == "session.context.append"]


def answers(server: FakeOpenAI, did: str) -> list[str]:
    return [m["content"][0]["text"] for m in server.received
            if m.get("type") == "delegation.context.append" and m.get("delegation_item_id") == did]


def say(server: FakeOpenAI, words: str) -> None:
    server.send({"type": "turn.done", "turn": {"role": "user", "transcript": words}})


def delegate(server: FakeOpenAI, did: str, text: str) -> None:
    server.send({"type": "delegation.created", "item": {"id": did, "type": "delegation", "target": "client",
                                                        "content": [{"type": "input_text", "text": text}]}})


@pytest.fixture
async def paused(ctx, tmp_path, monkeypatch):
    """A video paused for a chat (the real MediaController) + make() -> a connected live call."""
    monkeypatch.setattr(live_mod, "HANDOVER_MAX_S", 0.6)  # the fake voice never "speaks" its goodbye
    ctx.settings.set("profile", {"age_months": 30})
    backend = FakeBackend()
    media = MediaController(ctx, backend=backend, tick_seconds=3600)
    ctx.media = media
    await media.start()
    item = media.add_item(f"https://www.youtube.com/watch?v={VID_A}", "Bus Song | Nursery Rhymes #kids")
    await media.play(item["id"])
    st = media.player_state()
    await media.handle_player_event({"type": "playing", "load_id": st["load_id"], "video_id": VID_A,
                                     "player_state": 1, "current_time": 30.0})
    assert await media.hold("wake word", wait=0.0)
    assert ctx.state.state == RobotState.IDLE
    made: list[tuple[LiveVoiceService, FakeOpenAI]] = []

    async def make(provider: BaseProvider | None = None) -> tuple[LiveVoiceService, FakeOpenAI, BaseProvider]:
        provider = provider or ToolProvider()
        ctx.privacy.set(cloud_enabled=True)
        ctx.audio = LendingAudio()
        server, audio = FakeOpenAI(), FakeLiveAudio()
        svc = LiveVoiceService(ctx, auth=signed_in_auth(tmp_path), transport=httpx.MockTransport(server),
                               audio_factory=lambda: audio, provider=provider)
        await svc.start()
        result = await svc.start_session(parent_present=True)
        assert result["ok"], result
        await wait_for(lambda: server.channel_ready.is_set() and svc.status().get("phase") == "listening")
        made.append((svc, server))
        return svc, server, provider

    yield type("Paused", (), dict(media=media, backend=backend, make=make, item=item))
    for svc, server in made:
        await svc.stop()
        await server.close()
    await media.close()


# ------------------------------------------------------------------ the person's words / the voice's hand-off
@pytest.mark.parametrize("words,want", [
    ("keep watching", "resume"), ("Can I keep watching?", "resume"), ("resume the video", "resume"),
    ("play it again", "resume"), ("go back to the video", "resume"), ("I want my cartoon back", "resume"),
    ("unpause", "resume"), ("carry on with the show please", "resume"), ("put it back on", "resume"),
    ("let's go back to my video", "resume"), ("continue the video", "resume"),
    ("stop the video", "stop"), ("turn it off", "stop"), ("no more videos", "stop"), ("turn off the video", "stop"),
    ("I don't want the video any more", "stop"), ("we're done watching", "stop"), ("switch the cartoon off", "stop"),
    ("play the video", "resume"), ("play that video again", "resume"),
    ("what's the weather", None), ("play a video of dogs", None), ("don't stop the video", None),
    ("play video of trains", None), ("play the video of the cat", None), ("I want to watch peppa pig", None),
    ("I watched a video today", None), ("stop talking", None), ("turn it up", None), ("hey", None),
])
def test_video_words(words, want):
    assert _video_request(words) == want


async def test_the_voice_is_told_the_video_is_paused(paused):
    svc, server, _p = await paused.make()
    await wait_for(lambda: notes(server))
    note = notes(server)[0]
    assert "“Bus Song” is paused" in note  # the title as it may be said (speakable_title)
    assert "resume the video" in note and "stop the video" in note
    assert svc.status()["heard_user"] is False


async def test_keep_watching_carries_on_after_the_goodbye(paused, ctx):
    svc, server, provider = await paused.make()
    say(server, "Keep watching!")
    await wait_for(lambda: any("carries on where it stopped" in n for n in notes(server)))
    assert paused.media.status()["held"] is not None  # not while the call holds the speaker and face
    await wait_for(lambda: not svc.status()["active"])
    last = svc.status()["last_call"]
    assert last["ended_reason"] == live_mod.HANDOVER_REASON and last["handed_over"] == ["video resume"]
    assert last["video"] == "resume" and last["heard_user"] is True
    await wait_for(lambda: paused.media.status()["state"] == "loading")
    assert ctx.state.state == RobotState.PLAYING_MEDIA and paused.media.status()["held"] is None
    assert paused.media.player_state()["item"]["start_seconds"] == 28  # where it was, a moment earlier
    assert len(paused.backend.opened) == 2
    assert provider.calls == []  # no text model needed


async def test_stop_the_video_stops_it_and_the_chat_goes_on(paused, ctx):
    svc, server, provider = await paused.make()
    say(server, "stop the video please")
    await wait_for(lambda: paused.media.status()["state"] == "idle")
    await wait_for(lambda: any("stopped the video" in n for n in notes(server)))
    await asyncio.sleep(0.8)
    assert svc.status()["active"]  # the chat carries on, the face stays
    assert ctx.state.state in (RobotState.LISTENING, RobotState.SPEAKING, RobotState.THINKING)
    assert provider.calls == []


async def test_a_hand_off_to_resume_is_done_without_the_text_model(paused, ctx):
    svc, server, provider = await paused.make()
    delegate(server, "v1", "Resume the video")
    await wait_for(lambda: answers(server, "v1"))
    assert "carries on where it stopped" in answers(server, "v1")[0]
    assert provider.calls == []
    await wait_for(lambda: not svc.status()["active"])
    await wait_for(lambda: ctx.state.state == RobotState.PLAYING_MEDIA)


async def test_their_words_and_then_the_hand_off_do_it_once(paused, ctx):
    svc, server, provider = await paused.make()
    say(server, "go back to the video")
    await wait_for(lambda: any("carries on" in n for n in notes(server)))
    delegate(server, "v2", "resume the video")
    await wait_for(lambda: answers(server, "v2"))
    assert answers(server, "v2")[0].startswith("Done already")
    await wait_for(lambda: ctx.state.state == RobotState.PLAYING_MEDIA)
    assert svc.status()["last_call"]["handed_over"] == ["video resume"]
    assert provider.calls == []


async def test_the_text_models_resume_tool_hands_over_too(paused, ctx):
    provider = ToolProvider([("resume_video", {})], text="Okay, back to your video!")
    svc, server, _p = await paused.make(provider)
    delegate(server, "v3", "they said yes, they want it")  # not words LittleBot knows: the text model decides
    await wait_for(lambda: answers(server, "v3"))
    assert "this voice chat ends and the video starts" in answers(server, "v3")[0]
    await wait_for(lambda: ctx.state.state == RobotState.PLAYING_MEDIA)


async def test_without_a_paused_video_the_words_do_nothing_special(paused, ctx):
    await paused.media.stop()
    provider = ToolProvider([("stop_video", {})], text="There's no video on.")
    svc, server, _p = await paused.make(provider)
    say(server, "keep watching")
    delegate(server, "v4", "stop the video")
    await wait_for(lambda: answers(server, "v4"))
    assert provider.calls  # the tools answered it
    await asyncio.sleep(0.3)
    assert not notes(server) and svc.status()["active"]


# ------------------------------------------------------------------ the tools (typed chat and live hand-offs)
async def test_tools_resume_a_paused_video_after_the_turn(paused, ctx):
    from littlebot.chatgpt.tools import RobotToolbox

    box = RobotToolbox(ctx, mode="chat", defer=True)
    status = json.loads(await box.run("get_robot_status", "{}"))
    assert status["video"] == {"state": "paused", "title": "Bus Song | Nursery Rhymes #kids",
                               "paused_while_talking": True}
    result = await box.run("resume_video", "{}")
    assert result == ("ok: the video 'Bus Song' (from where it stopped) will start as soon as you have finished "
                      "speaking. Tell them briefly.")
    assert paused.media.status()["held"] is not None  # not before the reply was spoken
    assert await box.run_pending() == ["video resume"]
    assert ctx.state.state == RobotState.PLAYING_MEDIA and paused.media.player_state()["item"]["start_seconds"] == 28


async def test_tools_stop_a_paused_video(paused, ctx):
    from littlebot.chatgpt.tools import RobotToolbox

    box = RobotToolbox(ctx, mode="live", defer=True)
    await box.run("resume_video", "{}")
    assert [p.label for p in box.pending] == ["video resume"]
    assert await box.run("stop_video", "{}") == "ok: the video stopped"
    assert box.pending == [] and paused.media.status()["state"] == "idle"
    assert ctx.state.state == RobotState.IDLE
    assert await box.run("resume_video", "{}") == "ok: no video is paused"
