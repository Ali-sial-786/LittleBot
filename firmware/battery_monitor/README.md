# LittleBot battery monitor (RP2040-Zero, MicroPython)

The Pi cannot measure the battery itself (its I²C pins are under the screen), so a
Waveshare **RP2040-Zero** plugged into one of the Pi's USB ports does it. It prints one
JSON line per second on its USB serial port and nothing else:

```json
{"dev":"littlebot-battery","fw":1,"v":7.84,"cell1":3.92,"t":24.6,"chg":false,"full":false,"up":123}
```

| Key | Meaning |
|---|---|
| `dev` | always `"littlebot-battery"` (the robot accepts only a port that says this) |
| `fw` | firmware version (`1`) |
| `v` | pack voltage, volts (2 decimals), or `null` |
| `cell1` | the bottom cell (the midpoint tap), volts, or `null`; cell 2 = `v - cell1` |
| `t` | pack temperature (NTC), °C (1 decimal), or `null` |
| `chg` | the charger's "charging" output is on: `true` / `false` / `null` |
| `full` | the charger's "charged / standby" output is on: `true` / `false` / `null` |
| `up` | whole seconds since the monitor started |

The robot side is `littlebot/power/` (see [docs/POWER.md](../../docs/POWER.md), "Running on the battery").

## Pins

| Pin | Connected to | Notes |
|---|---|---|
| GP26 / ADC0 | pack + through 100 kΩ (top) / 33 kΩ (bottom) to GND | 8.4 V → ~2.08 V. `PACK_RATIO` in `main.py` |
| GP27 / ADC1 | midpoint (cell 1 +) through 100 kΩ / 47 kΩ | 4.2 V → ~1.34 V. `CELL_RATIO` |
| GP28 / ADC2 | 10 kΩ NTC (B = 3950) to GND, 10 kΩ pull-up to 3.3 V | `NTC_*` constants |
| GP2 | charger "charging" output | active-low, internal pull-up |
| GP3 | charger "charged / standby" output | active-low, internal pull-up |
| GND | pack − (common ground with the charger outputs) | |

If you change a resistor, change the matching ratio constant at the top of `main.py`.

**Anything may be unconnected** and the monitor still runs (it runs with nothing attached
at all). What it can't measure is `null`, never a made-up number:

- Analog pins: once a second each one is read with its pad's pull-down and then its
  pull-up. A pin that follows them from rail to rail has nothing on it (`null`). A reading
  out of range (pack 5–9 V, cell 2.5–4.5 V and `v - cell1` too, −30…100 °C), an NTC
  reading at a rail (open or shorted) or a value that jumps around within the second is
  also `null`.
- Charger outputs: read with the pull-down and the pull-up. Pulled low both times: on
  (`true`); driven high both times: off (`false`). If the pin just follows the pulls (an
  open-drain output that is off, a charger without power, or no wire) it is `null` until
  the pin has been seen driven once since start-up, then `false`.

## Flashing

You need the MicroPython firmware for the RP2040-Zero and `mpremote` on the computer you
flash from (`python3 -m pip install mpremote`, or `.venv/bin/python -m pip install mpremote`).

1. Download the MicroPython **UF2** for the RP2040 (the Raspberry Pi Pico build works on the
   RP2040-Zero: <https://micropython.org/download/RPI_PICO/>; Waveshare also links one on the
   RP2040-Zero wiki).
2. Hold the **BOOT** button on the RP2040-Zero while you plug it into USB (or hold BOOT and
   tap RESET). A USB drive called `RPI-RP2` appears.
3. Copy the `.uf2` file onto `RPI-RP2`. The board reboots into MicroPython (the drive goes away).
4. Copy the program and restart the board:

   ```bash
   cd firmware/battery_monitor
   mpremote cp main.py :main.py
   mpremote reset
   ```

5. Check it (Ctrl-] to leave `mpremote`, or Ctrl-C in `cat`'s case):

   ```bash
   mpremote                                   # shows the JSON lines
   # or, on the robot, with LittleBot's own reader (it finds the port by itself):
   .venv/bin/python scripts/check_battery.py
   ```

   With nothing attached you should see `"v":null,"cell1":null,"t":null,"chg":null,"full":null`.

Stop LittleBot (or at least close `check_battery.py`) before `mpremote` talks to the board:
only one program can use the serial port properly at a time. To update later, just repeat
step 4; `mpremote` interrupts the running program by itself.

## Tests

The maths and the line format are plain Python (the `machine` module is only imported by
`run()`), so the robot's test-suite imports `main.py` in CPython:
`tests/power/test_firmware.py`.
