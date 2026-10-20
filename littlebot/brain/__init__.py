"""Conversation engine + AI providers (offline scripted mock / Anthropic / OpenAI).

``create_service(ctx)`` returns a ``ConversationEngine`` implementing
``core.interfaces.BrainAPI``. See docs/modules/brain.md and docs/AI_SAFETY.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from littlebot.brain.conversation import ConversationEngine
    from littlebot.core.context import RobotContext


def create_service(ctx: "RobotContext") -> "ConversationEngine":
    from littlebot.brain.conversation import ConversationEngine

    return ConversationEngine(ctx)


__all__ = ["create_service"]
