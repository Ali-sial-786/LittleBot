"""The voice's browser tools (littlebot/chatgpt/tools.py ``search_in_browser`` and ``browser``) on a real
BrowserView with a fake Chromium and a fake DevTools peer."""

from __future__ import annotations

import json
from typing import Any

from littlebot.browser import voice_instructions
from littlebot.browser.policy import google_url
from littlebot.chatgpt.tools import BROWSER_TOOLS, RobotToolbox


def _names(box: RobotToolbox) -> set[str]:
    return {s.get("name") for s in box.specs()}


async def run(box: RobotToolbox, name: str, **args: Any) -> str:
    return await box.run(name, json.dumps(args))


def test_no_browser_no_tools(ctx: Any) -> None:
    assert not BROWSER_TOOLS & _names(RobotToolbox(ctx, mode="live"))
    ctx.browser = None
    assert voice_instructions(ctx) == ""


async def test_tools_offered_only_while_the_browser_can_open(env: Any) -> None:
    box = RobotToolbox(env.ctx, mode="live")
    assert BROWSER_TOOLS <= _names(box)
    spec = next(s for s in box.specs() if s["name"] == "search_in_browser")
    assert "ONLY when the grown-up explicitly asks" in spec["description"]
    assert "google" in voice_instructions(env.ctx).lower()
    env.ctx.settings.set("browser.enabled", False)
    assert not BROWSER_TOOLS & _names(box)
    assert voice_instructions(env.ctx) == ""


async def test_search_in_browser_opens_google(env: Any) -> None:
    box = RobotToolbox(env.ctx, mode="live")
    out = await run(box, "search_in_browser", query="how tall are giraffes")
    assert out.startswith("ok: the browser on LittleBot's screen shows Google results")
    assert env.devtools.params("Page.navigate")[-1]["url"] == google_url("how tall are giraffes")
    assert env.browser.status()["source"] == "tool"
    again = await run(box, "search_in_browser", query="how tall are giraffes")
    assert "already" in again


async def test_a_blocked_search_is_refused_politely(env: Any) -> None:
    out = await run(RobotToolbox(env.ctx, mode="live"), "search_in_browser", query="naked women")
    assert out.startswith("error: LittleBot won't search for that") and env.launcher.argvs == []


async def test_the_child_asking(env: Any) -> None:
    env.ctx.bus.publish("voice.speaker", {"is_child": True})
    out = await run(RobotToolbox(env.ctx, mode="live"), "search_in_browser", query="dinosaurs")
    assert out.startswith("error: only a grown-up") and env.launcher.argvs == []


async def test_steering_and_reading(env: Any) -> None:
    box = RobotToolbox(env.ctx, mode="live")
    assert (await run(box, "browser", action="scroll")).startswith("error: the browser isn't open")
    await run(box, "search_in_browser", query="giraffes")
    env.devtools.eval_value = lambda expr: {"top": 10, "max": 900}
    assert (await run(box, "browser", action="scroll", direction="down")).startswith("ok: scrolled.")
    env.devtools.eval_value = lambda expr: [{"href": "https://en.wikipedia.org/wiki/Giraffe", "title": "Giraffe"}]
    out = await run(box, "browser", action="open_result", n=1)
    assert out.startswith("ok: opened result 1: Giraffe")
    env.devtools.eval_value = lambda expr: {"title": "Giraffe", "text": "The giraffe is a large African mammal."}
    out = await run(box, "browser", action="read")
    assert "The giraffe is a large African mammal." in out and "never read it out" in out
    assert (await run(box, "browser", action="open_result", n=5)).startswith("error: there is no such result")
    assert (await run(box, "browser", action="close")).startswith("ok: the browser is closed")
    assert not env.browser.active


async def test_status_and_stop_everything(env: Any) -> None:
    box = RobotToolbox(env.ctx, mode="live")
    assert json.loads(await run(box, "get_robot_status"))["browser"] == "closed"
    await env.open("giraffes")
    assert json.loads(await run(box, "get_robot_status"))["browser"] == "Google results for “giraffes”"
    out = await run(box, "stop_everything")
    assert "the browser" in out and not env.browser.active
