"""Phone screen mirroring: an iPhone (AirPlay, UxPlay) or an Android phone (scrcpy over Wi-Fi) on LittleBot's screen.

"Mirror my iPhone", "show my phone on your screen", or the dashboard's buttons: the phone's screen fills LittleBot's
screen until the phone stops, someone says "stop mirroring", or something more important needs the screen. The
AirPlay receiver only runs while mirroring was asked for, and stops if no phone comes.

* ``service`` - MirrorService (``ctx.mirror``): start, stop, pair, forget, the gates and the programs
* ``adb``     - checking pairing input, running adb, reading what it prints

Triggers: the voice's tools ``mirror_phone`` / ``stop_mirroring`` (littlebot/skills/mirror.py) and the dashboard
(littlebot/dashboard/routes/mirror_routes.py). See docs/MIRRORING.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from littlebot.mirror.service import MirrorService

__all__ = ["create_service"]


def create_service(ctx: Any) -> MirrorService:
    """Factory used by ``app/robot.py``."""
    from littlebot.mirror.service import MirrorService  # noqa: PLC0415

    return MirrorService(ctx)
