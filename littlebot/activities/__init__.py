"""Activities: caregiver-friendly, YAML-scripted play (songs, peekaboo, counting, stories ...).

Scripts live in ``assets/activities/*.yaml`` (see docs/ACTIVITY_GUIDE.md for the
step language); the engine in ``engine.py`` runs one at a time and implements
``core.interfaces.ActivityAPI``.
"""

from __future__ import annotations

from typing import Any


def create_service(ctx: Any) -> Any:
    """Build the activity engine (loads every script; broken ones are logged and skipped)."""
    from littlebot.activities.engine import ActivityEngine

    return ActivityEngine(ctx)


__all__ = ["create_service"]
