"""The live-information tools (get_weather, get_time) and web search in LittleBot's toolbox (no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "info"))
from weather_fakes import FakeWeb, weather_service  # noqa: E402

from littlebot.chatgpt.tools import RobotToolbox  # noqa: E402


async def run(box: RobotToolbox, name: str, **args) -> str:
    return await box.run(name, json.dumps(args))


async def test_get_weather_at_home_and_elsewhere(ctx):
    ctx.privacy.set(cloud_enabled=True)
    web = FakeWeb()
    weather_service(ctx, web)
    box = RobotToolbox(ctx, mode="live")
    home = json.loads(await run(box, "get_weather"))
    assert home["place"] == "Toronto, Ontario" and home["now"]["conditions"] == "sunny"
    assert [d["day"] for d in home["forecast"]] == ["today", "tomorrow"]
    paris = json.loads(await run(box, "get_weather", place="Paris", days=30))  # days capped at 7
    assert paris["place"].startswith("Paris") and len(paris["forecast"]) == 7
    assert box.calls == ["get_weather", "get_weather"]


async def test_get_weather_problems_are_plain_errors(ctx):
    web = FakeWeb()
    weather_service(ctx, web)
    box = RobotToolbox(ctx)
    out = await run(box, "get_weather")
    assert out.startswith("error: cloud communication is off in Privacy") and "don't guess" in out
    ctx.privacy.set(cloud_enabled=True)
    out = await run(box, "get_weather", place="Atlantis")
    assert out.startswith("error: no place called Atlantis was found") and not box.pending
    assert (await box.run("get_weather", '{"days": "lots"}')).startswith("{")  # a bad number: the default


async def test_get_time_here_and_elsewhere(ctx):
    ctx.privacy.set(cloud_enabled=True)
    weather_service(ctx, FakeWeb())
    box = RobotToolbox(ctx, mode="live")
    here = json.loads(await run(box, "get_time"))
    assert {"time_24h", "time_12h", "day", "date", "part_of_day", "timezone"} <= set(here) and "place" not in here
    tokyo = json.loads(await run(box, "get_time", place="Tokyo"))
    assert tokyo["place"] == "Tokyo, Japan" and tokyo["timezone"] == "Asia/Tokyo" and tokyo["utc_offset"] == "+09:00"
    ctx.config.info.timezone = "America/Toronto"
    assert json.loads(await run(box, "get_time"))["timezone"] == "America/Toronto"
    # worked out, not left to the brain ("nine hours ahead" from Ontario; the robot, 2026-10-31)
    ahead = json.loads(await run(box, "get_time", place="Tokyo"))["compared_with_home"]
    assert ahead in ("13 hours ahead of home", "14 hours ahead of home")  # (summer time in Ontario or not)
    ctx.config.info.timezone = "Asia/Kolkata"
    assert json.loads(await run(box, "get_time", place="Tokyo"))["compared_with_home"] == "3½ hours ahead of home"
    ctx.config.info.timezone = "Asia/Tokyo"
    assert json.loads(await run(box, "get_time", place="Tokyo"))["compared_with_home"] == "the same time as home"
    assert "compared_with_home" not in json.loads(await run(box, "get_time"))
    ctx.config.info.timezone = "Nowhere/Special"
    assert json.loads(await run(box, "get_time"))["day"]  # a wrong setting: the robot's own clock
    out = await run(box, "get_time", place="43.6, -79.4")
    assert out.startswith("error: LittleBot doesn't know the time zone")


async def test_web_search_follows_the_setting_and_the_model_is_told_to_use_the_tools(ctx):
    box = RobotToolbox(ctx)
    assert box.specs()[-1] == {"type": "web_search", "name": "web_search"}
    note = box.instructions()
    assert "call get_weather" in note and "call get_time" in note and "use web search first" in note
    assert "news" in (await run(box, "describe_yourself"))
    assert (await run(box, "web_search")).startswith("error: there is no tool")  # OpenAI runs it, not LittleBot
    ctx.config.chatgpt.web_search = False
    assert all(s.get("type", "function") == "function" for s in box.specs())
    assert "web search" not in box.instructions() and "call get_weather" in box.instructions()
    assert "weather" in (await run(box, "describe_yourself")) and "news" not in (await run(box, "describe_yourself"))
