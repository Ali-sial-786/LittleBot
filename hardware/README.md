# LittleBot hardware: 3D-printable enclosures

Two complete designs. Both are parametric OpenSCAD models with ready-to-slice STLs and automated interference checks, and both hold the same parts:
- Raspberry Pi 5 (4 GB; every RAM size has the same board) + official Active Cooler, with the MPI3508 3.5" HDMI screen **plugged straight onto its GPIO header** – the screen + Pi stack sits in the head;
- InnoMaker U20CAM-1080P camera;
- Adafruit mini USB mic (3367) + mini USB speaker (3369);
- 3 × MG90S servos (head turn + two arms) and a PCA9685.

| | [v1 "TV head"](enclosure/README.md) | [v2 "companion"](enclosure_v2/README.md) |
|---|---|---|
| Look | ![v1](enclosure/images/front.png) | ![v2](enclosure_v2/images/front.png) |
| Size | 130 × 126 mm footprint, 212 mm tall, head 146 mm wide | 176 × 152 mm footprint, 340 mm tall |
| Parts | 11 STLs (12 prints) | 22 STLs (23 prints) + 2 for the wheel phase |
| Head | screen + Pi stack in the face, mic in the head's side | helmet head, black visor with the screen + Pi stack behind it, LED-ring ears, camera pod on top |
| Neck | geared turntable, head = 0.67 × servo | same |
| Body | camera in the chest, speaker + PCA9685 on the floor plate | speaker + mic in the chest; shelf (PCA9685), battery bay, wheel wells, sensor/light-bar plate in the base |
| Future phases | servo supply jack | wheels (TT motors), casters, HC-SR04, ESP32 + TB6612FNG, LED strips, power bank |
| Checks | 48 | 70 (incl. 12 mm finger-pinch clearance for the arms) |

v1 is quicker to print and smaller. v2 is the more "toy-like" character and is ready for driving around later.

To regenerate either design after changing a dimension (needs OpenSCAD and Python 3 + numpy):

```bash
cd hardware/enclosure_v2      # or hardware/enclosure
./build.sh                    # STLs + preview images + the part report
./run_checks.sh               # interference checks (+ a self-test and an INFO line)
```

Every dimension was checked against the manufacturers' drawings on 2026-10-23: lcdwiki MPI3508, Raspberry Pi 5 and Active Cooler, InnoMaker, Adafruit, TowerPro. Each README has the table of sources and a section *The screen sits on the Pi*. That section covers:
- the stack heights;
- whether the Active Cooler fits under the screen;
- which GPIO pins are still free for the PCA9685.

Neither design has been test-printed yet. Print the small fit-test parts listed in each README first (the face + `pi_carrier` before anything else), and measure the parts marked "MEASURE".
