# Raspberry Pi setup

How to go from an empty microSD card to LittleBot running on the Raspberry Pi 5 (2 GB)
with its 3.5" HDMI screen, USB camera, USB microphone and USB speaker.

> **Status: written before the hardware arrived.** Commands are standard Raspberry Pi
> OS procedure, but every LittleBot-specific step is **UNVERIFIED on the real robot**
> until someone works through [HARDWARE_ARRIVAL_CHECKLIST.md](../HARDWARE_ARRIVAL_CHECKLIST.md).
> Steps that differ between Raspberry Pi OS **Bookworm** (Debian 12, Python 3.11) and
> **Trixie** (Debian 13, Python 3.13) are marked.

Related guides: [DISPLAY_SETUP.md](DISPLAY_SETUP.md) (screen and touch),
[POWER.md](POWER.md) (power supply and power bank), [TROUBLESHOOTING.md](TROUBLESHOOTING.md),
[PERFORMANCE.md](PERFORMANCE.md), and [MOTION_SAFETY.md](MOTION_SAFETY.md) (servos, calibration;
format reference `config/calibration.example.yaml`).

---

## 0. What you need

- Raspberry Pi 5 (2 GB) with the official active cooler fitted.
- Patriot 64 GB A1/V30 microSD card and a card reader for your computer.
- **For bring-up: the official Raspberry Pi 27 W USB-C power supply** (5.1 V / 5 A).
  Test the power bank only later, following [POWER.md](POWER.md).
- Micro-HDMI to HDMI cable and the MPI3508 screen. A normal HDMI monitor/TV is handy
  for the very first boot.
- USB keyboard + mouse for the first boot (or set up SSH in Imager and skip them).
- Your home Wi-Fi name and password.

## 1. Flash the SD card (Raspberry Pi Imager)

1. Install **Raspberry Pi Imager** on your computer (raspberrypi.com/software).
2. *Choose Device*: **Raspberry Pi 5**.
3. *Choose OS*: **Raspberry Pi OS (64-bit)** - the one *with desktop* (not Lite: the
   face window and the YouTube player need the desktop; not 32-bit: the Python wheels
   for pygame-ce and OpenCV are 64-bit only).
4. *Choose Storage*: the Patriot card. Double-check it is not another drive.
5. *Edit settings* (OS customisation) - fill in **all** of these:
   - Hostname: `littlebot` (the dashboard will then be at `littlebot.local`).
   - Username + a strong password (write it down). Examples below use `pi`; use yours.
   - Wireless LAN: your Wi-Fi SSID and password, **Wireless LAN country** set correctly.
   - Locale: time zone and keyboard layout.
   - Services: **Enable SSH** (password authentication is fine at home, a public key is better).
6. Write and verify. Eject the card.

## 2. First boot

1. Insert the card, connect the screen (micro-HDMI port **HDMI0, next to the USB-C
   power socket**), keyboard/mouse, and *finally* the official power supply.
2. The first boot resizes the file system and reboots once; give it 2-3 minutes.
3. If the small screen shows nothing or a garbled picture, use a normal monitor for now
   and come back to [DISPLAY_SETUP.md](DISPLAY_SETUP.md) later. Everything below also
   works over SSH: `ssh pi@littlebot.local`.
4. Quick health check (before installing anything):

   ```bash
   cat /proc/device-tree/model; echo        # Raspberry Pi 5 Model B ...
   grep -E 'PRETTY_NAME|VERSION_CODENAME' /etc/os-release   # bookworm or trixie
   vcgencmd get_throttled                   # throttled=0x0 is what you want
   vcgencmd measure_temp
   free -h; df -h /
   ```

   If `get_throttled` is not `0x0`, read [POWER.md](POWER.md) before going further.

## 3. Update the system

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

Optionally update the bootloader EEPROM (the Pi 5 ships with a working one; updates
improve USB-PD and power handling):

```bash
sudo rpi-eeprom-update            # shows current vs latest
sudo rpi-eeprom-update -a && sudo reboot   # only if an update is offered
```

## 4. Get the code

```bash
sudo apt install -y git
cd ~
git clone <your LittleBot repository URL> LittleBot
cd LittleBot
```

(Or copy the folder from your computer with `scp -r LittleBot pi@littlebot.local:~/`.
Do **not** copy a `.venv` folder from another computer - it will not work on the Pi.)

## 5. Run the installer

```bash
scripts/install_pi.sh --dry-run     # read the plan first; changes nothing
scripts/install_pi.sh               # do it (asks before starting)
```

What it does (idempotent - safe to run again after `git pull`):

| Step | Details |
|---|---|
| Checks | Raspberry Pi OS / Debian, 64-bit, Python >= 3.11, free space, not run as root |
| apt packages | `python3-venv python3-dev python3-pip libportaudio2 espeak-ng v4l-utils alsa-utils pulseaudio-utils i2c-tools wlr-randr fonts-dejavu-core` + Chromium |
| Chromium | Bookworm: `chromium-browser` (Raspberry Pi's build); Trixie: `chromium`. Detected with `apt-cache policy`; skipped if already installed or with `--no-chromium` |
| Groups | adds you to `video audio input render` only if missing (log out/in afterwards) |
| `.venv` | created once, never deleted or recreated; `pip install -e ".[vision,audio,pi]"` |
| Live voice | `pip install -e ".[live]"` as its own step: aiortc for GPT-Live voice ([CHATGPT.md](CHATGPT.md)), prebuilt aarch64 wheels (~40 MB, nothing compiled). A failure only warns; `--no-live` skips it |
| Face and hand models | `scripts/download_models.py yunet hands sface` (vision module; ~47 MB into `models/`: the face detector, the hand models that count fingers on the robot and SFace, which recognises family members only once a parent switches it on (section 8); skipped when already verified; needs internet; `--no-models` skips) |
| Wake word | "Hey LittleBot" starts live voice ([CHATGPT.md](CHATGPT.md#wake-word-hey-littlebot)), heard offline on the robot: `pip install -e ".[local-stt]"` (vosk, one prebuilt aarch64 wheel, ~3 MB) and `scripts/download_models.py vosk-en` (40 MB download, unpacked to `models/vosk-model-small-en-us-0.15/`, ~140 MB RAM while the wake word is on). A failure only warns; `--no-wake-word` skips both, `--no-models` only the model |
| Files | `data/logs/`; `config/local.yaml` from `config/hardware.example.yaml` and `.env` from `.env.example` **only if missing**; `.env` set to mode 600 |
| Sound output | `scripts/set_default_speaker.sh`: the USB speaker and USB microphone become the default sound devices instead of the HDMI screen (a WirePlumber rule in `~/.config/wireplumber` + `wpctl set-default`; no sudo). A failure only warns; `--no-audio-default` skips it |
| Echo cancelling (opt-in) | Only with `--echo-cancel`: the apt packages `pipewire-alsa libspa-0.2-modules libpipewire-0.3-modules`, then `scripts/setup_echo_cancel.sh` (PipeWire's WebRTC echo canceller between the USB microphone and speaker, so a grown-up can talk over the live voice; [Echo cancelling](#echo-cancelling)). Untested on a real Pi; a failure only warns |
| Never | edits `config.txt`/`cmdline.txt`, enables I2C (unless `--enable-i2c`), enables servos |

Options: `--with-service` (autostart, section 10), `--headless-service`, `--enable-i2c`
(later, for the servo board), `--system-opencv`, `--dev` (pytest on the Pi),
`--skip-apt`, `--no-chromium`, `--no-models`, `--no-live`, `--no-wake-word`, `--no-audio-default`,
`--echo-cancel` (section [Echo cancelling](#echo-cancelling)), `--yes`. See `scripts/install_pi.sh --help`.

The wake word keeps the USB microphone open while it is switched on (**AI** → **Live voice**), and
hands it over to push to talk, the microphone test and live voice. On a 2 GB Pi it costs about
140 MB of RAM and roughly a tenth of one CPU core; turn the switch off if you don't use it.

### Why a plain venv with pip wheels (and when to use `--system-opencv`)

The default venv is isolated (no `--system-site-packages`). PyPI has ready-made
**aarch64 wheels** for everything LittleBot needs (pygame-ce, numpy,
opencv-python-headless, pydantic-core, psutil), so nothing is compiled, versions match
the development machine, and an `apt upgrade` can never break the app's Python packages.
The installer passes `--only-binary` for numpy / pygame-ce / OpenCV (and for PyAV, pylibsrtp,
cryptography and google-crc32c in the live-voice step) so that, if a wheel is ever missing, pip
fails within seconds instead of compiling for an hour and running a 2 GB Pi out of memory.

`--system-opencv` instead uses Debian's `python3-opencv` (+ `opencv-data` for the Haar
cascade files) through a `--system-site-packages` venv. Choose it only if the pip
wheel causes problems: it saves a ~50 MB download but ties the app to the OS's numpy
and OpenCV versions (OpenCV 4.x on Bookworm/Trixie, which still has `CascadeClassifier`;
the OpenCV 5 pip wheels do not - the vision module then uses YuNet, whose model the
installer downloads with `scripts/download_models.py`). Runtime RAM use is about the same either way.
Switching later means moving `.venv` aside and re-running the installer.

## 6. Check everything

```bash
.venv/bin/python scripts/diagnose.py          # PASS/WARN/FAIL/SKIP per area
.venv/bin/python scripts/diagnose.py --json > diagnose.json   # to paste into a report
```

`diagnose.py` only reads: it never records audio, captures images, writes to I2C or moves
servos, and it reports API keys as present/absent only. Fix FAIL lines first; each has a
hint, and [TROUBLESHOOTING.md](TROUBLESHOOTING.md) covers the usual causes.

Then use the per-device tools written by the subsystem modules (run each with `--help`
first; diagnose.py runs their `--list` mode automatically when they support it):

```bash
.venv/bin/python scripts/check_audio.py --help    # list/test microphone and speaker
.venv/bin/python scripts/check_camera.py --help   # list/test the camera
.venv/bin/python scripts/face_preview.py --help   # face rendering on this screen
```

Sound from the screen (HDMI) instead of the USB speaker: `wpctl status`, then
`wpctl set-default <ID>` with the number in front of the USB speaker under *Audio -> Sinks*
(one-off), or `scripts/set_default_speaker.sh` to make USB the default for good - details in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#sound-comes-out-of-the-screen-hdmi-instead-of-the-speaker).

### Echo cancelling

**Optional, and untested on a real Pi.** Without it the live voice is one at a time: the
microphone is shut while LittleBot talks, so nobody can interrupt it. With PipeWire's echo
canceller (`libpipewire-module-echo-cancel`, WebRTC) LittleBot's own voice is taken out of what the
microphone hears, and a grown-up can talk over the voice to stop it ([CHATGPT.md](CHATGPT.md#talking-over-it-barge-in)).
It also takes some of a YouTube video's sound out of what the wake word hears.

As the desktop user (no sudo; the script asks for nothing):

```bash
cd ~/LittleBot
scripts/install_pi.sh --echo-cancel --yes      # the packages + the setup below, or only the setup:
scripts/setup_echo_cancel.sh --dry-run         # what it would do and the files it would write
scripts/setup_echo_cancel.sh                   # set it up (safe to run again)
scripts/setup_echo_cancel.sh --check           # PASS / WARN / FAIL per item, changes nothing
```

What it does (all user-level, nothing in `/etc`):

* writes `~/.config/pipewire/pipewire.conf.d/60-littlebot-echo-cancel.conf`: the echo canceller between
  the USB microphone ("USB PnP Sound Device") and the USB speaker ("UACDemoV1.0"), found with
  `pw-dump` (`--mic NAME` / `--speaker NAME` to pick others). It makes a microphone
  `littlebot_ec_mic` and a speaker `littlebot_ec_speaker` ("LittleBot microphone / speaker (echo
  cancelled)" in `wpctl status`);
* adds a marked block to `~/.config/alsa/asoundrc` (ALSA reads it as well as `~/.asoundrc`, which
  something on Raspberry Pi OS deleted at boot; the rest of the file is kept) with two ALSA devices of the
  same names, `type pipewire` (package `pipewire-alsa`), so PortAudio, and so LittleBot, can open them;
* restarts PipeWire (`systemctl --user restart pipewire pipewire-pulse wireplumber`; sound stops for a
  moment), makes the two the default sound devices (`wpctl set-default`, so Chromium's YouTube sound
  goes through the canceller too; `--no-default` doesn't), restarts LittleBot if it runs as a user
  service, and runs `--check`.

LittleBot then uses them by itself (`audio.echo_cancel: auto`, with the speaker and microphone
device left on automatic) and says so in its journal: `journalctl --user-unit littlebot | grep "echo
cancelling"` shows "audio: echo cancelling on (PipeWire: littlebot_ec_mic / littlebot_ec_speaker)".
A device named in `config/local.yaml` (`audio.speaker.device: "UACDemo"` for example) wins over them.

Check by hand: `wpctl status` (the two under *Sinks* and *Sources*, marked `*` as default),
`pw-cli ls Node | grep littlebot_ec`, `arecord -L | grep -A1 littlebot_ec`, `aplay -L | grep -A1 littlebot_ec`,
`.venv/bin/python scripts/check_audio.py --list`.

Undo: `scripts/setup_echo_cancel.sh --remove` (removes the file and the `~/.config/alsa/asoundrc` block,
restarts PipeWire and makes the USB devices the defaults again with `scripts/set_default_speaker.sh`).
To keep it set up but not use it: `audio.echo_cancel: off` or only `chatgpt.live_barge_in: off`.

What to check on the Pi first (none of it has been tried on real hardware): `--check` passes; the
voice still sounds right through the canceller; in a live call, talking over
the voice stops it and it doesn't stop by itself ([CHATGPT.md troubleshooting](CHATGPT.md#7-troubleshooting));
YouTube still plays; the wake word still works; `top` shows PipeWire's CPU (the WebRTC canceller is
expected to take a few percent of one core).

## 7. Configure `config/local.yaml`

`config/local.yaml` is this robot's own settings file (git-ignored, never overwritten by
the installer). It starts as a copy of the annotated `config/hardware.example.yaml`.

```bash
nano config/local.yaml
.venv/bin/python scripts/diagnose.py --section config --section audio --section camera
```

Typical edits after running the check scripts:

- `audio.microphone.device` / `audio.speaker.device`: a **name substring** from the
  device list (more robust than an index, which can change when USB devices move).
- `camera.device`: `auto` normally finds the USB camera; set an index/path if not.
- `audio.speaker.max_volume`: lower it if the speaker is loud at close range.
- Keep `motion.physical_enabled: false` and `motion.backend: mock` - servos come last and
  follow [MOTION_SAFETY.md](MOTION_SAFETY.md): calibration with `scripts/calibrate_servos.py`
  runs with `physical_enabled: true` while `backend` is still `mock`, writes
  `config/calibration.yaml` (format: `config/calibration.example.yaml`), and only then
  `backend: pca9685`.

API keys (only if you will use cloud AI) go into `.env`, never into YAML. Cloud AI
stays off until a parent enables it in the dashboard, even with a key present.

## 8. First run

From the desktop (a terminal on the Pi, so the window can open on the screen):

```bash
cd ~/LittleBot
.venv/bin/python -m littlebot --mode pi
```

Over SSH, point it at the desktop session first (labwc on Raspberry Pi OS normally uses
`wayland-0`; Xwayland is `:0`):

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 DISPLAY=:0
.venv/bin/python -m littlebot --mode pi
```

Quit with Ctrl+C (or `scripts/safe_shutdown.sh --app-only`). Useful variants:
`--debug` (debug log + face overlay), `--headless` (no window; dashboard only),
`--run-for 60` (exit after 60 s).

### First-run dashboard password

There is no default password. On first start the dashboard prints a **one-time setup
code** in the app's console/log. Read it on the Pi and open the dashboard:

```bash
journalctl --user-unit littlebot -b | grep -i setup      # when running as a service
grep -i setup data/logs/littlebot.log | tail -3        # or from the app's log file
```

Open http://127.0.0.1:8765 in the Pi's browser, enter the code and choose a strong password.

### Recognising family members (optional)

So the voice chat knows who is in front of the camera ("Ali (dad)", "Amara (the baby)") and uses
their names. Everything runs on the robot; only names and 128 numbers per face sample are kept
(details: [AI_SAFETY.md §4](AI_SAFETY.md#4-privacy)). Off by default.

1. The model (the installer gets it; else, once): `.venv/bin/python scripts/download_models.py faces`
   (YuNet + SFace, 39 MB, SHA-256 checked). `--list` shows whether both are `verified`.
2. Dashboard **Privacy** → switch on **Recognise family members** (the camera must be on too).
3. Dashboard **Profile** → **Who's who**: add each person with a name and a few words ("Ali", "dad";
   "Amara", "the baby"), then press **Take a sample** about 5 times with only that person in front
   of the camera, about an arm's length away, facing it, in good light. Each press shows what the
   camera saw (green box: kept as numbers; the photo itself is not kept).
4. Press **Who's in view?** to check: it lists each face as a name or "someone LittleBot doesn't
   know", with its match score and the score needed (`vision.face_match_threshold`, 0.45).
5. Take new samples of the baby every month or so (the card says when it's time).

In the journal (`journalctl --user-unit littlebot -f`), look for `face recognition: models loaded in
... ms`, `face recognition: sample kept (N for that person)` and, in a voice chat,
`live voice: face recognition is on (N face(s) in view, N known)`, `live voice: N known face(s) in
view at the start` and `live voice: told who is in view (N known)`. `face recognition can't run
(no_models)` means step 1 is missing. Names and face numbers are never logged.

### Recognising family voices (optional)

So the voice chat knows which grown-up is talking, from "Hey LittleBot" on. Runs on the robot; only
voiceprints (192 numbers per sample) are kept, never recordings ([VOICES.md](VOICES.md)). Off by
default.

1. Once: `.venv/bin/python -m pip install -e '.[voices]'` (sherpa-onnx, prebuilt for the Pi) and
   `.venv/bin/python scripts/download_models.py voices` (26 MB, SHA-256 checked), then
   `systemctl --user restart littlebot`.
2. Dashboard **Privacy** → switch on **Recognise family voices** (the microphone must be on too).
3. Dashboard **Profile** → **Who's who** → **Record voice** next to each grown-up: they read the three
   sentences on the screen to the robot (about 20 s), alone, in a quiet room.

In the journal, look for `voice recognition: model loaded in ... ms`, `voice recognition: recording
kept (...)` and, in a voice chat, `live voice: the wake phrase was recognised by voice in ... ms` and
`live voice: told who is talking, by voice`. Names and voiceprints are never logged.

## 9. Use the dashboard from your phone (LAN opt-in)

By default the dashboard listens on `127.0.0.1` only, so nothing on the network can
reach it. To use your phone on the **same home Wi-Fi**:

1. Set the dashboard password first (above).
2. In `config/local.yaml`: `dashboard: { lan_access: true }` (or start once with `--lan`).
3. Restart LittleBot, then run `.venv/bin/python scripts/diagnose.py --section network` -
   it prints the phone URL, e.g. `http://192.168.1.50:8765` or `http://littlebot.local:8765`.
4. Do **not** port-forward 8765 on your router and do not enable it on guest/public Wi-Fi.
   Guest networks often isolate devices, so the phone cannot reach the Pi there anyway.

Tip: give the Pi a fixed address (DHCP reservation in your router) so the URL never changes.

## 10. Start automatically (autostart)

```bash
scripts/install_pi.sh --with-service
sudo raspi-config    # System Options -> Boot / Auto Login -> Desktop Autologin
sudo reboot
```

The face window needs the graphical session, so LittleBot runs **inside your desktop
login**. `--with-service` installs two cooperating pieces:

1. **systemd user service** `~/.config/systemd/user/littlebot.service` (from
   `scripts/systemd/littlebot.service`): restarts LittleBot after a crash with a
   back-off (5 s growing to 60 s on Trixie; fixed 5 s on Bookworm, whose systemd 252
   ignores `RestartSteps`), gives up after 5 failures in 10 minutes, logs to the journal,
   stops it with SIGTERM (servos de-energised), and runs `scripts/systemd/littlebot-launch.sh`,
   which waits up to 90 s for the Wayland socket / X display and exports
   `WAYLAND_DISPLAY`/`DISPLAY` itself.
2. **XDG autostart entry** `~/.config/autostart/littlebot.desktop`: when the desktop
   starts, it imports the session variables into systemd and starts the service.

Why both: labwc does **not** activate `graphical-session.target` on its own
([labwc(1)](https://labwc.github.io/labwc.1.html)), so a unit that only waits for that
target would never start. The unit is therefore wanted by `default.target` (your user
manager starts at autologin) and the launcher waits for the display; the autostart entry
makes sure the display variables are right. Starting an already running service is a no-op.

Everyday commands (no sudo - it is your user's service):

```bash
systemctl --user status littlebot
journalctl --user-unit littlebot -f            # live log (also data/logs/littlebot.log)
systemctl --user restart littlebot
systemctl --user stop littlebot
systemctl --user disable littlebot && rm ~/.config/autostart/littlebot.desktop   # turn autostart off
```

Overrides without editing the unit: `~/.config/littlebot/littlebot.env`
(e.g. `SDL_VIDEODRIVER=x11`, `LITTLEBOT_ARGS=--debug`), then `systemctl --user restart littlebot`.

### Alternatives and trade-offs

| Method | Pros | Cons |
|---|---|---|
| **systemd user service + XDG autostart** (default, `--with-service`) | auto-restart with back-off, journald logs, clean `systemctl stop` (used by `safe_shutdown.sh`), works with labwc and wayfire | two small files; needs Desktop Autologin |
| **labwc autostart line** (`scripts/autostart/labwc-autostart.snippet`) | runs exactly inside the compositor with the right environment | by default labwc reads only the *first* autostart file it finds, so creating `~/.config/labwc/autostart` **replaces** `/etc/xdg/labwc/autostart` (desktop, panel) unless you copy it first ([labwc-config(5)](https://labwc.github.io/labwc-config.5.html)); labwc-only |
| **XDG autostart running the launcher directly** (what `littlebot-session-start.sh` falls back to without the unit) | no systemd at all | no automatic restart; logs only via `systemd-cat` |
| **Headless system service** (`--headless-service`, `scripts/littlebot.service`) | starts at boot without anyone logged in | **no face window**, audio through PipeWire needs `loginctl enable-linger` (UNVERIFIED); for bring-up or screenless use |

Never enable the user service and the headless system service together: two copies
would fight over the camera and microphone (`diagnose.py` warns about this).

## 11. Updating

```bash
cd ~/LittleBot
systemctl --user stop littlebot          # if autostart is installed
git pull
scripts/install_pi.sh --yes              # picks up new dependencies; keeps your config/.env/.venv
.venv/bin/python scripts/diagnose.py
systemctl --user start littlebot
```

Compare `config/hardware.example.yaml` with your `config/local.yaml` after updates
(`diff config/hardware.example.yaml config/local.yaml`) for new options.

## 12. Backups

What is worth keeping (everything else can be re-installed):

| Path | Contents |
|---|---|
| `data/littlebot.db` | parent settings, dashboard password hash, allowlists, child profile, Who's who face samples (if any) |
| `config/local.yaml`, `config/calibration.yaml` | this robot's hardware settings and servo calibration |
| `.env` | API keys (secret: store the backup safely) |

The ChatGPT sign-in (`data/chatgpt_auth.json`) is deliberately left out: after a restore, sign
in again on the AI page ([CHATGPT.md](CHATGPT.md)).

```bash
systemctl --user stop littlebot     # so the database is not being written
tar czf ~/littlebot-backup-$(date +%F).tar.gz -C ~/LittleBot data/littlebot.db config/local.yaml config/calibration.yaml .env 2>/dev/null
systemctl --user start littlebot
scp pi@littlebot.local:~/littlebot-backup-*.tar.gz .     # from your computer
```

A full SD card image (Imager/`dd` on your computer) is the quickest way back after a card failure.

## 13. Shutting down

Always use `scripts/safe_shutdown.sh` (or the desktop's Shutdown menu) - never pull the
power while the Pi is running. Details in [POWER.md](POWER.md#6-safe-shutdown).
