"""The text model for a voice chat's text work (``text.provider``): ChatGPT, or Claude with ChatGPT behind it.

``text_provider(ctx, chatgpt, purpose=)`` is what the live voice's hand-offs and the screen director (its cards and
the browser's search words) ask (littlebot/chatgpt/live.py): ``chatgpt`` itself with ``text.provider: chatgpt``
(the default), else a ``FallbackProvider``: Claude first, and the same request to ChatGPT when Claude isn't set up
(no model, no key, the SDK not installed), fails or is slow to say its first word, as long as nothing was done or
said yet for that request (a tool already run, or words already handed on, are never repeated by another model).
Offline mode and the Privacy page's cloud switch stop Claude as they stop ChatGPT (``cloud_block``): then nothing
is sent to either. With ``text.fallback: false`` Claude is used alone.

Whatever answers, the callers check the words the same way afterwards (``validate_reply``: child safety, the
medicine dose rule; the prompts carry ``ai.share_child_name``), so the safety rules don't depend on the model.
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Awaitable, Callable, Collection

from littlebot.brain.providers.base import BaseProvider, Message, ProviderError, ProviderReply
from littlebot.brain.providers.claude import ClaudeProvider

log = logging.getLogger("littlebot.text_model")

NO_FALLBACK = ("offline", "cloud_off")  # nothing may be sent: not to ChatGPT either
client_factory: Callable[[], Any] | None = None  # tests: a fake Anthropic client for every ClaudeProvider made here


def cloud_block(ctx: Any) -> str | None:
    """Why no text model may be asked now ("offline", "cloud_off"), or None."""
    try:
        if ctx.config.is_offline:
            return "offline"
        if not ctx.privacy.cloud_enabled:
            return "cloud_off"
    except AttributeError:
        return "cloud_off"
    return None


def make_claude(ctx: Any, *, purpose: str = "", model: str | None = None) -> ClaudeProvider:
    c = ctx.config.claude
    return ClaudeProvider(api_key=ctx.config.anthropic_api_key, model=model or c.model, effort=c.effort,
                          thinking=c.thinking, max_tokens=c.max_tokens,
                          first_token_timeout=c.first_token_timeout_seconds, web_search=c.web_search,
                          web_search_max_uses=c.web_search_max_uses, base_url=c.base_url, purpose=purpose,
                          client=client_factory() if client_factory is not None else None,
                          allowed=lambda: cloud_block(ctx))


def text_provider(ctx: Any, chatgpt: BaseProvider, *, purpose: str = "") -> BaseProvider:
    cfg = ctx.config.text
    if cfg.provider != "claude":
        return chatgpt
    claude = make_claude(ctx, purpose=purpose)
    if not cfg.fallback:
        return claude
    return FallbackProvider(claude, chatgpt, purpose=purpose)


class FallbackProvider(BaseProvider):
    """``primary`` (Claude) first; ``backup`` (ChatGPT) when it can't answer and nothing was done yet."""

    is_cloud = True
    supports_tools = True

    def __init__(self, primary: ClaudeProvider, backup: BaseProvider, *, purpose: str = "") -> None:
        self.primary = primary
        self.backup = backup
        self.purpose = purpose
        self._told_missing: str | None = None

    @property
    def name(self) -> str:  # type: ignore[override]
        return self.primary.name

    @property
    def model(self) -> str:  # type: ignore[override]
        return self.primary.model

    def configured(self) -> bool:
        return self.primary.configured() or self.backup.configured()

    def describe(self) -> dict[str, Any]:
        return {**self.primary.describe(), "fallback": self.backup.describe()}

    async def warm(self, timeout: float = 5.0) -> bool:
        ok = False
        for p in (self.primary, self.backup):
            warm = getattr(p, "warm", None)
            if warm is not None:
                ok = bool(await warm(timeout)) or ok
        return ok

    async def aclose(self) -> None:
        await self.primary.aclose()  # the backup is ChatGPT's, owned by whoever made it

    async def reply(self, messages: list[Message], *, system: str, max_tokens: int, timeout: float,
                    images: list[bytes] | None = None, tools: list[dict[str, Any]] | None = None,
                    run_tool: Callable[[str, str], Awaitable[str]] | None = None, max_tool_rounds: int = 6,
                    detail: str | None = None, effort: str | None = None,
                    on_delta: Callable[[str], Any] | None = None,
                    final_tools: Collection[str] = ()) -> ProviderReply:
        """As ``ChatGPTProvider.reply`` (the same parameters, so callers that look at them pass the same)."""
        kw: dict[str, Any] = {"images": images, "tools": tools, "run_tool": run_tool,
                              "max_tool_rounds": max_tool_rounds, "detail": detail, "effort": effort,
                              "on_delta": on_delta, "final_tools": final_tools}
        missing = self.primary.missing()
        if missing:
            if self._told_missing != missing:
                self._told_missing = missing
                log.warning("text model: Claude isn't set up (%s): ChatGPT answers instead", missing)
            return await self._backup(messages, system, max_tokens, timeout, kw)
        acted: list[bool] = []
        if run_tool is not None:
            async def tracked_tool(name: str, arguments: Any = None) -> str:
                acted.append(True)
                return await run_tool(name, arguments)
            kw["run_tool"] = tracked_tool
        if on_delta is not None:
            def tracked_delta(text: str) -> Any:
                acted.append(True)
                return on_delta(text)
            kw["on_delta"] = tracked_delta
        t0 = time.monotonic()
        try:
            return await self.primary.reply(messages, system=system, max_tokens=max_tokens, timeout=timeout, **kw)
        except ProviderError as e:
            if e.kind in NO_FALLBACK or acted:
                raise
            log.warning("text model: Claude failed (%s) after %d ms; asking ChatGPT instead", e.kind,
                        (time.monotonic() - t0) * 1000)
        kw["run_tool"], kw["on_delta"] = run_tool, on_delta
        return await self._backup(messages, system, max_tokens, timeout, kw)

    async def _backup(self, messages: list[Message], system: str, max_tokens: int, timeout: float,
                      kw: dict[str, Any]) -> ProviderReply:
        """The backup with the options its ``reply`` takes (a plain provider gets none of them)."""
        try:
            params = inspect.signature(self.backup.reply).parameters
        except (TypeError, ValueError):
            params = {}
        opts = {k: v for k, v in kw.items() if k in params and v is not None}
        return await self.backup.reply(messages, system=system, max_tokens=max_tokens, timeout=timeout, **opts)

__all__ = ["FallbackProvider", "client_factory", "cloud_block", "make_claude", "text_provider"]
