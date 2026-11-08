# LittleBot v4 "Soft Arc"

The Soft Arc concept, built from the parts you already have:
- a soft egg-shaped body on a dark base;
- a black neck;
- a rounded head around the 7" touch screen (52Pi EP-0177), with a camera hood and a dark arch on top;
- paddle arms.

![v4 model](LittleBot_v4_model.png)

## What goes where, and why

| Where | What | Why |
|---|---|---|
| Head, back | 2 × 4 slits behind the screen's own speakers | The EP-0177 has speakers, so sound comes from the face and the USB speaker isn't needed. Set `audio.speaker.device: "hdmi"` in `config/local.yaml` (by default the software never picks HDMI). |
| Head, top | The camera hood: v2's camera bracket on 2 pivot screws through the hood's sides | Tilt adjustable. The dark arch is printed into the face: the head frame's first 8 layers use the 2nd filament. |
| Head, front | The screen on its own 4 corner standoffs, screwed from behind | Nothing touches or covers the glass. The face stays 0.6 mm off the glass's edges, so touch works to the edge. |
| Head, inside | The HDMI ribbon plug, the right-angle USB-C and the HDMI coupler | All hidden. Each end of the head has room to push a plug in from behind. |
| Neck | A black puck on v2's coupler, under the head | It turns with the head and raises it 8 mm, so the black neck shows. |
| Chest | 2 dots: the USB mic is behind the left one, the right one is a dimple | No speaker grille: the concept's clean front. |
| Base (inside) | Pi 5 + UPS on v2's floor | The Pi's fan blows down through the floor's vents and out through notches under the base, hidden against the table. Fresh air comes in through the slits high on the back. |
| Back | 4×AA servo box behind the dark door | 1 screw at the top, 2 hooks at the bottom: easy to swap cells. |
| Back, low | The USB-C charging slot and the UPS switch slider | As v3. |

Reused as printed:
- **from v2:** the floor (Pi 5 + UPS), the stack keeper, the PCA9685 clamps, the coupler, the ring gear, the pinion, the servo frame and the camera bracket;
- **from v3:** the neck plate and the UPS slider (the base ring is v3's, with its 4 screw tabs moved to the back).

## Files

| File | What |
|---|---|
| `littlebot_v4.scad` | the model (it includes `../enclosure_v3/littlebot_v3.scad`, which includes v2) |
| `LittleBot_v4_A1.3mf` | **the Bambu Studio project: 4 plates, colours, supports and brim set** |
| `LittleBot_v4_assembled.3mf` | the whole robot assembled and coloured, to look at (not to print) |
| `stl/*.stl` | each part on its own, in its print orientation (`head_inlay` is the head frame's dark arch) |
| `plates_v4.png` | top view of the 4 plates |
| `checks_v4.scad`, `run_checks_v4.sh` | 58 checks (`OPENSCAD=... ./run_checks_v4.sh`), all passing: interference, the arms' sweep and finger pinch with the head turned, the glass never touched, room to push each plug in, the slider, the AA box and its door |
| `export_v4.sh`, `make_v4_project.py`, `make_v4_assembly.py` | re-make the STLs, the project and the assembled 3MF |

Re-make everything after changing the model. Run from this folder:

```bash
OPENSCAD=openscad ./export_v4.sh
```

```bash
python3 make_v4_project.py
```

```bash
python3 make_v4_assembly.py
```

## Printing (Bambu Lab A1, PLA, 0.20 mm Standard, Textured PEI plate)

Filament slots: **1 white**, **2 dark grey**.

| Plate | Colour | Parts (orientation) | Notes |
|---|---|---|---|
| 1 | white | **body** (upright), **both arms** (flat, one inside the body's footprint) | 5 mm brim on the body. |
| 2 | white + dark | **head frame** (face down) | The dark arch is part of the same print (its first 8 layers switch filament where the arch is). **Tree supports from the build plate**, under the screen's 4 bosses. |
| 3 | white | **head back cover** (rim down), **neck plate** (top face down) | **Tree supports from the build plate** inside the cover's dome. They stay inside it. |
| 4 | dark | **base ring** (upright), **black neck** (top down), **AA door** (outer face down), **UPS slider** | |

## What to buy

Nothing beyond v3's list:
- the angled ribbon HDMI cable + an HDMI coupler;
- the right-angle USB-C cable;
- a 4×AA holder with a switch, and 4 NiMH AA cells.

The mini USB speaker isn't used.

## Screws and nuts (M3 button heads from the kit unless noted)

| Where | Screws | Into |
|---|---|---|
| Base ring → v2 floor | 4 × M3 × 12, through the ring's sides | the floor's 4 posts (as v3) |
| Body → base ring | 4 × M3 × 10, all through the body's back, just above the ring | nuts dropped into the ring's 4 tabs |
| Neck plate → body | 4 × M3 × 8, from the top | nuts dropped into the body's 4 ledge bosses |
| Ring gear → coupler; servo frame collars | as v2 (3 × M3 × 12; 2 × M3 × 10) | v2's threaded holes |
| Black neck → coupler | 3 × M3 × 12, down through the puck (heads sunk) | the coupler's own holes |
| Head → black neck | 3 × M3 × 10, down through the head's floor | pilot holes in the puck |
| Screen → head frame | 4 × M2.5 × 6 (or M3 × 6), from behind, down the 4 wells | the screen's corner standoffs |
| Back cover → head frame | 2 × M3 × 12, from under the head | the frame's 2 bosses (2 tabs hold the cover's top) |
| Camera bracket | 2 × M3 × 12, through the hood's sides (heads sunk 5 mm) | the bracket's pivots |
| AA door | 1 × M3 × 8, at its top | a pilot hole above the opening (2 hooks hold its bottom) |
| Arm servos, arms | as v3 | |

In total: 14 × M3 × 12, 9 × M3 × 10 and 5 × M3 × 8 (v2's neck screws included) and 8 M3 nuts.

## Assembly order

1. **Neck plate, on the bench (as v3):**
   - Put v2's coupler through the plate.
   - Screw the ring gear to it from below.
   - Hang the servo frame with the head servo and pinion, and mesh the gears with the servo centred.
2. **Black neck:** screw the puck onto the coupler (3 × M3 × 12).
3. **Arm servos:** slide each in from outside through its shoulder slot and screw its tabs.
4. **Body onto the base ring:**
   - Drop 4 nuts into the ring's tabs.
   - Lower the body onto the ring's lip.
   - Fit 4 × M3 × 10 through the back.
5. **UPS slider:** push it in from outside through the slot in the back, fork first.
6. **v2 floor:**
   - Turn the robot upside down and fit 4 × M3 × 12 through the ring's sides.
   - Turn it upright and work the slider so the switch's knob sits in its fork.
7. **Mic:** plug it into its USB extension and push it into its pocket behind the chest's left dot.
8. **Neck plate in:**
   - Thread the cables up first.
   - Drop 4 nuts into the ledge bosses.
   - Set the plate in and fit 4 × M3 × 8.
9. **Head:**
   - Head onto the puck: 3 × M3 × 10 down through its floor.
   - Screen: in through the face, then 4 corner screws from behind.
   - Plug in from behind: the ribbon HDMI, the right-angle USB-C, and the coupler with the original cable in its clips.
   - Camera:
     - clip the board into v2's bracket;
     - set the bracket into the hood from below, lens forwards;
     - fit the 2 pivot screws through the hood's sides;
     - set the tilt and tighten.
   - Back cover: hook its 2 slots over the frame's tabs, swing it down and fit 2 × M3 × 12 from below.
10. **Arms:** fit the horn on each arm, push it onto its spline at the neutral angle, and fit the spline screw.
11. **AA box:**
    - With the door off, slide the box in, lid out.
    - Its leads go in through the slot under it, to the PCA9685: red to V+, black to GND.
    - Hook the door's bottom in, swing it up and fit 1 × M3 × 8.

**Arm range:** −90° to **+95°**, as v3. Checked with the head turned ±60°.

## Measure before printing

| Parameter | Default | What to measure |
|---|---|---|
| `scr_standoff_h` | 3 mm | how far the screen's corner standoffs stand off its board |
| `scr_holes` | from 52Pi's drawing | the corner standoffs' centres (±0.5 mm) |
| `bat` | 65 × 70 × 20 mm | your AA holder: width, height, thickness (the door and its bay follow it) |

## Known

- The camera's 2 pivot screws show on the hood's sides, sunk 5 mm. They're what makes the tilt adjustable.
- The neck plate shows as a flat disc round the neck. v2's neck parts drop in from the top, so the opening is the plate's size.
- The head is 246 mm wide, so it fits the A1's 256 mm bed with the screen and every plug inside.
