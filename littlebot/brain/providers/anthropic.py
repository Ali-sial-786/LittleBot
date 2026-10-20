"""Anthropic Messages API provider (raw REST via httpx, no SDK).

    POST {base_url}/v1/messages
    headers: x-api-key, anthropic-version: 2023-06-01, content-type: application/json
    body:    {model, max_tokens, system, messages, [output_config.effort], [thinking]}

Response: ``content`` is a list of blocks; only ``type == "text"`` blocks are spoken
(current models may also return ``thinking`` blocks, which are ignored). ``usage``
carries ``input_tokens`` / ``output_tokens`` (+ cache token fields when caching applies).

Notes for current Claude models (verified against the claude-api reference, Oct 2026):
* Default model ``claude-haiku-5-5`` - the current fast, low-cost Haiku.
* Thinking is adaptive and ON by default and counts toward ``max_tokens``; we send
  ``output_config.effort = "low"`` and add ``thinking_headroom`` tokens so a short reply is
  never starved. ``thinking: {"type": "disabled"}`` is accepted by Haiku 5.5 (effort
  low..high) but returns 400 on Opus 5.5 / Sonnet 5.5, so it is opt-in via config.
* ``temperature`` / ``top_p`` / ``top_k`` are not sent (non-default values return 400).
* No assistant prefill: the message list always ends with a user turn.
* ``stop_reason == "refusal"`` means the safety classifier declined; the engine then
  uses a safe fallback line.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import SecretStr

from littlebot.brain.providers.base import Message, ProviderError, ProviderReply
from littlebot.brain.providers.http import HttpProvider

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-5-5"


class AnthropicProvider(HttpProvider):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: SecretStr | str | None,
        model: str = DEFAULT_MODEL,
        base_url: str = "https://api.anthropic.com",
        effort: str | None = "low",
        thinking: str = "adaptive",
        thinking_headroom: int = 400,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(api_key=api_key, model=model, base_url=base_url, client=client, transport=transport)
        self.effort = effort
        self.thinking = thinking
        self.thinking_headroom = max(0, int(thinking_headroom))

    @property
    def url(self) -> str:
        return f"{self.base_url}/v1/messages"

    def build_request(self, messages: list[Message], *, system: str,
                      max_tokens: int) -> tuple[dict[str, str], dict[str, Any]]:
        headers = {
            "x-api-key": self._secret(),
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        budget = int(max_tokens) + (self.thinking_headroom if self.thinking != "disabled" else 0)
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max(1, budget),
            "system": system,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        }
        if self.effort:
            body["output_config"] = {"effort": self.effort}
        if self.thinking == "disabled":
            body["thinking"] = {"type": "disabled"}
        return headers, body

    async def reply(self, messages: list[Message], *, system: str, max_tokens: int, timeout: float) -> ProviderReply:
        headers, body = self.build_request(messages, system=system, max_tokens=max_tokens)
        data, latency_ms = await self._post_json(self.url, headers=headers, body=body, timeout=timeout)
        if data.get("type") == "error":
            err = data.get("error") or {}
            detail = self._redact(str(err.get("message", "error")))[:160]
            raise ProviderError(f"anthropic: {detail}", kind="provider_error")
        content = data.get("content")
        if not isinstance(content, list):
            raise ProviderError("anthropic: response has no content list", kind="malformed")
        texts = [str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"]
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        input_tokens = _int(usage.get("input_tokens")) + _int(usage.get("cache_creation_input_tokens")) \
            + _int(usage.get("cache_read_input_tokens"))
        return ProviderReply(
            text=" ".join(t.strip() for t in texts if t.strip()),
            input_tokens=input_tokens,
            output_tokens=_int(usage.get("output_tokens")),
            model=str(data.get("model") or self.model),
            latency_ms=latency_ms,
            stop_reason=str(data.get("stop_reason")) if data.get("stop_reason") is not None else None,
        )


def _int(v: Any) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0
