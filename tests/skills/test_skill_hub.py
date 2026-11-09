"""SkillHub: tools from every skill, crashes contained, and RobotToolbox hands skill calls on."""

from __future__ import annotations

from typing import Any

from littlebot.chatgpt.tools import RobotToolbox
from littlebot.skills.base import ANNOUNCE, STR, Skill, ok, tool
from littlebot.skills.hub import SkillHub
from littlebot.testing import make_test_context


class Echo(Skill):
    name = "echo"

    def tools(self) -> list[dict[str, Any]]:
        return [tool("echo", "Say it back.", {"text": STR}, ("text",)), tool("boom", "Crash.")]

    async def run(self, name: str, args: dict[str, Any]) -> str:
        if name == "boom":
            raise RuntimeError("bang")
        return ok(args["text"])

    def prompt_note(self) -> str:
        return "Echo things."

    def context_note(self) -> str:
        return "Echo: ready."


class Hidden(Skill):
    name = "hidden"

    def available(self) -> bool:
        return False

    def tools(self) -> list[dict[str, Any]]:
        return [tool("secret", "Never offered.")]

    def prompt_note(self) -> str:
        return "Never said."


async def test_specs_notes_and_run() -> None:
    ctx = make_test_context()
    hub = SkillHub(ctx, [Echo(ctx), Hidden(ctx)])
    await hub.start()
    assert [s["name"] for s in hub.specs()] == ["echo", "boom"]
    assert hub.prompt_notes() == "Echo things."
    assert hub.context_notes() == "Echo: ready."
    assert await hub.run("echo", '{"text": "hi"}') == "ok: hi"
    assert await hub.run("echo", {"text": "yo"}) == "ok: yo"
    assert (await hub.run("boom")).startswith("error:")
    assert (await hub.run("nope")).startswith("error:")
    assert (await hub.run("echo", "not json")).startswith("error:")
    await hub.stop()


async def test_robot_toolbox_offers_and_runs_skill_tools() -> None:
    ctx = make_test_context()
    ctx.skills = SkillHub(ctx, [Echo(ctx)])
    box = RobotToolbox(ctx, mode="live")
    names = [s.get("name") for s in box.specs()]
    assert "echo" in names and "get_volume" in names
    assert await box.run("echo", '{"text": "there"}') == "ok: there"
    assert "echo" in box.calls


def test_announce_publishes() -> None:
    ctx = make_test_context()
    seen: list[dict[str, Any]] = []
    ctx.bus.subscribe(ANNOUNCE, lambda e: seen.append(dict(e.data)))
    Echo(ctx).announce("Time to go", card={"card": "clock"}, urgent=True)
    assert seen == [{"text": "Time to go", "kind": "echo", "urgent": True, "card": {"card": "clock"}}]


def test_default_hub_builds_without_missing_modules() -> None:
    ctx = make_test_context()
    hub = SkillHub(ctx)  # modules not written yet are skipped quietly
    assert isinstance(hub.skills, list)


async def test_a_skills_takeover_is_queued_for_after_the_turn() -> None:
    from littlebot.skills import takeover

    started: list[str] = []

    class Jukebox(Skill):
        name = "jukebox"

        def tools(self) -> list[dict[str, Any]]:
            return [tool("play_music", "Play.", {"query": STR}, ("query",))]

        async def run(self, name: str, args: dict[str, Any]) -> str:
            async def go() -> bool:
                started.append(args["query"])
                return True

            takeover.offer(self.ctx, "music jazz", go)
            return ok("it starts after you speak")

    ctx = make_test_context()
    ctx.skills = SkillHub(ctx, [Jukebox(ctx)])
    box = RobotToolbox(ctx, mode="chat", defer=True)
    assert (await box.run("play_music", {"query": "jazz"})).startswith("ok")
    assert [p.label for p in box.pending] == ["music jazz"] and started == []
    assert await box.run_pending() == ["music jazz"] and started == ["jazz"]
