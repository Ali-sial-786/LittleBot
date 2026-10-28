"""Claude as the text model (littlebot/brain/providers/claude.py, littlebot/brain/text_model.py) with a fake
Anthropic client: the request it sends, streaming, tools, the fallback to ChatGPT, the key, the cloud switches."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import pytest
from pydantic import SecretStr

from littlebot.brain import text_model
from littlebot.brain.providers.base import BaseProvider, ProviderError, ProviderReply
from littlebot.brain.providers.claude import WEB_SEARCH, ClaudeProvider
from littlebot.brain.text_model import FallbackProvider, cloud_block, make_claude, text_provider
from littlebot.core.config import load_config, read_key_file
from littlebot.testing import FakeAnthropic, make_test_context

KEY = "sk-ant-test-0123456789abcdef"


class FakeChatGPT(BaseProvider):
    """ChatGPT's place: the same reply() parameters, records what it was asked."""

    name = "chatgpt"
    is_cloud = True
    model = "gpt-fake"
    supports_tools = True

    def __init__(self, text: str = "ChatGPT here.") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def reply(self, messages: list[dict[str, str]], *, system: str, max_tokens: int, timeout: float,
                    images: Any = None, tools: Any = None, run_tool: Any = None, max_tool_rounds: int = 6,
                    detail: Any = None, effort: Any = None, on_delta: Any = None,
                    final_tools: Any = ()) -> ProviderReply:
        self.calls.append({"messages": messages, "tools": tools, "effort": effort, "on_delta": on_delta})
        if on_delta is not None:
            on_delta(self.text)
        return ProviderReply(text=self.text, model=self.model)


def claude(client: FakeAnthropic, **kw: Any) -> ClaudeProvider:
    kw.setdefault("allowed", lambda: None)
    return ClaudeProvider(api_key=SecretStr(KEY), model="claude-haiku-5-5", client=client, **kw)


@pytest.fixture
def cctx():
    ctx = make_test_context(text={"provider": "claude"}, claude={"model": "claude-haiku-5-5"})
    ctx.config.anthropic_api_key = SecretStr(KEY)
    ctx.privacy.set(cloud_enabled=True)
    yield ctx
    ctx.db.close()


@pytest.fixture
def fake(monkeypatch):
    client = FakeAnthropic()
    monkeypatch.setattr(text_model, "client_factory", lambda: client)
    return client


# ------------------------------------------------------------------ choosing the provider
def test_chatgpt_stays_the_text_model_unless_claude_is_chosen(ctx):
    chatgpt = FakeChatGPT()
    assert ctx.config.text.provider == "chatgpt" and ctx.config.claude.model is None
    assert text_provider(ctx, chatgpt, purpose="handoff") is chatgpt


def test_claude_comes_with_chatgpt_behind_it_or_alone(cctx, fake):
    chatgpt = FakeChatGPT()
    p = text_provider(cctx, chatgpt, purpose="handoff")
    assert isinstance(p, FallbackProvider) and p.primary.model == "claude-haiku-5-5" and p.backup is chatgpt
    assert p.supports_tools and p.primary.purpose == "handoff"
    cctx.config.text.fallback = False
    alone = text_provider(cctx, chatgpt, purpose="screen")
    assert isinstance(alone, ClaudeProvider)


def test_the_live_voice_uses_the_chosen_text_model_for_hand_offs_and_the_screen(cctx, fake, tmp_path):
    pytest.importorskip("httpx")
    from littlebot.chatgpt.auth import ChatGPTAuth
    from littlebot.chatgpt.live import LiveVoiceService
    from littlebot.chatgpt.responses import ChatGPTProvider

    svc = LiveVoiceService(cctx, auth=ChatGPTAuth(tmp_path / "auth.json"))
    for p, purpose in ((svc.provider(), "handoff"), (svc.screen_provider(), "screen")):
        assert isinstance(p, FallbackProvider) and isinstance(p.backup, ChatGPTProvider)
        assert p.primary.purpose == p.backup.purpose == purpose
    status = svc.status()["text_model"]
    assert status == {"provider": "claude", "claude_model": "claude-haiku-5-5", "has_anthropic_key": True}
    assert KEY not in json.dumps(svc.status(), default=str)


def test_hand_offs_try_the_handoff_models_first_and_nothing_changes_without_them(ctx, tmp_path):
    from littlebot.chatgpt.auth import ChatGPTAuth
    from littlebot.chatgpt.live import LiveVoiceService

    cfg = ctx.config.chatgpt
    default = list(cfg.models)
    assert cfg.handoff_models == []
    svc = LiveVoiceService(ctx, auth=ChatGPTAuth(tmp_path / "auth.json"))
    assert svc.provider().models == default
    cfg.handoff_models = ["quick-model", default[0]]
    svc = LiveVoiceService(ctx, auth=ChatGPTAuth(tmp_path / "auth.json"))
    assert svc.provider().models == ["quick-model", *default]
    assert svc.screen_provider().models[0] != "quick-model"  # cards keep the camera looks' models


# ------------------------------------------------------------------ the request
async def test_the_request_is_streamed_cached_and_short(caplog):
    client = FakeAnthropic([{"text": ["The cow ", "says moo."]}])
    pieces: list[str] = []
    p = claude(client, purpose="handoff")
    with caplog.at_level(logging.INFO, logger="littlebot.text_model"):
        reply = await p.reply([{"role": "user", "content": "What does a cow say?"}], system="Be brief.",
                              max_tokens=200, timeout=5, on_delta=pieces.append)
    assert reply.text == "The cow says moo." and pieces == ["The cow ", "says moo."]
    req = client.calls[0]
    assert req["model"] == "claude-haiku-5-5" and req["max_tokens"] >= 2048
    assert req["system"] == [{"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}]
    assert req["output_config"] == {"effort": "low"} and "thinking" not in req  # adaptive (the default)
    assert req["messages"] == [{"role": "user", "content": "What does a cow say?"}]
    assert "temperature" not in req and "tools" not in req
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("text model:"))
    assert "provider=claude" in line and "purpose=handoff" in line and "result=ok" in line
    assert "first_token_ms=" in line and "first_token_ms=-" not in line and "total_ms=" in line


async def test_a_simple_request_goes_without_thinking():
    client = FakeAnthropic([{"text": "Hi!"}, {"text": "Hi again!"}])
    p = claude(client)
    await p.reply([{"role": "user", "content": "hi"}], system="s", max_tokens=50, timeout=5, effort="minimal")
    assert client.calls[0]["thinking"] == {"type": "disabled"} and client.calls[0]["output_config"]["effort"] == "low"
    await p.reply([{"role": "user", "content": "hi"}], system="s", max_tokens=50, timeout=5, effort="medium")
    assert "thinking" not in client.calls[1] and client.calls[1]["output_config"]["effort"] == "medium"


async def test_a_model_that_must_think_is_asked_again_with_thinking():
    class BadRequest(Exception):
        status_code = 400
        body = {"error": {"message": "thinking.type disabled is not supported"}}

    BadRequest.__name__ = "BadRequestError"
    client = FakeAnthropic([{"error": BadRequest()}, {"text": "Fine."}, {"text": "Again."}])
    p = claude(client)
    reply = await p.reply([{"role": "user", "content": "hi"}], system="s", max_tokens=50, timeout=5, effort="none")
    assert reply.text == "Fine." and "thinking" in client.calls[0] and "thinking" not in client.calls[1]
    await p.reply([{"role": "user", "content": "hi"}], system="s", max_tokens=50, timeout=5, effort="none")
    assert "thinking" not in client.calls[2]  # remembered


async def test_history_is_turned_into_alternating_turns_ending_with_the_person():
    client = FakeAnthropic([{"text": "ok"}])
    await claude(client).reply([{"role": "assistant", "content": "Hello!"}, {"role": "user", "content": "a"},
                                {"role": "user", "content": "b"}, {"role": "assistant", "content": "c"}],
                               system="s", max_tokens=50, timeout=5)
    assert client.calls[0]["messages"] == [{"role": "user", "content": "a\nb"}, {"role": "assistant", "content": "c"},
                                           {"role": "user", "content": "(go on)"}]


# ------------------------------------------------------------------ tools
TOOLS = [{"name": "set_volume", "description": "Change the volume.",
          "parameters": {"type": "object", "properties": {"level": {"type": "integer"}}}},
         {"type": "web_search", "name": "web_search", "description": "OpenAI's web search"}]


async def test_tools_are_run_and_their_results_sent_back():
    client = FakeAnthropic([{"text": "Let me do that.", "tools": [("tu1", "set_volume", {"level": 5})],
                             "thinking": True},
                            {"text": "Done, it's at five."}])
    ran: list[tuple[str, Any]] = []

    async def run_tool(name: str, arguments: str) -> str:
        ran.append((name, json.loads(arguments)))
        return "ok: volume 5"

    reply = await claude(client).reply([{"role": "user", "content": "volume five"}], system="s", max_tokens=200,
                                       timeout=5, tools=TOOLS, run_tool=run_tool)
    assert ran == [("set_volume", {"level": 5})] and reply.text == "Done, it's at five."
    first, second = client.calls
    assert first["tools"][0] == {"name": "set_volume", "description": "Change the volume.",
                                 "input_schema": TOOLS[0]["parameters"]}
    assert first["tools"][1] == {"type": WEB_SEARCH, "name": "web_search", "max_uses": 2}
    assert first["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assistant, results = second["messages"][-2:]
    assert assistant["role"] == "assistant" and assistant["content"][0]["type"] == "thinking"  # passed back as it was
    assert results == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1",
                                                     "content": "ok: volume 5"}]}


async def test_an_answer_with_only_a_final_tool_needs_no_second_request():
    client = FakeAnthropic([{"text": "Genghis Khan founded the Mongol Empire in 1206.",
                             "tools": [("tu1", "show_on_screen", {"card": "facts"})]}])
    ran: list[str] = []

    async def run_tool(name: str, arguments: str) -> str:
        ran.append(name)
        return "ok"

    reply = await claude(client).reply([{"role": "user", "content": "who was genghis khan"}], system="s",
                                       max_tokens=200, timeout=5, tools=TOOLS, run_tool=run_tool,
                                       final_tools=("show_on_screen",))
    assert ran == ["show_on_screen"] and len(client.calls) == 1 and reply.text.startswith("Genghis Khan")


async def test_web_search_off_or_refused_is_not_offered():
    class BadRequest(Exception):
        status_code = 400
        body = {"error": {"message": "web_search is not enabled for this organization"}}

    BadRequest.__name__ = "BadRequestError"
    client = FakeAnthropic([{"error": BadRequest()}, {"text": "From what I know: Paris."}, {"text": "x"}])
    p = claude(client)
    reply = await p.reply([{"role": "user", "content": "capital of France"}], system="s", max_tokens=50, timeout=5,
                          tools=TOOLS, run_tool=None)
    assert reply.text == "From what I know: Paris."
    assert "tools" in client.calls[0] and "tools" not in client.calls[1]
    await p.reply([{"role": "user", "content": "again"}], system="s", max_tokens=50, timeout=5, tools=TOOLS)
    assert "tools" not in client.calls[2]
    off = FakeAnthropic([{"text": "ok"}])
    await claude(off, web_search=False).reply([{"role": "user", "content": "q"}], system="s", max_tokens=50,
                                              timeout=5, tools=TOOLS)
    assert "tools" not in off.calls[0]


async def test_a_refusal_is_an_empty_answer_not_another_models():
    client = FakeAnthropic([{"text": "", "stop": "refusal"}])
    chatgpt = FakeChatGPT()
    reply = await FallbackProvider(claude(client), chatgpt).reply([{"role": "user", "content": "x"}], system="s",
                                                                  max_tokens=50, timeout=5)
    assert reply.text == "" and reply.stop_reason == "refusal" and not chatgpt.calls


# ------------------------------------------------------------------ the fallback
async def test_claude_failing_goes_to_chatgpt(caplog):
    class Overloaded(Exception):
        status_code = 529

    client = FakeAnthropic([{"error": Overloaded()}])
    chatgpt = FakeChatGPT()
    pieces: list[str] = []
    with caplog.at_level(logging.INFO):
        reply = await FallbackProvider(claude(client), chatgpt).reply(
            [{"role": "user", "content": "x"}], system="s", max_tokens=50, timeout=5, on_delta=pieces.append,
            effort="minimal", tools=TOOLS)
    assert reply.text == "ChatGPT here." and pieces == ["ChatGPT here."]
    assert chatgpt.calls[0]["effort"] == "minimal" and chatgpt.calls[0]["tools"] == TOOLS  # the same request
    assert any("Claude failed (overloaded)" in r.getMessage() for r in caplog.records)


async def test_a_slow_first_word_goes_to_chatgpt():
    client = FakeAnthropic([{"text": "too late", "delay": 2.0}])
    chatgpt = FakeChatGPT()
    t0 = asyncio.get_running_loop().time()
    reply = await FallbackProvider(claude(client, first_token_timeout=0.2), chatgpt).reply(
        [{"role": "user", "content": "x"}], system="s", max_tokens=50, timeout=10)
    assert reply.text == "ChatGPT here." and asyncio.get_running_loop().time() - t0 < 1.5


async def test_nothing_is_done_twice_after_claude_acted():
    class Broken(Exception):
        status_code = 500

    client = FakeAnthropic([{"tools": [("tu1", "play_video", {"query": "baby shark"})]}, {"error": Broken()}])
    chatgpt = FakeChatGPT()
    ran: list[str] = []

    async def run_tool(name: str, arguments: str) -> str:
        ran.append(name)
        return "ok"

    with pytest.raises(ProviderError):
        await FallbackProvider(claude(client), chatgpt).reply([{"role": "user", "content": "play baby shark"}],
                                                              system="s", max_tokens=50, timeout=5, tools=TOOLS,
                                                              run_tool=run_tool)
    assert ran == ["play_video"] and not chatgpt.calls


async def test_claude_not_set_up_means_chatgpt_answers(caplog):
    chatgpt = FakeChatGPT()
    client = FakeAnthropic()
    no_model = ClaudeProvider(api_key=SecretStr(KEY), model=None, client=client)
    no_key = ClaudeProvider(api_key=None, model="claude-haiku-5-5", client=client)
    for p, why in ((no_model, "no_model"), (no_key, "no_key")):
        with caplog.at_level(logging.WARNING):
            reply = await FallbackProvider(p, chatgpt).reply([{"role": "user", "content": "x"}], system="s",
                                                             max_tokens=50, timeout=5)
        assert reply.text == "ChatGPT here." and why in caplog.text
    assert not client.calls


# ------------------------------------------------------------------ offline and the cloud switch
async def test_the_cloud_switch_stops_claude_and_chatgpt(cctx, fake):
    chatgpt = FakeChatGPT()
    p = text_provider(cctx, chatgpt, purpose="handoff")
    cctx.privacy.set(cloud_enabled=False)
    assert cloud_block(cctx) == "cloud_off"
    with pytest.raises(ProviderError) as e:
        await p.reply([{"role": "user", "content": "x"}], system="s", max_tokens=50, timeout=5)
    assert e.value.kind == "cloud_off" and not fake.calls and not chatgpt.calls
    assert not await p.primary.warm() and fake.models_listed == 0


def test_offline_mode_loads_no_key_and_blocks_claude(tmp_path):
    keyfile = tmp_path / "anthropic_api_key"
    keyfile.write_text(KEY)
    os.chmod(keyfile, 0o600)
    env = {"ANTHROPIC_API_KEY": KEY}
    cfg = load_config(mode="offline", environ=env, use_dotenv=False, include_local=False,
                      overrides={"data_dir": str(tmp_path), "text": {"provider": "claude"}})
    assert cfg.anthropic_api_key is None
    ctx = make_test_context(fakes=True)
    ctx.config.mode = cfg.mode
    assert cloud_block(ctx) == "offline"
    p = make_claude(ctx, purpose="handoff", model="claude-haiku-5-5")
    assert p.missing() == "no_key" or p._blocked() == "offline"
    ctx.db.close()


# ------------------------------------------------------------------ the key
def test_the_key_comes_from_env_or_a_private_file_and_never_shows(tmp_path, caplog):
    cfg = load_config(environ={"ANTHROPIC_API_KEY": KEY}, use_dotenv=False, include_local=False,
                      overrides={"data_dir": str(tmp_path)})
    assert cfg.anthropic_api_key.get_secret_value() == KEY
    public = cfg.public_dict()
    assert public["has_anthropic_key"] is True and KEY not in json.dumps(public, default=str)
    assert KEY not in repr(cfg) and KEY not in str(cfg.anthropic_api_key)

    keyfile = tmp_path / "anthropic_api_key"
    keyfile.write_text(KEY + "\n")
    os.chmod(keyfile, 0o644)
    with caplog.at_level(logging.WARNING):
        assert read_key_file(keyfile) is None  # others could read it: not used
    assert "chmod 600" in caplog.text and KEY not in caplog.text
    os.chmod(keyfile, 0o600)
    cfg = load_config(environ={}, use_dotenv=False, include_local=False, overrides={"data_dir": str(tmp_path)})
    assert cfg.anthropic_api_key.get_secret_value() == KEY


async def test_the_key_is_never_in_a_request_a_log_or_a_status(caplog):
    class Denied(Exception):
        status_code = 401

    Denied.__name__ = "AuthenticationError"
    client = FakeAnthropic([{"text": "hello"}, {"error": Denied()}])
    p = claude(client, purpose="screen")
    with caplog.at_level(logging.DEBUG):
        await p.reply([{"role": "user", "content": "hi"}], system="s", max_tokens=50, timeout=5)
        with pytest.raises(ProviderError) as e:
            await p.reply([{"role": "user", "content": "hi"}], system="s", max_tokens=50, timeout=5)
    assert e.value.kind == "auth_error"
    for text in (json.dumps(client.calls), caplog.text, repr(p), json.dumps(p.describe()), str(e.value)):
        assert KEY not in text
    assert p.describe()["has_key"] is True


def test_the_dashboard_pages_never_get_the_key():
    from pathlib import Path

    static = Path(__file__).resolve().parents[2] / "littlebot" / "dashboard" / "static"
    for f in static.rglob("*.js"):
        text = f.read_text(encoding="utf-8")
        assert "anthropic_api_key" not in text and "ANTHROPIC_API_KEY\"" not in text, f


# ------------------------------------------------------------------ the decider
async def test_claude_answers_the_deciders_questions(cctx, fake):
    from littlebot.decide.base import Choice, Score, YesNo
    from littlebot.decide.service import Decider

    cctx.settings.set("decide.provider", "claude")
    fake.turns = [{"text": json.dumps({"card": {"choice": "facts", "confidence": 0.8},
                                       "answered": {"probability_yes": 0.9},
                                       "level": {"level": 2, "confidence": 0.6}})}]
    d = Decider(cctx)
    assert d.why_off() == ""
    answers = await d.ask("cards", {"they_asked": "who was genghis khan"},
                          {"card": Choice("Which card?", {"facts": "facts about one thing", "none": "no card"}),
                           "answered": YesNo("Was it answered?"), "level": Score("How much?", ("no", "some", "a lot"))})
    assert answers["card"].value == "facts" and answers["card"].confidence == pytest.approx(0.8)
    assert answers["answered"].p == pytest.approx(0.9) and answers["level"].value == 2.0
    req = fake.calls[0]
    assert req["output_config"]["format"]["type"] == "json_schema" and req["thinking"] == {"type": "disabled"}
    assert req["output_config"]["format"]["schema"]["properties"]["card"]["properties"]["choice"]["enum"] == [
        "facts", "none"]
    assert d.status()["has_anthropic_key"] is True


def test_the_decider_says_why_claude_is_off(cctx):
    from littlebot.decide.service import Decider

    cctx.settings.set("decide.provider", "claude")
    cctx.config.claude.model = None
    assert "no Claude model" in Decider(cctx).why_off()
    cctx.config.anthropic_api_key = None
    assert Decider(cctx).why_off() == "ANTHROPIC_API_KEY isn't set"
    cctx.config.anthropic_api_key = SecretStr(KEY)
    cctx.config.claude.model = "claude-haiku-5-5"
    cctx.privacy.set(cloud_enabled=False)
    assert "cloud" in Decider(cctx).why_off()
