"""The ``chatgpt`` conversation provider: replies on the parent's ChatGPT plan (no API key).

    POST https://chatgpt.com/backend-api/codex/responses        (server-sent events)
    headers: Authorization: Bearer <access token>, ChatGPT-Account-ID, originator, session-id
    body:    {model, instructions, input: [messages], store: false, stream: true,
              reasoning: {effort}, text: {verbosity: "low"}, include: [...], prompt_cache_key}

Contract as WatchGPT measured it (08.2026, same as Codex): ``store`` must be false and
``stream`` true, ``instructions`` must be non-empty, and ``temperature``,
``max_output_tokens`` and ``previous_response_id`` are rejected - so the length cap is
applied afterwards by ``validate_reply`` (as for every provider) and history is resent.

UNDOCUMENTED endpoint; tested here only against mocked responses.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from littlebot.brain.providers.base import (
    AuthenticationError, BaseProvider, Message, NetworkError, ProviderError, ProviderReply, ProviderTimeoutError,
    RateLimitError,
)
from littlebot.chatgpt.auth import CODEX_BASE, ChatGPTAuth, ChatGPTAuthError

MODEL_UNAVAILABLE = re.compile(r"model.*(not supported|does not exist|not found|unsupported)", re.I)


class ChatGPTProvider(BaseProvider):
    name = "chatgpt"
    is_cloud = True

    def __init__(self, auth: ChatGPTAuth, *, models: list[str], effort: str = "low",
                 base_url: str = CODEX_BASE, transport: httpx.AsyncBaseTransport | None = None,
                 now: Any = time.time) -> None:
        self.auth = auth
        self.models = [m for m in models if m] or ["gpt-6-luna"]
        self._model_index = 0
        self.effort = effort
        self.url = f"{base_url.rstrip('/')}/responses"
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._session_id = str(uuid.uuid4())
        self._now = now

    @property
    def model(self) -> str:  # type: ignore[override]
        return self.models[min(self._model_index, len(self.models) - 1)]

    def configured(self) -> bool:
        return self.auth.signed_in

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "signed_in": self.auth.signed_in, "plan": self.auth.status().get("plan")}

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(transport=self._transport, follow_redirects=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def build_body(self, messages: list[Message], *, system: str, model: str,
                   images: list[bytes] | None = None) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for m in messages:
            user = m.get("role") == "user"
            items.append({"type": "message", "role": "user" if user else "assistant",
                          "content": [{"type": "input_text" if user else "output_text", "text": str(m.get("content", ""))}]})
        parts = [_image_part(img) for img in images or () if img]
        if parts:
            last_user = next((it for it in reversed(items) if it["role"] == "user"), None)
            if last_user is None:
                last_user = {"type": "message", "role": "user", "content": []}
                items.append(last_user)
            last_user["content"][:0] = parts  # images first, then the question (as `codex exec -i` sends it)
        return {
            "model": model,
            "instructions": system.strip() or "You are a friendly toy robot.",
            "input": items,
            "store": False,
            "stream": True,
            "reasoning": {"effort": self.effort},
            "text": {"verbosity": "low"},
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": self._session_id,
        }

    async def reply(self, messages: list[Message], *, system: str, max_tokens: int, timeout: float,
                    images: list[bytes] | None = None) -> ProviderReply:
        """``images``: JPEG bytes added to the LAST user message (sent once, never kept here)."""
        t0 = time.monotonic()
        try:
            bearer = await self.auth.bearer()
        except ChatGPTAuthError as e:
            raise _auth_error(e) from None
        tried_refresh = False
        while True:
            model = self.model
            try:
                text, usage, stop = await asyncio.wait_for(
                    self._stream(bearer, self.build_body(messages, system=system, model=model, images=images)),
                    timeout)
            except asyncio.TimeoutError:
                raise ProviderTimeoutError(f"chatgpt: no answer within {timeout:.0f}s") from None
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
            return ProviderReply(text=text.strip(), input_tokens=int(usage.get("input_tokens") or 0),
                                 output_tokens=int(usage.get("output_tokens") or 0), model=model,
                                 latency_ms=int((time.monotonic() - t0) * 1000), stop_reason=stop)

    async def _stream(self, bearer: Any, body: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
        headers = {**self.auth.headers(bearer), "Accept": "text/event-stream", "content-type": "application/json",
                   "session-id": self._session_id}
        out: list[str] = []
        usage: dict[str, Any] = {}
        stop = "end_turn"
        try:
            async with self._get_client().stream("POST", self.url, headers=headers, json=body,
                                                 timeout=httpx.Timeout(60.0, connect=8.0)) as resp:
                if resp.status_code >= 400:
                    raw = (await resp.aread()).decode("utf-8", "replace")
                    raise _http_error(resp.status_code, resp.headers, raw, self._now())
                async for event, data in _sse(resp.aiter_lines()):
                    kind = data.get("type") or event
                    if kind == "response.output_text.delta":
                        out.append(str(data.get("delta") or ""))
                    elif kind in ("response.completed", "response.incomplete"):
                        r = data.get("response") if isinstance(data.get("response"), dict) else {}
                        usage = r.get("usage") if isinstance(r.get("usage"), dict) else {}
                        if kind == "response.incomplete":
                            stop = "max_tokens"
                        break
                    elif kind == "response.failed":
                        r = data.get("response") if isinstance(data.get("response"), dict) else {}
                        raise _stream_error(r.get("error"))
                    elif kind == "error":
                        raise _stream_error(data.get("error") if isinstance(data.get("error"), dict) else data)
        except httpx.TimeoutException:
            raise ProviderTimeoutError("chatgpt: request timed out") from None
        except httpx.TransportError as e:
            raise NetworkError(f"chatgpt: network unavailable ({type(e).__name__})") from None
        if not out and stop == "end_turn" and not usage:
            raise ProviderError("chatgpt: the stream ended without an answer", kind="malformed")
        return "".join(out), usage, stop


IMAGE_DETAIL = "high"  # codex-rs/protocol/src/models.rs: DEFAULT_IMAGE_DETAIL = ImageDetail::High


def _image_part(jpeg: bytes) -> dict[str, Any]:
    """A Responses API ``input_image`` content part with the JPEG inlined as a data URL.

    The shape the Codex CLI sends (openai/codex, codex-rs/protocol/src/models.rs):
    ``ContentItem::InputImage { image: ImageReference::Inline { image_url }, detail: Some(detail) }``
    serialises (serde tag "type", snake_case, image flattened) to
    ``{"type": "input_image", "image_url": "data:<mime>;base64,<standard base64>", "detail": "high"}``
    (``data_url_from_bytes`` in codex-rs/utils/image/src/lib.rs). ``detail`` is always sent: Codex
    defaults it to "high", and the OpenAI spec marks it required (openai-python ``ResponseInputImageParam``).
    Codex shrinks images to fit 2048 px; ours are already 768 px wide.
    """
    return {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
            "detail": IMAGE_DETAIL}


class _Unauthorized(Exception):
    pass


class _ModelUnavailable(Exception):
    pass


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
