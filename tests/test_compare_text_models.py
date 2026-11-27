"""scripts/compare_text_models.py: the same card, hand-off and decider requests to ChatGPT and Claude, timed."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from littlebot.brain import text_model
from littlebot.testing import FakeAnthropic, make_test_context

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "chatgpt"))
from chatgpt_helpers import signed_in_auth  # noqa: E402


def _script():
    spec = importlib.util.spec_from_file_location("compare_text_models", ROOT / "scripts" / "compare_text_models.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _sse(text: str) -> str:
    events = [{"type": "response.created"}, {"type": "response.output_text.delta", "delta": text},
              {"type": "response.completed", "response": {"usage": {"input_tokens": 900, "output_tokens": 20}}}]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from littlebot.chatgpt import responses

    ctx = make_test_context(claude={"model": "claude-haiku-5-5"})
    ctx.config.anthropic_api_key = SecretStr("sk-ant-test-0123456789abcdef")
    ctx.privacy.set(cloud_enabled=True)
    ctx.extras["chatgpt_auth"] = signed_in_auth(tmp_path)
    asked: list[dict] = []

    def handler(r: httpx.Request) -> httpx.Response:
        if r.method == "HEAD":
            return httpx.Response(200)
        body = json.loads(r.content)
        asked.append(body)
        words = '{"do": "none", "why": "a test"}' if "JSON" in json.dumps(body["input"]) else "Sunlight scatters."
        return httpx.Response(200, text=_sse(words), headers={"content-type": "text/event-stream"})

    real = responses.ChatGPTProvider

    class Mocked(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            super().__init__(*a, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(responses, "ChatGPTProvider", Mocked)
    client = FakeAnthropic([{"text": '{"do": "none", "why": "a test"}'}, {"text": "Sunlight scatters off air."},
                            {"text": json.dumps({"route": {"choice": "robot", "confidence": 0.9}})}])
    monkeypatch.setattr(text_model, "client_factory", lambda: client)
    mod = _script()
    monkeypatch.setattr(mod, "build_context", lambda mode: ctx)
    return mod, ctx, client, asked


async def test_both_models_get_the_robots_own_requests_and_the_times_are_printed(setup, capsys):
    mod, _ctx, client, asked = setup
    assert await mod.run(argparse.Namespace(rounds=1, only=None, mode="pi")) == 0
    out = capsys.readouterr().out
    for request in ("card", "hand-off", "decider"):
        assert f"{request:9} claude" in out
    assert "card      chatgpt" in out and "hand-off  chatgpt" in out
    assert "route=robot" in out and "Claude cost for this run" in out
    card, handoff, decide = client.calls

    def last_words(req):
        content = req["messages"][-1]["content"]
        return content if isinstance(content, str) else content[-1]["text"]

    assert card["system"][0]["text"] and mod.CARD_HEARD in last_words(card) and "JSON" in last_words(card)
    assert last_words(handoff) == mod.HANDOFF_ASK
    assert all(t.get("type") != "web_search_20250305" for t in handoff["tools"])  # a light hand-off
    assert decide["output_config"]["format"]["type"] == "json_schema"
    assert len(asked) == 2  # ChatGPT: the card and the hand-off (it isn't a decision model)


async def test_nothing_is_asked_with_the_cloud_switch_off(setup, capsys):
    mod, ctx, client, asked = setup
    ctx.privacy.set(cloud_enabled=False)
    assert await mod.run(argparse.Namespace(rounds=1, only=None, mode="pi")) == 2
    assert "Not asking any model" in capsys.readouterr().out
    assert not client.calls and not asked


# ------------------------------------------------------------------ scripts/chatgpt_models.py
def _models_script():
    spec = importlib.util.spec_from_file_location("chatgpt_models", ROOT / "scripts" / "chatgpt_models.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


async def test_the_model_list_shows_names_and_labels_only(tmp_path, capsys):
    ctx = make_test_context()
    ctx.privacy.set(cloud_enabled=True)
    ctx.extras["chatgpt_auth"] = signed_in_auth(tmp_path)
    seen = {}

    def handler(r: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"] = str(r.url), r.headers.get("authorization", "")
        return httpx.Response(200, json={"models": [
            {"slug": "gpt-6-luna", "display_name": "GPT-6 Luna", "visibility": "list",
             "supported_reasoning_levels": [{"effort": "low", "description": "fast"}, {"effort": "high"}],
             "secret_thing": "tok-should-not-show"},
            {"slug": "gpt-6-mini-test", "description": "small"}]})

    mod = _models_script()
    args = argparse.Namespace(json=False, mode="pi")
    assert await mod.run(args, ctx=ctx, transport=httpx.MockTransport(handler)) == 0
    out = capsys.readouterr().out
    assert seen["url"].startswith("https://chatgpt.com/backend-api/codex/models") and seen["auth"].startswith("Bearer ")
    assert "gpt-6-luna  (GPT-6 Luna, effort low/high, list) <- used now" in out and "gpt-6-mini-test" in out
    assert "tok-should-not-show" not in out and seen["auth"][7:] not in out
    assert "live voice:             gpt-live-1-codex" in out
    ctx.db.close()


async def test_the_log_replay_sends_the_rebuilt_requests_and_prints_the_logged_times(setup, capsys):
    mod, _ctx, client, asked = setup
    client.turns = [{"text": '{"do": "none", "why": "a test"}'}] * 10
    assert await mod.run(argparse.Namespace(rounds=1, only=["claude"], mode="pi", from_log=True,
                                            claude_model=["claude-haiku-5-5", "claude-haiku-4-5"])) == 0
    out = capsys.readouterr().out
    assert len(client.calls) == 2 * len(mod.LOG_REQUESTS) and not asked  # Claude only, once each per model
    assert [c["model"] for c in client.calls[:2]] == ["claude-haiku-5-5", "claude-haiku-4-5"]
    assert "output_config" in client.calls[0] and "output_config" not in client.calls[1]
    assert "claude-haiku-4-5 cost for this run" in out and "claude-haiku-5-5 cost for this run" in out
    client.calls = client.calls[::2]  # Haiku 5.5's requests, in order
    assert "12181 (+11355)" in out and "2940 (+2875), 3661 (+3523), 7240 (+7178)" in out
    full = client.calls[1]
    assert any(str(t.get("type", "")).startswith("web_search") for t in full["tools"])  # the full hand-off searches
    assert full["messages"][0]["content"] == mod._KUBLAI  # with the conversation before it
    assert not any(str(t.get("type", "")).startswith("web_search") for t in client.calls[3]["tools"])
