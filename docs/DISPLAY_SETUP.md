# Display setup: DORHEA MPI3508 3.5" HDMI screen on the Raspberry Pi 5

The MPI3508 ("3.5inch HDMI Display-B") is two separate devices on one board:

| Part | How it connects | Status on Pi 5 |
|---|---|---|
| **Picture** | HDMI (micro-HDMI to HDMI cable from the Pi 5) | should work through the standard KMS driver; the mode needs care (below) - **UNVERIFIED** |
| **Power** | 5 V from the GPIO header (pins 2/4, GND pin 6) when plugged onto the header | standard |
| **Resistive touch** | SPI on the GPIO header (an XPT2046/ADS7846-type controller, driven by the kernel `ads7846` overlay) | **UNVERIFIED on Pi 5**; optional - LittleBot works without touch |

Panel facts (vendor page, [lcdwiki 3.5inch HDMI Display-B](http://www.lcdwiki.com/3.5inch_HDMI_Display-B)):
native **480x320**, HDMI input accepted "480x320 ~ 1920x1080" (the board has a scaler),
resistive touch. Bring the picture up first; leave touch for later.

> **Do NOT run the vendor's `LCD-show` / `MPI3508-show` script.** It was written for
> older Pis and the X11 desktop. Reading the script
> ([goodtft/LCD-show, MPI3508-show](https://github.com/goodtft/LCD-show/blob/master/MPI3508-show))
> shows that it overwrites `/boot/config.txt` wholesale with its own copy, edits
> `cmdline.txt`, installs X11 `xserver-xorg-input-evdev` and an X11 calibration file,
> copies its own `/etc/inittab`, appends an `xrandr` call to the LXDE (X11) autostart
> and reboots. On Raspberry Pi OS Bookworm/Trixie (Wayland/labwc, config in
> `/boot/firmware/`) that can leave you with a broken or unbootable setup, and none of the
> X11 parts apply. We only borrow its *parameters* (touch overlay values, mode choice) below.

---

## 1. Physical connection and fit

1. Use the Pi 5's **HDMI0** port (the micro-HDMI next to the USB-C power socket). In Linux
   it is the connector **`HDMI-A-1`**; HDMI1 is `HDMI-A-2`.
2. The Pi 5 has micro-HDMI, so the screen's rigid U-shaped HDMI adapter (made for full-size
   HDMI on Pi 3/4) does not fit: use the micro-HDMI to HDMI cable.
3. **Clearance with the active cooler (UNVERIFIED):** mounted directly on the GPIO header,
   the screen sits a few millimetres above the board - check that it does not touch the
   cooler's fan and that the fan can still breathe. If it is tight, use a GPIO stacking
   header/extender or connect only the needed pins with short jumper wires (5V, GND for
   power; plus the SPI pins later for touch). A blocked fan means thermal throttling.
4. Never connect or disconnect anything on the GPIO header while the Pi is powered.

## 2. First boot: see what the screen offers (EDID)

Boot with no display settings changed. Then, from SSH or a terminal:

```bash
for c in /sys/class/drm/card*-HDMI-A-*; do echo "$c: $(cat $c/status)"; head -5 $c/modes; done
.venv/bin/python scripts/diagnose.py --section display     # also decodes the EDID name / preferred mode
wlr-randr                                                   # in the desktop terminal: active mode
```

Three possible outcomes:

- **The EDID lists `480x320`** -> select it (section 3, option A) and you are done.
- **The EDID prefers something else** (often `800x480`, `1024x768`, `1280x720`, `1920x1080`):
  the screen scales it down to 480x320 - text looks soft and the aspect ratio may be
  squashed. Pick a better mode (section 3).
- **Black screen / "no signal"**: the board might not send usable EDID. Force a mode with
  the kernel command line (section 3, options B-D).

> **Measured on the real MPI3508 (2026-10-22, Pi 5, Trixie):** the EDID is named `MPI3508` and
> offers 1280x720 (preferred), 1920x1080, 1920x1080i, 1280x1024, 1440x900 and 1024x768, with no
> 480x320 and no 720x480. At the preferred 1280x720 the face looked squashed. A custom
> **720x480@60** mode (`wlr-randr --output HDMI-A-1 --custom-mode 720x480@60`) is accepted and
> the face looks right. LittleBot now requests it itself at start-up (`display.output_mode` in
> `config/pi.yaml`; set it to `null` to keep the desktop's mode).

## 3. Choosing the HDMI mode

On the Pi 5 the display is driven by the KMS driver (`dtoverlay=vc4-kms-v3d`, the default).
The legacy firmware settings the vendor documents (`hdmi_group=2`, `hdmi_mode=87`,
`hdmi_cvt 480 320 60 6 0 0 0`, `hdmi_timings`, `display_rotate`) are **ignored by KMS**
(diagnose.py warns if it finds them). Modes are set at runtime by the desktop
(Screen Configuration / `wlr-randr`) or at boot with a `video=` entry on the kernel
command line ([kernel modedb syntax](https://www.kernel.org/doc/html/latest/fb/modedb.html)).

**Editing `cmdline.txt` safely**: it is `/boot/firmware/cmdline.txt` on Bookworm and Trixie
(`/boot/cmdline.txt` only on older releases). It must stay **one single line**; add the
`video=` option at the end, separated by a space. Keep a backup and a way back: if the
screen stays black you can remove the option from your computer (the boot partition is
FAT and readable everywhere) or over SSH.

```bash
sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak
sudo nano /boot/firmware/cmdline.txt     # append e.g.  video=HDMI-A-1:720x480@60
sudo reboot
cat /proc/cmdline | tr ' ' '\n' | grep ^video=     # check it was applied
```

Try these in order (all **UNVERIFIED** with this screen - record which one works in the
checklist):

| Option | `cmdline.txt` addition | Why |
|---|---|---|
| A. native mode from EDID | none - pick 480x320 in Screen Configuration or `wlr-randr --output HDMI-A-1 --mode 480x320` | sharpest; only if the EDID offers it |
| B. **720x480** (recommended fallback) | `video=HDMI-A-1:720x480@60` | standard 480p mode every HDMI receiver supports (27 MHz pixel clock), and exactly **3:2 like 480x320**, so the scaler shrinks it by 1.5x with no distortion |
| C. native 480x320 via CVT | `video=HDMI-A-1:480x320M@60` | `M` = compute CVT timings. Gives a ~10-12 MHz pixel clock, below the 25 MHz HDMI minimum - many HDMI transmitters/receivers reject it. Try if you want pixel-exact output |
| D. 640x480 (vendor's choice) | `video=HDMI-A-1:640x480M@60` | what the current vendor script uses on new OS releases; universally supported but 4:3, so the face is squashed ~11 % vertically |

Append `D` (e.g. `720x480@60D`) to force the output on even if the screen's hot-plug
signal is not detected (the KMS equivalent of `hdmi_force_hotplug=1`).

**LittleBot is not tied to the HDMI mode.** `display.width/height: 480x320` in
`config/local.yaml` is the face's drawing size; in fullscreen the face module scales it to
whatever mode the desktop runs (lead/face module to confirm on hardware: aspect-preserving
scale, black borders if the aspect differs).

### Make the desktop mode stick (Wayland/labwc)

- Use **Screen Configuration** (Raspberry Pi menu -> Preferences; on Trixie also in the
  Control Centre) to choose the resolution; it saves the layout for the session
  (Raspberry Pi OS uses `kanshi` for this under labwc - UNVERIFIED details).
- Or test from a desktop terminal first:

  ```bash
  wlr-randr                                           # list outputs and modes
  wlr-randr --output HDMI-A-1 --mode 720x480
  wlr-randr --output HDMI-A-1 --custom-mode 480x320@60   # custom mode attempt (may be rejected)
  ```

- `raspi-config` -> *Display Options* on Bookworm/Trixie offers screen blanking and, on
  some versions, resolution - menu contents differ between releases.
- **Turn screen blanking off** for the robot (raspi-config -> Display Options -> Screen
  Blanking -> No), otherwise the face disappears after 10 minutes. LittleBot handles its
  own sleep face.

## 4. Rotation

Needed only if the screen is mounted upside down or portrait.

- Desktop (Wayland): Screen Configuration -> right-click the screen -> Orientation, or
  `wlr-randr --output HDMI-A-1 --transform 180` (values `normal`, `90`, `180`, `270`).
- Boot console: add `video=HDMI-A-1:720x480@60,rotate=180` (rotation for the text console
  only; supported values 0/90/180/270 on recent kernels - UNVERIFIED for this panel).
- `display_rotate=` / `lcd_rotate=` in `config.txt` do nothing under KMS.
- If touch is enabled, rotate the touch input to match (section 5.4).

## 5. Touch (later; optional)

The face and the parent dashboard (on a phone) do not need the touchscreen. Do this only
once everything else works, and keep a backup of `config.txt`.

**Screen not on the Pi's header** (the v2 enclosure since 2026-10-30: the Pi on its UPS in the
base, the screen in the head): touch needs 6 wires from the Pi's header to the **same pin
numbers** on the screen's 2 x 13 socket (the socket is numbered like the header it normally
plugs onto): 19, 21, 23 (SPI), 26 (chip select), 22 (pen interrupt, GPIO 25) and 20 (ground).
Male-to-female jumpers: female on the Pi, male into the screen's socket. Power the screen
through its micro-USB socket, not the header. If touch doesn't respond, add pin 1 (3.3 V) to
pin 1. The controller is read at 50 kHz (`speed=50000` below), so about 40 cm of wire is fine.
The settings below are the same either way.

### 5.1 Enable SPI and the ads7846 overlay (UNVERIFIED on Pi 5)

The Pi 5's GPIO header is driven by the RP1 chip but keeps the same pin functions
(SPI0 on GPIO 7-11, GPIO 25 for the pen interrupt), and the `ads7846` overlay ships with
Raspberry Pi OS. Values below are taken from the vendor's MPI3508 script (`cs=1`, pen IRQ
on GPIO 25) and have **not** been tested on a Pi 5:

```bash
sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.bak
sudo nano /boot/firmware/config.txt
```

Add at the end, under `[all]`:

```ini
dtparam=spi=on
dtoverlay=ads7846,cs=1,penirq=25,penirq_pull=2,speed=50000,keep_vref_on=0,swapxy=0,pmax=255,xohms=150,xmin=200,xmax=3900,ymin=200,ymax=3900
```

Reboot, then check:

```bash
ls /sys/bus/spi/devices/                 # spi0.1 expected
dmesg | grep -i ads7846                  # driver probe messages
grep -A4 -i ads7846 /proc/bus/input/devices   # "ADS7846 Touchscreen" + eventN
sudo apt install -y evtest && sudo evtest      # pick the touchscreen, tap the screen
.venv/bin/python scripts/diagnose.py --section touch
```

If nothing appears: re-check the screen is fully seated on the header (touch uses pins
19/21/23/26 + GPIO 25 on pin 22), and that no other overlay uses SPI0 CE1.
Remove the two lines to undo.

### 5.2 Which screen does touch belong to?

On Wayland the compositor maps touch input to an output. With a single screen this is
automatic. With two screens, labwc (0.7+) supports in `~/.config/labwc/rc.xml`:

```xml
<touch deviceName="ADS7846 Touchscreen" mapToOutput="HDMI-A-1" />
```

(UNVERIFIED with this panel; `labwc --reconfigure` or log out/in to apply.)

### 5.3 Calibration

If taps land in the wrong place (mirrored, swapped, shifted), run LittleBot's own calibration:

```bash
.venv/bin/python scripts/calibrate_touch.py      # touch the 5 crosses, then: sudo reboot
.venv/bin/python scripts/calibrate_touch.py --reset   # undo (then reboot)
```

It stops LittleBot while it shows crosses on the screen, reads the raw touch readings and saves a
libinput calibration matrix in `/etc/udev/rules.d/99-littlebot-touch.rules` (option 2 below, worked
out for you; any swap, mirror, rotation, offset or stretch). The middle cross checks the result.

`xinput_calibrator` and the vendor's `99-calibration.conf` are X11-only and do nothing on
Wayland. By hand on Wayland:

1. Adjust the overlay's `xmin/xmax/ymin/ymax` (raw ADC limits, 0-4095) until the corners
   line up - use `evtest` to read the raw values at each corner.
2. Or a libinput calibration matrix via udev (applies to Wayland compositors):

   ```bash
   sudo tee /etc/udev/rules.d/99-littlebot-touch.rules <<'EOF'
   ATTRS{name}=="ADS7846 Touchscreen", ENV{LIBINPUT_CALIBRATION_MATRIX}="1 0 0 0 1 0"
   EOF
   sudo udevadm control --reload && sudo reboot
   ```

### 5.4 Rotating touch with the screen

The overlay can swap axes (`swapxy=1`) but not invert them; use the matrix:

| Screen transform | `LIBINPUT_CALIBRATION_MATRIX` |
|---|---|
| normal | `1 0 0 0 1 0` |
| 90 | `0 -1 1 1 0 0` |
| 180 | `-1 0 1 0 -1 1` |
| 270 | `0 1 0 -1 0 1` |

## 6. Console-only / no-desktop option (not the default)

pygame can also draw directly on the KMS display without a desktop
(`SDL_VIDEODRIVER=kmsdrm`), saving the ~150-250 MB the desktop uses. LittleBot's YouTube
player needs Chromium, which needs a compositor, so this is only an option if video is
not wanted. UNVERIFIED; to try it, stop the desktop session (or use Raspberry Pi OS Lite)
and run from the text console: `SDL_VIDEODRIVER=kmsdrm .venv/bin/python -m littlebot --mode pi`
(the autostart launcher waits for a desktop and is not meant for this mode).
The rotation matrices in section 5.4 follow libinput's convention; if touch moves the wrong
way, try the 90/270 rows swapped.

## 7. What to record for the next development session

- Output of `.venv/bin/python scripts/diagnose.py --section display --section touch --json`
- `cat /sys/class/drm/card*-HDMI-A-1/modes` and `wlr-randr`
- Which option (A-D) gave a stable, sharp picture; photo of the face on the screen
- Whether the screen physically clears the active cooler
- (later) touch: `dmesg | grep -i ads7846`, `evtest` corner values
