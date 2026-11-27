"""The ``chatgpt`` conversation provider: replies on the parent's ChatGPT plan (no API key).

    POST https://chatgpt.com/backend-api/codex/responses        (server-sent events)
    headers: Authorization: Bearer <access token>, ChatGPT-Account-ID, originator, session-id
    body:    {model, instructions, input: [messages], store: false, stream: true,
              reasoning: {effort}, text: {verbosity: "low"}, include: [...], prompt_cache_key}

Contract as WatchGPT measured it (08.2026, same as Codex): ``store`` must be false and
``stream`` true, ``instructions`` must be non-empty, and ``temperature``,
``max_output_tokens`` and ``previous_response_id`` are rejected - so the length cap is
applied afterwards by ``validate_reply`` (as for every provider) and history is resent.

Function calling (ported from WatchGPT's ChatGptBrain): ``reply(..., tools=[specs], run_tool=fn)``
adds ``tools`` (``{"type": "function", name, description, strict: false, parameters}``),
``tool_choice: "auto"`` and ``parallel_tool_calls: false``. Each ``response.output_item.done`` item
is kept; a ``function_call`` item (name, JSON arguments, call_id) is run with ``run_tool`` and,
because the endpoint is stateless, the next request resends the history + the model's items
(reasoning, the call) + ``{"type": "function_call_output", call_id, output}``, up to
``max_tool_rounds`` times; then the text of the last answer is returned. Replayed items are sent
without their ``id`` (as Codex does: items aren't stored when ``store`` is false).

Web search (``chatgpt.web_search``): a *hosted* tool in ``tools`` (``{"type": "web_search"}``, as the Codex CLI
sends it when web search is on) that the endpoint runs itself, so it needs no ``run_tool``. Its output items
(``web_search_call``, announced with ``response.output_item.added`` / ``response.web_search_call.*`` events)
are kept and replayed the way Codex replays them (no ``id``); the answer is the text after the last search,
with inline citations (``([site](url))``, citation markers) removed. If the endpoint refuses the tool (a 4xx
or stream error naming tools / web search, or a 400 that doesn't say why), the same request is sent again
without it, and once that works the tool isn't offered again while LittleBot runs: web search can't break
an answer. Not yet tried against the real endpoint.

UNDOCUMENTED endpoint; tested here only against mocked responses.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Collection

import httpx

from littlebot.brain.providers.base import (
    AuthenticationError, BaseProvider, Message, NetworkError, ProviderError, ProviderReply, ProviderTimeoutError,
    RateLimitError,
)
from littlebot.brain.providers.timing import CallTimer
from littlebot.chatgpt.auth import CODEX_BASE, ChatGPTAuth, ChatGPTAuthError

MODEL_UNAVAILABLE = re.compile(r"model.*(not supported|does not exist|not found|unsupported)", re.I)
# Per-call reasoning effort (camera looks ask for the cheapest): when the endpoint rejects one for a
# model (HTTP 400 naming reasoning.effort) the next costlier one is tried, and remembered for next time.
EFFORTS = ("none", "minimal", "low", "medium", "high")
EFFORT_REJECTED = re.compile(r"\beffort\b", re.I)
KEEPALIVE_S = 120.0  # keep the TLS connection to chatgpt.com between requests (httpx's default is 5 s)
MAX_TOOL_ROUNDS = 6  # a request like "play baby shark" takes two: search, then play
MAX_TOOL_OUTPUT_CHARS = 4000
TOOL_FAILED = "error: the action failed"
# An endpoint error about the tools we sent (e.g. "Unsupported tool type: web_search", param "tools[3].type").
HOSTED_REJECTED = re.compile(r"web[_\s-]?search|\btools?\b|tool[_\s-]?(?:type|choice)|"
                             r"(?:unknown|unsupported|invalid) (?:tool|type)", re.I)
HOSTED_ITEMS = frozenset({"web_search_call"})  # output items a hosted tool adds (not ours to run)
_LIMIT_CODES = frozenset({"rate_limit_exceeded", "server_is_overloaded", "slow_down", "usage_limit_reached",
                          "usage_not_included"})

log = logging.getLogger("littlebot.chatgpt")

ToolRunner = Callable[[str, str], Awaitable[str]]  # (name, arguments JSON) -> short result for the model


class ChatGPTProvider(BaseProvider):
    name = "chatgpt"
    is_cloud = True
    supports_tools = True  # reply() takes tools= and run_tool= (other providers don't)

    def __init__(self, auth: ChatGPTAuth, *, models: list[str], effort: str = "low",
                 base_url: str = CODEX_BASE, transport: httpx.AsyncBaseTransport | None = None,
                 now: Any = time.time, purpose: str = "") -> None:
        self.auth = auth
        self.purpose = purpose  # what it is used for, in the timing log line (littlebot/brain/providers/timing.py)
        self.models = [m for m in models if m] or ["gpt-6-luna"]
        self._model_index = 0
        self.effort = effort
        self.url = f"{base_url.rstrip('/')}/responses"
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._session_id = str(uuid.uuid4())
        self._now = now
        self._rejected_efforts: set[tuple[str, str]] = set()  # (model, effort) the endpoint said no to
        self._rejected_hosted: set[str] = set()  # hosted tool types ("web_search") the endpoint refused

    @property
    def model(self) -> str:  # type: ignore[override]
        return self.models[min(self._model_index, len(self.models) - 1)]

    def configured(self) -> bool:
        return self.auth.signed_in

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "signed_in": self.auth.signed_in, "plan": self.auth.status().get("plan")}

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(transport=self._transport, follow_redirects=False,
                                             limits=httpx.Limits(keepalive_expiry=KEEPALIVE_S))
        return self._client

    async def warm(self, timeout: float = 5.0) -> bool:
        """Open the connection to chatgpt.com before it is needed (DNS + TCP + TLS, ~0.1-0.4 s on Wi-Fi),
        so the next request doesn't pay for it. A bare HEAD: no sign-in is sent and no model is used."""
        try:
            await asyncio.wait_for(self._get_client().request("HEAD", self.url), timeout)
            return True
        except Exception:  # noqa: BLE001 - best effort; the real request connects anyway
            return False

    def _effort_for(self, model: str, want: str | None) -> str:
        """``want`` (a per-call effort) unless the endpoint rejected it for ``model``; then the next costlier."""
        if not want:
            return self.effort
        chain = [*(EFFORTS[EFFORTS.index(want):] if want in EFFORTS else (want,)), self.effort]
        return next((e for e in chain if (model, e) not in self._rejected_efforts), self.effort)

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def build_body(self, messages: list[Message], *, system: str, model: str,
                   images: list[bytes] | None = None, tools: list[dict[str, Any]] | None = None,
                   extra_input: list[dict[str, Any]] | None = None, detail: str | None = None,
                   effort: str | None = None) -> dict[str, Any]:
        """``extra_input``: the model's earlier items and tool results, replayed after the messages.
        ``detail``: the images' detail ("low" | "high" | "auto"; default IMAGE_DETAIL); ``effort``:
        this request's reasoning effort (default: the provider's)."""
        items: list[dict[str, Any]] = []
        for m in messages:
            user = m.get("role") == "user"
            items.append({"type": "message", "role": "user" if user else "assistant",
                          "content": [{"type": "input_text" if user else "output_text", "text": str(m.get("content", ""))}]})
        parts = [_image_part(img, detail) for img in images or () if img]
        if parts:
            last_user = next((it for it in reversed(items) if it["role"] == "user"), None)
            if last_user is None:
                last_user = {"type": "message", "role": "user", "content": []}
                items.append(last_user)
            last_user["content"][:0] = parts  # images first, then the question (as `codex exec -i` sends it)
        body: dict[str, Any] = {
            "model": model,
            "instructions": system.strip() or "You are a friendly toy robot.",
            "input": items + list(extra_input or ()),
            "store": False,
            "stream": True,
            "reasoning": {"effort": effort or self.effort},
            "text": {"verbosity": "low"},
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": self._session_id,
        }
        functions = [hosted_tool(spec) if is_hosted(spec) else function_tool(spec) for spec in tools or ()]
        if functions:
            body["tools"] = functions
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = False
        return body

    async def reply(self, messages: list[Message], *, system: str, max_tokens: int, timeout: float,
                    images: list[bytes] | None = None, tools: list[dict[str, Any]] | None = None,
                    run_tool: ToolRunner | None = None, max_tool_rounds: int = MAX_TOOL_ROUNDS,
                    detail: str | None = None, effort: str | None = None,
                    on_delta: Callable[[str], Any] | None = None,
                    final_tools: Collection[str] = ()) -> ProviderReply:
        """``images``: JPEG bytes added to the LAST user message (sent once, never kept here).

        ``tools`` + ``run_tool``: function calling. ``timeout`` applies to each request; a reply
        that uses tools makes one more request per round.

        ``detail``: the images' detail level. ``effort``: reasoning effort for this call; if the
        endpoint rejects it for the model (400), the next costlier one is used (and remembered).
        ``on_delta(text)``: called with each piece of answer text as it streams in, before the
        reply is complete (unchecked text: the caller validates whatever it uses).
        ``final_tools``: tools whose result the answer doesn't need ("show_on_screen"): a reply that has its
        answer text and calls only these runs them and is the answer, with no second request for words.

        Hosted tools in ``tools`` (``{"type": "web_search", ...}``) are offered even without ``run_tool``;
        if the endpoint refuses one, the request is sent again without it (see the module docstring).

        Each call logs one timing line (provider, model, first word, total: littlebot/brain/providers/timing.py)."""
        timer = CallTimer(self.name, self.purpose)
        try:
            reply = await self._reply(messages, system=system, timeout=timeout, images=images, tools=tools,
                                      run_tool=run_tool, max_tool_rounds=max_tool_rounds, detail=detail,
                                      effort=effort, on_delta=on_delta, final_tools=final_tools, timer=timer)
        except ProviderError as e:
            timer.done(self.model, result=e.kind)
            raise
        timer.done(reply.model or self.model, tokens_in=reply.input_tokens, tokens_out=reply.output_tokens)
        return reply

    async def _reply(self, messages: list[Message], *, system: str, timeout: float, images: list[bytes] | None,
                     tools: list[dict[str, Any]] | None, run_tool: ToolRunner | None, max_tool_rounds: int,
                     detail: str | None, effort: str | None, on_delta: Callable[[str], Any] | None,
                     final_tools: Collection[str], timer: CallTimer) -> ProviderReply:
        t0 = time.monotonic()
        try:
            bearer = await self.auth.bearer()
        except ChatGPTAuthError as e:
            raise _auth_error(e) from None
        offered = [t for t in tools or () if run_tool is not None or is_hosted(t)]
        tried_refresh = False
        without_hosted = False  # the endpoint refused a hosted tool: this answer goes on without them
        forget_hosted: set[str] = set()  # ...and if that works, they aren't offered again
        replay: list[dict[str, Any]] = []
        rounds = 0
        tokens_in = tokens_out = 0
        while True:
            model = self.model
            used_effort = self._effort_for(model, effort)
            hosted = [] if without_hosted else [t for t in offered if is_hosted(t)
                                                and _hosted_type(t) not in self._rejected_hosted]
            sent = [t for t in offered if not is_hosted(t) or t in hosted]
            body = self.build_body(messages, system=system, model=model, images=images, tools=sent,
                                   extra_input=replay if hosted else [i for i in replay
                                                                      if i.get("type") not in HOSTED_ITEMS],
                                   detail=detail, effort=used_effort)
            try:
                result = await asyncio.wait_for(self._stream(bearer, body, on_delta, timer), timeout)
            except asyncio.TimeoutError:
                raise ProviderTimeoutError(f"chatgpt: no answer within {timeout:.0f}s") from None
            except _HostedRejected as e:
                without_hosted = True
                forget_hosted = {_hosted_type(t) for t in hosted} if e.remember else set()
                log.warning("chatgpt: the endpoint refused %s (%s); asking again without it",
                            ",".join(_hosted_type(t) for t in hosted) or "a hosted tool", e.api_error)
                continue
            except _EffortRejected as e:
                if used_effort == self.effort:  # even the configured effort: nothing to fall back to
                    if hosted:  # ...but maybe it's the effort with web search: try once without it
                        without_hosted, forget_hosted = True, {_hosted_type(t) for t in hosted}
                        continue
                    raise _tag(ProviderError("chatgpt: request not accepted", status_code=400, kind="bad_request"),
                               getattr(e, "api_error", {"status": 400})) from None
                self._rejected_efforts.add((model, used_effort))
                continue
            except ProviderError as e:
                if e.kind == "bad_request" and used_effort != self.effort:
                    # A 400 that doesn't say why: the per-call effort is the likely cause, and the configured
                    # one is known to work, so go straight back to it (remembered for this model).
                    cheaper = EFFORTS[:EFFORTS.index(self.effort)] if self.effort in EFFORTS else ()
                    self._rejected_efforts.update((model, x) for x in (*cheaper, used_effort))
                    continue
                if e.kind == "bad_request" and hosted:  # unexplained, with web search: try once without it
                    without_hosted, forget_hosted = True, {_hosted_type(t) for t in hosted}
                    log.warning("chatgpt: request not accepted (%s); asking again without %s",
                                getattr(e, "api_error", None), ",".join(sorted(forget_hosted)))
                    continue
                raise
            except _Unauthorized:
                if tried_refresh:
                    raise AuthenticationError("chatgpt: sign-in rejected", status_code=401, kind="expired") from None
                tried_refresh = True
                try:
                    bearer = await self.auth.on_unauthorized(bearer)
                except ChatGPTAuthError as e:
                    raise _auth_error(e) from None
                continue
            except _ModelUnavailable:
                if self._model_index + 1 >= len(self.models):
                    raise ProviderError("chatgpt: none of the configured models is available",
                                        status_code=400, kind="model_unavailable") from None
                self._model_index += 1
                continue
            if forget_hosted:  # it worked without them: don't offer them again while LittleBot runs
                self._rejected_hosted |= forget_hosted
                log.warning("chatgpt: not offering %s any more (the endpoint refused it)",
                            ",".join(sorted(forget_hosted)))
                forget_hosted = set()
            if result.searched:
                log.info("chatgpt: answered with a web search")
            tokens_in += int(result.usage.get("input_tokens") or 0)
            tokens_out += int(result.usage.get("output_tokens") or 0)
            if result.calls and run_tool is not None and final_tools and _is_answer(result.text) \
                    and all(call.name in final_tools for call in result.calls):
                for call in result.calls:  # the answer is written: these only go alongside it
                    await _run_tool(run_tool, call)
                return ProviderReply(text=result.text.strip(), input_tokens=tokens_in, output_tokens=tokens_out,
                                     model=model, latency_ms=int((time.monotonic() - t0) * 1000),
                                     stop_reason=result.stop)
            if result.calls and run_tool is not None and rounds < max(0, int(max_tool_rounds)):
                # Stateless endpoint: replay the model's items, then the results, and ask again.
                rounds += 1
                timer.rounds = rounds
                outputs = [{"type": "function_call_output", "call_id": call.call_id,
                            "output": await _run_tool(run_tool, call)} for call in result.calls]
                replay = replay + [it for it in (_replayable(i) for i in result.items) if it] + outputs
                continue
            return ProviderReply(text=result.text.strip(), input_tokens=tokens_in, output_tokens=tokens_out,
                                 model=model, latency_ms=int((time.monotonic() - t0) * 1000), stop_reason=result.stop)

    async def _stream(self, bearer: Any, body: dict[str, Any], on_delta: Callable[[str], Any] | None = None,
                      timer: CallTimer | None = None) -> "_StreamResult":
        headers = {**self.auth.headers(bearer), "Accept": "text/event-stream", "content-type": "application/json",
                   "session-id": self._session_id}
        res = _StreamResult()
        out: list[str] = []
        cut = 0  # the answer is the text after the last web search (any before it was a preamble)
        hosted = any(isinstance(t, dict) and t.get("type") != "function" for t in body.get("tools") or ())
        try:
            async with self._get_client().stream("POST", self.url, headers=headers, json=body,
                                                 timeout=httpx.Timeout(60.0, connect=8.0)) as resp:
                if resp.status_code >= 400:
                    raw = (await resp.aread()).decode("utf-8", "replace")
                    err = _http_error(resp.status_code, resp.headers, raw, self._now())
                    if hosted and _refuses_hosted(resp.status_code, raw):
                        raise _HostedRejected(getattr(err, "api_error", None), remember=True)
                    raise err
                async for event, data in _sse(resp.aiter_lines()):
                    kind = data.get("type") or event
                    if kind == "response.output_text.delta":
                        out.append(str(data.get("delta") or ""))
                        if timer is not None and out[-1]:
                            timer.first()
                        if on_delta is not None and out[-1]:
                            _notify(on_delta, out[-1])
                    elif kind in ("response.output_item.added", "response.output_item.done"):
                        item = data.get("item")
                        if timer is not None and isinstance(item, dict) and item.get("type") in (
                                "function_call", *HOSTED_ITEMS):
                            timer.first()  # a tool call is the answer's first piece too
                        if isinstance(item, dict) and item.get("type") in HOSTED_ITEMS:
                            res.searched, cut = True, len(out)
                        if kind == "response.output_item.done" and isinstance(item, dict):
                            res.items.append(item)
                            call = _function_call(item)
                            if call is not None:
                                res.calls.append(call)
                    elif kind in ("response.completed", "response.incomplete"):
                        r = data.get("response") if isinstance(data.get("response"), dict) else {}
                        res.usage = r.get("usage") if isinstance(r.get("usage"), dict) else {}
                        if kind == "response.incomplete":
                            res.stop = "max_tokens"
                        break
                    elif kind in ("response.failed", "error"):
                        if kind == "response.failed":
                            r = data.get("response") if isinstance(data.get("response"), dict) else {}
                            error = r.get("error")
                        else:
                            error = data.get("error") if isinstance(data.get("error"), dict) else data
                        if hosted and _names_hosted(error):
                            # refused before anything came: the tool itself; later: this search went wrong
                            raise _HostedRejected(_api_error(error),
                                                  remember=not (out or res.items or res.searched))
                        raise _stream_error(error)
                    # anything else (response.web_search_call.searching, annotations, ...) only reports progress
        except httpx.TimeoutException:
            raise ProviderTimeoutError("chatgpt: request timed out") from None
        except httpx.TransportError as e:
            raise NetworkError(f"chatgpt: network unavailable ({type(e).__name__})") from None
        if not out and not res.calls and res.stop == "end_turn" and not res.usage:
            raise ProviderError("chatgpt: the stream ended without an answer", kind="malformed")
        after = "".join(out[cut:])
        text = after if after.strip() else "".join(out)
        res.text = strip_citations(text) if res.searched else text
        return res


@dataclass(frozen=True)
class FunctionCall:
    call_id: str
    name: str
    arguments: str  # JSON text, as the model wrote it


@dataclass
class _StreamResult:
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    stop: str = "end_turn"
    calls: list[FunctionCall] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)  # every output item, for the replay
    searched: bool = False  # the answer used a hosted web search


def function_tool(spec: dict[str, Any]) -> dict[str, Any]:
    """A Responses API function tool from ``{name, description, parameters}`` (as WatchGPT sends it)."""
    params = spec.get("parameters") if isinstance(spec.get("parameters"), dict) else None
    return {"type": "function", "name": str(spec["name"]), "description": str(spec.get("description") or ""),
            "strict": False, "parameters": params or {"type": "object", "properties": {}}}


def is_hosted(spec: Any) -> bool:
    """A hosted tool spec (``{"type": "web_search", ...}``): run by the endpoint, not by ``run_tool``."""
    return isinstance(spec, dict) and str(spec.get("type") or "function") != "function"


def hosted_tool(spec: dict[str, Any]) -> dict[str, Any]:
    """The hosted tool as sent: its type and options, without our own ``name`` / ``description`` labels."""
    return {k: v for k, v in spec.items() if k not in ("name", "description")}


def _hosted_type(spec: dict[str, Any]) -> str:
    return str(spec.get("type"))


def _refuses_hosted(status: int, raw: str) -> bool:
    """An HTTP error that names the tools we sent (not a sign-in or a usage limit)."""
    return 400 <= status < 500 and status not in (401, 408, 429) and bool(HOSTED_REJECTED.search(raw or ""))


def _names_hosted(error: Any) -> bool:
    """A stream error that names the tools or web search (rate and usage limits are left alone)."""
    code = (error.get("code") or error.get("type")) if isinstance(error, dict) else None
    if code in _LIMIT_CODES:
        return False
    try:
        text = json.dumps(error) if not isinstance(error, str) else error
    except (TypeError, ValueError):
        return False
    return bool(HOSTED_REJECTED.search(text))


_CITATION_GROUP = re.compile(r"\s*\(\s*\[[^\]\n]*\]\([^)\s]*\)(?:\s*[,;]?\s*\[[^\]\n]*\]\([^)\s]*\))*\s*\)")
_CITATION_MARK = re.compile(r"[^]{0,200}|【[^】\n]{0,80}】|[-]")
_LINK = re.compile(r"\[([^\]\n]+)\]\(\s*https?://[^)\s]*\s*\)")
_BARE_URL = re.compile(r"\s*\(?<?https?://[^\s)>]+>?\)?")


def strip_citations(text: str) -> str:
    """A web-search answer without its inline citations, for speaking: " ([bbc.co.uk](https://...))" and
    citation markers go, a ``[label](url)`` link keeps its label, a bare URL goes."""
    t = _CITATION_GROUP.sub("", text or "")
    t = _CITATION_MARK.sub("", t)
    t = _LINK.sub(r"\1", t)
    t = _BARE_URL.sub("", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return re.sub(r"[ \t]+([.,;:!?])", r"\1", t).strip()


def _function_call(item: dict[str, Any]) -> FunctionCall | None:
    if item.get("type") != "function_call":
        return None
    call_id, name = item.get("call_id"), item.get("name")
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
        return None
    args = item.get("arguments")
    return FunctionCall(call_id, name, args if isinstance(args, str) and args.strip() else "{}")


def _replayable(item: dict[str, Any]) -> dict[str, Any] | None:
    """An output item as the next request's input, in the shape Codex sends (no ``id``/``status``)."""
    kind = item.get("type")
    if kind == "reasoning":
        out: dict[str, Any] = {"type": "reasoning", "summary": item.get("summary") if isinstance(
            item.get("summary"), list) else []}
        if item.get("encrypted_content"):
            out["encrypted_content"] = item["encrypted_content"]
        return out
    if kind == "function_call":
        call = _function_call(item)
        return None if call is None else {"type": "function_call", "name": call.name, "arguments": call.arguments,
                                          "call_id": call.call_id}
    if kind == "message":
        content = item.get("content") if isinstance(item.get("content"), list) else []
        text = "".join(str(c.get("text") or "") for c in content
                       if isinstance(c, dict) and c.get("type") == "output_text")
        return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]} \
            if text else None
    if kind in HOSTED_ITEMS:
        # As Codex replays a web search (codex-rs ResponseItem::WebSearchCall: no id, status, the action).
        action = item.get("action")
        if not isinstance(action, dict) or not isinstance(action.get("type"), str):
            return None
        out = {"type": kind, "action": {k: action[k] for k in ("type", "query", "url", "pattern")
                                        if isinstance(action.get(k), str)}}
        if isinstance(item.get("status"), str):
            out["status"] = item["status"]
        return out
    return None  # anything else isn't ours to replay


# Words before a tool call that only say what's coming ("Let me put that on the screen."): not an answer.
_PREAMBLE = re.compile(r"^\W*(?:(?:ok(?:ay)?|sure|alright|right)\W+)?(?:let me|let's|i'?ll|i will|i'?m going to|"
                       r"one (?:moment|sec)|just a (?:moment|sec)|hold on|give me|checking|looking|putting|showing|"
                       r"here(?:'s| is| are) (?:a|the) (?:card|picture))\b", re.I)
ANSWER_MIN_WORDS = 6


def _is_answer(text: str) -> bool:
    """``text`` (a reply's words next to its tool calls) is an answer, not a preamble."""
    return len(text.split()) >= ANSWER_MIN_WORDS and not _PREAMBLE.match(text)


async def _run_tool(run_tool: ToolRunner, call: FunctionCall) -> str:
    """Run one call; a crash becomes an error result for the model (never the conversation's end)."""
    try:
        out = await run_tool(call.name, call.arguments)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return TOOL_FAILED
    text = str(out if out is not None else "ok")
    return text if len(text) <= MAX_TOOL_OUTPUT_CHARS else text[:MAX_TOOL_OUTPUT_CHARS - 12] + " [truncated]"


IMAGE_DETAIL = "high"  # codex-rs/protocol/src/models.rs: DEFAULT_IMAGE_DETAIL = ImageDetail::High


def _image_part(jpeg: bytes, detail: str | None = None) -> dict[str, Any]:
    """A Responses API ``input_image`` content part with the JPEG inlined as a data URL.

    The shape the Codex CLI sends (openai/codex, codex-rs/protocol/src/models.rs):
    ``ContentItem::InputImage { image: ImageReference::Inline { image_url }, detail: Some(detail) }``
    serialises (serde tag "type", snake_case, image flattened) to
    ``{"type": "input_image", "image_url": "data:<mime>;base64,<standard base64>", "detail": "high"}``
    (``data_url_from_bytes`` in codex-rs/utils/image/src/lib.rs). ``detail`` is always sent: Codex
    defaults it to "high", and the OpenAI spec marks it required (openai-python ``ResponseInputImageParam``).
    Codex shrinks images to fit 2048 px; ours are already 768 px wide. ``detail`` "low" (one
    512 px view, fewer image tokens: faster) suits simple questions; "high" counting and reading.
    """
    return {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
            "detail": detail if detail in ("low", "high", "auto") else IMAGE_DETAIL}


class _Unauthorized(Exception):
    pass


class _ModelUnavailable(Exception):
    pass


class _EffortRejected(Exception):
    pass


class _HostedRejected(Exception):
    """The endpoint refused a hosted tool (``remember``: the tool itself, not one search going wrong)."""

    def __init__(self, api_error: Any = None, *, remember: bool = True) -> None:
        super().__init__("hosted tool refused")
        self.api_error = api_error
        self.remember = remember


def _notify(on_delta: Callable[[str], Any], text: str) -> None:
    try:
        on_delta(text)
    except Exception:  # noqa: BLE001 - a listener's bug must not break the answer
        logging.getLogger("littlebot.chatgpt").debug("on_delta listener failed", exc_info=True)


async def _sse(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield (event name, JSON data) for each server-sent event."""
    event, data = "", []
    async for line in lines:
        if line == "":
            if data:
                yield event, _json("\n".join(data))
            event, data = "", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, _json("\n".join(data))


def _json(text: str) -> dict[str, Any]:
    if text.strip() == "[DONE]":
        return {"type": "response.completed"}
    try:
        obj = json.loads(text)
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _auth_error(e: ChatGPTAuthError) -> ProviderError:
    if e.kind == "offline":
        return NetworkError(f"chatgpt: {e}")
    return AuthenticationError(f"chatgpt: {e}", status_code=e.status, kind=e.kind)


_SAFE_LABEL = re.compile(r"[A-Za-z0-9_.\-\[\]]{1,80}")


def _api_error(error: Any, status: int | None = None) -> dict[str, Any]:
    """The loggable part of an endpoint error: status plus ``type``/``code``/``param`` labels.

    Only short identifier-like labels are kept (``param`` is a field path such as
    ``input[0].content[1].image_url``); the error ``message``/``detail`` is never kept, so a
    log line can never carry the prompt, the image or a token."""
    out: dict[str, Any] = {"status": status}
    for key in ("type", "code", "param"):
        v = error.get(key) if isinstance(error, dict) else (error if key == "type" else None)
        out[key] = v if isinstance(v, str) and _SAFE_LABEL.fullmatch(v) else None
    return out


def _tag(e: Exception, api_error: dict[str, Any]) -> Exception:
    e.api_error = api_error  # type: ignore[attr-defined]
    return e


def _http_error(status: int, headers: httpx.Headers, body: str, now: float) -> Exception:
    try:
        obj = json.loads(body)
    except ValueError:
        obj = {}
    err = obj.get("error") if isinstance(obj, dict) else None
    return _tag(_http_error_kind(status, headers, err, obj, now), _api_error(err, status))


def _http_error_kind(status: int, headers: httpx.Headers, err: Any, obj: Any, now: float) -> Exception:
    etype = (err.get("type") or err.get("code")) if isinstance(err, dict) else (err if isinstance(err, str) else None)
    detail = str((obj.get("detail") if isinstance(obj, dict) else None) or
                 (err.get("message") if isinstance(err, dict) else "") or "")
    if status == 401:
        return _Unauthorized()
    if status == 429:
        if etype == "usage_limit_reached":
            resets = err.get("resets_at") if isinstance(err, dict) else None
            if not isinstance(resets, (int, float)):
                try:
                    resets = float(headers.get("x-codex-primary-reset-at", ""))
                except ValueError:
                    resets = None
            retry = max(0.0, float(resets) - now) if isinstance(resets, (int, float)) else None
            return RateLimitError("chatgpt: ChatGPT plan usage limit reached", retry_after=retry, kind="usage_limit")
        if etype == "usage_not_included":
            return ProviderError("chatgpt: this ChatGPT plan does not include this", status_code=429,
                                 kind="plan_not_included")
        return RateLimitError("chatgpt: rate limited", retry_after=_retry_after(headers))
    if status == 503 and etype in ("server_is_overloaded", "slow_down"):
        return RateLimitError("chatgpt: busy", status_code=503, retry_after=_retry_after(headers), kind="overloaded")
    param = err.get("param") if isinstance(err, dict) else None
    if status == 400 and (param == "reasoning.effort" or EFFORT_REJECTED.search(detail)):
        return _EffortRejected()  # checked first: "... effort 'none' is not supported with this model"
    if status == 400 and MODEL_UNAVAILABLE.search(detail):
        return _ModelUnavailable()
    if status == 400 and "does not represent a valid image" in detail:  # Codex: CodexErr::InvalidImageRequest
        return ProviderError("chatgpt: the photo was not accepted", status_code=400, kind="invalid_image")
    if status == 403:  # account / region / plan not allowed: signing in again would not help
        return ProviderError("chatgpt: not available for this ChatGPT account or region", status_code=403,
                             kind="forbidden")
    if status in (404, 405, 410):
        return ProviderError("chatgpt: the ChatGPT endpoint changed or is unavailable", status_code=status,
                             kind="endpoint_changed")
    if status == 400:
        return ProviderError("chatgpt: request not accepted", status_code=400, kind="bad_request")
    err_ = ProviderError(f"chatgpt: HTTP {status}", status_code=status, kind="server_error")
    err_.retryable = status >= 500
    return err_


def _retry_after(headers: httpx.Headers) -> float | None:
    try:
        return float(headers.get("retry-after-ms", "")) / 1000
    except ValueError:
        pass
    try:
        return float(headers.get("retry-after", ""))
    except ValueError:
        return None


def _stream_error(error: Any) -> ProviderError:
    e = _stream_error_kind(error)
    e.api_error = _api_error(error)  # type: ignore[attr-defined]
    return e


def _stream_error_kind(error: Any) -> ProviderError:
    code = (error.get("code") or error.get("type")) if isinstance(error, dict) else None
    if code in ("rate_limit_exceeded", "server_is_overloaded", "slow_down"):
        return RateLimitError("chatgpt: rate limited", kind="rate_limited")
    if code == "usage_limit_reached":
        return RateLimitError("chatgpt: ChatGPT plan usage limit reached", kind="usage_limit")
    if code == "usage_not_included":
        return ProviderError("chatgpt: this ChatGPT plan does not include this", kind="plan_not_included")
    if code in ("cyber_policy", "bio_policy", "invalid_prompt"):
        return ProviderError("chatgpt: request refused", kind="refused")
    return ProviderError(f"chatgpt: the answer failed ({code or 'unknown'})", kind="stream_error")
