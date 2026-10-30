"""The Waveshare UPS HAT (E) as the battery monitor (littlebot/power/ups_hat.py), with a fake I2C reader."""

from __future__ import annotations

import pytest
from power_helpers import feed, make

from littlebot.power import ups_hat
from littlebot.power.service import PowerService
from littlebot.testing import make_test_context


def le(*values: int) -> list[int]:
    out: list[int] = []
    for v in values:
        v &= 0xFFFF
        out += [v & 0xFF, v >> 8]
    return out


def regs(*, percent: int = 76, mv: int = 15600, ma: int = -850, status: int = 0x00,
         cells: tuple[int, ...] = (3900, 3905, 3898, 3897)) -> tuple[int, list[int], list[int]]:
    return status, le(mv, ma, percent, 4100, 210, 0), le(*cells)


class FakeHat:
    def __init__(self, *values: object, ident: int = 0x0A) -> None:
        self.values = list(values)
        self.ident_value = ident
        self.reads = 0

    def read(self) -> tuple[int, list[int], list[int]]:
        self.reads += 1
        v = self.values.pop(0) if len(self.values) > 1 else self.values[0]
        if isinstance(v, Exception):
            raise v
        return v  # type: ignore[return-value]

    def ident(self) -> int:
        return self.ident_value


def test_the_registers_become_a_reading():
    r = ups_hat.decode(*regs())
    assert r.voltage == 15.6 and r.percent == 76.0 and r.cells == (3.9, 3.905, 3.898, 3.897)
    assert r.charging is False and r.charged is False and r.temperature_c is None
    charging = ups_hat.decode(*regs(status=0x80 | 0x20 | 0b010, ma=1200))  # constant current, USB-C in
    assert charging.charging is True and charging.charged is False
    full = ups_hat.decode(*regs(status=0x20 | 0b101, percent=100))
    assert full.charged is True and full.charging is False
    resting = ups_hat.decode(*regs(status=0x20, percent=100))  # plugged in, charger done and idle
    assert resting.charged is True
    two = ups_hat.decode(*regs(cells=(4010, 4020, 0, 3)))  # empty slots read about 0: not cells
    assert two.cells == (4.01, 4.02)
    odd = ups_hat.decode(*regs(percent=900))
    assert odd.percent is None


def test_probe_only_says_yes_to_the_hat():
    assert ups_hat.probe(reader=FakeHat(regs()))
    assert not ups_hat.probe(reader=FakeHat(regs(), ident=0x42))

    class Broken:
        def ident(self) -> int:
            raise OSError(121, "Remote I/O error")

    assert not ups_hat.probe(reader=Broken())


def test_the_monitor_delivers_readings_and_says_when_it_answers():
    seen, status = [], []
    t = [0.0]
    mon = ups_hat.UpsHatMonitor(reader=FakeHat(OSError(121, "nothing"), regs(percent=55)), clock=lambda: t[0])
    mon._on_reading, mon._on_status = seen.append, lambda c, p: status.append((c, p))
    assert mon.poll() is None and not mon.status()["connected"] and mon.status()["bad_lines"] == 1
    r = mon.poll()
    assert r is not None and r.percent == 55 and seen == [r]
    assert status == [(True, "i2c-1 0x2d")] and mon.status()["connected"] and mon.status()["lines"] == 1
    mon.poll()
    assert status == [(True, "i2c-1 0x2d")]  # said once


def test_the_service_shows_the_hats_own_percentage():
    ctx, svc, clock = make()
    feed(svc, clock, ups_hat.decode(*regs(percent=42)), 5)
    st = svc.state()
    assert st["percent"] == 42 and len(st["cells"]) == 4 and "cell_imbalance" not in st["health"]
    feed(svc, clock, ups_hat.decode(*regs(percent=42, cells=(3900, 3700, 3900, 3900))), 120)
    assert "cell_imbalance" in svc.state()["health"]
    ctx.db.close()


def test_auto_uses_the_hat_when_it_answers(monkeypatch):
    monkeypatch.setattr(ups_hat, "probe", lambda bus, address: (bus, address) == (1, 0x2D))
    ctx = make_test_context(power={"monitor": "auto"})
    assert PowerService(ctx).source.kind == "ups_hat_e"
    ctx.config.power.i2c_address = 0x30
    assert PowerService(ctx).source.kind == "serial"  # nothing there: the serial ports, as before
    ctx.config.power.monitor = "ups_hat_e"
    assert PowerService(ctx).source.kind == "ups_hat_e"  # asked for: kept trying even when it doesn't answer yet
    ctx.db.close()


def test_never_written_to():
    calls: list[str] = []

    class Bus:
        def __init__(self, *a: object) -> None:
            pass

        def __enter__(self) -> "Bus":
            return self

        def __exit__(self, *a: object) -> None:
            pass

        def read_byte_data(self, addr: int, reg: int) -> int:
            calls.append(f"r{reg:02x}")
            return 0

        def read_i2c_block_data(self, addr: int, reg: int, n: int) -> list[int]:
            calls.append(f"r{reg:02x}")
            return [0] * n

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"UPS HAT: unexpected {name}")

    smbus2 = pytest.importorskip("smbus2")
    import unittest.mock as mock

    with mock.patch.object(smbus2, "SMBus", Bus):
        ups_hat.Bus().read()
        ups_hat.Bus().ident()
    assert calls == ["r02", "r20", "r30", "r00"]
