"""Phone notifications through ntfy (https://ntfy.sh): LittleBot's heads-ups and reminders on your phone too.

ntfy is a free notification service with phone apps (Android, iPhone) and no account: a message published to a
*topic* reaches every phone subscribed to it. The topic is the only thing that keeps it private, so LittleBot uses a
long random one (``scripts/setup_phone.py`` makes it and sends a test), kept like a key: ``NTFY_TOPIC`` in .env or
``data/ntfy_topic`` (chmod 600), never logged. ``skills.ntfy_server`` can point at your own ntfy server instead.

What is sent: the short heads-up or reminder text only (an event's title, time and place; a reminder's words), over
https, to the ntfy server. Nothing is sent in offline mode or while cloud communication is off in Privacy.

    POST {server}/  {"topic": ..., "title": ..., "message": ..., "tags": [...], "priority": 3}
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("littlebot.skills.push")

TIMEOUT_S = 8.0
MAX_TITLE = 120
MAX_MESSAGE = 3000
PRIORITY = {"low": 2, "default": 3, "high": 4}


class Push:
    """Sends a notification to the family's phones. Never raises: ``send`` says whether it went."""

    def __init__(self, ctx: Any, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.ctx = ctx
        self._transport = transport
        self.last_error: str | None = None

    def topic(self) -> str:
        return self.ctx.config.secret("ntfy_topic").strip()

    def server(self) -> str:
        url = str(getattr(self.ctx.config.skills, "ntfy_server", "") or "https://ntfy.sh").strip().rstrip("/")
        return url if url.startswith(("https://", "http://")) else "https://ntfy.sh"

    def set_up(self) -> bool:
        return bool(self.topic())

    def blocked_reason(self) -> str | None:
        if not self.set_up():
            return "not_set_up"
        if self.ctx.config.is_offline:
            return "offline"
        if not self.ctx.privacy.cloud_enabled:
            return "cloud_off"
        return None

    async def send(self, title: str, message: str, *, tags: tuple[str, ...] = (), priority: str = "default") -> bool:
        why = self.blocked_reason()
        if why is not None:
            self.last_error = why
            return False
        body: dict[str, Any] = {"topic": self.topic(), "title": " ".join(str(title).split())[:MAX_TITLE],
                                "message": str(message).strip()[:MAX_MESSAGE] or " ",
                                "priority": PRIORITY.get(priority, 3)}
        if tags:
            body["tags"] = [str(t) for t in tags][:5]
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=httpx.Timeout(TIMEOUT_S, connect=4.0),
                                         follow_redirects=False) as client:
                resp = await client.post(self.server() + "/", json=body)
        except httpx.TimeoutException:
            return self._failed("timeout")
        except httpx.HTTPError:
            return self._failed("network")
        if resp.status_code >= 400:
            return self._failed("rate_limited" if resp.status_code == 429 else f"http_{resp.status_code}")
        self.last_error = None
        log.info("push: a notification went to the phone (%s)", ",".join(body.get("tags") or []) or "note")
        return True

    def _failed(self, kind: str) -> bool:
        self.last_error = kind
        log.info("push: the notification didn't go (%s)", kind)
        return False


__all__ = ["Push"]
