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


# ---------------------------------------------------------------- function calling (tools)
REASONING = {"type": "reasoning", "id": "rs_1", "encrypted_content": "ENC", "summary": []}


def call_item(call_id: str, name: str, args: dict) -> dict:
    return {"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id, "name": name,
            "arguments": json.dumps(args), "status": "completed"}


def tool_round(*items: dict) -> str:
    return sse({"type": "response.created"},
               *({"type": "response.output_item.done", "item": it} for it in items),
               {"type": "response.completed", "response": {"usage": {"input_tokens": 100, "output_tokens": 7}}})


VOLUME_TOOL = {"name": "set_volume", "description": "Set the volume.",
               "parameters": {"type": "object", "properties": {"percent": {"type": "integer"}},
                              "required": ["percent"]}}


async def test_a_tool_call_runs_and_the_answer_continues_with_its_result(tmp_path):
    bodies: list[dict] = []
    rounds = [tool_round(REASONING, call_item("call_1", "set_volume", {"percent": 70})),
              sse({"type": "response.output_text.delta", "delta": "Louder now!"},
                  {"type": "response.completed", "response": {"usage": {"input_tokens": 120, "output_tokens": 3}}})]

    def handler(r: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(r.content))
        return httpx.Response(200, text=rounds[len(bodies) - 1])

    ran: list[tuple[str, str]] = []

    async def run_tool(name: str, args: str) -> str:
        ran.append((name, args))
        return "ok: speaker volume is now 70%"

    p = provider(tmp_path, handler)
    reply = await p.reply([{"role": "user", "content": "louder please"}], system="x", max_tokens=50, timeout=5,
                          tools=[VOLUME_TOOL], run_tool=run_tool)
    assert reply.text == "Louder now!"
    assert ran == [("set_volume", '{"percent": 70}')]
    assert reply.input_tokens == 220 and reply.output_tokens == 10  # both requests counted
    first, second = bodies
    # WatchGPT / Codex shape: function tools, auto choice, one call at a time; the contract is kept
    assert first["tools"] == [{"type": "function", "name": "set_volume", "description": "Set the volume.",
                               "strict": False, "parameters": VOLUME_TOOL["parameters"]}]
    assert first["tool_choice"] == "auto" and first["parallel_tool_calls"] is False
    for body in bodies:
        assert body["store"] is False and body["stream"] is True
        for rejected in ("temperature", "max_output_tokens", "previous_response_id"):
            assert rejected not in body
    # stateless endpoint: the history, then the model's items (no ids), then the result
    assert second["input"][0] == first["input"][0]
    assert second["input"][1:] == [
        {"type": "reasoning", "summary": [], "encrypted_content": "ENC"},
        {"type": "function_call", "name": "set_volume", "arguments": '{"percent": 70}', "call_id": "call_1"},
        {"type": "function_call_output", "call_id": "call_1", "output": "ok: speaker volume is now 70%"},
    ]
    assert second["tools"] == first["tools"]


async def test_tool_rounds_are_capped(tmp_path):
    n = {"requests": 0}

    def handler(r: httpx.Request) -> httpx.Response:
        n["requests"] += 1
        return httpx.Response(200, text=tool_round(call_item(f"c{n['requests']}", "set_volume", {"percent": 1})))

    async def run_tool(name: str, args: str) -> str:
        return "ok"

    p = provider(tmp_path, handler)
    reply = await p.reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5,
                          tools=[VOLUME_TOOL], run_tool=run_tool, max_tool_rounds=2)
    assert n["requests"] == 3 and reply.text == ""  # 2 tool rounds, then the last answer is returned


async def test_a_crashing_tool_is_reported_to_the_model(tmp_path):
    bodies: list[dict] = []
    rounds = [tool_round(call_item("c1", "set_volume", {"percent": 5})), OK]

    def handler(r: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(r.content))
        return httpx.Response(200, text=rounds[len(bodies) - 1])

    async def run_tool(name: str, args: str) -> str:
        raise RuntimeError("boom")

    reply = await provider(tmp_path, handler).reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10,
                                                    timeout=5, tools=[VOLUME_TOOL], run_tool=run_tool)
    assert reply.text == "Moo! Cows say moo."
    assert bodies[1]["input"][-1] == {"type": "function_call_output", "call_id": "c1",
                                      "output": "error: the action failed"}


async def test_without_a_tool_runner_no_tools_are_offered(tmp_path):
    seen = {}

    def handler(r: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(r.content)
        return httpx.Response(200, text=OK)

    p = provider(tmp_path, handler)
    await p.reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5, tools=[VOLUME_TOOL])
    for key in ("tools", "tool_choice", "parallel_tool_calls"):
        assert key not in seen["body"]
    assert p.supports_tools is True


async def test_assistant_message_items_are_replayed_as_plain_output_text(tmp_path):
    bodies: list[dict] = []
    message = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": "One moment.", "annotations": []}]}
    rounds = [tool_round(message, call_item("c1", "set_volume", {"percent": 5}),
                         {"type": "web_search_call", "id": "ws_1"}), OK]

    def handler(r: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(r.content))
        return httpx.Response(200, text=rounds[len(bodies) - 1])

    async def run_tool(name: str, args: str) -> str:
        return "ok"

    await provider(tmp_path, handler).reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10,
                                            timeout=5, tools=[VOLUME_TOOL], run_tool=run_tool)
    assert bodies[1]["input"][1:] == [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "One moment."}]},
        {"type": "function_call", "name": "set_volume", "arguments": '{"percent": 5}', "call_id": "c1"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
    ]
