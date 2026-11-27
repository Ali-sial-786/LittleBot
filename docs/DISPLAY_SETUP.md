# Display setup: the GeeekPi 7" 1024x600 touch screen on the Raspberry Pi 5

LittleBot's screen is a **GeeekPi 7" capacitive touch screen**: a 1024x600 IPS panel with an HDMI input,
5-point capacitive touch over USB and two small speakers of its own. It replaced the 3.5" DORHEA MPI3508
(480x320, plugged onto the GPIO header) in November 2026; the end of this page says what to undo from that
screen.

| Part | How it connects | Status |
|---|---|---|
| **Picture** | HDMI: micro-HDMI (the Pi 5's HDMI0) to the screen's HDMI socket | standard KMS driver, 1024x600 at 60 Hz - **UNVERIFIED on the robot** |
| **Touch** | USB: the screen's touch port to one of the Pi's USB-A ports (a standard USB HID multi-touch device, no driver, no overlay) | **UNVERIFIED on the robot**; optional, LittleBot works without touch |
| **Power** | on these boards the touch USB port usually powers the screen too (some have a separate 5 V power port) | budget it: [POWER.md](POWER.md) |
| **Speakers** | fed by the HDMI audio | optional: LittleBot keeps its USB speaker (section 6) |

Panel facts: 1024x600 pixels on about 154 x 86 mm of glass (7" diagonal), so **one pixel is about
0.15 mm**. The GPIO header is no longer used by the screen: all 40 pins are free (the servo board's I2C pins
are easy to reach now, [MOTION_SAFETY.md](MOTION_SAFETY.md)).

## What LittleBot draws on it

* `display.width` x `display.height` = **1024 x 600**: the screen in pixels (`config/default.yaml`).
* **Cards and games** draw on a 1024x600 canvas, pixel for pixel: nothing is scaled on the robot.
  Text sizes are set for reading across a room (body text 36 px, about 3.6 mm tall on the glass; at most
  8 lines at a time): see the readability rules in [SCREENS.md](SCREENS.md).
* **The face** is designed 320 units tall and as wide as the screen's shape (546x320,
  `littlebot/face/frame.py` `face_size`) and drawn straight at 1024x600, sharp: it fills the screen edge
  to edge with no bars. (On the 3:2 3.5" screen it was 480x320.)
* The **YouTube player** and the **browser view** are 1024x600 windows; the browser draws pages 1.25x
  bigger (`browser.zoom`; "zoom in" / "zoom out" change it).
* Edge swipes (volume on the right edge, brightness on the left) start within about 15 mm of the edge.

## 1. Connect it (Pi off)

1. **HDMI**: a micro-HDMI to HDMI cable from the Pi 5's **HDMI0** (the micro-HDMI next to the USB-C power
   socket; in Linux the connector `HDMI-A-1`) to the screen's HDMI socket. The other port (`HDMI-A-2`) works
   too: LittleBot finds the port the screen is on by itself (`display.output_name: auto`).
2. **USB**: the screen's touch / power USB port to one of the Pi's USB-A ports. A black USB 2 port is
   enough for touch; keep the camera on a blue USB 3 port ([POWER.md](POWER.md)).
3. Never plug or unplug anything on the GPIO header while the Pi is powered.

## 2. First boot: check the mode

Boot with no display settings changed, then from SSH or a terminal:

```bash
for c in /sys/class/drm/card*-HDMI-A-*; do echo "$c: $(cat $c/status)"; head -5 $c/modes; done
.venv/bin/python scripts/diagnose.py --section display     # EDID name, modes, preferred mode
wlr-randr                                                   # in the desktop terminal: the active mode
```

* **1024x600 is listed and current**: done. LittleBot also asks for it at start-up (`display.output_mode:
  "1024x600@60"` in `config/pi.yaml`), so a desktop that picked another mode is put back on the panel's own
  pixels.
* **The EDID offers other modes too** (some of these boards also list 1280x720 or 1920x1080, which their
  scaler squeezes into 1024x600 and makes soft): nothing to do, LittleBot sets 1024x600. To have it from
  boot on, add `video=HDMI-A-1:1024x600@60` to the kernel command line (below).
* **Black screen / "no signal"**: force the mode with `video=HDMI-A-1:1024x600@60D` (the `D` forces the
  output on even when the screen's hot-plug signal isn't seen).

**Editing `cmdline.txt` safely**: it is `/boot/firmware/cmdline.txt` on Bookworm and Trixie. It must stay
**one single line**; add the option at the end, separated by a space. Keep a backup: if the screen stays
black, remove the option from your computer (the boot partition is FAT) or over SSH.

```bash
sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak
sudo nano /boot/firmware/cmdline.txt     # append:  video=HDMI-A-1:1024x600@60
sudo reboot
cat /proc/cmdline | tr ' ' '\n' | grep ^video=     # check it was applied
```

The Pi 5 uses the KMS driver (`dtoverlay=vc4-kms-v3d`): the old firmware settings (`hdmi_group`,
`hdmi_mode`, `hdmi_cvt`, `hdmi_timings`, `display_rotate`) are **ignored** (diagnose.py warns about them).

**Turn screen blanking off** (raspi-config -> Display Options -> Screen Blanking -> No), otherwise the face
disappears after 10 minutes. LittleBot has its own sleep face.

## 3. Touch

The touch is a USB HID multi-touch device: Raspberry Pi OS needs no driver and no overlay. Check it:

```bash
grep -B1 -A4 -i -E 'touch|ctp' /proc/bus/input/devices      # its name and eventN
sudo apt install -y evtest && sudo evtest                    # pick it, tap the screen
.venv/bin/python scripts/diagnose.py --section touch
```

With one screen the desktop (labwc) maps the touch to it by itself. LittleBot reads taps, holds and drags
as the mouse (SDL turns the first finger into mouse events): a tap makes LittleBot giggle, a finger held still
for 5 seconds anywhere opens LittleBot's settings panel (volume, brightness, dark mode, the games...; a ring
fills around the finger from the first second), a slide up or down at the edge changes the volume or the
brightness.

**If taps land in the wrong place** (usually only after rotating the screen):

```bash
.venv/bin/python scripts/calibrate_touch.py          # touch the 5 crosses, then: sudo reboot
.venv/bin/python scripts/calibrate_touch.py --reset  # undo (then reboot)
```

It stops LittleBot while it shows crosses, reads the raw touch readings and saves a libinput calibration
matrix in `/etc/udev/rules.d/99-littlebot-touch.rules` (any swap, mirror, rotation, offset or stretch). By
hand, the same rule with one of these matrices:

| Screen transform | `LIBINPUT_CALIBRATION_MATRIX` |
|---|---|
| normal | `1 0 0 0 1 0` |
| 90 | `0 -1 1 1 0 0` |
| 180 | `-1 0 1 0 -1 1` |
| 270 | `0 1 0 -1 0 1` |

```bash
sudo tee /etc/udev/rules.d/99-littlebot-touch.rules <<'EOF'
ATTRS{name}=="<the name from /proc/bus/input/devices>", ENV{LIBINPUT_CALIBRATION_MATRIX}="-1 0 1 0 -1 1"
EOF
sudo udevadm control --reload && sudo reboot
```

## 4. Rotation

LittleBot sets the picture's orientation itself at every start (`wlr-randr --transform`): the right way up, or
turned round (180 degrees) when **Turn the screen upside down** is on (the robot's settings panel: hold a finger
still for 5 seconds; or the dashboard's Face page; setting `display.flipped`). A turn left in the desktop's own
settings (Screen Configuration, or a `transform 180` in `~/.config/kanshi/config` from the old screen) is undone
at the next start; remove it there too, so the desktop doesn't turn it back when the screen is plugged in again.
If the touch then lands mirrored, rotate it to match (section 3). LittleBot always draws landscape 1024x600: a
portrait mounting is not supported.

## 5. Brightness

Most HDMI screens like this one set their backlight with their own button or knob, not from the Pi:
`/sys/class/backlight` stays empty. LittleBot then dims in software (a veil over the face, never darker than
15 %) when someone slides the left edge or uses the dashboard. Set the screen's own brightness once to a
comfortable level for the room, then leave it.

## 6. The screen's speakers

The two small speakers play the Pi's HDMI audio. LittleBot keeps using its **USB speaker**
(`audio.speaker.device: null` never picks HDMI while a USB speaker is plugged in): the echo canceller and the
barge-in were tuned with it. To try the screen's speakers, set `audio.speaker.device: "hdmi"` in
`config/local.yaml` (the Pi 5 names it `vc4-hdmi-0`), re-run `scripts/setup_echo_cancel.sh` and test a voice
chat (LittleBot must not hear itself). `scripts/check_audio.py --list` shows the device names.

## 7. Coming from the 3.5" MPI3508

Undo what the old screen needed:

* `config/local.yaml`: delete the `display:` lines `width: 480`, `height: 320` and
  `output_mode: "720x480@60"` (or `scripts/set_config.py --unset display.width`, then `display.height`,
  then `display.output_mode`). Until you do, LittleBot reads them as the 7" screen's values and logs a
  warning at start-up.
* `/boot/firmware/cmdline.txt`: remove a `video=HDMI-A-1:720x480@60` you added (or change it to
  `1024x600@60`).
* `/boot/firmware/config.txt`: remove the `dtoverlay=ads7846,...` line (and `dtparam=spi=on` unless
  something else uses SPI). With no 3.5" screen on the header, the resistive touch driver only reads noise
  and can turn it into phantom taps; `diagnose.py --section touch` warns while it is there.
* `/etc/udev/rules.d/99-littlebot-touch.rules`: an old calibration names the ADS7846 touch and does
  nothing for the new screen; delete it (`calibrate_touch.py --reset`) unless you calibrate again.

The 3D-printed head in `hardware/` was designed around the 3.5" screen plugged onto the Pi; the 7" screen
does not fit it (see `hardware/README.md`).

## 8. What to record for the next development session

- Output of `.venv/bin/python scripts/diagnose.py --section display --section touch --json`
- `cat /sys/class/drm/card*-HDMI-A-1/modes` and `wlr-randr`
- The touch device's name in `/proc/bus/input/devices`
- A photo of the face and of a card (`scripts/screen_preview.py` renders the same cards as PNGs)
- `scripts/benchmark_face.py` and `scripts/perf_monitor.py` with a card and a game on screen
- The screen's current draw on a USB power meter, at the brightness you use
