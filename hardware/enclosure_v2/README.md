# LittleBot enclosure – version 2 ("companion")

The second design, inspired by the companion-robot reference picture:
- a round helmet head with LED-ring ears, a black visor around the screen and the camera in a pod on top;
- a rounded torso with a chest speaker grille, a microphone and two jointed-looking arms;
- a **mobility-ready base**: wheel wells, a front sensor / light-bar plate, a battery bay, an electronics shelf and a servo power input.

The parts are the same as in v1: Pi 5, MPI3508 screen, U20CAM camera, USB mic + speaker, 3 × MG90S servos, PCA9685. The base also has room for the later phases: ESP32, TB6612FNG, TT gear motors + wheels, HC-SR04 distance sensor, LED strips and a battery.

| Front | Back | Exploded |
|---|---|---|
| ![front](images/front.png) | ![back](images/back.png) | ![exploded](images/exploded.png) |

| Side | Side section | Plan section (z = 150 mm) |
|---|---|---|
| ![side](images/side.png) | ![section](images/section_side.png) | ![plan](images/section_plan.png) |

**Overall size:** 340 mm tall (about 35 cm, like the reference), base 176 × 152 mm, head 150 × 132 × 120 mm.
- All parts fit a 220 × 220 × 250 mm bed.
- Everything prints **without supports**.
- All parts come from one parametric OpenSCAD file.

v1 (`../enclosure/`) is smaller and simpler (212 mm, 11 parts). v2 is bigger, more of a character, and ready for the wheels.

## Files

| File | What |
|---|---|
| `littlebot_v2.scad` | the parametric model (all dimensions at the top) |
| `stl/*.stl` | ready-to-slice parts, already in print orientation |
| `checks.scad`, `run_checks.sh` | 68 automated interference and finger-pinch checks (head ±60°, arms −90…+100°), plus a self-test check that must fail |
| `build.sh`, `stl_tools.py` | rebuild the STLs and images. Report per part: size, watertight, number of separate bodies, bed fit, overhang area |

### What has been verified (by the scripts, not by a real print yet)

- **Every STL:** watertight, a single solid body (`lcd_tabs` is 4 pieces on purpose), fits the bed, and has only small overhangs (screw-hole tops, short bridges). Large overhangs were found and removed.
- **`run_checks.sh`:** no part collides with any electronics dummy or other part.
  - The head turns ±60° (the servo's full range) without touching anything.
  - The arms move over −90…+100° without touching anything.
  - Over that arm range the arms stay **≥ 12 mm** from the head and the base.
  - 110° fails on purpose: the arm comes closer than 12 mm to the head.
- **Not verified:** real-world fit. Clearances depend on your printer and the actual parts – see *Measure before you print*.

## Print it in 9 jobs (build plates)

The robot can't be printed as one piece:
- It's 340 mm tall, taller than most printers.
- The electronics have to go inside.
- The head and arms have to move.

Instead, `plates/` groups the parts into **9 ready-made build plates**, one STL per print job. Each fits a 220 × 220 mm bed, and the parts on each plate share a colour:

| Plate | Parts | Filament |
|---|---|---|
| `01_base.stl` | base | main colour |
| `02_torso.stl` | torso | main colour |
| `03_head.stl` | head + camera pod | main colour |
| `04_floor.stl` | floor + 2 well covers | main colour |
| `05_back_cap.stl` | head back cap + shelf | main colour |
| `06_arms.stl` | both arms + torso back cover | main colour |
| `07_small_parts.stl` | neck coupler, both gears, servo frame, camera bracket, rear plate, display tabs | main colour, print slowly for the gears |
| `08_visor.stl` | visor | **black** |
| `09_glow.stl` | both ears + front plate | **white / natural / translucent** |
| later: `10_mobile_floor.stl`, `11_sonar_plate.stl` | wheel-phase floor, HC-SR04 plate | |

![plates](images/plates.png)

Each plate is a single STL with several separate bodies. Most slicers can split it back into objects if you want different settings per part, such as more infill for the gears. On a bigger bed, run `python3 make_plates.py 256` (or your bed size) to check the layout still fits. Some plates could also be merged.

## Parts to print

| STL | Qty | Notes |
|---|---|---|
| `base.stl` | 1 | prints top-down; its walls form a **skirt** the robot stands on (no feet to come off) |
| `base_floor.stl` | 1 | stationary floor: battery-bay walls, shelf posts, bosses for the well covers |
| `shelf.stl` | 1 | electronics shelf over the battery bay (10 mm grid of 2.5 mm holes) |
| `well_cover.stl` | **2** | close the wheel wells until the wheels are fitted |
| `front_blank.stl` | 1 | front plate: two "headlight" dimples + **light bar** (1 mm skin over an LED-strip pocket) |
| `rear_usb.stl` | 1 | rear plate with a 16 × 8 mm slot for the power / charging cable (`rear_blank.stl` if you don't need it) |
| `torso.stl` | 1 | prints top-down; open rear (closed by `torso_back`) |
| `torso_back.stl` | 1 | rear cover with cooling slots in front of the Pi's fan |
| `coupler.stl`, `ring_gear.stl`, `pinion.stl`, `servo_frame.stl` | 1 each | geared neck. Same principle as v1, module 1.25, 36:24 teeth |
| `head.stl` | 1 | prints face down |
| `head_back.stl` | 1 | back cap with a dot grille |
| `visor.stl` | 1 | print in **black**: the frame around the screen; holds the display |
| `ear_left.stl`, `ear_right.stl` | 1 each | mirror images; LED-ring pocket behind a 1 mm skin |
| `camera_pod.stl` | 1 | the camera housing on top of the head |
| `camera.stl` | 1 | tilting camera bracket inside the pod |
| `lcd_tabs.stl` | 1 | four display clamp tabs |
| `arm_left.stl`, `arm_right.stl` | 1 each | print flat side (the side facing the body) down |
| later: `base_floor_mobile.stl`, `front_sonar.stl` | 1 each | wheel phase: wheel slots + motor/caster grids; HC-SR04 plate |

**Material and settings:**
- **Material:** PETG (tougher, copes better with the Pi's warmth) or PLA+.
- **Settings:** 0.2 mm layers, 3 perimeters, 15–25 % infill. For the gears: 5 perimeters / 50 %, printed slowly.
- **Supports:** none needed. A brim helps the tall base and torso.
- **Colours:** the ears and the front plate glow only when printed in **white, natural or translucent** filament; dark filament hides the LEDs. Print the visor in black.
- **Filament:** the stationary build is 940 cm³ of plastic (about 1.2 kg PETG at 100 % infill). Normal infill uses noticeably less: plan on about 1 kg of the main colour plus a little black and natural.

## Hardware (bill of materials)

| Item | Qty | Use |
|---|---|---|
| M3 × 10 countersunk | 4 + 8 + 4 | floor → base (from below), well covers, front/rear plates |
| M3 × 10 pan head | 4 + 3 + 2 + 2 | torso → base (from inside the base), head → neck, servo frame, camera pod → head |
| M3 × 8 pan head | 4 | shelf → posts |
| M3 × 12 countersunk | 4 | torso back cover |
| M3 × 20 pan head | 4 | ears (from inside the head) |
| M3 × 30 countersunk | 4 | head back cap (through the cap's bosses) |
| M2.5 × 12 | 3 | ring gear → neck spigot (from below) |
| M2.5 × 6 | 4 + 4 + 4 | Pi 5 → spine standoffs, display clamp tabs, visor → head |
| M2 × 8 | 2 | camera bracket pivots (snug = adjustable friction) |
| M2 × 6 + nuts | 4 | camera board → bracket |
| servo screws (in the servo bags) | | servo tabs, horns |
| PTFE tape or a drop of silicone grease | – | neck bearing ring |
| 1 mm foam tape, 2 small zip ties | | display clamp tabs, speaker gasket, speaker cradle |
| USB-A extension cable, male → female, 15–30 cm (socket end no bigger than about 18 × 9.5 mm) | 1 | Adafruit mini USB mic → chest sleeve |
| **90° HDMI adapter, male → female** (buy a 2-pack with both the 90° and 270° versions) | 1 | turns the official cable's plug towards the back of the head (see *Head* below) |
| male-to-female jumper wires, 30 cm | ~6 | display power (5 V + GND) now, PCA9685 later |
| optional ballast, ~250 g, sealed in a bag and strapped in | | battery bay in the stationary phase (stability) |
| **later:** 5.5 × 2.1 mm panel DC jack (8 mm hole) + mini toggle (6 mm hole) | 1 + 1 | separate 5 V servo supply ("SERVO 5V" on the back) |
| **later:** 2 × 12-LED WS2812 rings (37 mm OD / 23 mm ID), ≤ 5 mm-wide addressable LED strip ~55 mm | | ears + light bar |
| **later:** TT gear motors + 65 mm wheels, 2 ball casters, power bank ≤ 98 × 66 × 28 mm, hook-and-loop strap, HC-SR04, ESP32, TB6612FNG | | wheel phase |

## Where everything goes

| Area | What's there |
|---|---|
| **Head** | **Display:** the MPI3508 sits in locating ribs on the back of the visor, HDMI pointing **down**, held by four clamp tabs; the visor screws into the head from inside. The official Raspberry Pi cable's HDMI plug is about 45 mm long with its strain relief, too long to go straight down (34.7 mm of room). So it goes in through a **90° adapter** whose socket faces the back of the head, and the plug lies flat towards the neck (checked: adapter 24 × 15 × 22 mm + plug 22 × 50 × 12 mm).<br>**Ears:** LED-ring pockets; the wires go into the head through a hole under each ring.<br>**Back cap:** the 4 screws locate it.<br>**Camera:** in the top pod on a tilting bracket. Its cable drops into the head through a small hole and then down the neck bore. |
| **Neck** | Same geared turntable as v1. The head screws onto a coupler that turns on a hollow spigot (26 mm cable bore). A 36-tooth ring gear under the torso top is driven by a 24-tooth pinion in front of it, so the servo never carries the head and lifting by the head can't pull the shaft out. **Head = 0.67 × servo angle**: the servo's full range gives about ±60°, and all of it is collision-free. |
| **Torso** | **Front:** speaker behind the round chest grille, in a cradle with zip-tie slots; the Adafruit mini USB mic lies flat in a sleeve behind the small holes (viewer's left), holes facing out, its plug pointing to the middle where the extension cable's socket plugs on.<br>**Top:** head servo on its frame, gear guard, one arm servo per side on a rib (spline through the side wall).<br>**Middle:** the **Pi 5 stands upright** on a spine (cooler facing the back cover's slots). Its USB ports point down into the base, USB-C / micro-HDMI to the robot's left, and the GPIO header to its right with room for jumper wires. The official Active Cooler (63.5 × 42.5 × 13.7 mm) leaves about 11 mm of air in front of the back cover's slots. |
| **Base** | **Under the torso:** the cable pass-through.<br>**Shelf:** PCA9685 now; ESP32 + TB6612FNG later.<br>**Battery bay:** under the shelf.<br>**Rear:** cable slot plate and the servo-supply jack + switch.<br>**Front:** sensor / light-bar plate.<br>**Sides:** two wheel wells (covered for now). |

All openings for sound, the mic and cooling are 3 mm or smaller. The cable slot is 16 × 8 mm, in a 4.6 mm plate.

## Checked against your parts

| Part | Size used in the model | Source |
|---|---|---|
| Raspberry Pi 5 + official Active Cooler | 85 × 56 mm board, M2.5 holes 58 × 49; cooler 63.5 × 42.5 × 13.7 mm | official mechanical drawing; Raspberry Pi spec sheet for the cooler |
| MPI3508 3.5" HDMI display | 85.51 × 56 mm glass, 7.32 mm stack, HDMI on the bottom edge | lcdwiki MPI3508 drawing |
| Adafruit 3369 USB speaker | 84 × 43 × 32 mm | Adafruit product page |
| Adafruit 3367 mini USB mic | 22.2 × 18.3 × 7.0 mm | Adafruit product page |
| Official micro-HDMI → HDMI cable | HDMI plug ≈ 20 × 10 × 45 mm (with strain relief), 5 mm cable | typical plug size (no official drawing found); cable diameter from Adafruit |
| U20CAM-1080P camera | 32 × 32 mm board, M2 holes 28 mm apart | InnoMaker lists its 1080P USB modules as 32 × 32 mm with M2 holes; the spacing is assumed |
| MG90S servos | typical MG90S size | clones vary |

All of these fit with every check passing. The dimensions come from listings, not from your actual parts, so still measure the camera and one servo before the long prints.

## Measure before you print (assumed values)

These values come from datasheets or typical parts. Check each with calipers, change it at the top of `littlebot_v2.scad`, then run `./build.sh && ./run_checks.sh`.

| Parameter | Default | Check |
|---|---|---|
| `lcd_aa_offset` | 3.03 | With HDMI pointing down, is the wide black border on the **left**? If it's on the right, use −3.03 |
| `lcd_w`, `lcd_h`, `lcd_stack` | 85.51, 56, 7.32 | glass size, and glass-front-to-PCB-back thickness (lcdwiki drawing) |
| `cam_board`, `cam_hole_sp`, `cam_board_y` | 32, 28, 26 | camera board size, M2 hole spacing, lens length (pod front → board) |
| `sv_*` | MG90S typical | clones differ – measure one servo (`sv_spline_h`, `sv_tab_z`, `sv_tab_l`, `sv_hole_sp`) |
| `horn_plate_t` | 1.6 | thickness of the horn's arms. Trim the head-servo horn to **≤ 12.5 mm radius** (pinion pocket) and the arm horns to ≤ 15 mm |
| `spk_w/h/d` | 84 × 43 × 32 | Adafruit 3369 *Mini External USB Stereo Speaker* (Adafruit's listed size). Its cable can leave from either end through the windows in the cradle ribs |
| `mic_l/w/t` | 22.2 × 18.3 × 7.0 | Adafruit 3367 *Mini USB Microphone* (Adafruit's listed size) |
| `bat_*`, `sonar_sp`, HC-SR04 holes | | only for the later phases |

Test-print these first:
1. `ring_gear` + `pinion` – check they mesh;
2. `servo_frame` with one servo;
3. `rear_usb` – check that your plug fits the slot.

Then commit to the long prints: torso, base and head.

## Assembly order (stationary phase)

1. **Neck:** stick PTFE tape on the coupler's bearing ring and drop the coupler into the torso top. From inside, screw the ring gear onto its spigot (3 × M2.5 × 12).
2. **Head servo:**
   1. Fit the trimmed horn into the pinion's pocket and fix it with 2 small screws.
   2. Set the servo to 90°, with the coupler's mark pointing forward.
   3. Slide the pinion up into mesh with the ring gear.
   4. Screw the frame to the pillars (2 × M3 × 10), and the servo tabs to the frame.
3. **Arm servos:** from inside, push each servo through its rib window so the round boss pokes out of the side-wall hole. Screw the tabs to the rib.
4. **Speaker** into the chest cradle (foam ring towards the grille, 2 zip ties through the rib slots, cable out through a rib window). **Mic:** plug it into the extension cable and slide the dongle into its sleeve, holes facing the chest; tie the cable down behind the speaker.
5. **Pi 5 + active cooler** onto the spine standoffs (4 × M2.5 × 6), USB ports down. Plug in:
   - USB: speaker, mic, camera.
   - Micro-HDMI → HDMI cable to the head.
   - Display power jumpers: GPIO 5 V on pin 2 or 4, GND on pin 6.
   - PCA9685 jumpers: I²C pins 1, 3, 5, 6.
   - USB-C power: the cable comes in through the rear plate's slot.
6. **Torso onto the base:**
   1. Route the cables through the pass-through.
   2. Fix the torso with 4 × M3 × 10 from inside the base, up into the torso's corner wedges.
   3. Close the torso back cover (4 × M3 × 12 countersunk).
7. **Base:**
   1. Fit the shelf with the PCA9685 (zip ties or screws through its hole grid).
   2. Add the optional ballast, strapped into the battery bay.
   3. Fit the **floor** (4 × M3 × 10 countersunk from below).
   4. Push in the **well covers** from outside (4 screws each).
   5. Fit the **front and rear plates** (2 screws each, cable through the rear slot).
8. **Head:**
   1. Display into the visor frame, HDMI down; add the clamp tabs with foam. Plug the 90° HDMI adapter into the display so its socket faces the back of the head.
   2. Visor into the face seat (4 × M2.5 × 6 from inside).
   3. Ears: 2 × M3 × 20 each from inside; fit the LED rings later.
   4. Camera board onto the bracket, the bracket into the pod (2 × M2 × 8), the pod onto the head (2 × M3 × 10 from inside).
   5. Connect HDMI, the display jumpers and the camera cable; they come up the neck bore.
   6. Screw the head onto the coupler (3 × M3 × 10 from inside; the uneven pattern only fits one way).
   7. Close the back cap (4 × M3 × 30 countersunk).
9. **Arms:** set the arm servos to their calibrated neutral, screw the horn into the arm's pocket, press the arm onto the spline and drive the centre screw.

## Later: the wheel phase (prepared, not finished)

1. Print `base_floor_mobile` (wheel slots + M3 motor grid + front/rear caster grids) and `front_sonar`.
2. Leave the well covers off.
3. **Motors:** TT gear motors with **65 mm wheels**. Bolt them to the floor grid (x 40–50 mm from the centre line, y 46–106 mm).
   - **Motor brackets are not designed yet**, because they depend on your exact motors.
   - Mount them so the wheels stick out about **8 mm below the skirt** (wheel centre ≈ 24.5 mm above the table). The checks confirm a 65 × 27 mm wheel at that height fits the well with about 1 mm to spare.
4. **Casters:** two ball casters on the caster grids, height ≈ 18 mm from the floor underside to the table.
5. **Battery:** a power bank ≤ 98 × 66 × 28 mm, held by a strap through the floor slots, charged through the rear slot.
   - A Pi 5 wants 5 V / 5 A. Most power banks give 5 V / 3 A, which limits the Pi's USB power – see `docs/POWER.md`.
   - Servos and motors need their **own** supply through the rear jack and switch.
6. **Electronics:** ESP32 + TB6612FNG on the shelf next to the PCA9685. The HC-SR04 on `front_sonar` runs on 5 V, so its echo pin needs a level shifter or divider for the ESP32's 3.3 V input.
7. **LEDs:** the light bar and ear LEDs can run from the ESP32 (or the Pi), at low brightness.
8. **Not included yet:** a LiDAR / ToF sensor (e.g. a VL53L1X behind a hole in the front plate) and a bigger battery. Both need new plate or bay variants.

## Safety notes (important – this is for a baby)

* Use LittleBot **only under supervision**. Software tests don't establish physical toy safety (see `HARDWARE_ARRIVAL_CHECKLIST.md`, phase 12).
* **Small parts:**
  - No small parts can come off from outside: there are no feet (the base skirt stands on the table), and every screw is internal, recessed or countersunk.
  - The LEDs sit behind 1 mm printed skins.
  - The clamp tabs and camera bracket are enclosed.
  - The smallest outside parts are the ears (52 mm) and the plates.
* **Arm range:**
  - Keep the software limits for the arm servos within **−90…+100°** of arm movement (+ = forwards/up). Over that range the arm keeps a constant 3 mm gap to the torso and stays ≥ 12 mm from the head and base, so there's no closing finger gap.
  - Above about 110° it comes within 12 mm of the head.
  - Keep physical motion disabled until `docs/MOTION_SAFETY.md` is complete.
* **Stability:** the base is wide, but the head is high. Add ballast in the stationary phase and tip-test it. Never place LittleBot where it could fall onto the child.
* **Wheel phase:** wheels can pinch fingers and catch hair. Keep speeds very low, add wheel guards if the wells are open, and keep it on the floor (never on a table).
* **Batteries:** only a commercial power bank with protection circuits. Never loose Li-ion cells, and never charge it unattended inside the robot (`docs/POWER.md`).
* **Prints:** check the finished prints for sharp edges and layer splits, and sand any burrs. PETG is less brittle than PLA if it's dropped. Inspect regularly for cracks, especially the arms, neck and ears.
* The lower edges of the front and rear windows print as bridges. Any sag is hidden behind the plates.
