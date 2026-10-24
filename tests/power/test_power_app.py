"""The battery in the whole robot: built like every subsystem, quiet without a monitor, and the clean power-off
(``LittleBot.request_poweroff`` -> ``main`` exits cleanly, then runs ``systemctl poweroff``; faked here)."""

from __future__ import annotations

import time
from typing import Any

from littlebot.app.robot import SUBSYSTEMS, LittleBot
from littlebot.core.context import build_core_context
from littlebot.hardware.system import MockSystemMonitor
from littlebot.power import shutdown as S
from littlebot.power.service import PowerService
from littlebot.testing import make_test_config, make_test_context


def test_power_is_a_subsystem_like_the_others():
    assert ("power", "littlebot.power") in SUBSYSTEMS


def test_the_whole_robot_without_a_battery_monitor_is_unchanged(tmp_path):
    # the real default (look on the serial ports), with nothing there
    cfg = make_test_config(dashboard={"enabled": False},
                           power={"monitor": "auto", "port_globs": [str(tmp_path / "ttyACM*")], "scan_seconds": 1})
    ctx = build_core_context(cfg, db_path=":memory:", monitor=MockSystemMonitor())
    bot = LittleBot(ctx)
    bot.build_subsystems()
    seen: list[str] = []
    ctx.bus.subscribe("power.*", lambda e: seen.append(e.topic))
    bot.start()
    try:
        assert isinstance(ctx.power, PowerService)
        time.sleep(0.4)
        st = bot.status()
        assert st["services"]["power"]["status"] == "running"
        assert ctx.power.status()["detail"] == "not fitted" and not ctx.power.fitted
        assert seen == []  # not one battery event
        assert not bot.poweroff_requested
    finally:
        bot.shutdown("test end")


def test_request_poweroff_exits_cleanly_and_cancels_a_restart():
    ctx = make_test_context(dashboard={"enabled": False})
    bot = LittleBot(ctx)
    bot.build_subsystems(only=set())
    bot.request_shutdown("dashboard restart", restart=True)
    bot.request_poweroff("battery empty")
    assert bot.shutdown_event.is_set() and bot.poweroff_requested and not bot.restart_requested
    ctx.db.close()


def _fake_bot(monkeypatch: Any, *, poweroff: bool) -> None:
    ctx = make_test_context(dashboard={"enabled": False})

    class _Bot:
        def __init__(self) -> None:
            self.ctx = ctx
            self.restart_requested = False
            self.poweroff_requested = poweroff
            self.order: list[str] = []
            import threading

            self.shutdown_event = threading.Event()
            self.shutdown_event.set()

        def start(self) -> None:
            pass

        def shutdown(self, reason: str = "") -> None:
            calls.append("shutdown")

        def request_shutdown(self, *a: Any, **k: Any) -> None:
            pass

    monkeypatch.setattr("littlebot.app.robot.LittleBot.create", classmethod(lambda cls, cfg: _Bot()))


calls: list[str] = []


def test_main_powers_off_only_after_the_clean_exit(monkeypatch, tmp_path):
    from littlebot.app import main as main_mod

    calls.clear()
    monkeypatch.setattr(S, "power_off_system", lambda command=S.DEFAULT_COMMAND, **_: calls.append(
        "poweroff " + " ".join(command)) or True)
    _fake_bot(monkeypatch, poweroff=True)
    assert main_mod.main(["--headless", "--no-dashboard", "--mode", "offline", "--data-dir", str(tmp_path)]) == 0
    assert calls == ["shutdown", "poweroff systemctl --no-ask-password poweroff"]
    calls.clear()
    _fake_bot(monkeypatch, poweroff=False)  # a normal exit: no power-off
    assert main_mod.main(["--headless", "--no-dashboard", "--mode", "offline", "--data-dir", str(tmp_path)]) == 0
    assert calls == ["shutdown"]


def test_power_off_system_runs_the_command_once_and_survives_a_refusal(caplog):
    ran: list[list[str]] = []

    class Done:
        def __init__(self, code: int) -> None:
            self.returncode, self.stderr, self.stdout = code, "Access denied", ""

    assert S.power_off_system(("systemctl", "--no-ask-password", "poweroff"),
                              run=lambda cmd, **_: ran.append(list(cmd)) or Done(0)) is True
    assert ran == [["systemctl", "--no-ask-password", "poweroff"]]
    assert S.power_off_system(("systemctl", "poweroff"), run=lambda cmd, **_: Done(1)) is False

    def missing(cmd: Any, **_: Any) -> Any:
        raise FileNotFoundError("systemctl")

    assert S.power_off_system(("systemctl", "poweroff"), run=missing) is False
    assert any("power" in r.getMessage().lower() for r in caplog.records)


def test_poweroff_is_allowed_by_default_only_on_the_pi():
    assert S.poweroff_allowed(make_test_config(mode="pi")) is True
    assert S.poweroff_allowed(make_test_config()) is False  # the desktop simulation
    assert S.poweroff_allowed(make_test_config(power={"allow_poweroff": True})) is True
    assert S.poweroff_allowed(make_test_config(mode="pi", power={"allow_poweroff": False})) is False
