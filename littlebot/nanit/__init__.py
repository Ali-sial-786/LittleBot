"""The Nanit baby camera on LittleBot's screen, when a grown-up asks ("show the baby camera").

An unofficial community bridge (indiefan/home_assistant_nanit; built, signed in and started as a systemd user
unit by scripts/setup_nanit.sh) asks the camera to send its picture to the robot over the home network; ffplay
shows it full screen over the face until someone closes it, something more important needs the screen, or
``nanit.max_minutes`` pass.

* ``service`` - NanitService (``ctx.nanit``): show, close, status, the gates, the bridge's state and ffplay

Triggers: the voice's tools ``show_baby_camera`` / ``close_baby_camera`` (littlebot/skills/nanit.py) and the
dashboard (littlebot/dashboard/routes/nanit_routes.py). See docs/NANIT.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from littlebot.nanit.service import NanitService

__all__ = ["create_service"]


def create_service(ctx: Any) -> NanitService:
    """Factory used by ``app/robot.py``."""
    from littlebot.nanit.service import NanitService  # noqa: PLC0415

    return NanitService(ctx)
