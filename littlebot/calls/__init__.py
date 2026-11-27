"""Video and voice calls through WhatsApp Web on LittleBot's screen, only while it is on the screen.

"Hey LittleBot, video call Dad", "open WhatsApp", "answer on WhatsApp", "hang up": a Chromium window on
web.whatsapp.com (linked once to the owner's own WhatsApp with a QR code) opens, LittleBot's own cover hides the
chats, the call is placed by working WhatsApp's page, and afterwards Chromium is shut down so its memory comes back.
There is no WhatsApp in the background: the robot never rings by itself.

* ``service``      - CallsService (``ctx.calls``): open, call, answer, hang up, close, the gates, the watching
* ``page_scripts`` - every WhatsApp selector (in ONE place), the scripts that find and press things, the cover
* ``policy``       - where the window may go (web.whatsapp.com only)
* ``contacts``     - who can be called: the names as said -> the names as they appear in WhatsApp

Triggers: the instant phrases (littlebot/assistant/intents.py), the voice's tools (littlebot/skills/calls.py), the
dashboard (littlebot/dashboard/routes/calls_routes.py) and the settings panel on the screen. See docs/CALLS.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from littlebot.calls.service import CallsService

__all__ = ["create_service"]


def create_service(ctx: Any) -> CallsService:
    """Factory used by ``app/robot.py``."""
    from littlebot.calls.service import CallsService  # noqa: PLC0415

    return CallsService(ctx)
