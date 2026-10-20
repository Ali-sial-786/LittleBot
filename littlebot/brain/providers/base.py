"""Provider interface + typed errors shared by every conversation backend.

A provider turns ``messages`` (``[{"role": "user"|"assistant", "content": str}, ...]``)
plus a system prompt into one short reply. Providers never decide *whether* they may be
used (age gate, parent opt-in, privacy, budget) - the ConversationEngine does that - and
they never see camera data or unscrubbed personal information.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

Message = dict[str, str]


@dataclass(frozen=True)
class ProviderReply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    latency_ms: int = 0
    stop_reason: str | None = None  # e.g. end_turn | max_tokens | refusal | scripted
    expression: str | None = None  # optional face expression hint (mock provider)


class ProviderError(Exception):
    """Base class. ``kind`` is a short machine-readable label safe to show parents."""

    kind = "provider_error"
    retryable = False

    def __init__(self, message: str = "", *, status_code: int | None = None, kind: str | None = None) -> None:
        super().__init__(message or self.kind)
        self.status_code = status_code
        if kind:
            self.kind = kind


class AuthenticationError(ProviderError):
    """401/403: missing, invalid or revoked API key (or no permission)."""

    kind = "auth_error"


class RateLimitError(ProviderError):
    """429 from the provider (too many requests / quota)."""

    kind = "rate_limited"
    retryable = True

    def __init__(self, message: str = "", *, status_code: int | None = 429, retry_after: float | None = None,
                 kind: str | None = None) -> None:
        super().__init__(message, status_code=status_code, kind=kind)
        self.retry_after = retry_after


class ProviderTimeoutError(ProviderError, TimeoutError):
    """The request took longer than the configured timeout (also an instance of TimeoutError)."""

    kind = "timeout"
    retryable = True


class NetworkError(ProviderError):
    """No network / DNS failure / connection refused or reset."""

    kind = "network"
    retryable = True


class BaseProvider(abc.ABC):
    """Common interface. Subclasses set ``name``, ``is_cloud`` and ``model``."""

    name: str = "base"
    is_cloud: bool = False
    model: str = ""

    def configured(self) -> bool:
        """True when the provider has what it needs to run (e.g. an API key)."""
        return True

    @abc.abstractmethod
    async def reply(self, messages: list[Message], *, system: str, max_tokens: int, timeout: float) -> ProviderReply:
        """Generate one reply. Raises ProviderError subclasses on failure."""

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "configured": self.configured(), "model": self.model, "cloud": self.is_cloud}
