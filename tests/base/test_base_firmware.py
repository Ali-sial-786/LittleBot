"""The v5 base controller's MicroPython firmware, imported in CPython with a fake board (no ``machine``)."""

from __future__ import annotations

import importlib.util
import json
import zlib
from pathlib import Path

import pytest

FIRMWARE = Path(__file__).resolve().parents[2] / "firmware" / "base_controller" / "main.py"


@pytest.fixture(scope="module")
def fw():
    spec = importlib.util.spec_from_file_location("base_controller_main", FIRMWARE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # must not need the machine module, nor start run()
    return mod


class FakeBoard:
    def __init__(self, vbat=8.0):
        self.duty = [0.0, 0.0]
        self.servo = [None, None, None]
        self.cnt = [0, 0]
        self.v = vbat
        self.files = {}
        self.swapped = False

    def set_motor(self, i, duty):
        self.duty[i] = duty

    def set_servo(self, i, us):
        self.servo[i] = us

    def counts(self):
        return list(self.cnt)

    def vbat(self):
        return self.v

    def write_file(self, name, data):
        self.files[name] = data

    def swap_main(self):
        self.swapped = True


@pytest.fixture
def base(fw):
    return fw.Base(FakeBoard())


def run_for(base, ms, step=10):
    out = []
    for _ in range(ms // step):
        out += base.step(step)
    return out


def test_ping_and_unknown(base):
    assert base.handle("ping") == ["PONG littlebot-base 1"]
    assert base.handle("FLY")[0].startswith("ERR unknown")
    assert base.handle("") == []
    assert base.handle("D 10")[0].startswith("ERR D")


def test_wheels_ramp_up_and_never_pass_the_top_speed(base):
    base.handle("D 100 100")
    run_for(base, 300)
    assert 0 < base.v[0] < 350          # still ramping after 0.3 s (500 mm/s per s)
    for _ in range(10):                 # keep the orders coming
        base.handle("D 100 100")
        run_for(base, 200)
    assert base.v == [350, 350]
    assert 0 < base.hw.duty[0] < 1
    base.handle("D 500 -500")           # out of range: clamped
    assert base.target == [350, -350]


def test_wheels_stop_when_the_orders_stop(base):
    base.handle("D 50 50")
    run_for(base, 400)
    assert base.v[0] > 0
    run_for(base, 1500)                 # no more D: after 0.5 s the target is 0, then the ramp down
    assert base.v == [0, 0] and base.hw.duty == [0, 0]


def test_stop_ramps_down_not_dead(base):
    for _ in range(8):
        base.handle("D 100 100")
        run_for(base, 200)
    base.handle("STOP")
    base.step(10)
    assert base.v[0] == pytest.approx(345)   # one step of the ramp: it slows, it doesn't slam
    run_for(base, 1000)
    assert base.v == [0, 0]


def test_low_battery_stops_the_wheels_and_says_so(fw):
    hw = FakeBoard(vbat=5.5)
    b = fw.Base(hw)
    b.handle("D 50 50")
    out = run_for(b, 100)
    assert b.target == [0, 0] and any(o.startswith("LOWBAT 5.50") for o in out)


def test_motor_inversion(fw):
    hw = FakeBoard()
    b = fw.Base(hw, {"minv_l": 1})
    b.handle("D 50 50")
    run_for(b, 200)
    assert hw.duty[0] < 0 < hw.duty[1]


def test_open_loop_duty_follows_the_pack(fw):
    cfg = dict(fw.DEFAULTS)
    assert fw.open_loop_duty(cfg, 0, 8.0) == 0
    full, flat = fw.open_loop_duty(cfg, 350, 9.0), fw.open_loop_duty(cfg, 350, 6.5)
    assert 0 < full < flat < 1            # a weaker pack needs more duty for the same speed
    assert fw.open_loop_duty(cfg, -350, 9.0) == -full
    assert fw.open_loop_duty(cfg, 350, None) == full   # unknown pack: assume the fastest (9 V)


def test_speed_trim_with_encoders(fw):
    hw = FakeBoard()
    b = fw.Base(hw, {"cpr": 300})
    for _ in range(40):
        b.handle("D 50 50")
        run_for(b, 100)                   # the encoders never move: the trim adds duty, up to trim_max
    assert b.trim[0] == pytest.approx(0.25)
    plain = fw.open_loop_duty(b.cfg, b.v[0], 8.0)
    assert hw.duty[0] == pytest.approx(plain + 0.25)


def test_servos_limp_until_told_then_slew(base):
    run_for(base, 100)
    assert base.hw.servo == [None, None, None]
    assert base.handle("CENTER") == ["OK"]
    base.step(10)
    assert base.hw.servo[0] == pytest.approx(1500)       # the first move is a jump to the target
    base.handle("S 1 180 100")                           # 100 deg/s
    run_for(base, 500)
    assert base.servo_pos[1] == pytest.approx(140)
    run_for(base, 500)
    assert base.hw.servo[1] == pytest.approx(2500)
    base.handle("OFF 1")
    assert base.hw.servo[1] is None and base.hw.servo[0] is not None
    assert base.handle("S 3 90")[0].startswith("ERR")


def test_servo_limits(fw):
    b = fw.Base(FakeBoard(), {"s0_min": 30, "s0_max": 150})
    b.handle("S 0 0")
    b.step(10)
    assert b.servo_pos[0] == 30


def test_battery_and_encoders(fw):
    assert fw.pack_volts(0) is None                        # nothing on the pin
    assert fw.pack_volts(int(2.656 / 3.3 * 65535)) == pytest.approx(8.5, abs=0.01)
    hw = FakeBoard(vbat=7.9)
    b = fw.Base(hw, {"einv_l": 1})
    hw.cnt = [120, 80]
    b.step(10)
    assert b.handle("B") == ["B 7.90"]
    assert b.handle("E") == ["E -120 80"]
    b.hw.v = None
    b.step(10)
    assert b.handle("B") == ["B null"]


def test_a_floating_sense_pin_is_null(fw):
    full = 65535
    assert fw.floating(0, full)                                        # follows the pulls rail to rail: nothing there
    assert not fw.floating(int(0.40 * full), int(0.60 * full))        # the divider holds it: it moves under 1 V
    assert not fw.floating(full, full)                                 # stuck high (a short to 3.3 V) is a reading, not "nothing"


def test_telemetry(base):
    base.handle("T 10")
    out = run_for(base, 1000)
    assert len([o for o in out if o.startswith("T ")]) == 10


def test_settings(fw, base):
    assert base.handle("SET cpr 374") == ["OK cpr 374"]
    assert base.handle("SET nope 1")[0].startswith("ERR")
    assert json.loads(base.handle("GET")[0][4:])["cpr"] == 374
    assert base.handle("SAVE") == ["OK saved"]
    assert json.loads(base.hw.files["cfg.json"])["cpr"] == 374
    assert fw.Base(FakeBoard(), {"cpr": 10, "junk": 1}).cfg["cpr"] == 10


def test_crc_matches_zlib(fw):
    for data in (b"", b"hello", bytes(range(256)) * 3):
        assert fw.crc32(data) == zlib.crc32(data)


def test_put_a_new_main(fw):
    hw = FakeBoard()
    b = fw.Base(hw)
    lines = fw.Lines(b)
    new = b"print('hi')\n"
    head = b"PUT %d %d\n" % (len(new), zlib.crc32(new))
    out = lines.feed(head + new[:5]) + lines.feed(new[5:] + b"PING\n")
    assert [t for _, t in out] == ["READY 12", "OK put 12 (RESET to run it)", "PONG littlebot-base 1"]
    assert hw.files["main_new.py"] == new and hw.swapped


def test_put_refuses_bad_data(fw):
    b = fw.Base(FakeBoard())
    lines = fw.Lines(b)
    bad = b"def (:\n"
    out = lines.feed(b"PUT %d %d\n" % (len(bad), zlib.crc32(bad)) + bad)
    assert out[-1][1].startswith("ERR put:") and not b.hw.swapped
    out = lines.feed(b"PUT 3 1\nabc")
    assert out[-1][1] == "ERR put: crc"


def test_lines_and_reset(fw):
    lines = fw.Lines(fw.Base(FakeBoard()))
    assert lines.feed(b"PI") == []
    assert lines.feed(b"NG\r\nreset\n") == [("reply", "PONG littlebot-base 1"), ("reset", None)]
