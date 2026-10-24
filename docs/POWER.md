# Power: Raspberry Pi 5, USB devices, power bank and servos

Bad power is the most common cause of "random" Raspberry Pi problems: crashes, USB
devices dropping out, SD card corruption. **Bring the robot up on the official 27 W
power supply first**, and test the power bank only once everything works.

> Numbers marked *(estimate)* are typical values from datasheets and community
> measurements, **not measured on this robot**. Fill in the "measured" columns during
> [HARDWARE_ARRIVAL_CHECKLIST.md](../HARDWARE_ARRIVAL_CHECKLIST.md).

## 1. What the Pi 5 needs

From the official documentation
([Raspberry Pi hardware docs - power supply](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#power-supply)):

- Minimum to boot: a good USB-C supply giving **5 V at 3 A (15 W)**.
- Recommended: a **USB-PD supply that offers 5 V at 5 A (25 W)** - e.g. the official
  Raspberry Pi 27 W USB-C PSU (5.1 V / 5 A).
- With any supply that does not negotiate 5 A, the Pi 5 **limits all USB peripherals
  together to 600 mA** and the desktop shows a warning about it. With a 5 A supply the
  USB ports may draw up to **1.6 A** in total.
- Under-voltage detection trips when the input drops below about **4.63 V (±5 %)**.
  The Pi then throttles and logs "Undervoltage detected!".

Check what the Pi negotiated:

```bash
vcgencmd get_config usb_max_current_enable   # 1 = full USB current allowed, 0 = 600 mA limit
vcgencmd pmic_read_adc EXT5V_V               # input voltage at the Pi 5's PMIC (want >= ~4.9 V)
vcgencmd get_throttled                       # 0x0 = never under-voltage/throttled since boot
journalctl -k -b | grep -i voltage           # kernel under-voltage messages this boot
.venv/bin/python scripts/diagnose.py --section power   # all of the above, decoded
```

### Decoding `vcgencmd get_throttled`

| Bit | Value | Meaning |
|---|---|---|
| 0 | `0x1` | under-voltage **now** |
| 1 | `0x2` | ARM frequency capped now |
| 2 | `0x4` | throttled now |
| 3 | `0x8` | soft temperature limit active now |
| 16 | `0x10000` | under-voltage has occurred since boot |
| 17 | `0x20000` | frequency capping has occurred |
| 18 | `0x40000` | throttling has occurred |
| 19 | `0x80000` | soft temperature limit has occurred |

Example: `0x50005` = under-voltage and throttling right now *and* earlier;
`0x50000` = it happened earlier but not at this moment. `diagnose.py` and
`perf_monitor.py` decode it for you. Anything other than `0x0` after a test run means the
supply (or cable) is not good enough for that load - or, for bits 3/19, that the CPU got hot.

### `usb_max_current_enable` and `PSU_MAX_CURRENT` - caveats

Two settings remove the 600 mA USB limit when the supply cannot say it is 5 A capable:

- `usb_max_current_enable=1` in `/boot/firmware/config.txt`, or
- the EEPROM setting `PSU_MAX_CURRENT=5000` (`sudo rpi-eeprom-config --edit`), which makes
  the firmware skip USB-PD negotiation and assume a 5 A supply.

They do **not** make the supply stronger. If the power bank really can only give 3 A,
lifting the limit lets the Pi + USB devices draw more than it can deliver: the voltage
sags, the Pi throttles or resets, and an SD card write can be corrupted. Only use them
after measuring that the supply holds >= 4.9 V under the full robot load (section 4),
and prefer a supply that negotiates 5 A properly.

## 2. Current budget *(estimates - measure with a USB power meter)*

| Consumer | Path | Typical | Peak | Measured |
|---|---|---|---|---|
| Pi 5 (2 GB) idle, desktop | USB-C in | 0.5-0.6 A | - | TO MEASURE |
| Pi 5 busy (face + vision + Chromium) | USB-C in | 1.0-1.5 A | ~2 A | TO MEASURE |
| Active cooler fan | Pi (shares the USB current limit) | 0.05 A | 0.1 A | TO MEASURE |
| MPI3508 screen (backlight) | GPIO 5 V pins | 0.15-0.3 A | - | TO MEASURE |
| U20CAM-1080P camera (USB 2.0 UVC) | USB | 0.15-0.25 A | 0.3 A | TO MEASURE |
| Mini USB microphone | USB | 0.02-0.1 A | - | TO MEASURE |
| Mini USB speaker | USB | 0.1-0.3 A | 0.5 A at high volume | TO MEASURE |
| **USB total** (camera + mic + speaker) | | **~0.3-0.65 A** | **~0.9 A** | TO MEASURE |
| Servos (3x MG90S) | **separate supply** - never the Pi | 0.1-0.3 A moving each | ~0.7-0.8 A stall each | TO MEASURE |

The USB total sits **right at the 600 mA limit** a 3 A supply allows - which is why a
3 A-only power bank may work at low volume and then drop the speaker or camera when the
speaker gets loud. Plug the camera into a **blue USB 3 port** (lower latency, and keep it
away from the speaker's cable); a powered USB hub is the fallback if the budget does not fit.

## 3. Bring-up on the official PSU

1. Use the official 27 W PSU and its cable (thin/long cables cause voltage drop).
2. Boot, then run `.venv/bin/python scripts/diagnose.py --section power`.
   Expect `usb_max_current_enable=1`, EXT5V >= 4.9 V, `throttled=0x0`.
3. Run LittleBot with everything (camera, mic, speaker, YouTube) for 15 minutes with
   `perf_monitor.py` (section 4, step 4) - this is the reference result.

## 4. Testing the power bank (do this after bring-up works)

Many power banks only offer 5 V / 3 A (5 V / 5 A is not a standard USB-PD level), some
switch off when the load is low, and some cut out for a moment when you plug in a
charger (pass-through). Test before trusting one with a child's robot.

1. Note the bank's label: output voltages/currents, USB-PD support, capacity (Wh or mAh at 3.7 V).
2. Charge it fully. Use a short, good USB-C cable (5 A e-marked if the bank supports 5 A).
3. Boot the Pi from the bank and check the negotiation:

   ```bash
   vcgencmd get_config usb_max_current_enable
   vcgencmd pmic_read_adc EXT5V_V
   vcgencmd get_throttled
   ```

4. Load test - realistic first, then worst case - while recording:

   ```bash
   # terminal 1: start the app (or systemctl --user start littlebot), play a YouTube video,
   #             speaker at the maximum allowed volume
   # terminal 2: record 15 minutes
   .venv/bin/python scripts/perf_monitor.py --children --duration 15 --interval 5
   # worst case: CPU stress on top (sudo apt install stress-ng)
   stress-ng --cpu 4 --timeout 300s &
   watch -n 2 'vcgencmd pmic_read_adc EXT5V_V; vcgencmd get_throttled; vcgencmd measure_temp'
   ```

5. Pass criteria: `throttled` stays `0x0` (perf_monitor's summary shows `throttled_any`),
   EXT5V stays above ~4.8 V, no `Undervoltage detected` in `journalctl -k -b`, no USB
   device disconnects (`journalctl -k -b | grep -i 'usb.*disconnect'`), camera and
   speaker keep working.
6. Runtime: run from full until the Pi shuts off or warns, or estimate
   `hours ~= bank_Wh x 0.8 / robot_W` (0.8 = conversion losses, *estimate*). Example:
   a 10 000 mAh bank (37 Wh) at ~6 W -> roughly 5 h. TO MEASURE.
7. Write the results in the checklist (bank model, negotiated mode, worst EXT5V,
   throttled flags, runtime).

If the bank fails: use the official PSU, a bank that explicitly supports **5 V / 5 A
USB-PD for the Pi 5**, or a powered USB hub for the peripherals. Do not "fix" a failing
bank with `usb_max_current_enable=1`.

## 5. Servo power (later - read [MOTION_SAFETY.md](MOTION_SAFETY.md) first)

MG90S servos pull large current spikes (up to ~0.7-0.8 A each when stalled, *estimate*).
Powered from the Pi they cause brown-outs, resets and SD corruption.

- **Separate regulated 5-6 V supply** for the servos, rated >= 3 A for three MG90S
  (e.g. a 5 V 3-4 A adapter, or a UBEC from a battery). Connect it to the PCA9685's
  **V+ screw terminal**, never to the Pi's 5 V pins.
- **PCA9685 logic**: VCC from the Pi's **3.3 V** (pin 1), SDA pin 3, SCL pin 5. With the
  screen plugged onto the header these pins are under it: use a stacking header, a GPIO
  splitter or wires soldered under the Pi (see [MOTION_SAFETY.md](MOTION_SAFETY.md) section 2).
- **Common ground**: connect the servo supply's GND, the PCA9685 GND and a Pi GND pin
  together - without it the PWM signal has no reference.
- Add a 470-1000 uF electrolytic capacitor across V+/GND on the PCA9685 (most boards
  have a footprint for it) to absorb spikes.
- Put an **easy-to-reach switch** (and ideally a fuse) in the servo supply: it is the
  real emergency stop. The PCA9685 keeps sending its last PWM pulses even if the Pi
  crashes, so a software stop alone is not enough.
- `motion.physical_enabled` stays `false` until the wiring checks in MOTION_SAFETY.md are
  done; calibration then runs with `physical_enabled: true` and `backend: mock`.
  `diagnose.py` shows "calibration pending" at that stage and FAILs `backend: pca9685` +
  `physical_enabled: true` without a valid `config/calibration.yaml`. The installer never
  enables motion.

## 6. Safe shutdown

```bash
scripts/safe_shutdown.sh              # stop LittleBot, then power off (asks first)
scripts/safe_shutdown.sh --yes        # same, no question
scripts/safe_shutdown.sh --reboot     # reboot instead
scripts/safe_shutdown.sh --app-only   # just stop LittleBot
```

What it does, in order:

1. Stops LittleBot with SIGTERM (`systemctl --user stop littlebot`, the headless system
   service, or the process directly). On SIGTERM the app stops activities and video,
   makes the servos safe and de-energised, and closes the camera, microphone and database.
2. Waits up to 25 s and checks it really exited. If not, it **does not** power off:
   switch the servo supply off first, then re-run with `--force`.
3. `sync`, then `sudo shutdown -h now`.

**Never pull the power while the Pi is running.** The SD card may be in the middle of a
write (the settings database, logs, swap, the OS itself), and interrupting it can corrupt
files or the whole card. After shutdown, wait until the **green activity LED has stopped
flashing**; on the Pi 5 the power LED then turns red (low-power off state) and it is
safe to unplug. The Pi 5's power button also works: a short press starts a clean
shutdown (on the desktop it may first show the shutdown menu - press again; the app then
receives SIGTERM from systemd); holding it down forces power off - emergencies only.

## 7. Battery and child safety

- Use a branded power bank with over-current/over-temperature protection; never a
  damaged, swollen or very hot one. Lithium cells must not be crushed or punctured.
- Keep the battery and Pi out of the child's reach inside the enclosure, with
  ventilation: the Pi 5 + bank produce heat; check enclosure temperature after an hour
  (`vcgencmd measure_temp`, and by hand on the outside surfaces).
- Do not charge the bank inside a closed enclosure or unattended.
- Strain-relieve all cables so a pull cannot yank a connector or tip the robot over;
  no loose cables a child can reach, wrap around a neck, or chew.

## 8. Running on the battery

The cordless build: a **2S1P 21700 pack** (7.2 V nominal, 8.4 V full, about 36 Wh) with its
own BMS, a 2S USB-C charger board, and a **Pololu 5 V / 5 A step-down regulator** feeding
the Pi's 5 V. The Pi cannot measure the battery, so a **Waveshare RP2040-Zero** on one of its
USB ports does that and sends one JSON line per second. The robot's side is `littlebot/power/`
(`ctx.power`). Without the monitor, LittleBot behaves exactly as on mains: no battery card
values, no battery tool, no events, nothing said.

### 8.1 Tell the Pi it has a 5 A supply (`PSU_MAX_CURRENT`)

The regulator feeds the Pi's 5 V directly, so there is no USB-PD conversation and the Pi 5
assumes a weak supply: **all USB devices together are limited to 600 mA**. That is not enough
for the camera, microphone, speaker *and* the battery monitor (section 2). With the 5 A
regulator fitted, tell the firmware the supply gives 5 A:

```bash
sudo rpi-eeprom-config --edit
# add (or change) this line, save and exit:
PSU_MAX_CURRENT=5000
sudo reboot
```

The USB ports may then draw up to **1.6 A** in total instead of 600 mA. **Only do this with the
5 A regulator fitted**: the setting does not make a supply stronger (see "caveats" in
section 1). It has to be done by hand on the robot; LittleBot never changes system settings.

Then check on the battery, under the full robot load (video playing, speaker loud):

```bash
vcgencmd get_config usb_max_current_enable   # 1 = the USB limit is lifted
vcgencmd pmic_read_adc EXT5V_V               # the 5 V rail: want >= ~4.9 V, also with a nearly empty pack
vcgencmd get_throttled                       # want 0x0 (decode it with the table in section 1)
```

Repeat the `EXT5V_V` / `get_throttled` checks near the end of a charge (below about 20 %), when
the regulator has the least headroom.

### 8.2 Flash the battery monitor

Full details, the pin table and what `null` means are in
[firmware/battery_monitor/README.md](../firmware/battery_monitor/README.md). In short:

1. Hold **BOOT** on the RP2040-Zero while plugging it into a computer; the `RPI-RP2` drive appears.
2. Copy the MicroPython `.uf2` for the Raspberry Pi Pico onto it (it reboots into MicroPython).
3. `mpremote cp firmware/battery_monitor/main.py :main.py`, then `mpremote reset`.

The ratios of the voltage dividers (100k/33k for the pack, 100k/47k for the midpoint) are
constants at the top of `main.py`; change them there if a resistor changes. It runs with
nothing attached and reports what isn't connected as `null`.

### 8.3 Check it from the robot

Stop LittleBot first (`systemctl --user stop littlebot`): two programs reading one serial port
each get only some of the lines.

```bash
.venv/bin/python scripts/check_battery.py              # find the monitor, print readings until Ctrl-C
.venv/bin/python scripts/check_battery.py --seconds 20 # ... for 20 s
.venv/bin/python scripts/check_battery.py --list       # the serial ports it would look at (opens none)
.venv/bin/python scripts/check_battery.py --raw        # also the JSON lines as they arrive
.venv/bin/python scripts/check_battery.py --simulate --percent 15   # no hardware: the simulated pack
```

It exits 0 with readings, 1 when the monitor stopped sending, 2 when no monitor was found. The robot finds the
monitor itself (the `/dev/serial/by-id/` names of RP2040 / MicroPython boards, and `/dev/ttyACM*`
ports with the Raspberry Pi USB vendor id `2e8a`) and accepts only a port whose lines say `"dev":"littlebot-battery"`;
other USB serial devices are left alone. It never writes to the port. Unplugged and plugged
back in, it reconnects by itself (the dashboard shows "Battery monitor disconnected" meanwhile).

The dashboard's **Home** page has a **Battery** card: percent, charging or not, voltage (and each
cell), temperature, time left (once the level has fallen steadily for about 10 minutes), a
"Show on LittleBot" button and the battery settings. On mains it says "Not fitted".
`GET /api/power` (parent login) returns the same.

To preview it all on a desktop without hardware:
`LITTLEBOT__POWER__MONITOR=simulated .venv/bin/python -m littlebot`, then the simulator page
has a "Battery (simulated)" card to set the level, the charger and the temperature.

### 8.4 What LittleBot does

| When | What happens |
|---|---|
| Charger plugged in | The battery sign (a battery with a bolt) shows in the face's top-left corner for a few seconds. `power.charging`, later `power.charged`; unplugging: `power.unplugged` |
| Asked ("how much battery?") | The voice uses the `battery` tool: it answers and shows the sign |
| Waking from a doze below 30 % | The sign shows for a moment |
| **Low** (20 % for 5 s) | Says once, gently: "I'm getting a little sleepy. Can a grown-up charge my battery, please?" (only when idle, never while asleep, not if already charging). In a live voice chat: a quiet note to the voice instead, and the chat goes on. Dashboard banner. `power.low` |
| **Empty** (7 %, or one cell below 3.30 V, for 20 s) | Ends a live chat politely (the voice says goodbye), stops video and activities, says "My battery is empty, so I'm going to sleep now. Night night!" (silently if asleep), exits cleanly and switches the Pi off. **Never while the charger is in**; plugging it in during the goodbye cancels it. `power.critical` |
| **Hot** (50 °C for 10 s) | Dashboard warning, `power.hot` (`level: warm`) |
| **Too hot** (60 °C for 10 s) | Says "I'm a bit too warm, so I'm going to have a rest now", stops everything and shuts down cleanly, *also while charging*. `power.hot` (`level: shutdown`) |
| Charging below 0 °C | Dashboard alert (Li-ion must not be charged below freezing). `power.cold` |

The 20 s for "empty" is so a short voltage dip (the speaker at full volume) doesn't count. The
percentage comes from a 2S Li-ion table (the weaker cell decides), smoothed; it only goes down
on battery and only goes up while charging, so it doesn't jump around.

The parent can change the low level (5-50 %), the shutdown level (2-20 %), whether the low
battery is said out loud and whether it shuts down when empty, in the Battery card's settings
(`power.low_percent`, `power.critical_percent`, `power.announce_low`,
`power.shutdown_when_empty`); they apply at once. Everything else is in the `power:` section
of the config (`littlebot/core/config.py`, `PowerConfig`).

### 8.5 The power-off

There was no power-off in LittleBot before (the dashboard's Shutdown only stops the app, and
`scripts/safe_shutdown.sh` uses sudo). When the battery is empty or too hot, LittleBot first
exits exactly like a normal stop (subsystems stopped, servos safe, database closed) and only
then runs `systemctl --no-ask-password poweroff`, logged at CRITICAL level. No sudo.

- It only does this when `power.allow_poweroff` allows it. The default (empty) means: on the
  Raspberry Pi (`mode: pi`) yes, in the desktop simulation never (there it only logs and goes
  to sleep). Set `power.allow_poweroff: false` to never switch the Pi off.
- `systemctl poweroff` without sudo works for the user logged in on the robot's own screen
  (the normal autostart as a user service: polkit allows the active local session). From the
  headless system service, or a session that isn't "active", polkit refuses: the log says
  "power-off was refused", and the BMS cuts the power when the pack is really empty. To allow
  it there, a polkit rule for the robot's user is needed (`org.freedesktop.login1.power-off`);
  that is a system setting to add by hand.
- After it has switched off, wait for the green LED to stop before unplugging anything
  (section 6). Switch it back on with the Pi's power button once the pack is charged.
