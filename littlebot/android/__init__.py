"""Android on LittleBot's screen (Waydroid), only while it is on the screen.

"Open Android", "open the Aosu app" or the dashboard's button: Android (LineageOS with Google Play, in a container)
starts and shows full screen. As soon as it isn't on the screen any more (its window closed or behind another, the
screen dark after the quiet minutes, something else on the screen, "close Android"), it is shut down and its 1 to
1.5 GB of memory comes back for LittleBot.

* ``service`` - AndroidService (``ctx.android``): open, close, the gates, watching the window and the memory
* ``windows`` - which windows the desktop has open and which is in front (the compositor asked directly)
* ``inputs``  - when the touch screen was last used (only the time)

Triggers: the voice's tools ``open_android`` / ``close_android`` (littlebot/skills/android.py) and the dashboard
(littlebot/dashboard/routes/android_routes.py). Set up with scripts/setup_waydroid.sh; see docs/ANDROID_ON_PI.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from littlebot.android.service import AndroidService

__all__ = ["create_service"]


def create_service(ctx: Any) -> AndroidService:
    """Factory used by ``app/robot.py``."""
    from littlebot.android.service import AndroidService  # noqa: PLC0415

    return AndroidService(ctx)
