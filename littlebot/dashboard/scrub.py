"""Remove secrets from anything the dashboard sends to the browser.

API keys live only in ``ctx.config`` as ``SecretStr`` and ``public_dict()``
already excludes them; this is a second, independent safety net applied to
status payloads, config views, event logs and subsystem results.
"""

from __future__ import annotations

import re
from typing import Any

# Values that must never leave the server even if a module puts them in an event.
_SECRET_KEY_RE = re.compile(r"(^|[_\-])(api[_\-]?key|secret|password|passwd|token|authorization|cookie)$",
                            re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|sk-ant-[A-Za-z0-9_\-]{8,})")


def scrub(value: Any, depth: int = 0) -> Any:
    """Recursively drop secret-looking keys / values and truncate long strings."""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:60]:
            key = str(k)
            if _SECRET_KEY_RE.search(key):
                out[key] = "[hidden]"
            else:
                out[key] = scrub(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(v, depth + 1) for v in list(value)[:60]]
    if isinstance(value, str):
        value = _SECRET_VALUE_RE.sub("[hidden]", value)
        return value if len(value) <= 300 else value[:300] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return scrub(str(value), depth + 1)


def scrub_status(st: dict[str, Any]) -> dict[str, Any]:
    # Player URLs carry a kiosk token: keep them (parents may open the player), but scrub everything else.
    media = st.get("media")
    player = media.get("player") if isinstance(media, dict) else None
    out = scrub(st)
    if isinstance(player, dict) and isinstance(out.get("media"), dict):
        out["media"]["player"] = {k: v for k, v in player.items() if isinstance(v, (str, int, float, bool, type(None)))}
    return out
