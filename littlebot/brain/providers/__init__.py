"""Conversation providers: offline scripted mock + Anthropic / OpenAI over raw HTTPS."""

from littlebot.brain.providers.anthropic import AnthropicProvider
from littlebot.brain.providers.base import (
    AuthenticationError, BaseProvider, Message, NetworkError, ProviderError, ProviderReply, ProviderTimeoutError,
    RateLimitError,
)
from littlebot.brain.providers.mock import MockProvider
from littlebot.brain.providers.openai import OpenAIProvider

# Alias matching the error names used in the docs ("TimeoutError"); it is also a builtin TimeoutError.
TimeoutError = ProviderTimeoutError  # noqa: A001

__all__ = [
    "AnthropicProvider", "AuthenticationError", "BaseProvider", "Message", "MockProvider", "NetworkError",
    "OpenAIProvider", "ProviderError", "ProviderReply", "ProviderTimeoutError", "RateLimitError", "TimeoutError",
]
