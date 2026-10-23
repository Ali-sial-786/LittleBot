"""Typed / push-to-talk chat with the chatgpt provider: LittleBot's tools (function calling)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
from brain_helpers import FakeAPI, cloud_context

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chatgpt"))
from chatgpt_helpers import signed_in_auth  # noqa: E402

from littlebot.core.state import RobotState  # noqa: E402


def sse(*events: dict) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def call(call_id: str, name: str, **args: Any) -> str:
    return sse({"type": "response.output_item.done", "item": {"type": "function_call", "id": f"fc_{call_id}",
                                                             "call_id": call_id, "name": name,
                                                             "arguments": json.dumps(args)}},
               {"type": "response.completed", "response": {"usage": {"input_tokens": 50, "output_tokens": 5}}})


def text(words: str) -> str:
    return sse({"type": "response.output_text.delta", "delta": words},
               {"type": "response.completed", "response": {"usage": {"input_tokens": 60, "output_tokens": 4}}})


class FakeMedia:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.played: list[tuple[str, str | None, Any]] = []
        self.volume = 0.5

    def status(self) -> dict[str, Any]:
        return {"state": "idle", "item": None, "volume": self.volume}

    def search_allowed(self) -> bool:
        return True

    def video_blocked_reason(self) -> str | None:
        return None

    async def search(self, query: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        return [{"video_id": "XqZsoesa55w", "title": "Baby Shark Dance", "channel": "Pinkfong", "duration_seconds": 137}]

    async def play_video(self, video_id: str, **kw: Any) -> dict[str, Any]:
        # what was spoken by the time the video starts
        self.played.append((video_id, kw.get("title"), list(self.ctx.audio.spoken)))
        return {}

    def list_items(self) -> list[dict[str, Any]]:
        return []

    def set_volume(self, v: float) -> float:
        self.volume = v
        return v


def chatgpt_ctx(tmp_path):
    ctx = cloud_context(provider="chatgpt")
    ctx.extras["chatgpt_auth"] = signed_in_auth(tmp_path)
    return ctx


def scripted(*rounds: str) -> FakeAPI:
    def respond(req: httpx.Request) -> httpx.Response:
        n = sum(1 for r in api.requests if r.url.host == "chatgpt.com")
        return httpx.Response(200, text=rounds[min(n, len(rounds)) - 1])

    api = FakeAPI(respond)
    return api


async def test_play_a_video_starts_after_the_reply_is_spoken(make_brain, tmp_path):
    ctx = chatgpt_ctx(tmp_path)
    ctx.media = FakeMedia(ctx)
    api = scripted(call("c1", "play_youtube", query="baby shark song"), text("Here comes Baby Shark!"))
    brain = await make_brain(ctx, api)
    result = await brain.handle_utterance("play baby shark on youtube")
    assert result["provider"] == "chatgpt" and result["error"] is None
    assert result["reply"] == "Here comes Baby Shark!" and result["tools_used"] == ["play_youtube"]
    assert result["actions"] == ["video XqZsoesa55w"]
    # the reply was spoken first; the video started after the turn (and its lease) ended
    assert ctx.media.played == [("XqZsoesa55w", "Baby Shark Dance", ["Here comes Baby Shark!"])]
    first, second = api.body(0), api.body(1)
    assert {t.get("name") for t in first["tools"]} >= {"play_youtube", "set_volume", "get_robot_status", "get_weather"}
    assert first["tools"][-1] == {"type": "web_search"}  # OpenAI's hosted web search, as Codex sends it
    assert "Things you can do (tools)" in first["instructions"]
    assert "call get_weather" in first["instructions"] and "use web search" in first["instructions"]
    assert second["input"][-1] == {"type": "function_call_output", "call_id": "c1",
                                   "output": "ok: the video 'Baby Shark Dance' will start as soon as you have "
                                             "finished speaking. Tell them briefly."}
    assert "_tools" not in result


async def test_turn_the_volume_up(make_brain, tmp_path):
    ctx = chatgpt_ctx(tmp_path)
    api = scripted(call("c1", "volume_up"), text("Is that better?"))
    brain = await make_brain(ctx, api)
    result = await brain.handle_utterance("turn the volume up")
    assert result["reply"] == "Is that better?" and ctx.audio.volume == 0.6
    assert "actions" not in result  # done during the turn, nothing queued
    assert ctx.state.state == RobotState.IDLE


async def test_go_to_sleep_after_saying_goodnight(make_brain, tmp_path):
    ctx = chatgpt_ctx(tmp_path)
    api = scripted(call("c1", "go_to_sleep"), text("Night night!"))
    brain = await make_brain(ctx, api)
    result = await brain.handle_utterance("time for bed, go to sleep")
    assert ctx.audio.spoken == ["Night night!"] and result["actions"] == ["sleep"]
    assert ctx.state.state == RobotState.SLEEPING


async def test_a_failed_answer_starts_nothing(make_brain, tmp_path):
    ctx = chatgpt_ctx(tmp_path)
    ctx.media = FakeMedia(ctx)
    api = scripted(call("c1", "play_youtube", query="baby shark"),
                   sse({"type": "response.failed", "response": {"error": {"code": "server_error"}}}))
    brain = await make_brain(ctx, api)
    result = await brain.handle_utterance("play baby shark")
    assert result["error"] == "stream_error" and ctx.media.played == []


async def test_tools_can_be_switched_off(make_brain, tmp_path):
    ctx = chatgpt_ctx(tmp_path)
    ctx.config.chatgpt.tools_enabled = False
    api = scripted(text("Moo!"))
    brain = await make_brain(ctx, api)
    await brain.handle_utterance("what does a cow say")
    assert "tools" not in api.body(0) and "Things you can do" not in api.body(0)["instructions"]


async def test_other_providers_get_no_tools(make_brain):
    ctx = cloud_context(provider="anthropic")
    api = FakeAPI()
    brain = await make_brain(ctx, api)
    result = await brain.handle_utterance("hello")
    assert result["provider"] == "anthropic" and "tools" not in api.body(0)
