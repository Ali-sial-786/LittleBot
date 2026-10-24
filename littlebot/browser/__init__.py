"""The browser view: Google (SafeSearch on) on LittleBot's screen when a grown-up explicitly asks for it.

"Google it", "look it up online", "search that in the browser": the screen shows Google's results in a
Chromium window without an address bar, tabs or menus, with LittleBot's mini eyes and a big × in a corner.
Ordinary questions still get info cards (littlebot/screens); the browser is only for explicit requests.

* ``service``      - BrowserView (``ctx.browser``): open, control, read, close, the idle timer, the gates
* ``cdp``          - a small DevTools client over a localhost WebSocket (standard library only)
* ``policy``       - https only, SafeSearch forced on, the query's safety check
* ``page_scripts`` - LittleBot's strip on every page, and the scripts that read and steer pages

Triggers: the screen director's local rules (littlebot/screens/director.py) and the voice's tools
``search_in_browser`` / ``browser`` (littlebot/chatgpt/tools.py). See docs/BROWSER.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from littlebot.browser.service import BrowserView

__all__ = ["create_service", "voice_instructions"]


def create_service(ctx: Any) -> BrowserView:
    """Factory used by ``app/robot.py``."""
    from littlebot.browser.service import BrowserView  # noqa: PLC0415

    return BrowserView(ctx)


def voice_instructions(ctx: Any) -> str:
    """What the live voice is told about the browser ("" when there is none, or the parent switched it off)."""
    browser = getattr(ctx, "browser", None)
    try:
        if browser is None or not browser.offered():
            return ""
    except Exception:  # noqa: BLE001
        return ""
    from littlebot.browser.service import VOICE_INSTRUCTIONS  # noqa: PLC0415

    return VOICE_INSTRUCTIONS
