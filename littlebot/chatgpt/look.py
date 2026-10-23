"""Let ChatGPT look through LittleBot's camera to answer one question ("how many fingers?").

Privacy (the parent's standing rules): a photo is taken ONLY when someone asks a question that
needs it, one still frame per question, and only when the parent turned on the AI camera switch
(``privacy.ai_camera_enabled``, off by default) with the camera and cloud switches on. The JPEG
lives in memory for this one request and is dropped right after; it is never written to disk,
logged or kept. Taking it is visible: the face peeks and ``vision.photo`` is published (only
``{asked_by}``: never the question or the image).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
import time
from typing import Any

from littlebot.brain.prompts import build_system_prompt
from littlebot.brain.providers.base import (
    AuthenticationError, BaseProvider, NetworkError, ProviderError, ProviderTimeoutError, RateLimitError,
)
from littlebot.chatgpt import get_auth
from littlebot.chatgpt.auth import MESSAGES
from littlebot.core.events import Topics
from littlebot.safety.content import sanitize_user_text, validate_reply

log = logging.getLogger("littlebot.chatgpt")

_KEY = "camera_look"
MAX_WORDS = 40
DEFAULT_QUESTION = "What do you see?"
FALLBACK = "I'm not sure what I see. Let's ask a grown-up."
PHOTO_MAX_WIDTH = 768
PHOTO_QUALITY = 80
PHOTO_TIMEOUT_S = 3.0
REPLY_TIMEOUT_S = 20.0

# Friendly, parent-facing messages (no provider text is ever shown).
ERRORS: dict[str, str] = {
    # why looking is not possible right now (blocked_reason)
    "offline": "The robot is in offline mode, so ChatGPT cannot look through the camera.",
    "cloud_off": "Cloud features are off in Privacy. Turn them on to let ChatGPT look through the camera.",
    "camera_off": "The camera is turned off in Privacy.",
    "ai_camera_off": "Letting ChatGPT see the camera is off in Privacy.",
    "not_signed_in": MESSAGES["not_signed_in"],
    "no_camera": "No camera is working right now.",
    # what can go wrong while looking
    "busy": "LittleBot is already looking. Try again in a moment.",
    "photo_failed": "LittleBot could not take a photo. Try again.",
    "timeout": "ChatGPT took too long to answer. Try again.",
    "network": "Could not reach ChatGPT. Check the robot's internet connection.",
    "rate_limited": "ChatGPT is busy right now. Try again in a little while.",
    "usage_limit": "Your ChatGPT plan's usage limit was reached. Try again later.",
    "expired": MESSAGES["expired"],
    "auth": "ChatGPT did not accept the sign-in. Sign in again on the AI page.",
    "plan_not_included": "Your ChatGPT plan does not include this.",
    "refused": "ChatGPT would not answer that. Try asking another way.",
    "provider_error": "ChatGPT could not answer. Try again.",
}

LOOK_NOTE = """

Looking through your camera:
- You are {robot} looking through your own camera at a family with a very young child. Attached is one \
still photo, taken just now because someone asked you to look.
- Answer what you see plainly and briefly, in at most {words} words.
- Count things carefully (for example how many fingers are held up) and say the number.
- Never identify people, and never guess anyone's age, name or location. Never read out personal \
information such as names, addresses, numbers, screens or documents.
- If you can't tell, say so."""

_ASKED_BY = re.compile(r"[a-z][a-z0-9_]{0,23}")


class CameraLook:
    """One photo per question, answered by the ChatGPT text model. Shared via ``get_look(ctx)``."""

    def __init__(self, ctx: Any, *, provider: BaseProvider | None = None) -> None:
        self.ctx = ctx
        self._provider = provider
        self._busy = False
        self._day = _dt.date.today()
        self._photos_today = 0
        self._last_at: float | None = None

    @property
    def provider(self) -> BaseProvider:
        if self._provider is None:
            from littlebot.chatgpt.responses import ChatGPTProvider  # noqa: PLC0415

            cfg = self.ctx.config.chatgpt
            self._provider = ChatGPTProvider(get_auth(self.ctx), models=list(cfg.models), effort=cfg.reasoning_effort)
        return self._provider

    # ------------------------------------------------------------ gates
    def blocked_reason(self) -> str | None:
        ctx = self.ctx
        privacy = ctx.privacy
        if ctx.config.is_offline:
            return "offline"
        if not privacy.cloud_enabled:
            return "cloud_off"
        if not privacy.camera_enabled:
            return "camera_off"
        if not getattr(privacy, "ai_camera_enabled", False):
            return "ai_camera_off"
        if not self.provider.configured():
            return "not_signed_in"
        vision = ctx.vision
        if vision is None or not hasattr(vision, "capture_photo"):
            return "no_camera"
        try:
            camera = (vision.status() or {}).get("camera")
        except Exception:  # noqa: BLE001
            camera = None
        if camera not in ("ok", "starting"):
            return "no_camera"
        return None

    def status(self) -> dict[str, Any]:
        """``enabled`` is the parent's AI camera switch; ``blocked`` says why looking is not possible now."""
        self._roll_day()
        reason = self.blocked_reason()
        return {
            "enabled": bool(getattr(self.ctx.privacy, "ai_camera_enabled", False)),
            "blocked": reason,
            "blocked_detail": ERRORS.get(reason) if reason else None,
            "photos_today": self._photos_today,
            "last_at": self._last_at,
        }

    # ------------------------------------------------------------ look
    async def look(self, question: str, *, asked_by: str = "parent") -> dict[str, Any]:
        t0 = time.monotonic()
        asked_by = asked_by if isinstance(asked_by, str) and _ASKED_BY.fullmatch(asked_by) else "other"

        def result(answer: str | None = None, error: str | None = None) -> dict[str, Any]:
            return {"ok": error is None, "answer": answer if error is None else None, "error": error,
                    "detail": ERRORS.get(error) if error else None, "latency_ms": int((time.monotonic() - t0) * 1000)}

        reason = self.blocked_reason()
        if reason:
            return result(error=reason)
        if self._busy:
            return result(error="busy")
        self._busy = True
        try:
            text = sanitize_user_text(question or "", max_chars=300).strip() or DEFAULT_QUESTION
            try:
                photo: bytes | None = await self.ctx.vision.capture_photo(
                    max_width=PHOTO_MAX_WIDTH, quality=PHOTO_QUALITY, timeout=PHOTO_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001 - PhotoUnavailable(reason) or a broken camera
                code = getattr(e, "reason", None)
                code = code if code in ("camera_off", "no_camera") else "photo_failed"
                log.warning("camera look: no photo (%s)", getattr(e, "reason", type(e).__name__))
                return result(error=code)
            if not photo:
                return result(error="photo_failed")
            self._count_photo()
            self._show(asked_by)
            try:
                log.info("photo sent to ChatGPT (asked_by=%s)", asked_by)
                reply = await self.provider.reply([{"role": "user", "content": text}], system=self._system_prompt(),
                                                  max_tokens=200, timeout=REPLY_TIMEOUT_S, images=[photo])
            except (ProviderError, asyncio.TimeoutError, TimeoutError) as e:
                code = _error_code(e)
                log.warning("camera look: ChatGPT failed (%s)", code)
                return result(error=code)
            finally:
                photo = None  # drop the image right away (nothing else holds it)
            from littlebot.brain.face_actions import extract_tags  # noqa: PLC0415

            answer_text, _ = extract_tags(reply.text or "")  # a stray [face: ...] tag must never be read out
            checked = validate_reply(answer_text, max_words=MAX_WORDS)
            answer = checked.text if checked.ok and checked.text.strip() else FALLBACK
            return result(answer=answer)
        finally:
            self._busy = False

    # ------------------------------------------------------------ helpers
    def _system_prompt(self) -> str:
        ctx = self.ctx
        base = build_system_prompt(robot_name=ctx.config.robot_name, age_months=ctx.settings.child_age_months(),
                                   max_words=MAX_WORDS)
        return base + LOOK_NOTE.format(robot=ctx.config.robot_name, words=MAX_WORDS)

    def _roll_day(self) -> None:
        today = _dt.date.today()
        if today != self._day:
            self._day, self._photos_today = today, 0

    def _count_photo(self) -> None:
        self._roll_day()
        self._photos_today += 1
        self._last_at = time.time()

    def _show(self, asked_by: str) -> None:
        """Make the photo visible: the face peeks and the dashboard hears about it."""
        face = self.ctx.face
        if face is not None:
            try:
                face.set_expression("curious", hold=2.0)
                face.play_animation("peek")
            except Exception:  # noqa: BLE001 - a face glitch must not stop the answer
                log.debug("camera look: face reaction failed", exc_info=True)
        self.ctx.bus.publish(Topics.VISION_PHOTO, {"asked_by": asked_by}, source="chatgpt")


def _error_code(e: BaseException) -> str:
    if isinstance(e, (ProviderTimeoutError, asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(e, NetworkError):
        return "network"
    if isinstance(e, RateLimitError):
        return "usage_limit" if e.kind == "usage_limit" else "rate_limited"
    if isinstance(e, AuthenticationError):
        return e.kind if e.kind in ("not_signed_in", "expired") else "auth"
    kind = getattr(e, "kind", "")
    if kind in ("refused", "plan_not_included"):
        return kind
    return "provider_error"


def get_look(ctx: Any) -> CameraLook:
    """The one CameraLook shared by live voice and the dashboard (``ctx.extras["camera_look"]``)."""
    look = ctx.extras.get(_KEY)
    if look is None:
        look = ctx.extras[_KEY] = CameraLook(ctx)
    return look


__all__ = ["ERRORS", "CameraLook", "get_look"]
