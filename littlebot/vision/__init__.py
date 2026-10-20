"""Vision subsystem: camera devices, face detection (no recognition), tracking, presence events.

``create_service(ctx)`` returns a ``VisionService`` implementing ``core.interfaces.VisionAPI``.
OpenCV is optional: without it the service runs with the mock camera (simulation)
or reports the camera as unavailable - it never crashes the robot.
See docs/modules/vision.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from littlebot.core.context import RobotContext
    from littlebot.vision.service import VisionService


def create_service(ctx: "RobotContext") -> "VisionService":
    from littlebot.vision.service import VisionService

    return VisionService(ctx)


__all__ = ["create_service"]
