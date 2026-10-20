"""Brain test helpers: cloud-enabled contexts and httpx.MockTransport fakes (no network, no keys)."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
from pydantic import SecretStr

from littlebot.testing import make_test_context

# Fake keys: realistic shape so leaks would be obvious in logs / status.
ANTHROPIC_KEY = "sk-ant-api03-TESTKEY-abcdefghijklmnopqrstuvwxyz0123456789"
OPENAI_KEY = "sk-proj-TESTKEY-zyxwvutsrqponmlkjihgfedcba9876543210"


def anthropic_response(text: str = "The cow says moo! Can you moo?", *, input_tokens: int = 300,
                       output_tokens: int = 20, stop_reason: str = "end_turn", status: int = 200,
                       thinking: bool = True) -> httpx.Response:
    content: list[dict[str, Any]] = []
    if thinking:
        content.append({"type": "thinking", "thinking": "", "signature": "sig"})
    content.append({"type": "text", "text": text})
    return httpx.Response(status, json={
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "content": content, "stop_reason": stop_reason,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })


def openai_response(text: str = "The dog says woof!", *, refusal: str | None = None) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "chatcmpl-test", "object": "chat.completion", "model": "gpt-4o-mini",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": None if refusal else text, "refusal": refusal}}],
        "usage": {"prompt_tokens": 250, "completion_tokens": 12, "total_tokens": 262},
    })


def error_response(status: int, err_type: str = "error", message: str = "nope", **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"type": "error", "error": {"type": err_type, "message": message}},
                          headers=headers)


class FakeAPI:
    """Programmable MockTransport handler that records every request."""

    def __init__(self, respond: Callable[[httpx.Request], Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.respond = respond or (lambda req: anthropic_response())

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.respond(request)
        if hasattr(result, "__await__"):
            result = await result
        return result

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    @property
    def calls(self) -> int:
        return len(self.requests)

    def body(self, i: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[i].content)


def cloud_context(*, age_months: int | None = 36, provider: str = "anthropic", enabled: bool = True,
                  cloud: bool = True, key: bool = True, nickname: str = "", **config: Any):
    """A test context where cloud AI is allowed unless a flag says otherwise."""
    ai = config.pop("ai", {})
    ctx = make_test_context(ai=ai, **config)
    if key:  # even in offline mode, to prove offline wins
        ctx.config.anthropic_api_key = SecretStr(ANTHROPIC_KEY)
        ctx.config.openai_api_key = SecretStr(OPENAI_KEY)
    ctx.settings.set("ai.provider", provider)
    ctx.settings.set("ai.enabled", enabled)
    if cloud and not ctx.config.is_offline:
        ctx.privacy.set(cloud_enabled=True)
    ctx.settings.set("profile", {"age_months": age_months, "nickname": nickname})
    return ctx
