"""Sign in with ChatGPT: text replies and GPT-Live voice on the parent's own ChatGPT plan.

* ``auth``      - device-code sign-in, token storage/refresh/sign-out (tokens never leave the robot)
* ``responses`` - the ``chatgpt`` conversation provider (Codex Responses endpoint)
* ``wire``      - GPT-Live data-channel events (the Codex "quicksilver" variant)
* ``live``      - the live voice conversation service (WebRTC via aiortc, optional extra)

Everything here uses undocumented Codex endpoints; see docs/CHATGPT.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from littlebot.chatgpt.auth import ChatGPTAuth
    from littlebot.core.context import RobotContext

_KEY = "chatgpt_auth"


def get_auth(ctx: "RobotContext") -> "ChatGPTAuth":
    """The one ChatGPTAuth shared by the brain, live voice and the dashboard."""
    auth: Any = ctx.extras.get(_KEY)
    if auth is None:
        from pathlib import Path

        from littlebot import __version__
        from littlebot.chatgpt.auth import ChatGPTAuth

        cfg = ctx.config.chatgpt
        path = Path(cfg.token_file).expanduser()
        if not path.is_absolute():
            path = ctx.config.data_path / path
        auth = ChatGPTAuth(path, originator=cfg.originator, user_agent=f"LittleBot/{__version__} (Raspberry Pi)")
        ctx.extras[_KEY] = auth
    return auth


def create_service(ctx: "RobotContext") -> Any:
    """The live voice service (``ctx.live``). Works without aiortc; it then reports itself unavailable."""
    from littlebot.chatgpt.live import LiveVoiceService

    return LiveVoiceService(ctx)


__all__ = ["create_service", "get_auth"]
