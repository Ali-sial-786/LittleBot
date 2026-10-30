"""The fast voice pipeline: wake word -> streaming speech-to-text -> one brain -> streaming text-to-speech.

See littlebot/assistant/service.py and docs/ASSISTANT.md.
"""

from __future__ import annotations

from typing import Any


def create_service(ctx: Any) -> Any:
    from littlebot.assistant.service import AssistantService

    return AssistantService(ctx)


def voice_front(ctx: Any) -> Any:
    """The voice service the wake word (and the dashboard's "talk" button) starts: the assistant when
    ``assistant.pipeline`` says so (auto: once its keys are set), else GPT-Live (``ctx.live``)."""
    assistant = getattr(ctx, "assistant", None)
    try:
        if assistant is not None and assistant.use_cascade():
            return assistant
    except Exception:  # noqa: BLE001 - GPT-Live as before
        pass
    return getattr(ctx, "live", None)


def voice_active(ctx: Any) -> bool:
    """A voice conversation is on (either pipeline)."""
    for svc in (getattr(ctx, "assistant", None), getattr(ctx, "live", None)):
        try:
            if svc is not None and svc.status().get("active"):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False
