"""The chatgpt conversation provider against a mocked Codex Responses endpoint."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from chatgpt_helpers import signed_in_auth

from littlebot.brain.providers.base import AuthenticationError, ProviderError, RateLimitError
from littlebot.chatgpt.auth import ChatGPTAuth
from littlebot.chatgpt.responses import ChatGPTProvider


def sse(*events: dict) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


OK = sse({"type": "response.created"},
         {"type": "response.output_text.delta", "delta": "Moo! "},
         {"type": "response.output_text.delta", "delta": "Cows say moo."},
         {"type": "response.completed", "response": {"usage": {"input_tokens": 12, "output_tokens": 5}}})


def provider(tmp_path, handler, models=("gpt-6-luna", "gpt-5.6-luna")):
    auth = signed_in_auth(tmp_path)
    return ChatGPTProvider(auth, models=list(models), transport=httpx.MockTransport(handler))


async def test_reply_streams_text_and_sends_the_codex_contract(tmp_path):
    seen = {}

    def handler(r: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"], seen["body"] = str(r.url), r.headers, json.loads(r.content)
        return httpx.Response(200, text=OK, headers={"content-type": "text/event-stream"})

    p = provider(tmp_path, handler)
    reply = await p.reply([{"role": "user", "content": "what does a cow say"}], system="Be a toy robot.",
                          max_tokens=50, timeout=5)
    assert reply.text == "Moo! Cows say moo." and reply.input_tokens == 12 and reply.model == "gpt-6-luna"
    assert seen["url"] == "https://chatgpt.com/backend-api/codex/responses"
    body = seen["body"]
    assert body["store"] is False and body["stream"] is True and body["instructions"] == "Be a toy robot."
    assert body["input"] == [{"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": "what does a cow say"}]}]
    for rejected in ("temperature", "max_output_tokens", "previous_response_id"):
        assert rejected not in body
    assert seen["headers"]["chatgpt-account-id"] == "acct-123"
    assert seen["headers"]["originator"] == "littlebot"
    assert seen["headers"]["accept"] == "text/event-stream"


async def test_unavailable_model_falls_back_to_the_next(tmp_path):
    models = []

    def handler(r: httpx.Request) -> httpx.Response:
        m = json.loads(r.content)["model"]
        models.append(m)
        if m == "gpt-6-luna":
            return httpx.Response(400, json={"detail": "The 'gpt-6-luna' model is not supported when using Codex."})
        return httpx.Response(200, text=OK)

    p = provider(tmp_path, handler)
    reply = await p.reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5)
    assert models == ["gpt-6-luna", "gpt-5.6-luna"] and reply.model == "gpt-5.6-luna"
    await p.reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5)
    assert models[-1] == "gpt-5.6-luna"  # remembers the one that works


async def test_401_refreshes_once_and_retries(tmp_path):
    calls = {"n": 0}

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.host == "auth.openai.com":
            from chatgpt_helpers import access_token
            return httpx.Response(200, json={"access_token": access_token(), "refresh_token": "r2"})
        calls["n"] += 1
        return httpx.Response(401) if calls["n"] == 1 else httpx.Response(200, text=OK)

    auth = signed_in_auth(tmp_path, transport=httpx.MockTransport(handler))
    p = ChatGPTProvider(auth, models=["m"], transport=httpx.MockTransport(handler))
    reply = await p.reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5)
    assert reply.text and calls["n"] == 2


async def test_usage_limit_maps_to_rate_limit_with_reset(tmp_path):
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"type": "usage_limit_reached", "resets_at": time.time() + 600}})

    with pytest.raises(RateLimitError) as e:
        await provider(tmp_path, handler).reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5)
    assert e.value.kind == "usage_limit" and 590 < e.value.retry_after <= 600


async def test_stream_failure_and_changed_endpoint(tmp_path):
    def failed(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse({"type": "response.failed", "response": {"error": {"code": "invalid_prompt"}}}))

    with pytest.raises(ProviderError) as e:
        await provider(tmp_path, failed).reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5)
    assert e.value.kind == "refused"

    def gone(r: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(ProviderError) as e:
        await provider(tmp_path, gone).reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5)
    assert e.value.kind == "endpoint_changed"


async def test_not_signed_in(tmp_path):
    p = ChatGPTProvider(ChatGPTAuth(tmp_path / "none.json"), models=["m"])
    assert not p.configured()
    with pytest.raises(AuthenticationError) as e:
        await p.reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5)
    assert e.value.kind == "not_signed_in"


async def test_403_is_not_treated_as_a_bad_sign_in(tmp_path):
    def forbidden(r: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "unsupported_country"}})

    with pytest.raises(ProviderError) as e:
        await provider(tmp_path, forbidden).reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5)
    assert e.value.kind == "forbidden" and not isinstance(e.value, AuthenticationError)


async def test_images_become_input_image_parts_on_the_last_user_message(tmp_path):
    import base64

    seen = {}

    def handler(r: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(r.content)
        return httpx.Response(200, text=OK)

    p = provider(tmp_path, handler)
    jpeg = b"\xff\xd8\xff\xe0fake-jpeg"
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello!"},
               {"role": "user", "content": "how many fingers am I holding?"}]
    await p.reply(history, system="x", max_tokens=10, timeout=5, images=[jpeg])
    items = seen["body"]["input"]
    assert items[0]["content"] == [{"type": "input_text", "text": "hi"}]  # earlier turns: text only
    assert items[1]["content"] == [{"type": "output_text", "text": "Hello!"}]
    # Codex CLI shape: images first, then the text; data URL with standard base64; detail always sent ("high")
    assert items[2]["content"] == [
        {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(),
         "detail": "high"},
        {"type": "input_text", "text": "how many fingers am I holding?"},
    ]
    assert len(items) == 3 and history[2] == {"role": "user", "content": "how many fingers am I holding?"}


async def test_no_images_keeps_the_body_unchanged(tmp_path):
    p = provider(tmp_path, lambda r: httpx.Response(200, text=OK))
    msgs = [{"role": "user", "content": "hi"}]
    assert p.build_body(msgs, system="x", model="m") == p.build_body(msgs, system="x", model="m", images=[])
    assert p.build_body(msgs, system="x", model="m", images=None)["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    # images without any user message still go in a user message
    body = p.build_body([{"role": "assistant", "content": "Hello!"}], system="x", model="m", images=[b"\xff\xd8"])
    assert body["input"][-1]["role"] == "user" and body["input"][-1]["content"][0]["type"] == "input_image"


async def test_rejected_photo_and_http_errors_carry_only_safe_labels(tmp_path):
    def bad_image(r: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {
            "message": "The image data you provided does not represent a valid image. secret-token-xyz",
            "type": "invalid_request_error", "code": "invalid_image", "param": "input[0].content[0].image_url"}})

    with pytest.raises(ProviderError) as e:
        await provider(tmp_path, bad_image).reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10,
                                                  timeout=5, images=[b"\xff\xd8"])
    assert e.value.kind == "invalid_image" and e.value.status_code == 400
    assert e.value.api_error == {"status": 400, "type": "invalid_request_error", "code": "invalid_image",
                                 "param": "input[0].content[0].image_url"}
    assert "secret" not in repr(e.value.api_error) and "secret" not in str(e.value)

    def odd(r: httpx.Request) -> httpx.Response:  # free text in a label slot is dropped, detail-only bodies too
        return httpx.Response(400, json={"error": {"type": "has spaces: secret", "code": "x" * 200}})

    with pytest.raises(ProviderError) as e:
        await provider(tmp_path, odd).reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5)
    assert e.value.kind == "bad_request" and e.value.api_error == {"status": 400, "type": None, "code": None,
                                                                   "param": None}

    def failed(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse({"type": "response.failed", "response": {"error": {
            "code": "server_error", "message": "secret"}}}))

    with pytest.raises(ProviderError) as e:
        await provider(tmp_path, failed).reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10,
                                               timeout=5)
    assert e.value.kind == "stream_error" and e.value.api_error == {"status": None, "type": None,
                                                                    "code": "server_error", "param": None}
