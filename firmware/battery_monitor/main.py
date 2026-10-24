# LittleBot battery monitor - MicroPython for a Waveshare RP2040-Zero on one of the Pi's USB ports.
#
# Prints ONE JSON line per second on the USB serial port (nothing else, no Wi-Fi, no input):
#   {"dev":"littlebot-battery","fw":1,"v":7.84,"cell1":3.92,"t":24.6,"chg":false,"full":false,"up":123}
# v / cell1 (volts) and t (pack temperature, deg C) are numbers or null; chg / full (the charger's
# "charging" and "charged" outputs) are true / false or null; up = whole seconds since boot.
# A pin with nothing connected gives null, never a made-up number. See README.md (pins, flashing).
#
# The helpers above the "board" line are plain Python so the robot's tests can import this file in
# CPython (tests/power/test_firmware.py); the machine module is only needed by run().

import math

DEV = "littlebot-battery"
FW = 1

VREF = 3.3  # the RP2040-Zero's ADC reference (its 3.3 V rail)
ADC_FULL = 65535  # read_u16() full scale

# Pack voltage (GP26 / ADC0): 100 k top, 33 k bottom -> 8.4 V reads ~2.08 V at the pin.
PACK_RATIO = (100.0 + 33.0) / 33.0
# Midpoint = cell 1 (GP27 / ADC1): 100 k top, 47 k bottom -> 4.2 V reads ~1.34 V.
CELL_RATIO = (100.0 + 47.0) / 47.0
# Pack NTC (GP28 / ADC2): 10 k NTC (B = 3950) to GND, 10 k pull-up to 3.3 V.
NTC_PULLUP = 10000.0
NTC_R25 = 10000.0
NTC_BETA = 3950.0

# What a connected pin can read; anything else (a floating pin, a loose wire) is reported as null.
PACK_RANGE = (5.0, 9.0)  # 2S: the BMS cuts off at ~5 V, a full pack is 8.4 V
CELL_RANGE = (2.5, 4.5)
TEMP_RANGE = (-30.0, 100.0)
NTC_RAIL = 0.02  # within 2 % of either rail: no NTC (open) or a short
MAX_SPREAD_V = 0.5  # a connected divider is steady within one second (load spikes included)
MAX_SPREAD_C = 3.0
# Floating-pin probe: once a second each analog pin is read with its pad's pull-down, then its pull-up
# (50-80 k inside the RP2040). A floating pin follows the pulls rail to rail (~3.3 V apart); the dividers
# (Thevenin 25 k / 32 k / 5 k) move by at most ~1.3 V. A floating ADC pin can otherwise read a steady,
# plausible voltage (the charge left by the previous channel), so a range check alone is not enough.
FLOAT_DIFF_V = 2.2
PADS_BANK0 = 0x4001C000  # RP2040 pad control registers: GPIOn at +4 + 4n; bit 3 = pull-up, bit 2 = pull-down

SAMPLES = 8  # ADC reads averaged per tick
TICK_MS = 50  # 20 ticks per printed line


def adc_volts(raw):
    """A read_u16() value as volts at the pin."""
    return raw * VREF / ADC_FULL


def mean(values):
    return sum(values) / len(values) if values else None


def spread(values):
    return (max(values) - min(values)) if values else 0.0


def pack_volts(pin_volts):
    return pin_volts * PACK_RATIO


def cell_volts(pin_volts):
    return pin_volts * CELL_RATIO


def in_range(value, lo_hi):
    return value is not None and lo_hi[0] <= value <= lo_hi[1]


def ntc_celsius(raw):
    """deg C from the NTC divider's read_u16() value (the ratio cancels VREF), or None (open, short)."""
    if raw is None:
        return None
    frac = raw / ADC_FULL
    if frac <= NTC_RAIL or frac >= 1.0 - NTC_RAIL:
        return None
    r = NTC_PULLUP * frac / (1.0 - frac)
    try:
        inv = 1.0 / 298.15 + math.log(r / NTC_R25) / NTC_BETA
    except (ValueError, ZeroDivisionError):
        return None
    if inv <= 0:
        return None
    return 1.0 / inv - 273.15


def floating(low_volts, high_volts):
    """True when a pin read with the pull-down (``low_volts``) and the pull-up (``high_volts``) follows the
    pulls: nothing is connected to it."""
    return high_volts - low_volts > FLOAT_DIFF_V


def judge(pack_ticks, cell_ticks, ntc_ticks, floats=(False, False, False)):
    """One second of per-tick readings (volts at the pins, NTC raw) -> (v, cell1, t), each None when absent.

    A value counts only when its pin is not floating (``floats``: pack, cell, NTC), it is in range and it was
    steady over the second."""
    v = cell1 = t = None
    packs = [pack_volts(x) for x in pack_ticks]
    if packs and not floats[0] and spread(packs) <= MAX_SPREAD_V:
        m = mean(packs)
        v = m if in_range(m, PACK_RANGE) else None
    cells = [cell_volts(x) for x in cell_ticks]
    if cells and not floats[1] and spread(cells) <= MAX_SPREAD_V:
        m = mean(cells)
        if in_range(m, CELL_RANGE) and (v is None or in_range(v - m, CELL_RANGE)):
            cell1 = m
    temps = [ntc_celsius(x) for x in ntc_ticks]
    if temps and not floats[2] and None not in temps and spread(temps) <= MAX_SPREAD_C:
        m = mean(temps)
        t = m if in_range(m, TEMP_RANGE) else None
    return v, cell1, t


def status_pin(low_with_pulldown, low_with_pullup, seen):
    """An active-low charger output read with the pull-down, then the pull-up -> (value, seen).

    Driven low both times: asserted (True). High both times: driven high, not asserted (False).
    Following the pulls: nothing drives it (open-drain off, charger unpowered, or no wire): False once the
    pin has been seen driven since boot, else None (we can't tell "not charging" from "not connected")."""
    if low_with_pulldown and low_with_pullup:
        return True, True
    if not low_with_pulldown and not low_with_pullup:
        return False, True
    return (False if seen else None), seen


def _num(value, digits):
    if value is None:
        return "null"
    return ("%." + str(digits) + "f") % value


def _flag(value):
    return "null" if value is None else ("true" if value else "false")


def line(v, cell1, t, chg, full, up):
    """The JSON line, keys in a fixed order (MicroPython dicts don't keep one)."""
    return ('{"dev":"%s","fw":%d,"v":%s,"cell1":%s,"t":%s,"chg":%s,"full":%s,"up":%d}'
            % (DEV, FW, _num(v, 2), _num(cell1, 2), _num(t, 1), _flag(chg), _flag(full), int(up)))


# ------------------------------------------------------------------ board (MicroPython only)
def _read(adc):
    total = 0
    for _ in range(SAMPLES):
        total += adc.read_u16()
    return total / SAMPLES


def _pulls(gpio, up, down):
    from machine import mem32

    addr = PADS_BANK0 + 4 + 4 * gpio
    mem32[addr] = (mem32[addr] & ~0b1100) | (0b1000 if up else 0) | (0b0100 if down else 0)


def _probe(adc, gpio):
    """True when the analog pin ``gpio`` is floating (see FLOAT_DIFF_V); the pulls are off again after."""
    import time

    try:
        _pulls(gpio, False, True)
        time.sleep_us(300)
        low = adc_volts(_read(adc))
        _pulls(gpio, True, False)
        time.sleep_us(300)
        high = adc_volts(_read(adc))
    finally:
        _pulls(gpio, False, False)
    time.sleep_us(300)
    return floating(low, high)


def run():
    import time

    from machine import ADC, Pin

    adc_pack, adc_cell, adc_ntc = ADC(26), ADC(27), ADC(28)
    pins = (Pin(2, Pin.IN, Pin.PULL_UP), Pin(3, Pin.IN, Pin.PULL_UP))  # charging, charged (active-low)
    seen = [False, False]

    def read_status(i):
        pin = pins[i]
        pin.init(Pin.IN, Pin.PULL_DOWN)
        time.sleep_us(100)
        low_down = pin.value() == 0
        pin.init(Pin.IN, Pin.PULL_UP)
        time.sleep_us(100)
        low_up = pin.value() == 0
        value, seen[i] = status_pin(low_down, low_up, seen[i])
        return value

    up_ms = 0
    last = time.ticks_ms()
    errors = 0
    while True:
        try:
            floats = (_probe(adc_pack, 26), _probe(adc_cell, 27), _probe(adc_ntc, 28))
            packs, cells, ntcs = [], [], []
            for _ in range(1000 // TICK_MS):
                packs.append(adc_volts(_read(adc_pack)))
                cells.append(adc_volts(_read(adc_cell)))
                ntcs.append(_read(adc_ntc))
                time.sleep_ms(TICK_MS)
            v, cell1, t = judge(packs, cells, ntcs, floats)
            chg, full = read_status(0), read_status(1)
            now = time.ticks_ms()
            up_ms += time.ticks_diff(now, last)  # ticks_ms wraps after ~12 days; the sum doesn't
            last = now
            print(line(v, cell1, t, chg, full, up_ms // 1000))
            errors = 0
        except KeyboardInterrupt:
            raise  # mpremote / Thonny stopping the program
        except Exception:  # noqa: BLE001 - keep reporting; reset only if it keeps failing
            errors += 1
            if errors >= 10:
                import machine
                machine.reset()
            time.sleep_ms(500)


if __name__ == "__main__":
    run()
