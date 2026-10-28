"""The ``claude`` text provider: Claude on the Anthropic API, through the official SDK (``pip install -e '.[claude]'``).

The same ``reply()`` as ``littlebot.chatgpt.responses.ChatGPTProvider``, so the screen director, its cards and the
live voice's hand-offs (with their tool calls) can use either (``text.provider``, littlebot/brain/text_model.py):

* streamed (``messages.stream``): each piece of the answer's words goes to ``on_delta`` as it arrives, so the first
  sentence can be used before the rest is written;
* tools: LittleBot's functions (``{name, description, parameters}``) become Claude tools (``input_schema``), run with
  ``run_tool(name, arguments JSON)``; the model's turn (thinking blocks included, unchanged) and the
  ``tool_result`` blocks are sent back, up to ``max_tool_rounds``. ``final_tools`` as for ChatGPT: an answer whose
  only calls are those runs them and is the answer. OpenAI's hosted web search becomes Anthropic's
  (``web_search_20250305``, ``claude.web_search``); if the account refuses it, the request goes again without it
  and it isn't offered again while LittleBot runs;
* fast: ``max_retries=0`` (the fallback to ChatGPT is the retry), no first word within
  ``first_token_timeout`` -> ``ProviderTimeoutError``; a per-call effort of "none" / "minimal" (LittleBot's simple
  requests) asks for no thinking at low effort; the fixed part (tools + system prompt) is marked for prompt caching
  (``cache_control`` on the system block: tools are cached with it);
* never logs a prompt, an answer or the key; errors carry only a status and a short label.

The key (``ANTHROPIC_API_KEY``: .env, or data/anthropic_api_key with mode 0600) is passed in as a SecretStr and
only ever given to the SDK client. Refusals (``stop_reason: refusal``) come back as an empty answer with that
stop reason: the callers' checks then say "not sure" (no other model is asked to answer what Claude declined).

Tested here with a fake client only (tests/brain/test_claude_provider.py), not against the real API.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Collection

from pydantic import SecretStr

from littlebot.brain.providers.base import (
    AuthenticationError,
    BaseProvider,
    Message,
    NetworkError,
    ProviderError,
    ProviderReply,
    ProviderTimeoutError,
    RateLimitError,
)
from littlebot.brain.providers.timing import CallTimer

log = logging.getLogger("littlebot.claude")

MAX_TOOL_ROUNDS = 6
MAX_TOOL_OUTPUT_CHARS = 4000
TOOL_FAILED = "error: the action failed"
WEB_SEARCH = "web_search_20250305"  # the basic variant: the dynamic-filtering one isn't on Haiku-class models
MAX_PAUSES = 2  # a server tool's long turn (stop_reason "pause_turn") is continued at most this often
# Models from before adaptive thinking and the effort setting (Claude Haiku 4.5 and older): they think only when
# asked to, so a request to them carries neither (both would be refused).
_OLDER_MODEL = re.compile(r"^claude-(3|(haiku|sonnet|opus)-4-[015])(-|$)")
CHEAP_EFFORTS = ("none", "minimal")  # LittleBot's "simple request" efforts (ChatGPT's names): no thinking here
ToolRunner = Callable[[str, str], Awaitable[str]]


def sdk_installed() -> bool:
    try:
        import anthropic  # noqa: F401, PLC0415
    except Exception:  # noqa: BLE001
        return False
    return True


def _secret(key: SecretStr | str | None) -> str:
    if isinstance(key, SecretStr):
        return key.get_secret_value()
    return str(key or "")


class ClaudeProvider(BaseProvider):
    name = "claude"
    is_cloud = True
    supports_tools = True

    def __init__(self, *, api_key: SecretStr | str | None, model: str | None, effort: str = "low",
                 thinking: str = "adaptive", max_tokens: int = 2048, first_token_timeout: float = 4.0,
                 web_search: bool = True, web_search_max_uses: int = 2, base_url: str = "https://api.anthropic.com",
                 purpose: str = "", client: Any = None, allowed: Callable[[], str | None] | None = None) -> None:
        self._key = api_key if isinstance(api_key, SecretStr) else SecretStr(str(api_key or ""))
        self.model = str(model or "")
        self.effort = effort
        self.thinking = thinking
        self.max_tokens = int(max_tokens)
        self.first_token_timeout = float(first_token_timeout)
        self.web_search = bool(web_search)
        self.web_search_max_uses = int(web_search_max_uses)
        self.base_url = base_url
        self.purpose = purpose
        self._client = client  # injected (tests) or made on first use
        self._allowed = allowed  # () -> why requests may not go now ("offline", "cloud_off") or None
        self._no_web_search = False  # the account refused Anthropic's web search: not offered again
        self._must_think = False  # the model refused thinking "disabled" (not a Haiku-class one): never sent again

    @property
    def older_model(self) -> bool:
        """Claude Haiku 4.5 or older: no adaptive thinking and no effort setting."""
        return bool(_OLDER_MODEL.match(self.model))

    def __repr__(self) -> str:  # never the key
        return f"ClaudeProvider(model={self.model!r}, purpose={self.purpose!r})"

    def configured(self) -> bool:
        return bool(self.model and _secret(self._key) and (self._client is not None or sdk_installed()))

    def missing(self) -> str | None:
        """Why Claude can't be used ("no_model", "no_key", "not_installed"), or None."""
        if not self.model:
            return "no_model"
        if not _secret(self._key):
            return "no_key"
        if self._client is None and not sdk_installed():
            return "not_installed"
        return None

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "has_key": bool(_secret(self._key)), "missing": self.missing()}

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic  # noqa: PLC0415

            self._client = anthropic.AsyncAnthropic(api_key=_secret(self._key), base_url=self.base_url,
                                                    max_retries=0, timeout=30.0)
        return self._client

    async def warm(self, timeout: float = 5.0) -> bool:
        """Open the connection before it is needed (a free models listing: no tokens are used)."""
        if self.missing() or self._blocked():
            return False
        try:
            await asyncio.wait_for(self._get_client().models.list(limit=1), timeout)
            return True
        except Exception:  # noqa: BLE001 - best effort
            return False

    async def aclose(self) -> None:
        client, self._client = self._client, None
        close = getattr(client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001
                log.debug("claude: closing the client failed", exc_info=True)

    def _blocked(self) -> str | None:
        try:
            return self._allowed() if self._allowed is not None else None
        except Exception:  # noqa: BLE001 - can't tell: don't send
            return "cloud_off"

    async def ask_json(self, *, system: str, user: str, schema: dict[str, Any], timeout: float,
                       max_tokens: int = 1024) -> Any:
        """One short structured answer (``output_config.format``: JSON that fits ``schema``), for the decider.
        No thinking, low effort; the system prompt is cached. Raises ProviderError like ``reply``."""
        why = self.missing() or self._blocked()
        if why:
            raise ProviderError(f"claude: not available ({why})", kind=why)
        timer = CallTimer(self.name, self.purpose)
        kwargs: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        }
        if self.older_model:
            del kwargs["output_config"]["effort"]
        elif not self._must_think:
            kwargs["thinking"] = {"type": "disabled"}
        try:
            try:
                msg = await asyncio.wait_for(self._get_client().messages.create(**kwargs), timeout)
            except asyncio.TimeoutError:
                raise ProviderTimeoutError(f"claude: no answer within {timeout:g}s") from None
            except ProviderError:
                raise
            except Exception as e:  # noqa: BLE001
                raise _mapped(e) from None
        except ProviderError as e:
            timer.done(self.model, result=e.kind)
            raise
        timer.first_at = timer.first_at or time.monotonic()  # not streamed: the first word came with the rest
        usage = getattr(msg, "usage", None)
        timer.done(self.model, tokens_in=_int(getattr(usage, "input_tokens", 0)),
                   tokens_out=_int(getattr(usage, "output_tokens", 0)),
                   cache_read=_int(getattr(usage, "cache_read_input_tokens", 0)))
        if str(getattr(msg, "stop_reason", "")) == "refusal":
            raise ProviderError("claude: declined", kind="refused")
        text = "".join(str(_get(b, "text", "") or "") for b in getattr(msg, "content", None) or []
                       if _get(b, "type") == "text")
        try:
            return json.loads(text)
        except ValueError:
            raise ProviderError("claude: the answer wasn't JSON", kind="malformed") from None

    # ------------------------------------------------------------ the request
    def build_request(self, messages: list[Message], *, system: str, tools: list[dict[str, Any]] | None = None,
                      images: list[bytes] | None = None, effort: str | None = None,
                      max_tokens: int = 0) -> dict[str, Any]:
        """The ``messages.stream`` arguments (no secrets: the key lives in the client)."""
        msgs: list[dict[str, Any]] = []
        for m in messages:
            role = "user" if m.get("role") == "user" else "assistant"
            text = str(m.get("content", ""))
            if msgs and msgs[-1]["role"] == role and isinstance(msgs[-1]["content"], str):
                msgs[-1]["content"] += "\n" + text  # two in a row (a caption pair): one turn
            else:
                msgs.append({"role": role, "content": text})
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        if not msgs or msgs[-1]["role"] != "user":
            msgs.append({"role": "user", "content": "(go on)"})  # no prefill: the last turn is always the user's
        if images:
            last = msgs[-1]
            parts: list[dict[str, Any]] = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(img).decode("ascii")}} for img in images if img]
            last["content"] = [*parts, {"type": "text", "text": str(last["content"])}]
        cheap = effort in CHEAP_EFFORTS
        level = "low" if cheap or effort not in ("low", "medium", "high") else effort
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max(self.max_tokens, int(max_tokens or 0)),
            # tools are rendered before the system prompt: this one breakpoint caches both
            "system": [{"type": "text", "text": system.strip() or "You are a friendly toy robot.",
                        "cache_control": {"type": "ephemeral"}}],
            "messages": msgs,
            "output_config": {"effort": self.effort if effort is None else level},
        }
        if self.older_model:
            del kwargs["output_config"]  # no effort setting, and no thinking unless asked for
        elif (cheap or self.thinking == "disabled") and not self._must_think:
            kwargs["thinking"] = {"type": "disabled"}
        converted = [t for t in (self._tool(spec) for spec in tools or ()) if t]
        if converted:
            kwargs["tools"] = converted
            kwargs["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        return kwargs

    def _tool(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        kind = str(spec.get("type") or "function")
        if kind == "web_search":
            if not self.web_search or self._no_web_search:
                return None
            return {"type": WEB_SEARCH, "name": "web_search", "max_uses": self.web_search_max_uses}
        if kind != "function" or not spec.get("name"):
            return None  # another hosted tool: ChatGPT's only
        params = spec.get("parameters") if isinstance(spec.get("parameters"), dict) else None
        return {"name": str(spec["name"]), "description": str(spec.get("description") or ""),
                "input_schema": params or {"type": "object", "properties": {}}}

    async def reply(self, messages: list[Message], *, system: str, max_tokens: int, timeout: float,
                    images: list[bytes] | None = None, tools: list[dict[str, Any]] | None = None,
                    run_tool: ToolRunner | None = None, max_tool_rounds: int = MAX_TOOL_ROUNDS,
                    detail: str | None = None, effort: str | None = None,
                    on_delta: Callable[[str], Any] | None = None,
                    final_tools: Collection[str] = ()) -> ProviderReply:
        """As ``ChatGPTProvider.reply``. ``timeout`` bounds each request; ``first_token_timeout`` its first word."""
        del detail  # ChatGPT's image detail: Claude sizes images itself
        why = self.missing() or self._blocked()
        if why:
            raise ProviderError(f"claude: not available ({why})", kind=why)
        timer = CallTimer(self.name, self.purpose)
        offered = [t for t in tools or () if run_tool is not None or str(t.get("type") or "function") != "function"]
        kwargs = self.build_request(messages, system=system, tools=offered, images=images, effort=effort,
                                    max_tokens=max_tokens)
        rounds = pauses = 0
        tokens_in = tokens_out = cached = 0
        try:
            while True:
                try:
                    msg = await self._stream(kwargs, timer, on_delta, timeout)
                except _WebSearchRefused:
                    self._no_web_search = True
                    log.warning("claude: the account refused web search; asking again without it")
                    kwargs["tools"] = [t for t in kwargs.get("tools") or () if t.get("type") != WEB_SEARCH]
                    if not kwargs["tools"]:
                        kwargs.pop("tools")
                        kwargs.pop("tool_choice", None)
                    continue
                except ProviderError as e:
                    if e.kind != "bad_request" or kwargs.get("thinking") != {"type": "disabled"} or self._must_think:
                        raise
                    self._must_think = True  # this model won't run without thinking (only Haiku-class ones do)
                    log.warning("claude: %s didn't accept thinking off; asking again with adaptive thinking",
                                self.model)
                    kwargs.pop("thinking")
                    continue
                usage = getattr(msg, "usage", None)
                tokens_in += sum(_int(getattr(usage, k, 0)) for k in (
                    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                tokens_out += _int(getattr(usage, "output_tokens", 0))
                cached += _int(getattr(usage, "cache_read_input_tokens", 0))
                blocks = list(getattr(msg, "content", None) or [])
                stop = str(getattr(msg, "stop_reason", "") or "end_turn")
                text = _answer_text(blocks)
                calls = [b for b in blocks if _get(b, "type") == "tool_use"]
                if stop == "pause_turn" and pauses < MAX_PAUSES:  # a server tool's long turn: let it go on
                    pauses += 1
                    kwargs["messages"] = [*kwargs["messages"], {"role": "assistant", "content": blocks}]
                    continue
                if stop == "refusal":
                    timer.done(self.model, result="refused", rounds=rounds, tokens_in=tokens_in,
                               tokens_out=tokens_out, cache_read=cached)
                    return ProviderReply(text="", input_tokens=tokens_in, output_tokens=tokens_out, model=self.model,
                                         latency_ms=timer.total_ms, stop_reason="refusal")
                if calls and run_tool is not None and stop == "tool_use":
                    if final_tools and _is_answer(text) and all(_get(c, "name") in final_tools for c in calls):
                        for call in calls:  # the answer is written: these only go alongside it
                            await _run(run_tool, call)
                        break
                    if rounds < max(0, int(max_tool_rounds)):
                        rounds += 1
                        results = [{"type": "tool_result", "tool_use_id": _get(c, "id"),
                                    "content": await _run(run_tool, c)} for c in calls]
                        kwargs["messages"] = [*kwargs["messages"], {"role": "assistant", "content": blocks},
                                              {"role": "user", "content": results}]
                        continue
                break
        except ProviderError as e:
            timer.done(self.model, result=e.kind, rounds=rounds, tokens_in=tokens_in, tokens_out=tokens_out,
                       cache_read=cached)
            raise
        timer.done(self.model, rounds=rounds, tokens_in=tokens_in, tokens_out=tokens_out, cache_read=cached)
        return ProviderReply(text=text.strip(), input_tokens=tokens_in, output_tokens=tokens_out,
                             model=str(getattr(msg, "model", "") or self.model), latency_ms=timer.total_ms,
                             stop_reason="max_tokens" if stop == "max_tokens" else "end_turn")

    async def _stream(self, kwargs: dict[str, Any], timer: CallTimer, on_delta: Callable[[str], Any] | None,
                      timeout: float) -> Any:
        """One streamed request: its final message. Words go to ``on_delta`` as they come."""
        first = asyncio.Event()
        start = time.monotonic()

        async def run() -> Any:
            async with self._get_client().messages.stream(**kwargs) as stream:
                async for event in stream:
                    kind = getattr(event, "type", "")
                    if kind == "text":
                        piece = str(getattr(event, "text", "") or "")
                        if piece:
                            timer.first()
                            first.set()
                            if on_delta is not None:
                                _notify(on_delta, piece)
                    elif kind in ("content_block_start", "input_json") and _is_tool_start(event):
                        timer.first()
                        first.set()
                return await stream.get_final_message()

        task = asyncio.ensure_future(run())
        waiter = asyncio.ensure_future(first.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, timeout=self.first_token_timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if task not in done and not first.is_set():
                raise ProviderTimeoutError(f"claude: no first word within {self.first_token_timeout:g}s")
            left = max(0.1, timeout - (time.monotonic() - start))
            return await asyncio.wait_for(asyncio.shield(task), left)
        except asyncio.TimeoutError:
            raise ProviderTimeoutError(f"claude: no answer within {timeout:.0f}s") from None
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001 - the SDK's errors, mapped below
            raise _mapped(e, web_search=any(t.get("type") == WEB_SEARCH for t in kwargs.get("tools") or ())) from None
        finally:
            waiter.cancel()
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001 - cancelled on purpose
                    pass


class _WebSearchRefused(ProviderError):
    kind = "web_search_refused"


def _get(block: Any, key: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _is_tool_start(event: Any) -> bool:
    if getattr(event, "type", "") == "input_json":
        return True
    block = getattr(event, "content_block", None)
    return _get(block, "type") in ("tool_use", "server_tool_use")


def _answer_text(blocks: list[Any]) -> str:
    """The words after the last server tool (web search) result: earlier ones were a preamble."""
    cut = 0
    for i, b in enumerate(blocks):
        if _get(b, "type") in ("server_tool_use", "web_search_tool_result"):
            cut = i + 1
    after = "".join(str(_get(b, "text", "") or "") for b in blocks[cut:] if _get(b, "type") == "text")
    return after if after.strip() else "".join(str(_get(b, "text", "") or "") for b in blocks
                                               if _get(b, "type") == "text")


async def _run(run_tool: ToolRunner, call: Any) -> str:
    args = _get(call, "input")
    if not isinstance(args, dict):
        return "error: the arguments weren't readable"
    try:
        out = await run_tool(str(_get(call, "name")), json.dumps(args, ensure_ascii=False))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a broken tool is an error result, never the conversation's end
        return TOOL_FAILED
    text = str(out if out is not None else "ok")
    return text if len(text) <= MAX_TOOL_OUTPUT_CHARS else text[:MAX_TOOL_OUTPUT_CHARS - 12] + " [truncated]"


_PREAMBLE_WORDS = ("let me", "let's", "i'll", "i will", "i'm going to", "one moment", "just a moment", "hold on",
                   "checking", "looking")


def _is_answer(text: str) -> bool:
    t = text.strip().lower()
    return len(t.split()) >= 6 and not t.startswith(_PREAMBLE_WORDS)


def _notify(on_delta: Callable[[str], Any], text: str) -> None:
    try:
        on_delta(text)
    except Exception:  # noqa: BLE001 - a listener's bug must not break the answer
        log.debug("on_delta listener failed", exc_info=True)


def _mapped(e: Exception, *, web_search: bool = False) -> ProviderError:
    """The SDK's exception as LittleBot's (a status and a label only: never the request or the key)."""
    name = type(e).__name__
    status = getattr(e, "status_code", None)
    if name in ("APITimeoutError", "DeadlineExceededError"):
        return ProviderTimeoutError("claude: request timed out")
    if name == "APIConnectionError":
        return NetworkError("claude: network unavailable")
    if name in ("AuthenticationError", "PermissionDeniedError"):
        return AuthenticationError(f"claude: key rejected (HTTP {status})", status_code=status)
    if name == "RateLimitError":
        return RateLimitError("claude: rate limited", status_code=429)
    if name == "NotFoundError":
        return ProviderError("claude: the model wasn't found", status_code=404, kind="model_unavailable")
    if name == "BadRequestError":
        body = getattr(e, "body", None)
        detail = json.dumps(body)[:2000] if isinstance(body, (dict, list)) else str(body or "")
        if web_search and "web_search" in detail:
            return _WebSearchRefused("claude: web search refused", status_code=400)
        return ProviderError("claude: request not accepted", status_code=400, kind="bad_request")
    if isinstance(status, int):
        err = ProviderError(f"claude: HTTP {status}", status_code=status,
                            kind="overloaded" if status == 529 else "server_error")
        err.retryable = status >= 500
        return err
    return ProviderError(f"claude: the answer failed ({name})", kind="stream_error")


def _int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


__all__ = ["ClaudeProvider", "sdk_installed"]
