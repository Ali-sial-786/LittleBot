# LittleBot hardware: 3D-printable enclosures

Two complete designs. Both are parametric OpenSCAD models with ready-to-slice STLs and automated interference checks, and both hold the same parts:
- Raspberry Pi 5 (4 GB; every RAM size has the same board) + official Active Cooler and the MPI3508 3.5" HDMI screen. In v1 the screen is **plugged straight onto the Pi's GPIO header** and the stack sits in the head. In v2 (since 2026-10-30) the Pi sits on a **Waveshare UPS HAT (E)** battery in the base and the screen is alone in the head, connected by an HDMI cable, a power cable and 6 touch wires;
- InnoMaker U20CAM-1080P camera;
- Adafruit mini USB mic (3367) + mini USB speaker (3369);
- 3 × MG90S servos (head turn + two arms) and a PCA9685.

| | [v1 "TV head"](enclosure/README.md) | [v2 "companion"](enclosure_v2/README.md) |
|---|---|---|
| Look | ![v1](enclosure/images/front.png) | ![v2](enclosure_v2/images/front.png) |
| Size | 130 × 126 mm footprint, 212 mm tall, head 146 mm wide | 176 × 152 mm footprint, 340 mm tall |
| Parts | 11 STLs (12 prints) | 22 STLs (23 prints) + 2 for the wheel phase |
| Head | screen + Pi stack in the face, mic in the head's side | helmet head, black visor with the screen behind it, LED-ring ears, camera pod on top |
| Neck | geared turntable, head = 0.67 × servo | same |
| Body | camera in the chest, speaker + PCA9685 on the floor plate | speaker + mic in the chest; Pi on its UPS battery, PCA9685, wheel wells, sensor/light-bar plate in the base |
| Future phases | servo supply jack | wheels (TT motors), casters, HC-SR04, ESP32 + TB6612FNG, LED strips |
| Checks | 48 | 69 (incl. 12 mm finger-pinch clearance for the arms) |

v1 is quicker to print and smaller. v2 is the more "toy-like" character and is ready for driving around later.

To regenerate either design after changing a dimension (needs OpenSCAD and Python 3 + numpy):

```bash
cd hardware/enclosure_v2      # or hardware/enclosure
./build.sh                    # STLs + preview images + the part report
./run_checks.sh               # interference checks (+ a self-test and an INFO line)
```

Every dimension was checked against the manufacturers' drawings on 2026-10-23: lcdwiki MPI3508, Raspberry Pi 5 and Active Cooler, InnoMaker, Adafruit, TowerPro. Each README has the table of sources. v1's section *The screen sits on the Pi* covers the stack heights, whether the Active Cooler fits under the screen and which GPIO pins are still free; v2's sections *The screen in the head* and *The Pi and the UPS in the base* cover its layout since 2026-10-30. Waveshare publishes only the UPS HAT (E)'s board size, so v2's stack sizes are estimates: measure them first.

Print the small fit-test parts listed in each README first (v1: the face + `pi_carrier`; v2: the visor + `screen_clamp`), and measure the parts marked "MEASURE".
