"""Photos and videos by voice, the gallery, sharing to a phone (``ctx.capture``; docs/PHOTOS.md).

``create_service(ctx)`` returns the :class:`~littlebot.capture.service.CaptureService`.
"""

from __future__ import annotations

from typing import Any


def create_service(ctx: Any) -> Any:
    from littlebot.capture.service import CaptureService  # noqa: PLC0415

    return CaptureService(ctx)


__all__ = ["create_service"]
