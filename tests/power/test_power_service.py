"""``ctx.power``: events, the gentle low-battery line, the goodbye + clean shutdown, heat, the face's battery sign."""

from __future__ import annotations

import asyncio
import logging

import pytest
from power_helpers import FakeLive, FakeRobot, events, feed, make, reading, topics

from littlebot.core.events import Topics
from littlebot.core.state import RobotState
from littlebot.power import fitted, voice_instructions
from littlebot.power.monitor import SerialMonitor, SimulatedMonitor
from littlebot.power.service import EMPTY_LINE, HOT_LINE, LOW_LINE, NEARLY_EMPTY_LINE, PowerService
from littlebot.testing import make_test_context


# ---------------------------------------------------------------- mains power: nothing changes
async def test_without_a_monitor_nothing_at_all_changes():
    ctx = make_test_context(power={"monitor": "auto"})  # the default: look on the serial ports (here: none)
    scans: list[int] = []
    src = SerialMonitor(lister=lambda: scans.append(1) or [], scan_s=0.02)
    svc = PowerService(ctx, source=src)
    ctx.power = svc
    seen = []
    ctx.bus.subscribe("*", seen.append)
    await svc.start()
    await asyncio.sleep(0.3)
    svc.drain()
    ctx.bus.publish(Topics.FACE_DOZE, {"dozing": False, "reason": "test"})  # waking up shows nothing either
    await svc.stop()
    assert len(scans) >= 2  # it kept looking, quietly
    assert [e.topic for e in seen] == [Topics.FACE_DOZE]  # not one event of its own
    assert ctx.audio.spoken == [] and ctx.face.calls == []
    st = svc.status()
    assert st["fitted"] is False and st["detail"] == "not fitted" and st["percent"] is None and st["health"] == []
    assert not fitted(ctx) and voice_instructions(ctx) == "" and svc.describe() is None and svc.show() is False
    ctx.db.close()


async def test_power_monitor_none_is_disabled():
    ctx = make_test_context(power={"monitor": "none"})
    svc = PowerService(ctx)
    await svc.start()
    assert svc.source is None and svc.health()["status"] == "disabled"
    await svc.stop()
    ctx.db.close()


# ---------------------------------------------------------------- state and events
async def test_readings_make_a_state_and_events_at_most_every_few_seconds():
    ctx, svc, clock = make()
    feed(svc, clock, reading(80, t=24.0), 60)
    st = svc.state()
    assert st["fitted"] and st["connected"] and st["percent"] == 80 and st["voltage"] == pytest.approx(8.04, abs=0.01)
    assert st["cells"] == [pytest.approx(4.02, abs=0.01)] * 2 and st["temperature_c"] == 24.0
    assert (st["charging"], st["charged"], st["plugged_in"]) == (False, False, False)
    assert topics(ctx).count(Topics.POWER_BATTERY) <= 3  # 60 s of the same: hardly any events
    assert topics(ctx)[0] == Topics.POWER_MONITOR and events(ctx, Topics.POWER_MONITOR)[0]["connected"] is True
    before = topics(ctx).count(Topics.POWER_BATTERY)
    feed(svc, clock, reading(80, charging=True), 1)  # a change: published at once
    assert topics(ctx).count(Topics.POWER_BATTERY) == before + 1
    assert events(ctx, Topics.POWER_BATTERY)[-1]["charging"] is True
    assert fitted(ctx) is False  # ctx.power isn't this service here
    ctx.power = svc
    assert fitted(ctx) and "battery" in voice_instructions(ctx)
    ctx.db.close()


async def test_charging_charged_unplugged_and_the_face_sign():
    ctx, svc, clock = make()
    feed(svc, clock, reading(60), 3)
    assert Topics.POWER_CHARGING not in topics(ctx) and Topics.POWER_SHOW not in topics(ctx)  # no news at start
    feed(svc, clock, reading(60, charging=True), 3)
    assert topics(ctx).count(Topics.POWER_CHARGING) == 1
    show = events(ctx, Topics.POWER_SHOW)
    assert len(show) == 1 and show[0]["charging"] is True and show[0]["seconds"] == 4.0
    feed(svc, clock, reading(100, charging=False, charged=True), 3)
    assert topics(ctx).count(Topics.POWER_CHARGED) == 1 and len(events(ctx, Topics.POWER_SHOW)) == 1  # still plugged in
    feed(svc, clock, reading(98), 3)
    assert topics(ctx).count(Topics.POWER_UNPLUGGED) == 1
    assert svc.show(2) and events(ctx, Topics.POWER_SHOW)[-1]["seconds"] == 2.0  # when asked
    ctx.db.close()


async def test_waking_from_a_doze_shows_a_low_battery():
    ctx, svc, clock = make()
    await svc.start()  # (no source: only its bus subscriptions)
    feed(svc, clock, reading(60), 2)
    ctx.bus.publish(Topics.FACE_DOZE, {"dozing": False, "reason": "someone walked in"})
    assert Topics.POWER_SHOW not in topics(ctx)  # 60 %: nothing to show
    svc.gauge.reset()
    feed(svc, clock, reading(25), 2)
    ctx.bus.publish(Topics.FACE_DOZE, {"dozing": True, "reason": "quiet"})
    assert Topics.POWER_SHOW not in topics(ctx)
    ctx.bus.publish(Topics.FACE_DOZE, {"dozing": False, "reason": "someone walked in"})
    assert events(ctx, Topics.POWER_SHOW)[-1]["percent"] == 25
    await svc.stop()
    ctx.db.close()


async def test_monitor_lost_and_found_again():
    ctx, svc, clock = make()
    feed(svc, clock, reading(70), 2)
    svc.set_connected(False)
    assert events(ctx, Topics.POWER_MONITOR)[-1]["connected"] is False
    assert "monitor_lost" in svc.state()["health"] and svc.status()["detail"] == "monitor lost"
    assert svc.describe()["percent"] == 70 and "isn't answering" in svc.describe()["note"]
    feed(svc, clock, reading(70), 1)
    assert svc.connected and "monitor_lost" not in svc.state()["health"]
    ctx.db.close()


async def test_health_flags_and_time_left():
    ctx, svc, clock = make()
    feed(svc, clock, reading(70, t=None, cell2=3.70), 2)
    flags = svc.state()["health"]
    assert "cell_imbalance" in flags and "temperature_unknown" in flags
    svc.gauge.reset()
    p = 60.0
    for _ in range(700):  # ~0.5 %/min for 11+ minutes
        svc.process(reading(p))
        clock.t += 1.0
        p -= 0.5 / 60
    st = svc.state()
    assert st["time_left_minutes"] is not None and 90 <= st["time_left_minutes"] <= 130
    assert svc.describe()["about_minutes_left"] == st["time_left_minutes"]
    ctx.db.close()


# ---------------------------------------------------------------- low
async def test_low_says_once_gently_that_it_needs_charging():
    ctx, svc, clock = make()
    feed(svc, clock, reading(40), 3)
    fired = feed(svc, clock, reading(18), 10)
    await svc.idle()
    assert fired == ["low"] and topics(ctx).count(Topics.POWER_LOW) == 1
    assert events(ctx, Topics.POWER_LOW)[0]["percent"] == 18
    assert ctx.audio.spoken == [LOW_LINE]
    assert ("set_expression", ("sleepy",), {"hold": 4.0}) in ctx.face.calls
    assert Topics.POWER_SHOW in topics(ctx)
    feed(svc, clock, reading(17), 120)
    await svc.idle()
    assert ctx.audio.spoken == [LOW_LINE]  # once
    ctx.db.close()


async def test_low_waits_while_asleep_or_busy_and_is_dropped_once_charging():
    ctx, svc, clock = make()
    ctx.state.transition(RobotState.SLEEPING, "night")
    feed(svc, clock, reading(15), 10)
    await svc.idle()
    assert ctx.audio.spoken == [] and topics(ctx).count(Topics.POWER_LOW) == 1  # never out loud at night
    ctx.state.transition(RobotState.IDLE, "morning")
    feed(svc, clock, reading(15), 1)
    await svc.idle()
    assert ctx.audio.spoken == [LOW_LINE]
    # a second robot: busy, then plugged in before it got the chance
    ctx2, svc2, clock2 = make()
    ctx2.state.transition(RobotState.PLAYING_ACTIVITY, "peekaboo", force=True)
    feed(svc2, clock2, reading(15), 10)
    feed(svc2, clock2, reading(15, charging=True), 2)
    ctx2.state.transition(RobotState.IDLE, "done", force=True)
    feed(svc2, clock2, reading(15, charging=True), 2)
    await svc2.idle()
    assert ctx2.audio.spoken == []
    ctx.db.close()
    ctx2.db.close()


async def test_low_in_a_live_voice_chat_is_a_quiet_note():
    ctx, svc, clock = make()
    live = FakeLive(ctx)
    feed(svc, clock, reading(16), 10)
    await svc.idle()
    assert ctx.audio.spoken == []
    assert len(live.notes) == 1 and live.notes[0][1] is False and "16%" in live.notes[0][0]
    assert "sleepy" in live.notes[0][0] and live.active  # the chat goes on
    ctx.db.close()


async def test_low_can_be_kept_quiet_and_its_level_is_a_live_setting():
    ctx, svc, clock = make()
    ctx.settings.set("power.announce_low", False)
    ctx.settings.set("power.low_percent", 30)
    assert ctx.settings.set("power.low_percent", 99) == 50 and ctx.settings.set("power.low_percent", "30") == 30
    with pytest.raises(ValueError):
        ctx.settings.set("power.critical_percent", "lots")
    fired = feed(svc, clock, reading(28), 10)
    await svc.idle()
    assert fired == ["low"] and ctx.audio.spoken == []  # the dashboard banner only
    assert svc.status()["settings"]["low_percent"] == 30
    ctx.db.close()


# ---------------------------------------------------------------- critical: goodbye and a clean shutdown
async def test_critical_ends_the_chat_stops_everything_says_goodbye_and_powers_off_once():
    ctx, svc, clock = make(allow_poweroff=True, goodbye_wait_seconds=2)
    robot = FakeRobot(ctx)
    live = FakeLive(ctx)
    feed(svc, clock, reading(20), 3)
    assert feed(svc, clock, reading(6), 10) == ["low"]  # not yet: 20 s first
    assert robot.poweroffs == []
    fired = feed(svc, clock, reading(6), 15)
    assert fired == ["critical"]
    await svc.idle()
    assert [g for _, g in live.notes] == [False, True] and "goodbye" in live.notes[-1][0]  # low note, then goodbye
    assert not live.active and live.stopped == []  # it hung up by itself after the goodbye
    assert robot.stop_alls == ["power"]
    assert ctx.audio.spoken[-1] == EMPTY_LINE
    assert robot.poweroffs == ["battery empty"] and svc.poweroff_calls == 1
    assert events(ctx, Topics.POWER_CRITICAL)[0]["reason"] == "the battery is at 6%"
    assert events(ctx, Topics.POWER_SHUTDOWN)[0] == {"reason": "battery empty", "poweroff": True}
    # more empty readings, a hot pack, another request: still only once
    feed(svc, clock, reading(5, t=65), 60)
    assert not svc.begin_shutdown("again")
    await svc.idle()
    assert robot.poweroffs == ["battery empty"] and svc.poweroff_calls == 1
    ctx.db.close()


async def test_a_chat_that_does_not_hang_up_is_ended():
    ctx, svc, clock = make(allow_poweroff=True, goodbye_wait_seconds=0.2)
    robot = FakeRobot(ctx)
    live = FakeLive(ctx, ends_on_goodbye=False)
    feed(svc, clock, reading(4, sag=0.0), 25)
    await svc.idle()
    assert live.stopped == ["battery empty"] and robot.poweroffs == ["battery empty"]
    ctx.db.close()


async def test_a_cell_sagging_under_load_for_twenty_seconds_is_critical_but_a_spike_is_not():
    ctx, svc, clock = make(allow_poweroff=True)
    robot = FakeRobot(ctx)
    feed(svc, clock, reading(40), 3)
    spike = reading(40, cell2=3.2)  # one cell dips below 3.3 V while the speaker is loud
    for _ in range(3):
        feed(svc, clock, spike, 8)
        feed(svc, clock, reading(40), 3)
    await svc.idle()
    assert robot.poweroffs == [] and Topics.POWER_CRITICAL not in topics(ctx)
    assert "critical" in feed(svc, clock, reading(40, cell2=3.2), 25)
    await svc.idle()
    assert "below 3.30 V" in events(ctx, Topics.POWER_CRITICAL)[0]["reason"] and robot.poweroffs == ["battery empty"]
    ctx.db.close()


async def test_never_shuts_down_while_charging():
    ctx, svc, clock = make(allow_poweroff=True)
    robot = FakeRobot(ctx)
    feed(svc, clock, reading(3, charging=True), 300)
    feed(svc, clock, reading(3, charging=False, charged=True), 60)
    await svc.idle()
    assert robot.poweroffs == [] and robot.stop_alls == [] and Topics.POWER_CRITICAL not in topics(ctx)
    ctx.db.close()


async def test_plugging_in_during_the_goodbye_cancels_the_shutdown():
    ctx, svc, clock = make(allow_poweroff=True)
    robot = FakeRobot(ctx)
    gate = asyncio.Event()
    said: list[str] = []

    async def slow_say(text, *, rate=None):
        said.append(text)
        await gate.wait()
        return True

    ctx.audio.say = slow_say
    feed(svc, clock, reading(5), 25)
    await asyncio.sleep(0.05)
    assert said[-1] == EMPTY_LINE and "shutting_down" in svc.state()["health"]  # (after the low line)
    feed(svc, clock, reading(5, charging=True), 1)  # the charger goes in while it says goodbye
    gate.set()
    await svc.idle()
    assert robot.poweroffs == [] and svc.poweroff_calls == 0
    assert events(ctx, Topics.POWER_SHUTDOWN)[-1]["cancelled"] is True
    ctx.db.close()


async def test_where_power_off_is_not_allowed_it_sleeps_and_says_so_loudly(caplog):
    ctx, svc, clock = make()  # the desktop simulation: power.allow_poweroff None -> not allowed
    robot = FakeRobot(ctx)
    with caplog.at_level(logging.CRITICAL, logger="littlebot.power"):
        feed(svc, clock, reading(5), 25)
        await svc.idle()
    assert robot.stop_alls == ["power"] and ctx.audio.spoken[-1] == EMPTY_LINE
    assert robot.poweroffs == [] and svc.poweroff_calls == 0
    assert ctx.state.is_(RobotState.SLEEPING)
    assert any("doesn't allow" in r.getMessage() for r in caplog.records)
    assert events(ctx, Topics.POWER_SHUTDOWN)[0]["poweroff"] is False
    ctx.db.close()


async def test_without_the_robot_facade_it_never_powers_off_by_itself(caplog):
    ctx, svc, clock = make(allow_poweroff=True)  # no FakeRobot: nothing to exit cleanly first
    with caplog.at_level(logging.CRITICAL, logger="littlebot.power"):
        feed(svc, clock, reading(5), 25)
        await svc.idle()
    assert svc.poweroff_calls == 1 and any("not powering off" in r.getMessage() for r in caplog.records)
    assert topics(ctx, Topics.STOP_ALL) == [Topics.STOP_ALL]  # it still stopped everything
    ctx.db.close()


async def test_asleep_it_shuts_down_without_a_word():
    ctx, svc, clock = make(allow_poweroff=True)
    robot = FakeRobot(ctx)
    ctx.state.transition(RobotState.SLEEPING, "night")
    feed(svc, clock, reading(5), 25)
    await svc.idle()
    assert ctx.audio.spoken == [] and robot.poweroffs == ["battery empty"]
    ctx.db.close()


async def test_the_parent_can_switch_the_shutdown_off():
    ctx, svc, clock = make(allow_poweroff=True)
    robot = FakeRobot(ctx)
    ctx.settings.set("power.shutdown_when_empty", False)
    feed(svc, clock, reading(5), 25)
    await svc.idle()
    assert robot.poweroffs == [] and robot.stop_alls == [] and ctx.audio.spoken[-1] == NEARLY_EMPTY_LINE
    assert topics(ctx).count(Topics.POWER_CRITICAL) == 1
    ctx.db.close()


# ---------------------------------------------------------------- heat
async def test_hot_warns_and_too_hot_shuts_down_even_while_charging():
    ctx, svc, clock = make(allow_poweroff=True)
    robot = FakeRobot(ctx)
    feed(svc, clock, reading(70, t=52), 15)
    hot = events(ctx, Topics.POWER_HOT)
    assert hot == [{"temperature_c": 52, "level": "warm"}] and robot.poweroffs == []
    feed(svc, clock, reading(70, t=62, charging=True), 15)
    await svc.idle()
    assert events(ctx, Topics.POWER_HOT)[-1]["level"] == "shutdown"
    assert ctx.audio.spoken[-1] == HOT_LINE and robot.poweroffs == ["battery too hot"]
    ctx.db.close()


async def test_charging_below_freezing_is_an_alert_only():
    ctx, svc, clock = make(allow_poweroff=True)
    robot = FakeRobot(ctx)
    feed(svc, clock, reading(50, t=-2), 15)
    assert Topics.POWER_COLD not in topics(ctx)
    feed(svc, clock, reading(50, t=-2, charging=True), 15)
    assert events(ctx, Topics.POWER_COLD) == [{"temperature_c": -2}] and robot.poweroffs == []
    assert "cold" in svc.state()["health"]
    ctx.db.close()


# ---------------------------------------------------------------- the simulated pack through start()
async def test_the_simulated_pack_goes_through_the_inbox_and_can_be_set():
    ctx = make_test_context(power={"monitor": "simulated"})
    svc = PowerService(ctx, source=SimulatedMonitor(percent=55, interval_s=0.02))
    await svc.start()
    deadline = asyncio.get_running_loop().time() + 5
    while svc.percent is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert svc.fitted and svc.status()["kind"] == "simulated" and svc.percent == pytest.approx(55, abs=2)
    assert svc.simulate(percent=12, charging=False)
    assert svc.percent == pytest.approx(12, abs=1)
    assert svc.simulate(temperature_c=None) and svc.state()["temperature_c"] is None
    await svc.stop()
    other = PowerService(ctx, source=SerialMonitor(lister=lambda: []))
    assert other.simulate(percent=50) is False  # only the simulated pack can be set
    ctx.db.close()
