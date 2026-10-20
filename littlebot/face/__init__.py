"""LittleBot's animated face: expressive glowing eyes, display loop and simulator window.

Modules
-------
* ``expressions`` - expression parameter sets (pure data)
* ``effects``     - one-shot animation curves and particle overlays (pure)
* ``animator``    - time-based animation model, deterministic with a seeded RNG (pure)
* ``frame``       - plain-data frame passed from the animator to the renderer
* ``renderer``    - pygame drawing with caching (imports pygame)
* ``controller``  - ``FaceController`` service implementing ``FaceAPI`` (+ ``track_gaze``)
* ``display``     - ``run_display(robot)`` main-thread window / simulator (imports pygame)

Importing this package is cheap: pygame is only imported by the renderer and display.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from littlebot.core.context import RobotContext
    from littlebot.face.controller import FaceController

__all__ = ["create_service"]


def create_service(ctx: "RobotContext") -> "FaceController":
    """Factory used by ``app/robot.py``."""
    from littlebot.face.controller import FaceController

    return FaceController(ctx)
