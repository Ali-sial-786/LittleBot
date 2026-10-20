# Hardware arrival checklist

Work through this in order on the day the parts arrive. Each phase only adds one new
thing, so when something fails you know what caused it. Tick the boxes, note results in
the blanks, and paste the outputs listed in [What to report back](#what-to-report-back-to-claude)
into the next development session.

Guides: [docs/PI_SETUP.md](docs/PI_SETUP.md) · [docs/DISPLAY_SETUP.md](docs/DISPLAY_SETUP.md) ·
[docs/POWER.md](docs/POWER.md) · [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) ·
[docs/PERFORMANCE.md](docs/PERFORMANCE.md) · [docs/MOTION_SAFETY.md](docs/MOTION_SAFETY.md) (servos;
calibration format `config/calibration.example.yaml`)

**Ground rules:** power off before plugging anything into the GPIO header · official
27 W PSU for all bring-up · servos stay disconnected until phase 12 · keep small parts
(screws, standoffs, jumpers, SD card) away from the child at all times.

---

## Phase 0 - Unpack and inspect

- [ ] Raspberry Pi 5 2 GB: no bent pins or damage; note board revision printed on it: ________
- [ ] Official active cooler: fan blade free, thermal pads present, push-pins intact
- [ ] DORHEA MPI3508 3.5" screen: glass not cracked, header socket straight
- [ ] InnoMaker U20CAM-1080P camera, mini USB microphone, mini USB speaker
- [ ] Patriot 64 GB microSD, micro-HDMI to HDMI cable
- [ ] Official Raspberry Pi 27 W USB-C PSU available for bring-up (borrow/buy one if not)
- [ ] Power bank: write down model and label (outputs, PD support, Wh/mAh): ________
- [ ] Optional but useful: USB-C power meter (shows V/A), normal HDMI monitor, USB keyboard/mouse
- [ ] Put all small parts in a closed box out of the child's reach

## Phase 1 - Cooler and SD card

- [ ] Fit the active cooler (remove pad films; push-pins fully home; fan cable into the
      **FAN** connector) - with the Pi unpowered
- [ ] Flash **Raspberry Pi OS (64-bit) with desktop** with Raspberry Pi Imager; set
      hostname `littlebot`, user/password, Wi-Fi + country, locale, enable SSH (PI_SETUP.md §1)
- [ ] Note the OS version Imager wrote (Bookworm or Trixie): ________

## Phase 2 - Power FIRST (official PSU, nothing else plugged in)

- [ ] Boot with only the SD card + official PSU (+ a normal monitor if you have one)
- [ ] `vcgencmd get_throttled` -> `throttled=0x0`
- [ ] `vcgencmd get_config usb_max_current_enable` -> `1` (5 A negotiated)
- [ ] `vcgencmd pmic_read_adc EXT5V_V` -> ______ V (expect >= 4.9 V)
- [ ] `vcgencmd measure_temp` at idle -> ______ C; fan spins up under load (it may be
      off while cool)
- [ ] `sudo apt update && sudo apt full-upgrade -y && sudo reboot` (PI_SETUP.md §3)

## Phase 3 - Install LittleBot

- [ ] Clone/copy the repo to `~/LittleBot` (no `.venv` from another computer)
- [ ] `scripts/install_pi.sh --dry-run`, read the plan, then `scripts/install_pi.sh`
- [ ] Log out/in if the installer changed group membership
- [ ] `.venv/bin/python scripts/diagnose.py` - save the output; no FAIL in python/packages/config
- [ ] `.venv/bin/python -m pytest -q` (only if installed with `--dev`) - result: ________

## Phase 4 - Display (HDMI picture first, touch later)

- [ ] Power off; connect the MPI3508 (HDMI0 port next to USB-C power); check it clears the
      cooler fan (DISPLAY_SETUP.md §1) - fits directly / needs header extender / jumper wires
- [ ] Boot. Picture? yes / no / garbled
- [ ] `cat /sys/class/drm/card*-HDMI-A-1/modes` - EDID modes: ________
- [ ] `.venv/bin/python scripts/diagnose.py --section display` saved
- [ ] Pick the mode (DISPLAY_SETUP.md §3): option A / B (720x480) / C / D - chosen: ________
- [ ] Screen blanking off (raspi-config -> Display Options)
- [ ] Rotation needed? ______ (wlr-randr / Screen Configuration)
- [ ] Do **not** run the vendor LCD-show script
- [ ] Touch: postpone until phase 11

## Phase 5 - Camera

- [ ] Plug the camera into a **blue USB 3 port**
- [ ] `v4l2-ctl --list-devices` and `v4l2-ctl -d /dev/video0 --list-formats-ext | head -40`
- [ ] `.venv/bin/python scripts/check_camera.py --help`, then its list/test mode
- [ ] `.venv/bin/python scripts/diagnose.py --section camera` - detector usable (haar / yunet)?
- [ ] Note the camera's name / by-id path: ________

## Phase 6 - Microphone

- [ ] Plug in the mic; `arecord -l`
- [ ] `.venv/bin/python scripts/check_audio.py --help`, then its list/test mode
- [ ] 3-second test recording plays back clearly (delete it afterwards)
- [ ] Note the device name substring: ________

## Phase 7 - Speaker

- [ ] Plug in the speaker; `aplay -l`
- [ ] Desktop volume menu: USB speaker selected as output (not HDMI)
- [ ] Test at **low volume first**, at the child's listening distance; find a comfortable
      maximum and set `audio.speaker.max_volume` (<= 0.8)
- [ ] Note the device name substring: ________
- [ ] Re-check power with all three USB devices: `vcgencmd get_throttled` -> ______

## Phase 8 - Configure and first run

- [ ] Edit `config/local.yaml`: microphone / speaker device substrings, camera, volume
- [ ] `.venv/bin/python scripts/diagnose.py` - all device checks PASS (save output)
- [ ] From the desktop terminal: `.venv/bin/python -m littlebot --mode pi`
- [ ] Face fills the screen, sharp, not squashed; eyes blink; cursor hidden
- [ ] Face follows a person in front of the camera
- [ ] Sounds/voice come from the USB speaker at a gentle volume
- [ ] Ctrl+C exits cleanly (log ends with "shutdown complete")

## Phase 9 - Dashboard, phone and autostart

- [ ] Dashboard on the Pi: http://127.0.0.1:8765; first-run setup code from the log;
      strong password set
- [ ] Opt in to phone access: `dashboard.lan_access: true`, restart
- [ ] Phone (same Wi-Fi) opens `http://<ip>:8765` (diagnose prints it) / `http://littlebot.local:8765`
- [ ] STOP button on the phone stops everything immediately
- [ ] `scripts/install_pi.sh --with-service`, enable Desktop Autologin, reboot ->
      LittleBot starts by itself within ~30 s: ______ s
- [ ] `systemctl --user status littlebot` active; `scripts/safe_shutdown.sh --app-only` stops it

## Phase 10 - Performance, YouTube and the power bank

- [ ] PERFORMANCE.md scenarios 1-5 with `scripts/perf_monitor.py` (official PSU)
- [ ] `scripts/benchmark_face.py` (face module) result: ________
- [ ] YouTube: add an allowed video in the dashboard, play it; smooth? audio on the USB
      speaker? stops at the session limit? RAM available minimum: ______ MB
- [ ] 30-minute thermal soak in the enclosure: max temp ______ C, `throttled_any` ______
- [ ] **Now** the power bank, following POWER.md §4 (realistic load, then stress):
      negotiated `usb_max_current_enable` ___, min EXT5V ___ V, `throttled_any` ___,
      USB disconnects yes/no, runtime ___ h
- [ ] Decision: power bank OK / needs a 5 V 5 A PD bank / needs a powered hub / mains only

## Phase 11 - Touch (optional)

- [ ] Follow DISPLAY_SETUP.md §5 (SPI + ads7846 overlay; back up config.txt first)
- [ ] `dmesg | grep -i ads7846`, `/proc/bus/input/devices` shows the touchscreen
- [ ] `evtest`: taps register; corners roughly right; calibration needed? ______

## Phase 12 - Enclosure and child-safety review (before the child uses it)

- [ ] **Small parts**: no screws, standoffs, jumpers, SD card or loose pieces reachable or
      able to fall out (anything that fits through a toilet-roll tube is a choking hazard)
- [ ] **Exposed electronics**: no reachable PCB, pins, header or solder joints; screen
      glass protected against impact and against sharp edges if it cracks
- [ ] **Pinch points**: no gaps where fingers can be caught (servo arms come later -
      re-check then)
- [ ] **Heat**: outside surfaces stay comfortable to touch after 1 h of use; vents not
      blocked; Pi temperature under load ______ C
- [ ] **Cables**: none reachable or long enough to wrap around a neck; strain-relieved;
      the robot cannot be pulled over by a cable
- [ ] **Stability**: does not tip when pushed or pulled by a toddler; heavy parts (battery) low
- [ ] **Battery**: branded bank with protection, not swollen, cannot be reached or
      removed by the child, not charged unattended inside the enclosure (POWER.md §7)
- [ ] **Sound level** comfortable at ear distance; volume ceiling set
- [ ] **Camera/mic privacy**: dashboard privacy switches work; nothing stored by default
- [ ] **Supervision**: always used with an adult present; you know how to stop it fast
      (phone STOP button, `safe_shutdown.sh`, power switch)

## Phase 13 - Servos LAST (only after everything above)

- [ ] Read [docs/MOTION_SAFETY.md](docs/MOTION_SAFETY.md) completely; work through its
      verification checklist parts A-E alongside the steps below
- [ ] Separate regulated 5-6 V >= 3 A servo supply with an easy-to-reach switch; PCA9685
      V+ from it, VCC from Pi 3.3 V, **common ground** (POWER.md §5); never from the Pi's 5 V
- [ ] `scripts/install_pi.sh --enable-i2c`, reboot;
      `.venv/bin/python scripts/diagnose.py --section i2c --i2c-scan` -> PCA9685 at 0x40
- [ ] Checklist parts A-C done -> in `config/local.yaml`: `motion.physical_enabled: true`
      with `motion.backend` **still `mock`** (diagnose.py now says "calibration pending")
- [ ] Practise first: `.venv/bin/python scripts/calibrate_servos.py --simulate`; then calibrate
      each joint on hardware as MOTION_SAFETY.md section 5 describes (servo horns detached
      for the first test). It writes `config/calibration.yaml` (format reference:
      `config/calibration.example.yaml`)
- [ ] diagnose.py: `motion_safety` reports "calibration valid; motion still simulated"
- [ ] Only after checklist part D: `motion.backend: pca9685`; diagnose.py must not FAIL
      `motion_safety`; a parent arms motion in the dashboard after each start
- [ ] Re-do the pinch-point and stability checks with the arms/head moving

---

## What to report back to Claude

Paste these into the next development session (text, not screenshots, where possible).
Remove anything private (Wi-Fi names are not in these outputs; API keys never are).

1. `.venv/bin/python scripts/diagnose.py --json` (after phase 8; again after phase 10)
2. `cat /proc/device-tree/model; echo; grep PRETTY_NAME /etc/os-release; uname -a; python3 -V`
3. Display: `cat /sys/class/drm/card*-HDMI-A-*/modes`, `wlr-randr` (from the desktop
   terminal), the `video=` option you used, and a photo of the face on the screen
4. Camera: `v4l2-ctl --list-devices` and `v4l2-ctl -d /dev/videoN --list-formats-ext`
5. Audio: `aplay -l`, `arecord -l`, `pactl list short sinks`, `pactl list short sources`
6. Output of `scripts/check_audio.py` / `scripts/check_camera.py` list modes
7. Power: `vcgencmd get_throttled`, `vcgencmd get_config usb_max_current_enable`,
   `vcgencmd pmic_read_adc EXT5V_V` - on the PSU and on the power bank, plus the power
   bank model and the phase-10 results
8. Performance: the `perf_monitor.py` summaries (`data/perf/*.summary.json`) for each
   scenario and the `benchmark_face.py` output
9. Logs from any failure: `journalctl --user -u littlebot -b | tail -100`,
   `tail -50 data/logs/littlebot.log`, `journalctl -k -b | tail -50`
10. Touch (if tried): `dmesg | grep -i ads7846`, `grep -A5 -i touch /proc/bus/input/devices`
11. Physical notes: does the screen clear the cooler, enclosure temperature, anything
    that did not fit, and the answers you wrote in the blanks above
12. Servos (phase 13 only): `.venv/bin/python scripts/diagnose.py --section config --section i2c --i2c-scan --json`
    and the `scripts/calibrate_servos.py` output (`config/calibration.yaml` contains no secrets)
