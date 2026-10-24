# Troubleshooting (Raspberry Pi)

Start every investigation with:

```bash
cd ~/LittleBot
.venv/bin/python scripts/diagnose.py            # add --verbose for details, --json to share
```

Each FAIL/WARN line has a hint. The sections below explain the usual causes.
Hardware-specific advice is **UNVERIFIED** until the robot has been built.

## Where are the logs?

| What | Where / command |
|---|---|
| App log (rotating JSON lines, 3 x 2 MB) | `data/logs/littlebot.log` (`logging.file`, relative to `data/`) |
| Autostart user service | `journalctl --user -u littlebot -b` (add `-f` to follow) |
| Headless system service | `journalctl -u littlebot -b` |
| Launcher messages ("display ready ...") | same journal, tag `littlebot` |
| Kernel (USB, under-voltage, camera, touch) | `journalctl -k -b` or `dmesg` (may need `sudo`) |
| Previous boot (after a crash/reset) | `journalctl --user -u littlebot -b -1`, `journalctl -k -b -1` |
| Pretty-print the JSON log | `tail -20 data/logs/littlebot.log \| python3 -m json.tool --json-lines` |
| **What happened lately, readable** (gaps marked, warnings, the Pi's memory and temperature now) | `python3 scripts/log_timeline.py` (`--minutes 90`, `--since 21:40`, `--all`) |

More detail: set `logging.level: DEBUG` in `config/local.yaml` or start with `--debug`.

**It stops responding for a while.** `littlebot/core/stall_watch.py` asks the main loop to answer
every second. When it doesn't within 2 s the log says `the main loop has been stuck for N s; it is
busy in: <file:line function ...>` (and `answered again after N s` when it recovers): that names the
code to fix. `LittleBot as a whole stood still` means the whole Pi froze (usually out of memory and
swapping, or a power dip). Memory low, swapping, heat and under-voltage are warned about as they
start, and a `health:` line every 5 minutes gives memory, swap, temperature, load and LittleBot's
own memory and CPU. `scripts/log_timeline.py` shows all of this in order.

**It stops answering in the middle of a voice chat** (and "Hey LittleBot" does nothing until the
chat's time limit). Every 20 s of a chat the log has a `live voice: still on` line: `mic 0 frames
sent` means the microphone stopped delivering sound. A microphone or speaker that gives nothing
for 2 s is now opened again (`the microphone stopped (nothing from it for 2 s); opening the
speaker and microphone again`); if that fails, or it stops a third time, the chat ends (`ending
(the microphone stopped)`) and the wake word listens again. It happening often points at the USB
side: `dmesg | grep -i usb | tail -30` shows resets (a weak power supply, or a hub). While the
voice chat has the speaker, the audio service no longer tests the speaker underneath it, and after
a video is closed for a chat the earcon waits until the browser has let go of the speaker.

## No picture on the screen

1. Cable in **HDMI0** (next to USB-C power)? Screen powered (backlight on)?
2. `cat /sys/class/drm/card*-HDMI-A-*/status` - `connected`? If `disconnected`, try
   another cable, or force the output with `video=HDMI-A-1:720x480@60D` (DISPLAY_SETUP.md).
3. Works on a normal monitor but not on the MPI3508 -> mode problem: DISPLAY_SETUP.md
   section 3 (try 720x480, then 640x480).
4. You ran the vendor `LCD-show` script? Restore `config.txt`/`cmdline.txt` from the
   `.bak` files (or from your computer: the boot partition is FAT) - see DISPLAY_SETUP.md.
5. Boots to a console instead of the desktop: you flashed *Lite*, or the desktop failed -
   `sudo raspi-config` -> System Options -> Boot / Auto Login -> Desktop Autologin.

## Wrong resolution / blurry / squashed face

- The panel is 480x320; anything else is scaled by the screen's own chip.
  `diagnose.py --section display` shows the EDID modes and the active one.
- 4:3 modes (640x480, 1024x768) squash a 3:2 face; 16:9 (1280x720, 1920x1080) too.
  720x480 is 3:2 and scales cleanly.
- `hdmi_cvt` / `hdmi_mode` / `display_rotate` in `config.txt` are ignored on the Pi 5.
- Face drawn in a small window or with the desktop panel visible: `display.fullscreen: true`
  in `config/local.yaml`; check `diagnose.py --section config`.

## No sound / sound from the wrong device

The Pi 5 has **no headphone jack**; sound must go to the USB speaker. HDMI audio goes to
the screen, which (most likely) has no speaker.

1. `aplay -l` - is the USB speaker listed (`USB-Audio`)? If not: re-plug, try another
   port, check `journalctl -k -b | grep -i usb` for errors or over-current.
2. Desktop volume icon (right-click) -> choose the USB device as output. Or:
   `pactl list short sinks` then `pactl set-default-sink <name>`.
   On Trixie/Bookworm the sound server is PipeWire; `wpctl status` shows the same.
   Sound keeps going to the screen: see the next section.
3. Pin it for LittleBot: `audio.speaker.device: "<name substring>"` in `config/local.yaml`
   (names from `scripts/check_audio.py --list` or `diagnose.py --section audio --verbose`).
4. Test outside the app: `speaker-test -t wav -c 2 -l 1` or
   `aplay /usr/share/sounds/alsa/Front_Center.wav` (start with low volume!).
5. Very quiet: check `alsamixer` (F6 -> the USB card) and the app volume/ceiling
   (`audio.speaker.volume`, `max_volume`, dashboard volume).
6. Crackling / dropouts at high volume: often power - see POWER.md (600 mA USB limit).

## Sound comes out of the screen (HDMI) instead of the speaker

YouTube in Chromium, `aplay` / `speaker-test` without `-D`, and LittleBot without
`audio.speaker.device` all play on the system default output. If that is HDMI:

- **One-off fix**: `wpctl status`, find the USB speaker (`UACDemoV1.0`) under
  *Audio -> Sinks*, then `wpctl set-default <ID>` with the number in front of it
  (the `*` marks the default). WirePlumber remembers this across reboots, until someone
  picks HDMI in the volume menu.
- **Permanent fix** (the installer runs it; no sudo, run it as the desktop user):

  ```bash
  scripts/set_default_speaker.sh --dry-run   # what it would do
  scripts/set_default_speaker.sh             # USB speaker + USB microphone become the defaults
  ```

  It adds a WirePlumber rule that ranks USB above HDMI
  (`~/.config/wireplumber/wireplumber.conf.d/51-littlebot-usb-audio.conf`; on Bookworm
  `~/.config/wireplumber/main.lua.d/51-littlebot-usb-audio.lua`), restarts WirePlumber
  and runs `wpctl set-default` for the USB devices. With the speaker unplugged, HDMI is
  the fallback; plug it back in and USB wins again. Wrong device (e.g. the speaker's
  own microphone)? `--speaker NAME` / `--mic NAME` (part of the name). Undo: `--undo`.
- Check: `wpctl inspect @DEFAULT_AUDIO_SINK@ | grep node.description`.
- Only one app still uses HDMI: it was moved there by hand. Move it back in the volume
  menu, or restart the app.

## Microphone not found / not hearing

1. `arecord -l` lists the USB microphone? Record 3 s and play it back:
   `arecord -d 3 -f S16_LE -r 16000 /tmp/t.wav && aplay /tmp/t.wav` (delete it afterwards).
2. `alsamixer` -> F6 -> USB mic -> F4 (capture): is capture muted or very low?
3. Set `audio.microphone.device` to its name substring.
4. The parent dashboard can switch the microphone off (privacy) - check it is enabled.
5. `sounddevice unavailable: PortAudio library not found` -> `sudo apt install libportaudio2`.

## Camera busy or not found

1. `v4l2-ctl --list-devices` - the USB camera appears with (usually) two `/dev/videoN`
   nodes; the first is the image stream. On the Pi 5, `/dev/video19` and up are the ISP /
   decoder (`pispbe`, `rpi-hevc-dec`) - **not** cameras.
2. "Device busy": another program has it. `diagnose.py --section camera` shows which
   process; often a second LittleBot (user service + manual start, or user + system service).
   `fuser -v /dev/video0` also tells you.
3. Permission denied: `groups` must include `video` (installer adds it; log out/in).
4. Blank or very slow frames: use MJPG at 640x480 (`camera.fourcc: MJPG`, the default);
   use a blue USB 3 port; check power (USB limit).
5. The dashboard's camera privacy switch may be off.
6. Face detection never triggers: `diagnose.py --section camera` reports which detector
   (Haar / YuNet) is usable; with the OpenCV 5 pip wheel Haar is unavailable and YuNet
   needs its model file (`scripts/download_models.py`, if present).

## Dashboard unreachable from the phone

1. Is it running? `curl -sI http://127.0.0.1:8765/ | head -1` on the Pi.
2. Phone access is **off by default**. `diagnose.py --section network` says
   "phone access disabled" until you set `dashboard.lan_access: true` (and restart).
3. Same network? The phone must be on the same Wi-Fi - not mobile data, not a guest
   network (guest networks usually block device-to-device traffic).
4. Use the IP shown by diagnose (`http://192.168.x.y:8765`). `littlebot.local` needs mDNS:
   works on iPhone/macOS, on Android only in some browsers - use the IP there.
5. Firewall: Raspberry Pi OS has none by default; if you added `ufw`, allow 8765 from your LAN only.
6. Login problems: the first-run setup code is in the log (`grep -i setup data/logs/littlebot.log`).

## High CPU or temperature

1. `vcgencmd measure_temp` - above ~80 C the Pi 5 throttles. Is the active cooler's fan
   spinning under load (`diagnose.py --section power` shows RPM)? Is the fan cable in the
   FAN connector? Does the screen or enclosure block the airflow?
2. `perf_monitor.py` (PERFORMANCE.md) shows whether LittleBot or Chromium is the load.
3. Reduce load in `config/local.yaml`: `display.fps: 24`, `display.idle_fps: 10`,
   `camera.detect_fps: 3`, `camera.capture_fps: 10`.
4. YouTube in Chromium is the heaviest part (software video decoding at 480p/720p on a
   480x320 screen); keep video sessions short (that is also the parental default).

## Under-voltage / "power supply is not capable of supplying 5A"

- `vcgencmd get_throttled` not `0x0`, a lightning-bolt icon, or the 5 A warning popup:
  read [POWER.md](POWER.md). Use the official 27 W PSU for bring-up; test power banks
  with the procedure there. Symptoms of bad power: USB devices disappearing, random
  reboots, SD card errors, sound crackles.

## Service won't start (autostart)

1. `systemctl --user status littlebot` and `journalctl --user -u littlebot -b | tail -50`.
2. "no desktop session after 90s": Desktop Autologin is not enabled, or you only logged
   in over SSH. Enable it in raspi-config, or start it from the desktop session.
3. "start request repeated too quickly": it failed 5 times in 10 minutes. Fix the cause,
   then `systemctl --user reset-failed littlebot && systemctl --user start littlebot`.
4. `.venv/bin/python not found`: re-run `scripts/install_pi.sh`.
5. Works when started by hand but not as a service: compare environments - the service
   reads `~/.config/littlebot/littlebot.env`; try `SDL_VIDEODRIVER=x11` there.
6. Window opens on the wrong screen or not at all under Wayland: set
   `SDL_VIDEODRIVER=x11` (uses Xwayland) or `SDL_VIDEODRIVER=wayland` in that file.
7. Two copies running: `systemctl is-enabled littlebot` (system) and
   `systemctl --user is-enabled littlebot` - keep only one enabled.

## Out of memory / Chromium killed

- `diagnose.py --section memory` shows available RAM, swap (zram on Trixie via
  `rpi-swap`, a swap file via `dphys-swapfile` on Bookworm) and OOM kills this boot.
- Close the Pi's own browser tabs; don't run the desktop browser and the kiosk at once.
- See PERFORMANCE.md for the memory budget.

## SD card full or read-only

- `df -h /` - free space with `sudo apt clean`, `journalctl --vacuum-size=100M`, deleting
  old `data/perf/*.csv`.
- A file system that suddenly turns read-only usually means a failing card or a power
  problem: back up `data/` and `config/` immediately (PI_SETUP.md, Backups).

## ChatGPT sign-in or live voice

See the troubleshooting table in [CHATGPT.md](CHATGPT.md#5-troubleshooting): code sign-in off,
expired sign-in, "not available for this account or region" (403), plan usage limit (429),
endpoint changed (404), "Not installed on this robot yet", echo, volume.

## Anything else

Collect and share: `diagnose.py --json`, the last 100 lines of
`journalctl --user -u littlebot -b`, `journalctl -k -b | tail -100`, and what you did just
before the problem.
