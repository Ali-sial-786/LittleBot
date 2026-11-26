# LittleBot v5 base controller (RP2040, MicroPython)

The RP2040 on the v5 deck (a VCC-GND YD-RP2040, sold by Elecrow as "RP2040 Core Board"; any board with the Raspberry Pi Pico's pin layout works; on the YD-RP2040, VSYS is labelled Vin and VBUS Vout) drives the
two wheels through the Cytron MDD3A, counts their encoders, moves the three servos (head, left arm, right arm) and measures
the 6 × AA pack. The Pi gives it orders over three wires (its UART). It has no Wi-Fi and needs none.

Wiring: [`hardware/enclosure_v5/WIRING.md`](../../hardware/enclosure_v5/WIRING.md) and the video `LittleBot_v5_wiring.mp4` next to it.

| Pin | Use |
|---|---|
| GP0 (TX), GP1 (RX), GND | the Pi's pins 10 (RX), 8 (TX), 6 (GND): 115200 baud |
| GP6, GP7 / GP8, GP9 | left / right encoder: yellow (A), green (B); their blue to 3V3, black to GND |
| GP10, GP11 / GP12, GP13 | MDD3A M1A, M1B (left motor) / M2A, M2B (right motor), 20 kHz PWM |
| GP14, GP15, GP16 | the head, left arm and right arm servos' orange wires (50 Hz) |
| GP26 (ADC0) | the pack: Wago A → 22 kΩ → GP26 → 10 kΩ → AGND |

**YD-RP2040: bridge its VREF jumper** (two tiny pads marked VREF, near pin 26) with a blob of solder, or its ADC has no reference and reads 65535 on every pin (`B` then says `null`). Only the battery reading needs it.

## Loading it (once, from a computer, before the board goes in the robot)

1. Install Thonny (thonny.org).
2. Hold the board's **BOOT** button, plug it into the computer with a USB-C cable, let go. A drive called `RPI-RP2` appears
   (a brand-new board may show it without the button).
3. In Thonny: click the bottom-right corner of the window → **Install MicroPython…** → variant **Raspberry Pi • Pico / Pico H**
   → **Install**. The drive goes away when it's done.
4. Open `main.py` (this folder), **File → Save as… → Raspberry Pi Pico**, name it `main.py`.
5. Click **Stop**, then type `PING` in the shell: it answers `PONG littlebot-base 1`. `B` answers `B null` while nothing is wired.

Or with `mpremote` (`python3 -m pip install mpremote`): after step 3, `mpremote cp main.py :main.py` and `mpremote reset`.

**Centring a servo at the bench** (before fitting the pinion or an arm): with the board on USB, the servo's brown to a
GND pin, red to VBUS (pin 40, top right; labelled Vout on the YD-RP2040), orange to GP14; type `CENTER` in Thonny's
shell. One servo at a time, nothing on its horn. If it only buzzes, check its ground first: a loose GND joint or jumper
does exactly that (the servo then borrows ground through the signal pin).

## Commands (one line each; the same over the UART and in Thonny's shell)

| Command | Reply | |
|---|---|---|
| `PING` | `PONG littlebot-base 1` | |
| `D <left> <right>` | (none) | wheel speeds, -100..100 % of `max_mm_s`. Send it at least every 0.5 s, or the wheels ramp to a stop |
| `STOP` | `OK` | both wheels ramp to a stop |
| `S <n> <deg> [deg/s]` | (none) | servo 0 head, 1 left arm, 2 right arm, to 0..180 (within its limits), at that speed |
| `CENTER` | `OK` | all three to 90 |
| `OFF [n]` | `OK` | that servo (or all) limp |
| `E` | `E <left> <right>` | encoder counts (+ = forwards once `base_test.py wheels` has run) |
| `B` | `B <volts>` or `B null` | the pack |
| `T <hz>` | `OK`, then `T <ms> <encL> <encR> <volts>` lines | telemetry; `T 0` stops it |
| `GET`, `SET <name> <value>`, `SAVE` | `CFG {...}`, `OK ...` | the settings below; `SAVE` keeps them in `cfg.json` on the board |
| `PUT <bytes> <crc32>` then the bytes, `RESET` | `READY`, `OK put` | a new `main.py`, checked, then swapped in (the old one stays as `main_prev.py`) |

Unprompted: `HELLO littlebot-base 1` at start-up, `LOWBAT <volts>` every 5 s while the pack is under `low_v`,
`ERR crash ...` if the program fails (it then stops everything and restarts).

**Safety:** the wheels never go over `max_mm_s` (350 mm/s: a tall robot pitches over its caster if it stops dead), speed up
and slow down at `accel` (500 mm/s²), stop when the orders stop (0.5 s), when the pack is low and if the program fails. The
servos stay limp until their first command.

Settings: `max_mm_s`, `accel`, `wheel_mm` (67), `rpm6` (170: the motors' no-load speed at 6 V), `kick`, `cpr` (encoder counts
per wheel turn: `base_test.py calibrate` sets it), `ki`, `trim_max`, `swap` (1: the robot's left wheel is on M2 with its encoder on GP8/GP9, its right on M1 with GP6/GP7; nothing to rewire), `m2_gp`, `m2_gp2` (motor 2's IN1 and IN2 pins: 12 and 13 by default; if those are damaged, any 2 of the free GP2-5, GP12-13, GP18-22 that aren't one PWM output (GP2 + GP18, for example, are); m2_gp2 = 0 means the pin after m2_gp. On the chip-down YD-RP2040: GP2-5 are right-side rows 4-7, GP22 left row 12, GP21-18 left rows 14-17. SAVE, then RESET), `drv8871` (1: two DRV8871 boards instead of the MDD3A, GP10/GP11 to board 1's IN1/IN2 and GP12/GP13 to board 2's: each input pair is driven high + pulsed low, drive / brake, as the MDD3A's pattern (one pulsed, the other low) leaves a DRV8871 coasting at 20 kHz and the wheels don't turn), `minv_l`/`minv_r` and `einv_l`/`einv_r` (a motor or
encoder the other way round: `base_test.py wheels` sets them), `low_v` (5.8), `us_min`/`us_max` (servo pulse at 0 and 180°),
`s0_min` … `s2_max` (servo limits in degrees).

## From the Pi

Add `dtparam=uart0=on` to `/boot/firmware/config.txt` and reboot (the port is `/dev/ttyAMA0`). Then, in the LittleBot folder:

```bash
python3 scripts/base_test.py ping
```

```bash
python3 scripts/base_test.py battery
```

```bash
python3 scripts/base_test.py center
```

```bash
python3 scripts/base_test.py wheels
```

```bash
python3 scripts/base_test.py calibrate
```

```bash
python3 scripts/base_test.py update
```

`wheels` (robot on a box, wheels off the table) spins each wheel slowly, asks which way it went and saves the directions;
`calibrate` has you turn each wheel 5 times by hand and saves the counts per turn; `update` sends this folder's `main.py`
over the UART, so the board never needs its USB port again once it's in the robot.

## Tests

Everything above `run()` is plain Python: `tests/base/test_base_firmware.py` imports `main.py` in CPython with a fake board.
