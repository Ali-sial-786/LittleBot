"""Phone notifications with a link (littlebot/skills/push.py ``click`` / ``attach``): what a shared photo sends to
ntfy, only http(s) addresses, never the photo itself."""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import SecretStr

from littlebot.skills.push import Push
from littlebot.testing import make_test_context


def _push() -> tuple[Push, list[dict[str, Any]], Any]:
    ctx = make_test_context()
    ctx.config.ntfy_topic = SecretStr("littlebot-testtopic")
    ctx.privacy.set(cloud_enabled=True)
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "x"})

    return Push(ctx, transport=httpx.MockTransport(handler)), sent, ctx


async def test_a_link_and_a_preview() -> None:
    push, sent, ctx = _push()
    url = "http://192.168.2.112:8765/share/AbCdEfGhIjKlMnOpQrStUv"
    ok = await push.send("A photo from LittleBot", "Tap to open it.", tags=("camera",), click=url, attach=url + "/file",
                         filename="littlebot-20261106-143100.jpg")
    assert ok is True
    (body,) = sent
    assert body["topic"] == "littlebot-testtopic" and body["click"] == url
    assert body["actions"] == [{"action": "view", "label": "Open", "url": url, "clear": True}]
    assert body["attach"] == url + "/file" and body["filename"] == "littlebot-20261106-143100.jpg"
    assert body["tags"] == ["camera"]
    ctx.db.close()


async def test_only_web_addresses() -> None:
    push, sent, ctx = _push()
    await push.send("t", "m", click="javascript:alert(1)", attach="file:///etc/passwd", filename="x.jpg")
    (body,) = sent
    assert "click" not in body and "actions" not in body and "attach" not in body and "filename" not in body
    ctx.db.close()


async def test_a_plain_notification_is_unchanged() -> None:
    push, sent, ctx = _push()
    await push.send("Heads up", "Dentist at 3 pm")
    (body,) = sent
    assert set(body) == {"topic", "title", "message", "priority"}
    ctx.db.close()
