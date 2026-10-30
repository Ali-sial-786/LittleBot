"""The briefing, smart-home, routines, music and translator skills as the default SkillHub builds them, and as
GPT-Live's RobotToolbox offers them: names, unique tools, specs the models accept, notes that never crash."""

from __future__ import annotations

import json

from pydantic import SecretStr

from littlebot.chatgpt.tools import TOOL_NAMES, RobotToolbox
from littlebot.skills.hub import SKILLS, SkillHub
from littlebot.testing import make_test_context

MINE = {"briefing": "Briefing", "home": "SmartHome", "routines": "Routines", "music": "Music",
        "translator": "Translator"}
TOOLS = {"get_briefing", "get_headlines", "home_devices", "home_control", "create_routine", "list_routines",
         "delete_routine", "run_routine", "play_music", "music_control", "start_translator", "stop_translator"}


class Media:
    def search_allowed(self) -> bool:
        return True

    async def search(self, query: str, *, limit: int | None = None) -> list:
        return []

    async def play_video(self, video_id: str, **kw) -> dict:
        return {}

    def status(self) -> dict:
        return {"state": "idle"}


def test_the_hub_lists_and_builds_them():
    listed = {m.rsplit(".", 1)[1]: c for m, c in SKILLS}
    for module, cls in MINE.items():
        assert listed[module] == cls
    ctx = make_test_context(skills={"home_assistant_url": "http://homeassistant.local:8123"})
    try:
        ctx.config.home_assistant_token = SecretStr("t")
        ctx.media = Media()
        hub = SkillHub(ctx)
        built = {s.name: type(s).__name__ for s in hub.skills}
        assert {k: built.get(k) for k in MINE} == MINE
        names = [s["name"] for s in hub.specs()]
        assert TOOLS <= set(names) and len(names) == len(set(names))
        assert not TOOLS & set(TOOL_NAMES)  # never shadows one of LittleBot's own tools
        for spec in hub.specs():
            if spec["name"] in TOOLS:
                assert spec["parameters"]["type"] == "object" and spec["description"]
                json.dumps(spec)
        notes = hub.prompt_notes()
        for word in ("get_briefing", "home_control", "create_routine", "play_music", "start_translator"):
            assert word in notes
        hub.context_notes()
        assert {s["name"] for s in hub.status()["skills"]} >= set(MINE)
    finally:
        ctx.db.close()


async def test_robot_toolbox_offers_and_runs_them():
    ctx = make_test_context()
    try:
        ctx.media = Media()
        ctx.skills = SkillHub(ctx)
        box = RobotToolbox(ctx, mode="live")
        names = {s["name"] for s in box.specs()}
        assert {"get_briefing", "play_music", "run_routine", "start_translator"} <= names
        assert "home_control" not in names  # not set up: not offered
        assert (await box.run("start_translator", '{"language": "French"}')).startswith("ok:")
        assert "Interpreter mode" in ctx.skills.context_notes()
        assert (await box.run("stop_translator", "{}")).startswith("ok:")
        await ctx.skills.stop()
    finally:
        ctx.db.close()
