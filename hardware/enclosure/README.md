# LittleBot enclosure – version 1 ("TV head")

A parametric, 3D-printable enclosure for LittleBot, designed around the actual parts list
and the planned future servos (head rotation + two arms). Every part fits a 220 × 220 mm
bed (most fit 180 × 180), prints **without supports**, and is generated from one OpenSCAD file.

| Front | Back | Exploded |
|---|---|---|
| ![front](images/front.png) | ![back](images/back.png) | ![exploded](images/exploded.png) |

| Side section | Plan section (z = 80 mm) |
|---|---|
| ![side](images/section_side.png) | ![plan](images/section_plan.png) |

Overall: **130 × 126 mm body footprint, ~212 mm tall**. Head 146 × 106 × 58 mm (it was 114 mm
wide before the Pi moved into the head).

## Files

| File | What |
|---|---|
| `littlebot_enclosure.scad` | the parametric model (all dimensions at the top) |
| `stl/*.stl` | ready-to-slice parts, already in print orientation |
| `checks.scad`, `run_checks.sh` | automated interference checks (48 checks incl. head ±45° and arm sweeps), a self-test that must find an overlap, and an INFO line on the cooler under the screen |
| `build.sh`, `stl_tools.py` | regenerate STLs + images; size / watertight / bed-fit report |

## Parts to print

| STL | Qty | Orientation (already set) | Notes |
|---|---|---|---|
| `body.stl` | 1 | top face on the bed | top edge is a 45° chamfer so it prints cleanly upside-down |
| `base.stl` | 1 | flat | speaker cradle, PCA9685 wall, ballast pockets, feet recesses |
| `head.stl` | 1 | screen side on the bed | best surface finish is on the face; glass ribs + four posts for the Pi carrier inside |
| `head_back.stl` | 1 | outside face on the bed | |
| `coupler.stl` | 1 | flat top on the bed | the visible neck / turntable |
| `ring_gear.stl` | 1 | flat | 36 teeth, module 1.5 |
| `pinion.stl` | 1 | flat | 24 teeth, holds the head-servo horn |
| `arm.stl` | **2** | outer face on the bed | same part for left and right |
| `servo_frame.stl` | 1 | flat | holds the head servo |
| `camera.stl` | 1 | plate on the bed | tilting / sliding camera bracket (slotted holes for 26–28.5 mm board-hole spacing) |
| `pi_carrier.stl` | 1 | back face on the bed | frame with four standoffs for the Pi 5; screws onto the head posts |

**Suggested settings:** PETG (tougher, better near the warm Pi) or PLA+, 0.2 mm layers,
3 perimeters, 20–25 % gyroid infill (gears: 5 perimeters / 50 %), no supports, brim
optional for the body. Print the gears slowly for accurate teeth.

## Hardware (bill of materials)

| Item | Qty | Use |
|---|---|---|
| M3 × 10 screws (self-tapping or machine screws into the printed pilots) | 4 + 3 + 4 | base plate, head → neck, head back cover |
| M3 × 10 | 2 | head-servo frame → pillars |
| M2.5 × 12 | 3 | ring gear → neck (from below) |
| M2.5 × 8 pan head | 4 + 4 | Pi 5 → carrier standoffs (through the Pi, before the screen goes on); carrier → head posts |
| M2.5 × 6 | 4 | PCA9685 |
| M2.5 × 8 | 2 | camera bracket pivots (snug = adjustable friction) |
| M2 × 6 + nuts | 4 | camera board → bracket |
| M2 × 6 self-tapping | 6 | servo horns → pinion / arms (the servo kits' small screws) |
| 10 mm self-adhesive rubber feet | 4 | |
| 1 mm foam tape | ~20 cm | glass gasket inside the face, speaker gasket |
| PTFE tape or a drop of silicone grease | – | neck bearing ring |
| **micro-HDMI → HDMI "U" adapter for 3.5" HDMI screens on a Pi 4** (micro-HDMI male + HDMI male, about 35 × 20 × 10 mm) | 1 | links the Pi's HDMI0 to the screen's HDMI socket right below the stack (a flat FPC kit also fits) |
| USB-C power cable, ≥ 1 m, slim plug (≤ 12.5 × 7.5 mm, ≤ 30 mm long with its strain relief) | 1 | in through the back of the body, up the neck to the Pi |
| female-to-female jumper wires, 30 cm | 4 | PCA9685 – see *The screen sits on the Pi* for which pins are still free |
| 5.5 × 2.1 mm panel-mount DC jack (8 mm hole) | 1 | separate 5 V servo supply (future) |
| Mini toggle switch (6 mm hole) | 1 | hardware servo power switch (future) |
| Steel washers | ~100 g | optional ballast in the base pockets (lower centre of gravity) |
| 2 short zip ties | 2 | speaker in its cradle |

## Where everything goes

* **Head:** the MPI3508 screen **plugged straight onto the Pi 5's 40-pin header**, HDMI socket pointing **down**.
  - **Mounting:** the glass sits in locating ribs inside the face, on 1 mm foam. The Pi is screwed to the **Pi carrier**, which screws onto four posts inside the face. The stack is held by the Pi's own M2.5 holes; the screen has none.
  - **Layout seen from the front:** the Pi's USB-A / Ethernet end is on the viewer's right, its microSD end on the viewer's left, USB-C power and micro-HDMI pointing down.
  - **Plugs:** the camera and speaker cables and the mic dongle go into the USB-A ports, with 25 mm of room beside them. That room is why the head is now 146 mm wide.
  - **Below the screen:** 34 mm hold the micro-HDMI → HDMI U adapter and the USB-C power plug.
  - **Air:** the cooler's fins blow towards the microSD end; slots in that side of the head let the warm air out, and the back cover's slots let fresh air in.
  - **Microphone:** the Adafruit mini USB mic plugs straight into the Pi (front-lower USB port), behind 9 small holes in the head's side on the viewer's right.
* **Neck:** the head screws onto a **turntable** that turns on a hollow spigot in the body
  top. All head cables (USB-C power, speaker, camera, PCA9685 wires) pass through the **26 mm bore**.
  - A 36-tooth ring gear under the top plate is driven by a 24-tooth pinion on the head servo. The servo never carries the head's weight.
  - If the robot is lifted by its head, the ring gear catches under the top plate, so the servo shaft is not loaded.
  - The head turns **0.67 × the servo angle** (gear ratio 24:36), so servo 30–150° ≈ head ±40°. Calibrate the head joint with that in mind; the software's limits are in servo degrees.
* **Body, top area:** head servo (frame on two pillars), gear guard (keeps wires out of the
  teeth), both arm servos on vertical ribs with the output shaft through the side walls.
* **Body, front:** camera behind the round chest aperture (pivots ±10° and slides ±3 mm for
  lens length), speaker behind the grille.
* **Body, floor (base plate):** speaker cradle, PCA9685 on its own wall along the left side, ballast pockets.
* **Back:** USB-C power cable entry, servo power jack + switch ("SERVO 5V"), vents.

All openings are 3 mm or smaller (grilles, vents) so little fingers can't reach inside.

## The screen sits on the Pi

The MPI3508 plugs onto pins 1–26 of the Pi's header, its glass faces away from the Pi, and its PCB rests just above the Pi's USB ports ("pretty flush"). From the two drawings:

| | mm | Source |
|---|---|---|
| glass front → back of the screen PCB | 7.32 (touch 1.2 + LCD 2.49 + gap 2.01 + PCB 1.6) | lcdwiki drawing |
| screen's 2 × 13 socket below its PCB | 13.54 (20.86 − 7.32) | lcdwiki drawing |
| plastic of the Pi's header | 2.5 (pins reach 8.57) | Raspberry Pi 5 drawing |
| **screen PCB → Pi PCB** | **16.04** (the Pi's USB ports are 15.8 tall, so it sits 0.2 mm above them) | |
| **glass front → underside of the Pi** | **24.76** (Pi PCB 1.4 per the drawing) | |
| where the Pi sits under the screen | Pi corner 0.48 mm in from the screen's wide-border end, 0.40 mm above its HDMI edge (header pin 1 is 8.88 / 8.40 mm from those ends) | both drawings |

**Does the Active Cooler fit under the screen?** Mostly yes, with one doubtful corner:
- The cooler's top is about **12 mm** above the Pi PCB (13.7 mm overall including the push-pin tails below the board). It sits between x 0.5–64 and y 6.5–49 on the Pi, so its body has about **4 mm** to spare under the screen PCB.
- The doubtful corner is the screen's **HDMI socket**. It hangs 6.7 mm below the screen PCB, and its inner corner overlaps the cooler's last fin row by up to about **2.5 × 1.3 × 2.7 mm** in the drawings. This is the `INFO` line of `run_checks.sh`.
- That is the same size as the drawing tolerances, so it may just touch or just clear. You say the screen sits flush, which suggests it clears – if the cooler is already fitted.
- **Check it:** with the screen pressed down, look along the HDMI edge. A sheet of paper should slide between the HDMI socket and the fins, and nothing should push the screen up.
- **If it doesn't fit**, the options are:
  1. **GPIO stacking/extension header** (2 × 20, about 8.5 mm extra). Change `pi_hdr_h` to 11 and rebuild: the head posts and carrier follow, and the checks show whether the 58 mm deep head still fits.
  2. A **low-profile heatsink** (≤ 6 mm) instead of the Active Cooler. Watch the temperature.
  3. Mount the screen **separately** and wire 5 V/GND (pins 2/4 and 6) and, for touch, the SPI pins.

**GPIO pins:** the screen's socket covers pins 1–26. Those include the PCA9685's I²C pins (3, 5) and both 3.3 V pins (1, 17). Pins 27–40 stick out only 7.5 mm under the screen PCB, which is too short for jumper housings. So the PCA9685 needs one of these: a stacking header, wires soldered to the underside of the Pi's header pins, or a GPIO splitter.

## Checked against your parts (2026-10-23)

The robot reports the screen as `MPI3508` (EDID), the camera as `U20CAM-1080P`, the speaker as `UACDemoV1.0` and the mic as `USB PnP Sound Device`, matching these parts. The full source list with links is in [`../enclosure_v2/README.md`](../enclosure_v2/README.md#checked-against-your-parts-2026-10-23). The v1 values:

| Part | Dimensions used | Source |
|---|---|---|
| MPI3508 screen | glass 85.51 × 56.0, active area 73.44 × 48.96, 2.32 mm off-centre (borders 8.35 / 3.72); stack 7.32; no mounting holes (four 2.3 × 10.5 mm PCB tabs at 10–20.5 / 64.5–75 mm); socket 13.54; HDMI socket centre 33.0, opening 1.70 in from the edge; jack at 53.68 | [lcdwiki MPI3508 drawing](https://www.lcdwiki.com/res/MPI3508/MPI3508-size.pdf), 2025-08-18 |
| Raspberry Pi 5 | 85 × 56, M2.5 Ø2.7 holes 58 × 49, header pin 1 (8.40, 51.24), USB stack 15.8 | [Pi 5 mechanical drawing](https://datasheets.raspberrypi.com/rpi5/raspberry-pi-5-mechanical-drawing.pdf) |
| Active Cooler | 63.5 × 42.5, 13.7 overall, top ≈ 12 above the Pi | [product brief](https://datasheets.raspberrypi.com/cooling/raspberry-pi-active-cooler-product-brief.pdf) |
| U20CAM-1080P | 32 × 32, holes Ø2.2 (spacing not published), M12 lens on an 18 mm holder, connector on the lens side | [InnoMaker](https://www.inno-maker.com/product/u20cam-1080p/), [manual](https://github.com/INNO-MAKER/U20CAM-1080P-S1) |
| Adafruit 3369 speaker | 84.0 × 43.0 × 32.0 | [Adafruit 3369](https://www.adafruit.com/product/3369) |
| Adafruit 3367 mic | 22.2 × 18.3 × 7.0 (10.2 sticks out of the USB port) | [Adafruit 3367](https://www.adafruit.com/product/3367) |
| MG90S | A 32.5, B 22.8, C 28.4, D 12.4, E 32.1, F 18.5 | [TowerPro MG90S](https://www.towerpro.com.tw/product/mg90s-3/) |
| PCA9685 (Adafruit 815) | 62.2 × 25.4, holes 3.30 / 59.18 × 3.30 / 22.35 (55.88 × 19.05) | [Adafruit 815](https://www.adafruit.com/product/815), [fab print](https://learn.adafruit.com/16-channel-pwm-servo-driver/downloads) |

## What changed (2026-10-23)

- **The Pi moved from the base plate into the head, under the screen** (the screen is plugged onto its header). The base plate lost the Pi standoffs; the body lost the side mic grille; the back opening now just lets the USB-C cable in.
- **Head 114 → 146 mm wide:** the Pi's USB plugs need about 25 mm beside the Pi. The arm sweeps (up to 170°, head ±45°) still pass because the head's 22 mm corner radius keeps its bottom edge in.
- **Screen mounting:** the four clamp tabs and their bosses are gone (the screen has no holes). The new `pi_carrier` screws onto four posts inside the face. Glass ribs are 3 mm deep, with a 1 mm foam gasket.
- **Screen window:** centred on the active area with the corrected 2.32 mm offset (was 3.03), margin 0.5 mm (was 1.0), so the window is 74.44 × 49.96 mm.
- **HDMI:** a U adapter right below the stack replaces the straight plug or sideways-adapter route.
- **Microphone:** plugs straight into the Pi; 9 holes in the head's side on the viewer's right.
- **Camera bracket:** slots for 26–28.5 mm hole spacing, cut-outs for the lens holder's ears and the 5-pin connector.
- **MG90S:** spline height 32.0 → 32.5, width 12.2 → 12.4, boss 28.5 → 28.4, tabs 32.2 → 32.1.
- **PCA9685 holes:** 3.3/59.2 × 3.3/22.1 → 3.30/59.18 × 3.30/22.35 (Adafruit fab print), board 62.5 → 62.23.

## Measure before you print (assumed values)

These came from drawings, listings or estimates and **must be checked with calipers** against
your parts. Change them at the top of `littlebot_enclosure.scad`, then run `./build.sh` and `./run_checks.sh`.

| Parameter | Default | How to measure |
|---|---|---|
| `lcd_aa_offset` | 2.32 | screen on, white picture: black border at both short ends (expect 8.35 and 3.72); offset = (wide − narrow) / 2. With HDMI down the wide border should be on the **left**, otherwise use −2.32 |
| `lcd_sock_h`, `pi_hdr_h` | 13.54, 2.5 | screen pressed onto the Pi: gap between the two PCBs next to the header (expect 16.0) |
| `cooler_h` | 12.0 | cooler fitted: Pi PCB to the top of the fan / fins; then do the HDMI-corner check above |
| `pi_pcb_t` | 1.4 | Pi PCB thickness |
| `hdmi_link` | 26 × 14 | your U adapter: width along the edge, drop below the glass edge |
| `cam_hole_sp` | 26…28.5 | camera board hole centres (the bracket slots cover this) |
| `cam_board_y` | 23 | ≈ lens length after focusing; the bracket slots allow ±3 mm |
| `sv_spline_h`, `sv_tab_z`, `sv_tab_l`, `sv_hole_sp` | 32.5, 15.9, 32.1, 27.6 | MG90S clones differ – measure one servo |
| `horn_plate_t` | 1.6 | thickness of the horn's arms |
| `pca_holes` | Adafruit 815 | clones usually copy 55.88 × 19.05 – check |
| `spk_w/h/d` | 84 × 43 × 32 | Adafruit 3369 Mini External USB Stereo Speaker (Adafruit's listed size) |

**Fit test first:**
1. The **face**: print `head` (or just its front, by cutting it in the slicer) + `pi_carrier`. Then:
   - screw the Pi to the carrier and plug the screen on;
   - check the glass sits in the ribs and the window shows the whole lit area;
   - check the HDMI U adapter, USB-C and USB plugs fit.
2. `camera` with the camera board.
3. `ring_gear` + `pinion` (check they mesh) and the `servo_frame` with one servo.

Then commit to the 8-hour body print.

## Assembly order

1. **Arm servos:** from inside the body, push each servo's top through its rib window so
   the round boss pokes out of the side-wall hole. Screw the tabs to the rib (servo screws).
   Check that the spline sticks out ~4 mm. If it's too short or too long, add washers under
   the tabs, or adjust `arm_gap` and reprint.
2. **Neck:** stick PTFE tape on the coupler's bearing ring, drop the coupler into the top
   hole and screw the ring gear onto its spigot from inside (3 × M2.5 × 12).
3. **Head servo:**
   1. Fit a cross horn (or a horn trimmed to 14 mm radius) into the pinion's pocket, then fix it with 2 small screws through the horn holes (drill 1.3 mm pilots using the horn as a template).
   2. Centre the servo electrically (90°) and point the coupler's front mark forward.
   3. Slide the pinion up into mesh with the ring gear, and screw the frame to the pillars.
4. **Camera:** board onto the bracket (M2), bracket between the chest pillars with the two
   pivot screws, tilt to taste.
5. **Base plate:** speaker in the cradle (foam ring towards the grille, two zip ties), PCA9685 on the wall, optional ballast. Route the USB-C power, speaker, camera and PCA9685 cables up through the neck bore.
6. Screw the base plate into the body (4 × M3, from underneath), add the rubber feet.
7. **Head:**
   1. Stick 1 mm foam tape around the window inside the face.
   2. Pi 5 + Active Cooler onto the carrier (4 × M2.5 × 8 through the Pi), then press the screen onto the header and plug in the HDMI U adapter.
   3. Put the stack in through the open back, glass into the ribs, and screw the carrier to the four posts (4 × M2.5 × 8).
   4. Plug the mic into the Pi's front-lower USB port (behind the side holes), and connect the camera, speaker and USB-C cables.
   5. Screw the head onto the coupler from inside (3 × M3; the uneven pattern only fits one way), then close the back cover.
8. **Arms:** with the arm servos at their calibrated neutral, push each arm (horn screwed
   into its pocket) onto the spline and drive the centre screw through the arm's hole.

## Safety notes (important – this is for a baby)

* Use LittleBot **only under supervision**. Software tests don't establish physical toy safety.
* All screws are internal or recessed. There are no small detachable parts: the arms and
  head are large, and the Pi carrier is inside the closed head.
* Arms move in a plane 3 mm from the body with a **constant gap** (no closing pinch point).
  The raised arms stay clear of the head (checked over the full sweep). Keep physical motion
  disabled until `docs/MOTION_SAFETY.md` is complete.
* The base is wide and heavy at the bottom, but the Pi now sits in the head; add ballast if your build feels top-heavy.
* The Pi sits right behind the screen: after 30 minutes of use check that the face and the vented side are only warm and `vcgencmd get_throttled` stays at 0x0.
* Don't use a hot-glued battery inside; power it from a proper supply (see `docs/POWER.md`).
* Check the finished print for sharp edges and layer splits; sand any burrs. PETG is less
  brittle than PLA if it gets dropped.
* Inspect regularly for cracks, especially the arms and neck.
