"""Servo output drivers.

Three layers, all speaking the PCA9685 register map (NXP PCA9685 datasheet, rev. 4):

* ``PCA9685Chip``  - an *emulated* chip: register file, MODE1 sleep/auto-increment rules,
  PRE_SCALE write-protection, LEDn ON/OFF 12-bit counters, ALL_LED broadcast registers,
  full-ON/full-OFF bits, and the resulting output pulse per channel.
* ``MockSMBus``    - an smbus2-compatible bus object in front of emulated chips, with
  fault injection (``OSError`` like a real I2C NACK) for tests.
* ``_PCA9685Base`` - the register-programming logic (init sequence, pulse -> counts,
  channel/all off). ``MockPCA9685`` runs it against an emulated chip;
  ``PCA9685Driver`` runs the very same code against ``smbus2.SMBus`` on the Pi.

So the simulator and the test-suite exercise the *real* driver's register writes.
Nothing here decides whether hardware may move; that is MotionController's job
(physical_enabled + calibration + parent arming - see docs/MOTION_SAFETY.md).

``smbus2`` is imported lazily, only inside ``PCA9685Driver`` when no bus is injected.
"""

from __future__ import annotations

import collections
import logging
import math
import threading
import time
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("littlebot.motion.drivers")

# --------------------------------------------------------------------------- registers
MODE1 = 0x00
MODE2 = 0x01
SUBADR1 = 0x02
SUBADR2 = 0x03
SUBADR3 = 0x04
ALLCALLADR = 0x05
LED0_ON_L = 0x06  # LEDn_ON_L = 0x06 + 4n, then ON_H, OFF_L, OFF_H
ALL_LED_ON_L = 0xFA
ALL_LED_ON_H = 0xFB
ALL_LED_OFF_L = 0xFC
ALL_LED_OFF_H = 0xFD
PRE_SCALE = 0xFE
TEST_MODE = 0xFF

# MODE1 bits
MODE1_RESTART = 0x80
MODE1_EXTCLK = 0x40
MODE1_AI = 0x20  # register auto-increment
MODE1_SLEEP = 0x10  # low-power mode, oscillator off
MODE1_SUB1 = 0x08
MODE1_SUB2 = 0x04
MODE1_SUB3 = 0x02
MODE1_ALLCALL = 0x01

# MODE2 bits
MODE2_INVRT = 0x10
MODE2_OCH = 0x08
MODE2_OUTDRV = 0x04  # totem-pole outputs (what servo boards use)

FULL_BIT = 0x10  # bit 4 of LEDn_ON_H (full on) / LEDn_OFF_H (full off)

NUM_CHANNELS = 16
OSC_HZ = 25_000_000  # internal oscillator
PRESCALE_MIN = 3  # hardware minimum (datasheet 7.3.5)
PRESCALE_MAX = 255
COUNTS = 4096  # 12-bit PWM counter

# Absolute pulse bounds accepted by any LittleBot driver (defence in depth: the
# joint layer clamps tighter, per servo).
ABS_MIN_PULSE_US = 500.0
ABS_MAX_PULSE_US = 2500.0

I2C_REMOTE_IO_ERROR = 121  # errno EREMOTEIO: what smbus2 raises when nothing ACKs


class DriverError(RuntimeError):
    """The servo controller could not be opened, verified or written."""


@runtime_checkable
class ServoDriver(Protocol):
    """What MotionController needs from an output stage.

    Optional attributes: ``blocking_io`` (bool, True when calls do real bus I/O and
    should run off the event loop) and ``probe()`` (re-verify the device).
    """

    def set_pulse_us(self, channel: int, pulse_us: float) -> None: ...

    def off(self, channel: int) -> None:
        """De-energise one channel (no pulses: the servo goes limp)."""

    def all_off(self) -> None:
        """De-energise every channel with as few bus transactions as possible."""

    def close(self) -> None: ...

    def describe(self) -> dict[str, Any]: ...


def prescale_for(frequency_hz: float, oscillator_hz: float = OSC_HZ) -> int:
    """Datasheet equation 1: prescale = round(osc / (4096 * update_rate)) - 1."""
    if not frequency_hz or frequency_hz <= 0:
        raise ValueError("PWM frequency must be positive")
    value = int(round(oscillator_hz / (COUNTS * float(frequency_hz)))) - 1
    return max(PRESCALE_MIN, min(PRESCALE_MAX, value))


def frequency_for(prescale: int, oscillator_hz: float = OSC_HZ) -> float:
    """Actual output frequency produced by a PRE_SCALE value."""
    return oscillator_hz / (COUNTS * (int(prescale) + 1))


def pulse_to_counts(pulse_us: float, prescale: int, oscillator_hz: float = OSC_HZ) -> int:
    """OFF count for a pulse that starts at count 0: pulse / period * 4096 (rounded, 0..4095)."""
    period_us = 1_000_000.0 / frequency_for(prescale, oscillator_hz)
    counts = int(round(float(pulse_us) * COUNTS / period_us))
    return max(0, min(COUNTS - 1, counts))


def counts_to_pulse(on: int, off: int, prescale: int, oscillator_hz: float = OSC_HZ) -> float:
    period_us = 1_000_000.0 / frequency_for(prescale, oscillator_hz)
    high = (int(off) - int(on)) % COUNTS
    return high * period_us / COUNTS


# --------------------------------------------------------------------------- emulated chip
class PCA9685Chip:
    """Register-level emulation of one PCA9685 (enough for servo use, deliberately strict).

    Emulated behaviour:
    * power-on defaults: MODE1=0x11 (SLEEP|ALLCALL), MODE2=0x04, PRE_SCALE=0x1E, every
      LEDn_OFF_H=0x10 (all outputs full-off).
    * PRE_SCALE writes are ignored unless MODE1.SLEEP is set; values < 3 are forced to 3.
    * block writes auto-increment only when MODE1.AI is set (otherwise every byte lands
      in the same register - a classic driver bug the emulation exposes).
    * ALL_LED_* writes are copied to every channel's register; they read back as 0.
    * LEDn_ON_H / OFF_H keep only bits 4..0; reserved/unknown registers ignore writes.
    * no output pulses while SLEEP is set or a channel's full-OFF bit is set
      (full-OFF wins over full-ON).
    """

    def __init__(self, *, oscillator_hz: float = OSC_HZ, history: int = 20_000) -> None:
        self.oscillator_hz = oscillator_hz
        self.regs = bytearray(256)
        self.history: collections.deque[tuple[float, int, int]] = collections.deque(maxlen=history)
        self.blocked_writes: list[tuple[int, int]] = []  # writes the chip ignored (reg, value)
        self.power_on_reset()

    def power_on_reset(self) -> None:
        self.regs[:] = bytes(256)
        self.regs[MODE1] = MODE1_SLEEP | MODE1_ALLCALL
        self.regs[MODE2] = MODE2_OUTDRV
        self.regs[SUBADR1], self.regs[SUBADR2], self.regs[SUBADR3] = 0xE2, 0xE4, 0xE8
        self.regs[ALLCALLADR] = 0xE0
        self.regs[PRE_SCALE] = 0x1E
        for ch in range(NUM_CHANNELS):
            self.regs[LED0_ON_L + 4 * ch + 3] = FULL_BIT

    # -- bus-level access -------------------------------------------------
    def read(self, reg: int) -> int:
        reg &= 0xFF
        if ALL_LED_ON_L <= reg <= ALL_LED_OFF_H:
            return 0
        return self.regs[reg]

    def write(self, reg: int, value: int) -> None:
        reg &= 0xFF
        value &= 0xFF
        self.history.append((time.monotonic(), reg, value))
        if reg == MODE1:
            # RESTART is "write 1 to clear"; this emulation never needs to hold it.
            self.regs[MODE1] = value & ~MODE1_RESTART & 0xFF
        elif reg == MODE2:
            self.regs[MODE2] = value & 0x1F
        elif reg == PRE_SCALE:
            if not self.regs[MODE1] & MODE1_SLEEP:
                self.blocked_writes.append((reg, value))
                return
            self.regs[PRE_SCALE] = max(PRESCALE_MIN, value)
        elif LED0_ON_L <= reg < LED0_ON_L + 4 * NUM_CHANNELS:
            self.regs[reg] = value & 0x1F if (reg - LED0_ON_L) % 2 == 1 else value
        elif ALL_LED_ON_L <= reg <= ALL_LED_OFF_H:
            offset = reg - ALL_LED_ON_L
            stored = value & 0x1F if offset % 2 == 1 else value
            for ch in range(NUM_CHANNELS):
                self.regs[LED0_ON_L + 4 * ch + offset] = stored
        elif reg in (SUBADR1, SUBADR2, SUBADR3, ALLCALLADR):
            self.regs[reg] = value
        else:
            self.blocked_writes.append((reg, value))

    def auto_increment_target(self, reg: int, index: int) -> int:
        if not self.regs[MODE1] & MODE1_AI:
            return reg
        nxt = reg + index
        # Auto-increment rolls over from 0xFD (ALL_LED_OFF_H) to 0x00 per datasheet; good enough.
        return nxt if nxt <= 0xFF else nxt - 0x100

    # -- decoded state ----------------------------------------------------
    @property
    def sleeping(self) -> bool:
        return bool(self.regs[MODE1] & MODE1_SLEEP)

    @property
    def prescale(self) -> int:
        return self.regs[PRE_SCALE]

    @property
    def frequency_hz(self) -> float:
        return frequency_for(self.prescale, self.oscillator_hz)

    def channel_registers(self, channel: int) -> dict[str, int | bool]:
        base = LED0_ON_L + 4 * channel
        on_l, on_h, off_l, off_h = self.regs[base:base + 4]
        return {
            "on": ((on_h & 0x0F) << 8) | on_l,
            "off": ((off_h & 0x0F) << 8) | off_l,
            "full_on": bool(on_h & FULL_BIT),
            "full_off": bool(off_h & FULL_BIT),
        }

    def output_pulse_us(self, channel: int) -> float | None:
        """High time per period in microseconds, or None when the output is not pulsing."""
        r = self.channel_registers(channel)
        if self.sleeping or r["full_off"]:
            return None
        period = 1_000_000.0 / self.frequency_hz
        if r["full_on"]:
            return period  # constant high: never a valid servo signal
        return counts_to_pulse(int(r["on"]), int(r["off"]), self.prescale, self.oscillator_hz)

    def energised_channels(self) -> list[int]:
        return [ch for ch in range(NUM_CHANNELS) if self.output_pulse_us(ch) is not None]


class MockSMBus:
    """Minimal smbus2.SMBus look-alike talking to emulated chips.

    ``fail_next(n)`` makes the next n transactions raise ``OSError(121)``;
    ``fail_always = True`` simulates a disconnected / unpowered board.
    """

    def __init__(self, devices: dict[int, PCA9685Chip] | None = None) -> None:
        self.devices: dict[int, PCA9685Chip] = dict(devices or {})
        self.transactions = 0
        self.fail_always = False
        self._fail_count = 0
        self.closed = False

    def fail_next(self, n: int = 1) -> None:
        self._fail_count = max(0, int(n))

    def _chip(self, addr: int) -> PCA9685Chip:
        if self.closed:
            raise OSError(9, "Bad file descriptor (bus closed)")
        self.transactions += 1
        if self.fail_always or self._fail_count > 0:
            if self._fail_count > 0:
                self._fail_count -= 1
            raise OSError(I2C_REMOTE_IO_ERROR, "Remote I/O error")
        chip = self.devices.get(addr)
        if chip is None:
            raise OSError(I2C_REMOTE_IO_ERROR, "Remote I/O error")
        return chip

    def read_byte_data(self, i2c_addr: int, register: int, force: bool | None = None) -> int:
        return self._chip(i2c_addr).read(register)

    def write_byte_data(self, i2c_addr: int, register: int, value: int, force: bool | None = None) -> None:
        self._chip(i2c_addr).write(register, value)

    def write_i2c_block_data(self, i2c_addr: int, register: int, data: list[int], force: bool | None = None) -> None:
        chip = self._chip(i2c_addr)
        for i, b in enumerate(data):
            chip.write(chip.auto_increment_target(register, i), b)

    def read_i2c_block_data(self, i2c_addr: int, register: int, length: int, force: bool | None = None) -> list[int]:
        chip = self._chip(i2c_addr)
        return [chip.read(chip.auto_increment_target(register, i)) for i in range(length)]

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- driver logic
class _PCA9685Base:
    """PCA9685 register programming shared by the mock and the real driver."""

    name = "pca9685"
    blocking_io = False

    def __init__(self, bus: Any, address: int, frequency_hz: float, *, oscillator_hz: float = OSC_HZ,
                 bus_number: int | None = None) -> None:
        self._bus = bus
        self.address = int(address)
        self.bus_number = bus_number
        self.oscillator_hz = oscillator_hz
        self.requested_frequency_hz = float(frequency_hz)
        self.prescale = prescale_for(frequency_hz, oscillator_hz)
        self._lock = threading.RLock()
        self._closed = False
        self.initialised = False
        self.writes = 0

    # -- raw register access ------------------------------------------------
    def _w(self, reg: int, value: int) -> None:
        self._bus.write_byte_data(self.address, reg, value & 0xFF)
        self.writes += 1

    def _r(self, reg: int) -> int:
        return int(self._bus.read_byte_data(self.address, reg))

    def _wblock(self, reg: int, data: list[int]) -> None:
        self._bus.write_i2c_block_data(self.address, reg, [b & 0xFF for b in data])
        self.writes += 1

    # -- lifecycle ------------------------------------------------------------
    def initialise(self) -> None:
        """Datasheet init: outputs off -> SLEEP -> PRE_SCALE -> wake with auto-increment.

        Raises DriverError if the device does not answer or does not behave like a PCA9685.
        """
        with self._lock:
            try:
                self._r(MODE1)  # does anything ACK at this address?
            except OSError as e:
                raise DriverError(f"no I2C device answering at 0x{self.address:02x}: {e}") from e
            try:
                self._w(ALL_LED_OFF_H, FULL_BIT)  # every output full-off before anything else
                self._w(ALL_LED_ON_H, 0)
                self._w(MODE2, MODE2_OUTDRV)  # totem pole, non-inverted
                self._w(MODE1, MODE1_ALLCALL | MODE1_SLEEP)  # oscillator off: PRE_SCALE is writable
                self._w(PRE_SCALE, self.prescale)
                self._w(MODE1, MODE1_ALLCALL | MODE1_AI)  # wake, auto-increment on
                time.sleep(0.001)  # oscillator needs >= 500 us after leaving SLEEP
                mode1 = self._r(MODE1)
                prescale = self._r(PRE_SCALE)
            except OSError as e:
                raise DriverError(f"I2C error while initialising PCA9685 at 0x{self.address:02x}: {e}") from e
            if prescale != self.prescale or mode1 & MODE1_SLEEP or not mode1 & MODE1_AI:
                raise DriverError(
                    f"device at 0x{self.address:02x} does not behave like a PCA9685 "
                    f"(MODE1=0x{mode1:02x}, PRE_SCALE={prescale}, expected {self.prescale})")
            self.initialised = True

    def probe(self) -> None:
        """Re-verify the chip (used before clearing a driver-error emergency stop)."""
        with self._lock:
            try:
                mode1 = self._r(MODE1)
                prescale = self._r(PRE_SCALE)
            except OSError as e:
                raise DriverError(f"PCA9685 at 0x{self.address:02x} not responding: {e}") from e
            if prescale != self.prescale or mode1 & MODE1_SLEEP:
                # Chip was power-cycled (V_CC brown-out) - program it again from scratch.
                self.initialise()

    @property
    def frequency_hz(self) -> float:
        return frequency_for(self.prescale, self.oscillator_hz)

    def counts_for(self, pulse_us: float) -> int:
        return pulse_to_counts(pulse_us, self.prescale, self.oscillator_hz)

    # -- outputs -------------------------------------------------------------
    @staticmethod
    def _check_channel(channel: int) -> int:
        ch = int(channel)
        if not 0 <= ch < NUM_CHANNELS:
            raise ValueError(f"PCA9685 channel must be 0..15, got {channel}")
        return ch

    def set_pulse_us(self, channel: int, pulse_us: float) -> None:
        ch = self._check_channel(channel)
        us = float(pulse_us)
        if not math.isfinite(us):
            raise ValueError("pulse width must be a finite number")
        if us < ABS_MIN_PULSE_US or us > ABS_MAX_PULSE_US:
            log.warning("pulse %.0f us on channel %d clamped to %.0f..%.0f", us, ch, ABS_MIN_PULSE_US, ABS_MAX_PULSE_US)
            us = min(ABS_MAX_PULSE_US, max(ABS_MIN_PULSE_US, us))
        off = self.counts_for(us)
        with self._lock:
            self._ensure_open()
            # ON at count 0, OFF after `off` counts; bit 4 of both _H registers cleared.
            self._wblock(LED0_ON_L + 4 * ch, [0x00, 0x00, off & 0xFF, (off >> 8) & 0x0F])
        self._record(ch, us)

    def off(self, channel: int) -> None:
        ch = self._check_channel(channel)
        with self._lock:
            self._ensure_open()
            self._wblock(LED0_ON_L + 4 * ch, [0x00, 0x00, 0x00, FULL_BIT])
        self._record(ch, None)

    def all_off(self) -> None:
        # One single-byte transaction: full-OFF bit into every LEDn_OFF_H (wins over full-ON).
        with self._lock:
            self._ensure_open()
            self._w(ALL_LED_OFF_H, FULL_BIT)
        self._record(None, None)

    def sleep(self) -> None:
        with self._lock:
            self._ensure_open()
            self._w(MODE1, MODE1_ALLCALL | MODE1_AI | MODE1_SLEEP)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self.all_off()
                self.sleep()
            except (OSError, DriverError) as e:
                log.warning("could not switch PCA9685 outputs off while closing: %s", e)
            finally:
                self._closed = True
                try:
                    self._bus.close()
                except Exception:  # noqa: BLE001
                    pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise DriverError("driver is closed")

    def _record(self, channel: int | None, pulse_us: float | None) -> None:  # overridden by the mock
        pass

    def describe(self) -> dict[str, Any]:
        return {
            "driver": self.name,
            "address": f"0x{self.address:02x}",
            "bus": self.bus_number,
            "requested_frequency_hz": self.requested_frequency_hz,
            "frequency_hz": round(self.frequency_hz, 3),
            "prescale": self.prescale,
            "initialised": self.initialised,
            "closed": self._closed,
            "writes": self.writes,
        }


class MockPCA9685(_PCA9685Base):
    """Simulation driver: the real register logic driving an emulated chip.

    ``history`` records ``(t, channel | None, pulse_us | None)`` for every output change
    (channel None = all channels off) - used by tests and the simulator.
    """

    name = "mock_pca9685"
    blocking_io = False

    def __init__(self, frequency_hz: float = 50.0, address: int = 0x40, *, chip: PCA9685Chip | None = None,
                 history: int = 5000) -> None:
        self.chip = chip or PCA9685Chip()
        self.bus = MockSMBus({int(address): self.chip})
        super().__init__(self.bus, address, frequency_hz, oscillator_hz=self.chip.oscillator_hz)
        self.history: collections.deque[tuple[float, int | None, float | None]] = collections.deque(maxlen=history)
        self.all_off_calls = 0
        self.initialise()

    def _record(self, channel: int | None, pulse_us: float | None) -> None:
        if channel is None:
            self.all_off_calls += 1
        self.history.append((time.monotonic(), channel, pulse_us))

    def pulse_us(self, channel: int) -> float | None:
        """What the emulated chip is actually outputting on a channel right now."""
        return self.chip.output_pulse_us(channel)

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d["simulated"] = True
        d["energised_channels"] = self.chip.energised_channels()
        return d


class PCA9685Driver(_PCA9685Base):
    """Real PCA9685 on the Raspberry Pi I2C bus (smbus2).

    Constructing it talks to the hardware (verifies the chip and switches every output
    off). MotionController only constructs it when backend == "pca9685" AND
    physical_enabled AND a valid calibration file exist; even then outputs stay off until
    a parent arms motion. ``bus`` may be injected (tests use MockSMBus).
    """

    name = "pca9685"
    blocking_io = True

    def __init__(self, bus_number: int = 1, address: int = 0x40, frequency_hz: float = 50.0, *,
                 bus: Any | None = None) -> None:
        owns_bus = bus is None
        if bus is None:
            try:
                from smbus2 import SMBus  # lazy: optional `pi` extra
            except ImportError as e:
                raise DriverError("smbus2 is not installed (pip install -e '.[pi]')") from e
            try:
                bus = SMBus(int(bus_number))
            except (OSError, FileNotFoundError) as e:
                raise DriverError(f"cannot open /dev/i2c-{bus_number} ({e}); enable I2C with raspi-config") from e
        super().__init__(bus, address, frequency_hz, bus_number=bus_number)
        try:
            self.initialise()
        except Exception:
            if owns_bus:
                try:
                    bus.close()
                except Exception:  # noqa: BLE001
                    pass
            raise
