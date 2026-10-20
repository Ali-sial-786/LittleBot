"""PCA9685 register-level emulation and the shared driver logic (mock + real driver on a mock bus)."""

from __future__ import annotations

import sys

import pytest

from littlebot.motion import drivers as D
from littlebot.motion.drivers import (
    DriverError, MockPCA9685, MockSMBus, PCA9685Chip, PCA9685Driver, ServoDriver, frequency_for, prescale_for,
    pulse_to_counts,
)


def datasheet_counts(pulse_us: float, freq_hz: float = 50.0) -> int:
    """PCA9685 datasheet: OFF count = pulse width / period * 4096 (ON = 0)."""
    return round(pulse_us / (1_000_000 / freq_hz) * 4096)


# ----------------------------------------------------------------- prescale / counts
def test_prescale_matches_datasheet_equation():
    # prescale = round(25 MHz / (4096 * f)) - 1
    assert prescale_for(50) == 121
    assert prescale_for(60) == 101
    assert prescale_for(200) == 0x1E  # datasheet example: 200 Hz -> 30 (also the power-on default)
    assert prescale_for(1526) == 3  # hardware minimum
    assert prescale_for(10) == 255  # clamped to 8 bits
    assert frequency_for(121) == pytest.approx(50.03, abs=0.01)
    with pytest.raises(ValueError):
        prescale_for(0)


@pytest.mark.parametrize("pulse,expected", [(500, 102), (1000, 205), (1500, 307), (2000, 410), (2500, 512)])
def test_pulse_counts_match_datasheet_formula_at_50hz(pulse, expected):
    assert datasheet_counts(pulse) == expected
    assert pulse_to_counts(pulse, prescale_for(50)) == expected  # actual 50.03 Hz period, same counts


# ----------------------------------------------------------------- emulated chip
def test_chip_power_on_defaults():
    chip = PCA9685Chip()
    assert chip.sleeping
    assert chip.prescale == 0x1E
    assert chip.read(D.MODE1) == D.MODE1_SLEEP | D.MODE1_ALLCALL
    assert chip.read(D.MODE2) == D.MODE2_OUTDRV
    assert chip.energised_channels() == []
    assert all(chip.channel_registers(ch)["full_off"] for ch in range(16))


def test_chip_blocks_prescale_writes_while_awake_and_forces_minimum():
    chip = PCA9685Chip()
    chip.write(D.MODE1, D.MODE1_AI)  # awake
    chip.write(D.PRE_SCALE, 121)
    assert chip.prescale == 0x1E and (D.PRE_SCALE, 121) in chip.blocked_writes
    chip.write(D.MODE1, D.MODE1_SLEEP)
    chip.write(D.PRE_SCALE, 1)
    assert chip.prescale == 3


def test_chip_block_write_without_auto_increment_hits_one_register():
    chip = PCA9685Chip()
    bus = MockSMBus({0x40: chip})
    chip.write(D.MODE1, 0x00)  # awake, AI off
    bus.write_i2c_block_data(0x40, D.LED0_ON_L, [1, 2, 3, 4])
    assert chip.regs[D.LED0_ON_L] == 4 and chip.regs[D.LED0_ON_L + 1] == 0


def test_chip_all_led_registers_broadcast_and_read_back_zero():
    chip = PCA9685Chip()
    chip.write(D.ALL_LED_OFF_L, 0x33)
    chip.write(D.ALL_LED_OFF_H, 0x01)
    assert chip.read(D.ALL_LED_OFF_L) == 0
    assert all(chip.channel_registers(ch)["off"] == 0x133 for ch in range(16))
    assert not any(chip.channel_registers(ch)["full_off"] for ch in range(16))
    chip.write(D.ALL_LED_ON_H, 0xFF)  # reserved bits masked
    assert chip.regs[D.LED0_ON_L + 1] == 0x1F


def test_full_off_wins_over_full_on():
    chip = PCA9685Chip()
    chip.write(D.MODE1, D.MODE1_AI)
    chip.write(D.LED0_ON_L + 1, D.FULL_BIT)
    chip.write(D.LED0_ON_L + 3, D.FULL_BIT)
    assert chip.output_pulse_us(0) is None
    chip.write(D.LED0_ON_L + 3, 0)
    assert chip.output_pulse_us(0) == pytest.approx(1e6 / chip.frequency_hz)


# ----------------------------------------------------------------- mock driver (shared logic)
def test_mock_driver_init_sequence_is_datasheet_order():
    drv = MockPCA9685(frequency_hz=50)
    assert isinstance(drv, ServoDriver)
    chip = drv.chip
    assert not chip.sleeping
    assert chip.read(D.MODE1) & D.MODE1_AI
    assert chip.read(D.MODE2) == D.MODE2_OUTDRV
    assert chip.prescale == 121
    assert chip.blocked_writes == []
    assert chip.energised_channels() == []
    regs = [r for (_t, r, _v) in chip.history]
    # outputs forced off first, PRE_SCALE written between SLEEP and wake
    assert regs.index(D.ALL_LED_OFF_H) < regs.index(D.PRE_SCALE)
    mode1_writes = [v for (_t, r, v) in chip.history if r == D.MODE1]
    assert mode1_writes[0] & D.MODE1_SLEEP and not mode1_writes[-1] & D.MODE1_SLEEP
    assert drv.frequency_hz == pytest.approx(50.03, abs=0.01)


@pytest.mark.parametrize("pulse", [500, 1000, 1500, 2000, 2500])
def test_set_pulse_writes_led_registers_like_the_chip(pulse):
    drv = MockPCA9685(frequency_hz=50)
    drv.set_pulse_us(3, pulse)
    base = D.LED0_ON_L + 4 * 3
    on_l, on_h, off_l, off_h = drv.chip.regs[base:base + 4]
    counts = datasheet_counts(pulse)
    assert (on_l, on_h) == (0, 0)
    assert off_l == counts & 0xFF and off_h == counts >> 8  # full-off bit cleared
    assert drv.pulse_us(3) == pytest.approx(pulse, abs=1e6 / 50 / 4096)  # within one count (~4.9 us)
    assert drv.chip.energised_channels() == [3]


def test_off_and_all_off():
    drv = MockPCA9685()
    for ch in (0, 1, 2):
        drv.set_pulse_us(ch, 1500)
    drv.off(1)
    assert drv.pulse_us(1) is None and drv.chip.channel_registers(1)["full_off"]
    before = drv.bus.transactions
    drv.all_off()
    assert drv.bus.transactions - before == 1  # one I2C transaction de-energises everything
    assert drv.chip.energised_channels() == []
    assert drv.all_off_calls == 1
    assert drv.history[-1][1:] == (None, None)


def test_driver_hard_clamps_pulses_and_validates_input():
    drv = MockPCA9685()
    drv.set_pulse_us(0, 3000)
    assert drv.pulse_us(0) == pytest.approx(2500, abs=5)
    drv.set_pulse_us(0, 100)
    assert drv.pulse_us(0) == pytest.approx(500, abs=5)
    with pytest.raises(ValueError):
        drv.set_pulse_us(0, float("nan"))
    with pytest.raises(ValueError):
        drv.set_pulse_us(16, 1500)


# ----------------------------------------------------------------- real driver on a mock bus
def test_real_driver_logic_on_mock_bus():
    chip = PCA9685Chip()
    bus = MockSMBus({0x40: chip})
    drv = PCA9685Driver(bus=bus, address=0x40, frequency_hz=50)
    assert drv.blocking_io is True
    drv.set_pulse_us(0, 1500)
    assert chip.output_pulse_us(0) == pytest.approx(1500, abs=5)
    drv.close()
    assert chip.energised_channels() == [] and chip.sleeping and bus.closed
    with pytest.raises(DriverError):
        drv.set_pulse_us(0, 1500)
    drv.close()  # idempotent


def test_real_driver_refuses_missing_or_wrong_device():
    with pytest.raises(DriverError, match="no I2C device"):
        PCA9685Driver(bus=MockSMBus({}), address=0x40)
    bus = MockSMBus({0x40: PCA9685Chip()})
    bus.fail_always = True
    with pytest.raises(DriverError):
        PCA9685Driver(bus=bus, address=0x40)

    class NotAPca(PCA9685Chip):
        def write(self, reg: int, value: int) -> None:  # ignores everything (e.g. an EEPROM)
            self.history.append((0.0, reg, value))

    with pytest.raises(DriverError, match="does not behave like a PCA9685"):
        PCA9685Driver(bus=MockSMBus({0x40: NotAPca()}), address=0x40)


def test_real_driver_without_smbus2_raises_driver_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "smbus2", None)  # import -> ImportError
    with pytest.raises(DriverError, match="smbus2"):
        PCA9685Driver(bus_number=1)


def test_probe_reinitialises_after_power_cycle():
    chip = PCA9685Chip()
    drv = PCA9685Driver(bus=MockSMBus({0x40: chip}))
    chip.power_on_reset()  # V_CC brown-out: back to 200 Hz, sleeping
    drv.probe()
    assert chip.prescale == 121 and not chip.sleeping
    drv._bus.fail_always = True  # noqa: SLF001
    with pytest.raises(DriverError):
        drv.probe()


def test_bus_fault_injection_raises_oserror():
    drv = MockPCA9685()
    drv.bus.fail_next(1)
    with pytest.raises(OSError):
        drv.set_pulse_us(0, 1500)
    drv.set_pulse_us(0, 1500)  # next transaction works again
    assert drv.pulse_us(0) is not None
