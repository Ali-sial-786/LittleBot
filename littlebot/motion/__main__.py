"""Stand-alone "all servos off" command.

    python -m littlebot.motion off                 # real PCA9685 on /dev/i2c-1 at 0x40
    python -m littlebot.motion off --bus 1 --address 0x40
    python -m littlebot.motion off --simulate      # emulated chip (works anywhere)

Used by scripts/safe_shutdown.sh when the app had to be killed and could not
de-energise the servos itself. It only ever switches outputs OFF (full-off bit on
every channel, then oscillator sleep): it never energises anything, so it needs no
calibration or arming. Exit codes: 0 = outputs off (or no PCA9685/I2C present, so
nothing can be driving servos), 1 = a PCA9685 answered but the off command failed.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Sequence

from littlebot.motion.drivers import (
    ALL_LED_OFF_H, FULL_BIT, MODE1, MODE1_ALLCALL, MODE1_SLEEP, MockPCA9685,
)


def switch_off(bus: Any, address: int) -> None:
    """Two single-byte writes: every output full-off, then put the chip to sleep."""
    bus.write_byte_data(address, ALL_LED_OFF_H, FULL_BIT)
    bus.write_byte_data(address, MODE1, MODE1_ALLCALL | MODE1_SLEEP)


def _open_real_bus(bus_number: int) -> tuple[Any | None, str]:
    try:
        from smbus2 import SMBus
    except ImportError:
        return None, "smbus2 not installed - no PCA9685 can be in use by LittleBot"
    try:
        return SMBus(int(bus_number)), ""
    except (OSError, FileNotFoundError) as e:
        return None, f"/dev/i2c-{bus_number} not available ({e}) - I2C is off, nothing to switch off"


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m littlebot.motion", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    off = sub.add_parser("off", help="switch every PCA9685 servo output off")
    off.add_argument("--bus", type=int, default=1, help="I2C bus number (default 1)")
    off.add_argument("--address", type=lambda s: int(s, 0), default=0x40, help="PCA9685 address (default 0x40)")
    off.add_argument("--simulate", action="store_true", help="use the emulated chip instead of real I2C")
    args = p.parse_args(argv)

    if args.simulate:
        drv = MockPCA9685(address=args.address)
        drv.set_pulse_us(0, 1500)  # something energised, so the demo shows it going off
        switch_off(drv.bus, args.address)
        left = drv.chip.energised_channels()
        print(f"simulated PCA9685 at 0x{args.address:02x}: outputs off (energised channels now: {left or 'none'})")
        return 0 if not left else 1

    bus, why = _open_real_bus(args.bus)
    if bus is None:
        print(f"servos off: skipped - {why}")
        return 0
    try:
        try:
            bus.read_byte_data(args.address, MODE1)
        except OSError:
            print(f"servos off: no PCA9685 answering at 0x{args.address:02x} on bus {args.bus} - nothing to switch off")
            return 0
        try:
            switch_off(bus, args.address)
        except OSError as e:
            print(f"servos off: FAILED to write to the PCA9685 ({e}). Switch the SERVO SUPPLY OFF by hand.",
                  file=sys.stderr)
            return 1
        print(f"servos off: all PCA9685 outputs at 0x{args.address:02x} switched off and chip put to sleep")
        return 0
    finally:
        try:
            bus.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
