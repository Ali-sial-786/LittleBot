"""Parent-controlled YouTube playback (official IFrame Player API only).

* ``allowlist``     - YouTube link parsing / validation (only parent-approved items can play)
* ``repository``    - SQLite tables for the allowlist and daily watch time
* ``controller``    - ``MediaController`` implementing ``core.interfaces.MediaAPI``
* ``bridge``        - desired-state / event bridge to the player page (token protected)
* ``player_routes`` - ``create_player_router(media)`` for the dashboard to mount
* ``display``       - where the page is shown: Chromium kiosk (Pi), desktop browser, or none

See docs/modules/media.md and docs/YOUTUBE_ON_PI.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from littlebot.core.context import RobotContext
    from littlebot.media.controller import MediaController


def create_service(ctx: "RobotContext") -> "MediaController":
    from littlebot.media.controller import MediaController

    return MediaController(ctx)


def create_player_router(media):  # noqa: ANN001, ANN201 - thin lazy re-export (imports FastAPI)
    from littlebot.media.player_routes import create_player_router as _create

    return _create(media)


__all__ = ["create_service", "create_player_router"]
