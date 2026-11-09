"""Phone notifications through ntfy (https://ntfy.sh): LittleBot's heads-ups and reminders on your phone too.

ntfy is a free notification service with phone apps (Android, iPhone) and no account: a message published to a
*topic* reaches every phone subscribed to it. The topic is the only thing that keeps it private, so LittleBot uses a
long random one (``scripts/setup_phone.py`` makes it and sends a test), kept like a key: ``NTFY_TOPIC`` in .env or
``data/ntfy_topic`` (chmod 600), never logged. ``skills.ntfy_server`` can point at your own ntfy server instead.

What is sent: the short heads-up or reminder text only (an event's title, time and place; a reminder's words), over
https, to the ntfy server. Nothing is sent in offline mode or while cloud communication is off in Privacy. A photo or
video shared to a phone (littlebot/capture) sends only a link to it on the robot itself: the phone opens it, and the
ntfy app fetches its preview, straight from LittleBot over the home Wi-Fi.

Each grown-up on Who's who can have their own phone too (``data/ntfy_topic_p<id>``, made on the dashboard or with
``setup_phone.py --person NAME``): their own events' heads-ups go to their phone; the family's events and reminders
go to the household's topic and to everyone's own.

    POST {server}/  {"topic": ..., "title": ..., "message": ..., "tags": [...], "priority": 3}
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("littlebot.skills.push")

TIMEOUT_S = 8.0
MAX_TITLE = 120
MAX_MESSAGE = 3000
PRIORITY = {"low": 2, "default": 3, "high": 4}
PERSON_TOPIC = "ntfy_topic_p{id}"  # a person's own phone (Who's who id), chmod 600


def new_topic() -> str:
    """A long random topic: nobody can guess it, so nobody else can read the notifications."""
    return "littlebot-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y").lower()


class Push:
    """Sends a notification to the family's phones. Never raises: ``send`` says whether it went."""

    def __init__(self, ctx: Any, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.ctx = ctx
        self._transport = transport
        self.last_error: str | None = None
        self._dir = Path(ctx.extras.get("phone_topic_dir") or ctx.config.data_path)  # tests: their own

    def topic(self, person_id: int | None = None) -> str:
        """The household's topic (None) or a person's own ("" when there is none)."""
        if person_id is None:
            return self.ctx.config.secret("ntfy_topic").strip()
        from littlebot.core.keys import read_private  # noqa: PLC0415

        return (read_private(self._person_file(person_id)) or "").strip()

    def _person_file(self, person_id: int) -> Path:
        return self._dir / PERSON_TOPIC.format(id=int(person_id))

    def person_ids(self) -> list[int]:
        """Everyone who has their own phone set up."""
        out = []
        for f in sorted(self._dir.glob("ntfy_topic_p*")) if self._dir.is_dir() else []:
            with contextlib.suppress(ValueError):
                out.append(int(f.name.removeprefix("ntfy_topic_p")))
        return out

    def topics(self, to: int | None = None) -> list[str]:
        """Where a notification goes: a person's own phone (their topic, else the household's), or for the whole
        family (None) the household's topic and everyone's own."""
        if to is not None:
            own = self.topic(to)
            return [own] if own else [t for t in [self.topic()] if t]
        found = [self.topic()] + [self.topic(pid) for pid in self.person_ids()]
        return list(dict.fromkeys(t for t in found if t))

    def make_topic(self, person_id: int | None) -> str:
        """A new topic for the household (data/ntfy_topic) or a person: what their phone subscribes to."""
        from littlebot.core.keys import write_private  # noqa: PLC0415

        topic = new_topic()
        path = self._dir / "ntfy_topic" if person_id is None else self._person_file(person_id)
        write_private(path.parent, path.name, topic)
        if person_id is None:
            from pydantic import SecretStr  # noqa: PLC0415

            self.ctx.config.ntfy_topic = SecretStr(topic)
        log.info("push: a new phone topic was made")
        return topic

    def remove_topic(self, person_id: int | None) -> bool:
        path = self._dir / "ntfy_topic" if person_id is None else self._person_file(person_id)
        had = path.exists()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        if person_id is None:
            from pydantic import SecretStr  # noqa: PLC0415

            from littlebot.core.keys import current_env  # noqa: PLC0415

            left = current_env().get("NTFY_TOPIC", "").strip()  # the .env one, if any, is used again
            self.ctx.config.ntfy_topic = SecretStr(left) if left else None
        return had

    def server(self) -> str:
        url = str(getattr(self.ctx.config.skills, "ntfy_server", "") or "https://ntfy.sh").strip().rstrip("/")
        return url if url.startswith(("https://", "http://")) else "https://ntfy.sh"

    def set_up(self, person_id: int | None = None) -> bool:
        """Some phone is set up (``person_id``: that person's own, or the household's they fall back to)."""
        return bool(self.topics(person_id))

    def blocked_reason(self, to: int | None = None) -> str | None:
        if not self.set_up(to):
            return "not_set_up"
        if self.ctx.config.is_offline:
            return "offline"
        if not self.ctx.privacy.cloud_enabled:
            return "cloud_off"
        return None

    async def send(self, title: str, message: str, *, tags: tuple[str, ...] = (), priority: str = "default",
                   to: int | None = None, click: str | None = None, attach: str | None = None,
                   filename: str | None = None) -> bool:
        """To one person's phone (``to``) or the whole family's. True when it reached at least one.

        ``click``: a web address the notification opens when tapped (it also gets an "Open" button); ``attach``: a
        picture's web address the ntfy app fetches itself and shows (ntfy passes the address on, it doesn't
        download it), ``filename`` its name. Both only http(s)."""
        why = self.blocked_reason(to)
        if why is not None:
            self.last_error = why
            return False
        extra: dict[str, Any] = {}
        if click and str(click).startswith(("http://", "https://")):
            extra["click"] = str(click)[:1000]
            extra["actions"] = [{"action": "view", "label": "Open", "url": str(click)[:1000], "clear": True}]
        if attach and str(attach).startswith(("http://", "https://")):
            extra["attach"] = str(attach)[:1000]
            if filename:
                extra["filename"] = str(filename)[:100]
        went = False
        for topic in self.topics(to):
            went = await self._send_one(topic, title, message, tags, priority, extra) or went
        return went

    async def _send_one(self, topic: str, title: str, message: str, tags: tuple[str, ...], priority: str,
                        extra: dict[str, Any] | None = None) -> bool:
        body: dict[str, Any] = {"topic": topic, "title": " ".join(str(title).split())[:MAX_TITLE],
                                "message": str(message).strip()[:MAX_MESSAGE] or " ",
                                "priority": PRIORITY.get(priority, 3), **(extra or {})}
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
        log.info("push: a notification went to a phone (%s)", ",".join(body.get("tags") or []) or "note")
        return True

    def _failed(self, kind: str) -> bool:
        self.last_error = kind
        log.info("push: the notification didn't go (%s)", kind)
        return False


__all__ = ["Push", "new_topic"]
