"""OpenAI's hosted web search in the chatgpt provider: the request, the stream it produces, and falling back
without it when the endpoint refuses it (mocked Codex Responses endpoint; the real one is untested)."""

from __future__ import annotations

import json

import httpx
import pytest
from chatgpt_helpers import signed_in_auth

from littlebot.brain.providers.base import ProviderError, RateLimitError
from littlebot.chatgpt.responses import ChatGPTProvider, strip_citations
from littlebot.chatgpt.tools import WEB_SEARCH


def sse(*events: dict) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def done(usage: int = 5) -> dict:
    return {"type": "response.completed", "response": {"usage": {"input_tokens": 10, "output_tokens": usage}}}


def delta(text: str) -> dict:
    return {"type": "response.output_text.delta", "delta": text}


SEARCH_ITEM = {"type": "web_search_call", "id": "ws_1", "status": "completed",
               "action": {"type": "search", "query": "toronto zoo opening hours",
                          "sources": [{"type": "url", "url": "https://www.torontozoo.com/"}]}}
SEARCHED = sse(
    {"type": "response.created"},
    {"type": "response.output_item.added", "output_index": 0,
     "item": {"type": "web_search_call", "id": "ws_1", "status": "in_progress"}},
    {"type": "response.web_search_call.in_progress", "item_id": "ws_1", "output_index": 0},
    {"type": "response.web_search_call.searching", "item_id": "ws_1", "output_index": 0},
    {"type": "response.web_search_call.completed", "item_id": "ws_1", "output_index": 0},
    {"type": "response.output_item.done", "output_index": 0, "item": SEARCH_ITEM},
    {"type": "response.output_item.added", "output_index": 1,
     "item": {"type": "message", "id": "msg_1", "role": "assistant", "status": "in_progress", "content": []}},
    delta("The zoo is open from 9 to 5 today "),
    delta("([torontozoo.com](https://www.torontozoo.com/?utm_source=openai))."),
    {"type": "response.output_text.annotation.added", "item_id": "msg_1", "annotation_index": 0,
     "annotation": {"type": "url_citation", "url": "https://www.torontozoo.com/", "title": "Toronto Zoo",
                    "start_index": 35, "end_index": 101}},
    {"type": "response.output_item.done", "output_index": 1,
     "item": {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
              "content": [{"type": "output_text", "text": "The zoo is open from 9 to 5 today.", "annotations": []}]}},
    done(),
)
PLAIN = sse(delta("Moo!"), done())
VOLUME = {"name": "set_volume", "description": "Set the volume.",
          "parameters": {"type": "object", "properties": {"percent": {"type": "integer"}}}}


def provider(tmp_path, handler) -> ChatGPTProvider:
    return ChatGPTProvider(signed_in_auth(tmp_path), models=["test-model"], transport=httpx.MockTransport(handler))


async def noop_tool(name: str, args: str) -> str:
    return "ok"


async def ask(p: ChatGPTProvider, **kw):
    return await p.reply([{"role": "user", "content": "when is the zoo open?"}], system="x", max_tokens=50,
                         timeout=5, **kw)


async def test_web_search_is_sent_as_the_hosted_tool_codex_uses(tmp_path):
    bodies: list[dict] = []

    def handler(r: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(r.content))
        return httpx.Response(200, text=PLAIN)

    p = provider(tmp_path, handler)
    await ask(p, tools=[VOLUME, WEB_SEARCH], run_tool=noop_tool)
    body = bodies[0]
    assert body["tools"][0]["type"] == "function" and body["tools"][0]["name"] == "set_volume"
    assert body["tools"][1] == {"type": "web_search"}  # exactly that: no name or description of ours
    assert body["tool_choice"] == "auto" and body["parallel_tool_calls"] is False
    assert body["store"] is False and body["stream"] is True
    # A hosted tool needs nothing run on the robot: offered without a tool runner too (functions aren't).
    await ask(p, tools=[VOLUME, WEB_SEARCH])
    assert bodies[1]["tools"] == [{"type": "web_search"}]


async def test_a_searched_answer_streams_and_comes_back_without_citations(tmp_path):
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SEARCHED, headers={"content-type": "text/event-stream"})

    pieces: list[str] = []
    reply = await ask(provider(tmp_path, handler), tools=[WEB_SEARCH], on_delta=pieces.append)
    assert reply.text == "The zoo is open from 9 to 5 today."
    assert pieces == ["The zoo is open from 9 to 5 today ",
                      "([torontozoo.com](https://www.torontozoo.com/?utm_source=openai))."]  # raw, as it streamed
    assert reply.input_tokens == 10 and reply.output_tokens == 5


async def test_words_before_a_search_are_not_part_of_the_answer(tmp_path):
    stream = sse(delta("Let me check! "),
                 {"type": "response.output_item.done", "item": SEARCH_ITEM},
                 delta("It opens at nine."), done())

    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    assert (await ask(provider(tmp_path, handler), tools=[WEB_SEARCH])).text == "It opens at nine."


async def test_a_search_and_a_function_call_are_replayed_like_codex(tmp_path):
    bodies: list[dict] = []
    first = sse({"type": "response.output_item.done", "item": SEARCH_ITEM},
                {"type": "response.output_item.done",
                 "item": {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "set_volume",
                          "arguments": '{"percent": 40}'}}, done())
    rounds = [first, PLAIN]

    def handler(r: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(r.content))
        return httpx.Response(200, text=rounds[len(bodies) - 1])

    reply = await ask(provider(tmp_path, handler), tools=[VOLUME, WEB_SEARCH], run_tool=noop_tool)
    assert reply.text == "Moo!"
    assert bodies[1]["input"][1:] == [
        # no id, no sources: what codex-rs's ResponseItem::WebSearchCall serialises
        {"type": "web_search_call", "action": {"type": "search", "query": "toronto zoo opening hours"},
         "status": "completed"},
        {"type": "function_call", "name": "set_volume", "arguments": '{"percent": 40}', "call_id": "c1"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
    ]
    assert bodies[1]["tools"][-1] == {"type": "web_search"}


@pytest.mark.parametrize("status, error", [
    (400, {"error": {"message": "Unsupported tool type: web_search", "type": "invalid_request_error",
                     "param": "tools[1].type", "code": "unsupported_value"}}),
    (400, {"detail": "Invalid value: 'web_search'. Supported values are: 'function' and 'custom'."}),
    (422, {"detail": [{"loc": ["body", "tools", 1, "type"], "msg": "Input should be 'function'"}]}),
    (403, {"detail": "Web search is not available for this account."}),
])
async def test_a_refused_web_search_is_dropped_and_not_offered_again(tmp_path, status, error):
    bodies: list[dict] = []

    def handler(r: httpx.Request) -> httpx.Response:
        body = json.loads(r.content)
        bodies.append(body)
        if any(t.get("type") == "web_search" for t in body.get("tools") or ()):
            return httpx.Response(status, json=error)
        return httpx.Response(200, text=PLAIN)

    p = provider(tmp_path, handler)
    reply = await ask(p, tools=[VOLUME, WEB_SEARCH], run_tool=noop_tool)
    assert reply.text == "Moo!" and len(bodies) == 2
    assert [t.get("name") for t in bodies[1]["tools"]] == ["set_volume"]  # the same request, without it
    assert {k: v for k, v in bodies[0].items() if k != "tools"} == {k: v for k, v in bodies[1].items() if k != "tools"}
    await ask(p, tools=[VOLUME, WEB_SEARCH], run_tool=noop_tool)
    assert len(bodies) == 3 and all(t["type"] == "function" for t in bodies[2]["tools"])  # remembered


async def test_an_unexplained_400_with_web_search_is_tried_without_it(tmp_path):
    bodies: list[dict] = []

    def handler(r: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(r.content))
        return httpx.Response(400, json={}) if len(bodies) == 1 else httpx.Response(200, text=PLAIN)

    p = provider(tmp_path, handler)
    assert (await ask(p, tools=[WEB_SEARCH])).text == "Moo!"
    assert "tools" in bodies[0] and "tools" not in bodies[1]
    await ask(p, tools=[WEB_SEARCH])
    assert "tools" not in bodies[2]


async def test_a_stream_error_naming_web_search_before_any_output(tmp_path):
    bodies: list[dict] = []
    refused = sse({"type": "response.created"},
                  {"type": "error", "code": "invalid_request_error", "message": "Tool 'web_search' is not enabled",
                   "param": "tools"})

    def handler(r: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(r.content))
        return httpx.Response(200, text=refused if len(bodies) == 1 else PLAIN)

    p = provider(tmp_path, handler)
    assert (await ask(p, tools=[WEB_SEARCH])).text == "Moo!" and "tools" not in bodies[1]
    await ask(p, tools=[WEB_SEARCH])
    assert "tools" not in bodies[2]


async def test_a_search_that_fails_midway_is_retried_without_it_but_offered_next_time(tmp_path):
    bodies: list[dict] = []
    failed = sse({"type": "response.output_item.added", "item": {"type": "web_search_call", "id": "ws_1"}},
                 {"type": "response.failed", "response": {"error": {"code": "server_error",
                                                                     "message": "web_search_call failed"}}})

    def handler(r: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(r.content))
        return httpx.Response(200, text=failed if len(bodies) == 1 else PLAIN)

    p = provider(tmp_path, handler)
    assert (await ask(p, tools=[WEB_SEARCH])).text == "Moo!" and "tools" not in bodies[1]
    await ask(p, tools=[WEB_SEARCH])
    assert bodies[2]["tools"] == [{"type": "web_search"}]  # one search going wrong isn't the tool refused


async def test_a_400_that_isnt_about_web_search_is_still_an_error(tmp_path):
    bodies: list[dict] = []

    def handler(r: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(r.content))
        return httpx.Response(400, json={"error": {"message": "Invalid schema for function 'set_volume'",
                                                   "param": "tools[0].parameters"}})

    p = provider(tmp_path, handler)
    with pytest.raises(ProviderError) as e:
        await ask(p, tools=[VOLUME, WEB_SEARCH], run_tool=noop_tool)
    assert e.value.kind == "bad_request" and len(bodies) == 2  # tried once without web search, then gave up
    with pytest.raises(ProviderError):
        await ask(p, tools=[VOLUME, WEB_SEARCH], run_tool=noop_tool)
    assert bodies[2]["tools"][-1] == {"type": "web_search"}  # not blamed on web search


async def test_sign_in_and_usage_limits_are_not_taken_for_a_refused_tool(tmp_path):
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"type": "usage_limit_reached", "message": "tools and more"}})

    with pytest.raises(RateLimitError):
        await ask(provider(tmp_path, handler), tools=[WEB_SEARCH])


async def test_without_web_search_nothing_changes(tmp_path):
    seen: list[dict] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(json.loads(r.content))
        return httpx.Response(200, text=sse(delta("See ([a](https://a.example)) you."), done()))

    reply = await ask(provider(tmp_path, handler))
    assert "tools" not in seen[0] and reply.text == "See ([a](https://a.example)) you."  # left to validate_reply


def test_citations_are_removed_for_speaking():
    assert strip_citations("It's 12°C in Toronto ([weather.gc.ca](https://weather.gc.ca/?utm_source=openai)). "
                           "Bring a hat!") == "It's 12°C in Toronto. Bring a hat!"
    assert strip_citations("Rain later ([a.com](https://a.com), [b.org](https://b.org/x)).") == "Rain later."
    assert strip_citations("The citeturn0search0score was 3 to 2.") == "The score was 3 to 2."
    assert strip_citations("Open till 5【3†source】.") == "Open till 5."
    assert strip_citations("See [the zoo](https://zoo.example/hours) today.") == "See the zoo today."
    assert strip_citations("More at https://example.com/page today.") == "More at today."
