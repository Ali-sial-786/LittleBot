"""`python -m littlebot.motion off`: the stand-alone servo de-energise command."""

from __future__ import annotations

import sys
import types

from littlebot.motion import __main__ as off_cmd
from littlebot.motion.drivers import MockPCA9685, MockSMBus


def test_switch_off_de_energises_every_channel():
    drv = MockPCA9685()
    for ch in (0, 1, 2):
        drv.set_pulse_us(ch, 1500)
    assert drv.chip.energised_channels() == [0, 1, 2]
    off_cmd.switch_off(drv.bus, 0x40)
    assert drv.chip.energised_channels() == []
    assert drv.chip.sleeping


def test_simulate_mode_exit_code(capsys):
    assert off_cmd.main(["off", "--simulate"]) == 0
    assert "outputs off" in capsys.readouterr().out


def test_real_mode_without_smbus2_is_a_harmless_skip(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "smbus2", None)  # import raises ImportError
    assert off_cmd.main(["off"]) == 0
    assert "skipped" in capsys.readouterr().out


def _fake_smbus(monkeypatch, bus_obj):
    mod = types.ModuleType("smbus2")
    mod.SMBus = lambda n: bus_obj  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "smbus2", mod)


def test_real_mode_switches_a_present_chip_off(monkeypatch, capsys):
    drv = MockPCA9685()
    drv.set_pulse_us(1, 1200)
    _fake_smbus(monkeypatch, drv.bus)
    assert off_cmd.main(["off", "--bus", "1", "--address", "0x40"]) == 0
    assert drv.chip.energised_channels() == []
    assert "switched off" in capsys.readouterr().out


def test_real_mode_with_no_chip_on_the_bus(monkeypatch, capsys):
    _fake_smbus(monkeypatch, MockSMBus({}))  # nothing ACKs at 0x40
    assert off_cmd.main(["off"]) == 0
    assert "nothing to switch off" in capsys.readouterr().out


def test_real_mode_write_failure_is_reported(monkeypatch, capsys):
    drv = MockPCA9685()
    bus = drv.bus

    class FailingWrites:
        def read_byte_data(self, *a, **k):
            return bus.read_byte_data(*a, **k)

        def write_byte_data(self, *a, **k):
            raise OSError("remote I/O error")

        def close(self):
            pass

    _fake_smbus(monkeypatch, FailingWrites())
    assert off_cmd.main(["off"]) == 1
    assert "SERVO SUPPLY OFF" in capsys.readouterr().err


def test_any_emergency_stop_disarms_physical_motion(ctx):
    """Keyboard / panel / driver-fault e-stops must withdraw arming, not just the dashboard one."""
    from littlebot.motion.controller import MotionController

    ctrl = MotionController(ctx)
    ctx.settings.set("motion.physical_armed", True)
    ctrl.emergency_stop("keyboard")
    assert ctx.settings.get("motion.physical_armed") is False
    ctrl.reset_emergency_stop()
    assert ctx.settings.get("motion.physical_armed") is False  # reset does not re-arm


def test_shutdown_stop_is_quiet_and_keeps_setting(ctx, caplog):
    from littlebot.motion.controller import MotionController

    ctrl = MotionController(ctx)
    with caplog.at_level("WARNING"):
        ctrl.emergency_stop("shutdown")
    assert not [r for r in caplog.records if "EMERGENCY" in r.getMessage()]
