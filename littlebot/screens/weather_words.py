"""Which view of the weather card words ask for: "the hourly", "this week", "tomorrow", "right now".

Shared by the quick weather answer (littlebot/chatgpt/quick.py: the card goes up in that view) and the screen
director (littlebot/screens/director.py: "show me the hourly" with the weather card up steers it at once,
without asking a model). "Early" counts as hourly: speech recognition often hears "hourly" as "early".
"""

from __future__ import annotations

import re

_HOURS = re.compile(r"\b(?:hourly|early|hour[- ]by[- ]hour|by the hour|each hour|every hour|hours|"
                    r"next (?:few |couple of |couple )?hours|morning|afternoon|evening|tonight|"
                    r"later(?: today| on)?)\b", re.I)
_WEEK = re.compile(r"\b(?:week(?:ly|end)?|(?:7|seven|5|five) days?|next (?:few |couple of |couple )?days|"
                   r"days ahead|coming days)\b", re.I)
_NOW = re.compile(r"\b(?:right now|now|currently|at the moment)\b", re.I)
_WEEKDAY = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
_TOMORROW = re.compile(r"\btomorrow\b", re.I)
_TODAY = re.compile(r"\btoday\b", re.I)


def weather_view(text: str) -> tuple[str | None, str | None]:
    """(view, day) for the weather card ("hours", "tomorrow"), or (None, None) when the words don't say."""
    text = str(text or "")
    m = _WEEKDAY.search(text)
    day = "tomorrow" if _TOMORROW.search(text) else m.group(1).capitalize() if m else None
    if _HOURS.search(text):
        return "hours", day or ("today" if _TODAY.search(text) else None)
    if _WEEK.search(text):
        return "week", None
    if day:
        return "day", day
    if _NOW.search(text):
        return "now", None
    return None, None


__all__ = ["weather_view"]
