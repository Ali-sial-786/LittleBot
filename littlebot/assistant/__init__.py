"""The fast voice pipeline: wake word -> streaming speech-to-text -> one brain -> streaming text-to-speech.

See littlebot/assistant/service.py and docs/ASSISTANT.md.
"""

from __future__ import annotations

from typing import Any


def create_service(ctx: Any) -> Any:
    from littlebot.assistant.service import AssistantService

    return AssistantService(ctx)
