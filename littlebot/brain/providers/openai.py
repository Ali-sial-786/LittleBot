"""OpenAI Chat Completions provider (raw REST via httpx, no SDK).

    POST {base_url}/chat/completions          (base_url default https://api.openai.com/v1)
    headers: Authorization: Bearer <key>, content-type: application/json
    body:    {model, messages: [{role: system}, ...], max_completion_tokens}

Response: ``choices[0].message.content`` (``message.refusal`` set when the model declines),
``usage.prompt_tokens`` / ``usage.completion_tokens``.

UNVERIFIED against the live API in this repository (no key available); the request shape
follows OpenAI's documented Chat Completions format. ``max_completion_tokens`` replaced the
deprecated ``max_tokens`` field.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import SecretStr

from littlebot.brain.providers.anthropic import _int
from littlebot.brain.providers.base import Message, ProviderError, ProviderReply
from littlebot.brain.providers.http import HttpProvider

DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIProvider(HttpProvider):
    name = "openai"

    def __init__(
        self,
        *,
        api_key: SecretStr | str | None,
        model: str = DEFAULT_MODEL,
        base_url: str = "https://api.openai.com/v1",
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(api_key=api_key, model=model, base_url=base_url, client=client, transport=transport)

    @property
    def url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def build_request(self, messages: list[Message], *, system: str,
                      max_tokens: int) -> tuple[dict[str, str], dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self._secret()}", "content-type": "application/json"}
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m["role"], "content": m["content"]} for m in messages],
            "max_completion_tokens": max(1, int(max_tokens)),
        }
        return headers, body

    async def reply(self, messages: list[Message], *, system: str, max_tokens: int, timeout: float) -> ProviderReply:
        headers, body = self.build_request(messages, system=system, max_tokens=max_tokens)
        data, latency_ms = await self._post_json(self.url, headers=headers, body=body, timeout=timeout)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("openai: response has no choices", kind="malformed")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ProviderError("openai: response has no message", kind="malformed")
        refusal = message.get("refusal")
        content = message.get("content")
        if isinstance(content, list):  # tolerate content-part arrays
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        finish = choices[0].get("finish_reason")
        return ProviderReply(
            text="" if refusal else str(content or "").strip(),
            input_tokens=_int(usage.get("prompt_tokens")),
            output_tokens=_int(usage.get("completion_tokens")),
            model=str(data.get("model") or self.model),
            latency_ms=latency_ms,
            stop_reason="refusal" if refusal else (str(finish) if finish is not None else None),
        )
