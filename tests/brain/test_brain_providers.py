"""Anthropic / OpenAI adapters over httpx.MockTransport: request shape, parsing, typed errors."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from littlebot.brain.providers import (
    AnthropicProvider, AuthenticationError, NetworkError, OpenAIProvider, ProviderError, ProviderTimeoutError,
    RateLimitError,
)
from littlebot.brain.providers.anthropic import ANTHROPIC_VERSION

from brain_helpers import ANTHROPIC_KEY, OPENAI_KEY, FakeAPI, anthropic_response, error_response, openai_response

MSGS = [{"role": "user", "content": "what does the cow say"}]


def _anthropic(api: FakeAPI, **kw) -> AnthropicProvider:
    return AnthropicProvider(api_key=ANTHROPIC_KEY, transport=api.transport, **kw)


def _openai(api: FakeAPI, **kw) -> OpenAIProvider:
    return OpenAIProvider(api_key=OPENAI_KEY, transport=api.transport, **kw)


# ---------------------------------------------------------------- Anthropic
async def test_anthropic_request_shape():
    api = FakeAPI()
    p = _anthropic(api)
    await p.reply(MSGS, system="You are LittleBot.", max_tokens=120, timeout=5)
    req = api.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == ANTHROPIC_KEY
    assert req.headers["anthropic-version"] == ANTHROPIC_VERSION == "2023-06-01"
    assert req.headers["content-type"] == "application/json"
    assert "authorization" not in req.headers
    body = api.body()
    assert body["model"] == "claude-haiku-5-5"
    assert body["system"] == "You are LittleBot."
    assert body["messages"] == MSGS
    # Adaptive thinking counts toward max_tokens: reply budget + headroom, low effort.
    assert body["max_tokens"] == 120 + 400
    assert body["output_config"] == {"effort": "low"}
    for forbidden in ("thinking", "temperature", "top_p", "top_k", "stream", "tools"):
        assert forbidden not in body
    await p.aclose()


async def test_anthropic_thinking_disabled_and_custom_model():
    api = FakeAPI()
    p = _anthropic(api, model="claude-sonnet-5-5", effort=None, thinking="disabled",
                   base_url="https://proxy.example/")
    await p.reply(MSGS, system="s", max_tokens=100, timeout=5)
    body = api.body()
    assert str(api.requests[0].url) == "https://proxy.example/v1/messages"
    assert body["model"] == "claude-sonnet-5-5"
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 100
    assert "output_config" not in body


async def test_anthropic_parses_text_blocks_usage_and_stop_reason():
    api = FakeAPI(lambda req: httpx.Response(200, json={
        "type": "message", "model": "claude-haiku-5-5", "stop_reason": "end_turn",
        "content": [{"type": "thinking", "thinking": "", "signature": "x"},
                    {"type": "text", "text": "Moo! "}, {"type": "text", "text": "Can you moo?"}],
        "usage": {"input_tokens": 100, "output_tokens": 9, "cache_read_input_tokens": 50,
                  "cache_creation_input_tokens": 5},
    }))
    r = await _anthropic(api).reply(MSGS, system="s", max_tokens=50, timeout=5)
    assert r.text == "Moo! Can you moo?"
    assert (r.input_tokens, r.output_tokens) == (155, 9)
    assert r.model == "claude-haiku-5-5" and r.stop_reason == "end_turn"
    assert isinstance(r.latency_ms, int) and r.latency_ms >= 0


async def test_anthropic_refusal_is_reported():
    api = FakeAPI(lambda req: anthropic_response("", stop_reason="refusal", thinking=False))
    r = await _anthropic(api).reply(MSGS, system="s", max_tokens=50, timeout=5)
    assert r.stop_reason == "refusal" and r.text == ""


@pytest.mark.parametrize(("status", "exc", "kind"), [
    (401, AuthenticationError, "auth_error"),
    (403, AuthenticationError, "auth_error"),
    (429, RateLimitError, "rate_limited"),
    (500, ProviderError, "server_error"),
    (529, ProviderError, "overloaded"),
    (400, ProviderError, "bad_request"),
    (404, ProviderError, "not_found"),
    (402, ProviderError, "billing"),
])
async def test_anthropic_http_errors_are_typed(status, exc, kind):
    api = FakeAPI(lambda req: error_response(status, "some_error", "went wrong", **{"retry-after": "7"}))
    with pytest.raises(exc) as info:
        await _anthropic(api).reply(MSGS, system="s", max_tokens=50, timeout=5)
    assert info.value.kind == kind
    assert info.value.status_code == status
    assert info.value.retryable == (status in (429, 500, 529))
    if status == 429:
        assert info.value.retry_after == 7.0


async def test_anthropic_malformed_json():
    api = FakeAPI(lambda req: httpx.Response(200, content=b"<html>oops</html>"))
    with pytest.raises(ProviderError) as info:
        await _anthropic(api).reply(MSGS, system="s", max_tokens=50, timeout=5)
    assert info.value.kind == "malformed"


@pytest.mark.parametrize("payload", [{"type": "message"}, {"content": "not a list"}, ["a", "list"]])
async def test_anthropic_unexpected_shape(payload):
    api = FakeAPI(lambda req: httpx.Response(200, json=payload))
    with pytest.raises(ProviderError) as info:
        await _anthropic(api).reply(MSGS, system="s", max_tokens=50, timeout=5)
    assert info.value.kind == "malformed"


async def test_anthropic_timeout_maps_to_timeout_error():
    def boom(req):
        raise httpx.ReadTimeout("timed out", request=req)

    with pytest.raises(ProviderTimeoutError) as info:
        await _anthropic(FakeAPI(boom)).reply(MSGS, system="s", max_tokens=50, timeout=5)
    assert isinstance(info.value, TimeoutError) and info.value.kind == "timeout"


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError])
async def test_anthropic_network_errors(exc):
    def boom(req):
        raise exc("unreachable", request=req)

    with pytest.raises(NetworkError) as info:
        await _anthropic(FakeAPI(boom)).reply(MSGS, system="s", max_tokens=50, timeout=5)
    assert info.value.kind == "network"


async def test_slow_server_respects_httpx_timeout():
    async def slow(req):
        await asyncio.sleep(5)
        return anthropic_response()

    # MockTransport does not enforce httpx timeouts, so the engine also bounds every call
    # with asyncio.wait_for (see test_brain_engine). Here: the provider honours cancellation.
    p = _anthropic(FakeAPI(slow))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(p.reply(MSGS, system="s", max_tokens=50, timeout=0.1), 0.2)


async def test_no_key_means_not_configured_and_no_request():
    api = FakeAPI()
    for key in (None, "", "   "):
        p = AnthropicProvider(api_key=key, transport=api.transport)
        assert not p.configured()
        with pytest.raises(AuthenticationError) as info:
            await p.reply(MSGS, system="s", max_tokens=50, timeout=5)
        assert info.value.kind == "no_key"
    assert api.calls == 0


async def test_key_never_appears_in_errors():
    leak = f"Incorrect API key provided: {ANTHROPIC_KEY}. Also sk-ant-api03-OTHERKEY123456."
    api = FakeAPI(lambda req: error_response(401, "authentication_error", leak))
    with pytest.raises(AuthenticationError) as info:
        await _anthropic(api).reply(MSGS, system="s", max_tokens=50, timeout=5)
    text = str(info.value) + repr(info.value) + repr(info.value.args)
    assert ANTHROPIC_KEY not in text and "OTHERKEY" not in text and "[redacted]" in text
    p = _anthropic(api)
    assert ANTHROPIC_KEY not in repr(p.__dict__)  # SecretStr hides it


async def test_injected_client_is_used_and_not_closed():
    api = FakeAPI()
    client = httpx.AsyncClient(transport=api.transport)
    p = AnthropicProvider(api_key=ANTHROPIC_KEY, client=client)
    await p.reply(MSGS, system="s", max_tokens=10, timeout=5)
    await p.aclose()
    assert api.calls == 1 and not client.is_closed
    await client.aclose()


async def test_owned_client_closed_on_aclose_and_recreated():
    api = FakeAPI()
    p = _anthropic(api)
    await p.reply(MSGS, system="s", max_tokens=10, timeout=5)
    await p.aclose()
    await p.reply(MSGS, system="s", max_tokens=10, timeout=5)  # lazily re-created
    assert api.calls == 2
    await p.aclose()


# ------------------------------------------------------------------ OpenAI
async def test_openai_request_shape_and_parse():
    api = FakeAPI(lambda req: openai_response("The dog says woof!"))
    p = _openai(api)
    r = await p.reply(MSGS, system="Be kind.", max_tokens=80, timeout=5)
    req = api.requests[0]
    assert str(req.url) == "https://api.openai.com/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {OPENAI_KEY}"
    assert "x-api-key" not in req.headers
    body = api.body()
    assert body["model"] == "gpt-4o-mini"
    assert body["messages"][0] == {"role": "system", "content": "Be kind."}
    assert body["messages"][1:] == MSGS
    assert body["max_completion_tokens"] == 80
    assert "temperature" not in body
    assert r.text == "The dog says woof!"
    assert (r.input_tokens, r.output_tokens, r.model, r.stop_reason) == (250, 12, "gpt-4o-mini", "stop")


async def test_openai_refusal():
    api = FakeAPI(lambda req: openai_response(refusal="I can't help with that."))
    r = await _openai(api).reply(MSGS, system="s", max_tokens=10, timeout=5)
    assert r.text == "" and r.stop_reason == "refusal"


async def test_openai_errors_and_quota():
    leak = f"Incorrect API key provided: {OPENAI_KEY[:12]}***{OPENAI_KEY[-4:]}"
    api = FakeAPI(lambda req: httpx.Response(401, json={"error": {"message": leak, "type": "invalid_request_error",
                                                                 "code": "invalid_api_key"}}))
    with pytest.raises(AuthenticationError) as info:
        await _openai(api).reply(MSGS, system="s", max_tokens=10, timeout=5)
    assert OPENAI_KEY[:12] not in str(info.value)
    api = FakeAPI(lambda req: httpx.Response(429, json={"error": {"message": "You exceeded your current quota",
                                                                 "type": "insufficient_quota"}}))
    with pytest.raises(RateLimitError) as info:
        await _openai(api).reply(MSGS, system="s", max_tokens=10, timeout=5)
    assert info.value.kind == "quota_exceeded"


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{"message": "x"}]}])
async def test_openai_malformed(payload):
    api = FakeAPI(lambda req: httpx.Response(200, json=payload))
    with pytest.raises(ProviderError) as info:
        await _openai(api).reply(MSGS, system="s", max_tokens=10, timeout=5)
    assert info.value.kind == "malformed"


async def test_openai_network_error():
    def boom(req):
        raise httpx.ConnectError("no route to host", request=req)

    with pytest.raises(NetworkError):
        await _openai(FakeAPI(boom)).reply(MSGS, system="s", max_tokens=10, timeout=5)
