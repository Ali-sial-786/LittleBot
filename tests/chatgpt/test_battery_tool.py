"""The ``battery`` tool and the battery in ``get_robot_status``: only while a battery monitor is fitted."""

from __future__ import annotations

import json
from typing import Any

from littlebot.chatgpt.tools import BATTERY_TOOL, RobotToolbox
from littlebot.core.events import Topics
from littlebot.power.monitor import cell_for_percent
from littlebot.power.protocol import Reading
from littlebot.power.service import PowerService


def _names(ctx: Any, mode: str = "chat") -> set[str]:
    return {s.get("name") for s in RobotToolbox(ctx, mode=mode).specs()}


def _fitted(ctx: Any, percent: float = 42, *, charging: bool = False) -> PowerService:
    power = PowerService(ctx)
    c = cell_for_percent(percent) + (0.1 if charging else 0.0)
    power.process(Reading(voltage=round(2 * c, 3), cell1=round(c, 3), temperature_c=27.0, charging=charging,
                          charged=False, uptime_s=9, fw=1))
    ctx.power = power
    return power


async def test_on_mains_power_there_is_no_battery_tool_or_status(ctx):
    assert BATTERY_TOOL not in _names(ctx) and BATTERY_TOOL not in _names(ctx, "live")
    assert (await RobotToolbox(ctx).run(BATTERY_TOOL, "{}")).startswith("error:")
    status = json.loads(await RobotToolbox(ctx).run("get_robot_status", "{}"))
    assert "battery" not in status
    ctx.power = PowerService(ctx)  # a power service still looking for its monitor: the same
    assert BATTERY_TOOL not in _names(ctx)
    assert "battery" not in json.loads(await RobotToolbox(ctx).run("get_robot_status", "{}"))
    assert "battery" not in (await RobotToolbox(ctx).run("describe_yourself", "{}")).lower()


async def test_the_battery_tool_reports_and_shows_the_sign(ctx):
    _fitted(ctx, 42)
    assert BATTERY_TOOL in _names(ctx) and BATTERY_TOOL in _names(ctx, "live")
    shown: list[Any] = []
    ctx.bus.subscribe(Topics.POWER_SHOW, shown.append)
    out = await RobotToolbox(ctx).run(BATTERY_TOOL, "{}")
    data = json.loads(out)
    assert data["percent"] == 42 and data["charging"] is False
    assert len(shown) == 1 and shown[0].get("percent") == 42
    status = json.loads(await RobotToolbox(ctx).run("get_robot_status", "{}"))
    assert status["battery"]["percent"] == 42
    assert "battery" in (await RobotToolbox(ctx).run("describe_yourself", "{}")).lower()


async def test_while_charging_the_tool_says_so(ctx):
    _fitted(ctx, 60, charging=True)
    data = json.loads(await RobotToolbox(ctx).run(BATTERY_TOOL, "{}"))
    assert data["charging"] is True
