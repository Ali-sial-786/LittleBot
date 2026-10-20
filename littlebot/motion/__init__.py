"""Motion subsystem: servo abstraction, mock + PCA9685 drivers, gestures, e-stop.

Physical motion is OFF by default and needs two independent keys:
config (backend=pca9685 + physical_enabled + valid calibration) AND a parent arming
it at runtime (``motion.physical_armed``, never persisted). See docs/MOTION_SAFETY.md
and docs/modules/motion.md.
"""

from __future__ import annotations

from typing import Any


def create_service(ctx: Any) -> Any:
    """Factory used by app/robot.py. Returns a MotionController (MotionAPI + Service)."""
    from littlebot.motion.controller import MotionController

    return MotionController(ctx)


__all__ = ["create_service"]
