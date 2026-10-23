"""The time for LittleBot's tools: local time, date and day of the week (no network).

"What time is it?" and "what day is it?" are answered from the robot's own clock, in the robot's
time zone (``info.timezone``, else the system's), or in another place's zone ("what time is it in
Tokyo?": the zone comes from the place search in ``weather``). Models often guess these wrong
without a tool, so the answer is always looked up.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def zone(name: str | None) -> _dt.tzinfo | None:
    """The IANA time zone ``name``; None for the robot's own zone. ValueError when it doesn't exist."""
    name = str(name or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {name[:60]}") from None


def local_zone_name() -> str | None:
    """The system's IANA zone name ("Europe/London"), when it can be read; else None."""
    tz = os.environ.get("TZ", "").lstrip(":").strip()
    if tz and "/" in tz and not tz.startswith("/"):
        return tz
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        return name or None
    except OSError:
        return None


def part_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def now_info(tz_name: str | None = None, *, now: _dt.datetime | None = None) -> dict[str, Any]:
    """The time now (or at ``now``), in ``tz_name`` or the robot's own zone.

    ``{"time_24h": "21:30", "time_12h": "9:30 pm", "day": "Friday", "date": "Friday 23 October 2026",
    "part_of_day": "evening", "timezone": "America/Toronto", "utc_offset": "-04:00"}``."""
    tz = zone(tz_name)
    base = now if now is not None else _dt.datetime.now(_dt.timezone.utc)
    if base.tzinfo is None:
        base = base.astimezone()  # a naive time is the robot's local time
    local = base.astimezone(tz) if tz is not None else base.astimezone()
    offset = local.strftime("%z")
    hour12 = local.hour % 12 or 12
    return {
        "time_24h": local.strftime("%H:%M"),
        "time_12h": f"{hour12}:{local:%M} {'am' if local.hour < 12 else 'pm'}",
        "day": local.strftime("%A"),
        "date": f"{local:%A} {local.day} {local:%B %Y}",
        "part_of_day": part_of_day(local.hour),
        "timezone": (str(tz_name).strip() if tz is not None else local_zone_name()) or local.tzname() or "local time",
        "utc_offset": f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset,
    }


__all__ = ["local_zone_name", "now_info", "part_of_day", "zone"]
