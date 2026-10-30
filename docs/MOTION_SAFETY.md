# Motion safety (servos)

LittleBot will get four MG90S micro servos (head pan, head tilt, left arm, right arm; the tilt is
optional) driven by a PCA9685 PWM board on the Raspberry Pi 5's I2C bus (what the software does
with them: [MOVEMENT.md](MOVEMENT.md)). It is meant for a ~10-month-old child.
**Until every step in this document has been done, the servos stay simulated.**

> **Software tests do not establish physical toy safety.** The motion code limits angles,
> speed and acceleration, and it can switch the outputs off. It cannot stop a pinched finger,
> a swallowed screw, a hot servo or a wiring fault. Those depend on the mechanical design,
> the enclosure, the power wiring and adult supervision. A home-built robot is not a certified
> toy (EN 71 / ASTM F963). Use it only with an adult present and within reach.

---

## 1. How physical motion is gated (two keys)

| Key | Who | Where | Persisted? |
|-----|-----|-------|------------|
| **1. Configuration**: `motion.backend: pca9685` **and** `motion.physical_enabled: true` **and** a valid `config/calibration.yaml` that covers **every** joint | the person who built and verified the hardware | `config/local.yaml` (git-ignored) + calibration file | yes |
| **2. Arming**: `motion.physical_armed = true` | a parent, in the dashboard, each session | settings store | **never**. Every start begins disarmed |

* If key 1 is missing in any way, `MotionController` uses `MockPCA9685`, an emulated chip.
  `status()` then reports `simulation_only: true` and a `reason`. The real driver
  (`PCA9685Driver`, `smbus2`) is never constructed and `smbus2` is never imported.
* If key 1 is present but the board does not answer, or `smbus2` is missing, the controller
  falls back to simulation and reports `PCA9685 unavailable: ...` (health: degraded).
* With key 1 present, the driver initialises the chip with **all outputs off**. Motion commands
  are refused and nothing is energised until key 2 (arming) is turned on.
* Disarming switches every output off immediately, from whichever thread made the change.
* Motion only ever happens when something explicitly commands it (an activity step, a
  dashboard button, a gesture). The controller never moves on its own. The only exception
  is the slow return to neutral after a parent's STOP (section 6), which only affects joints
  that are already energised.

## 2. Wiring

```
 Raspberry Pi 5 (40-pin header)              PCA9685 16-channel board
 -----------------------------               ------------------------------------------
 pin 1   3V3  ------------------------------> VCC   logic supply (3.3 V ONLY)
 pin 3   GPIO2 / SDA1 ----------------------> SDA
 pin 5   GPIO3 / SCL1 ----------------------> SCL
 pin 6   GND  ------------------------------> GND   (logic side)
 (option) a GPIO + pull-up -----------------> OE    HIGH = all outputs disabled (see 3.2)
 pin 2/4 5V   --X  NOT connected to V+  X--

 Separate regulated 5-6 V servo supply (>= 3-4 A, section 4)
   (+) --[ ON/OFF switch ]--[ fuse ~3 A ]---> V+  screw terminal (servo power rail)
   (-) -------------------------------------> GND screw terminal
                                              (COMMON GROUND with the Pi through the board)
   470-1000 uF electrolytic across V+/GND at the board (observe polarity)

 Servo headers (columns: PWM | V+ | GND)        MG90S lead colours
   channel 0 -> head (pan)  orange = PWM, red = V+, brown = GND
   channel 1 -> left_arm
   channel 2 -> right_arm
   channel 3 -> head_tilt   (optional: without it, list only the other joints in motion.servos)
```

**With the MPI3508 screen plugged onto the header** (the way LittleBot's enclosures mount it),
the screen's socket covers pins 1-26, so pins 1, 3, 5 and 6 can't take jumper wires, and pins
27-40 have only about 7.5 mm under the screen: too little for jumper housings. Reach the same
pins in one of these ways:

* a **GPIO stacking header** between the Pi and the screen (its long pins stick out beside the
  screen's socket; it also raises the screen about 8.5 mm, which the enclosure models handle with
  one parameter, `pi_hdr_h` = 11: see `hardware/enclosure_v2/README.md`);
* a **GPIO splitter / extender** with two 40-pin sockets; or
* wires **soldered to the underside** of the Pi's header pins 1, 3, 5 and 6.

Same pins, same rules (3.3 V for VCC); only the way to reach them changes.

The Pi 5 keeps its own official 5 V / 5 A USB-C supply. **Never** power servos from the
Pi's 5 V pins: stall and inrush currents brown out the Pi, corrupt the SD card and can
damage the board.

## 3. PCA9685 notes

### 3.1 V+ versus VCC
* **VCC** is the chip's logic supply. Connect it to the Pi's **3.3 V** so the I2C levels
  match. Do not put 5 V on it.
* **V+** is only passed through to the servo headers (the red wire). The chip does not use
  it. Connect it to the external 5-6 V servo supply.
* The grounds must be common, otherwise the PWM signal has no reference.

### 3.2 OE pin as a hardware disable
`OE` (output enable) is active-low and is pulled low on most breakout boards, which leaves the
outputs enabled. Pulling it **high** (to VCC) disables every output at once in hardware,
whatever the software does. Recommended options, best first:
1. A physical **power switch on V+** that the supervising adult can reach. This is the
   real emergency stop.
2. OE wired to a switch (to 3.3 V), or to a GPIO with a pull-up resistor so that outputs stay
   **disabled** until software drives the GPIO low. That design fails safe when the Pi is off
   or crashed. *LittleBot's software does not drive OE yet*, so with option 2 you must add
   that later or tie OE low after testing.

The PCA9685 keeps generating the **last** pulses on its own if the Pi crashes, hangs or loses
the I2C connection. The servos then hold their position energised. A software e-stop cannot
reach a chip it cannot talk to, so the V+ switch (or OE) is the backstop.

### 3.3 Enabling I2C on the Pi 5
```bash
sudo raspi-config            # Interface Options -> I2C -> Enable   (or add dtparam=i2c_arm=on
                             # to /boot/firmware/config.txt), then reboot
ls /dev/i2c-1                # must exist
sudo apt install -y i2c-tools
sudo usermod -aG i2c $USER   # log out / in afterwards
i2cdetect -y 1               # expect "40" (the board) and usually "70" (PCA9685 all-call)
pip install -e '.[pi]'       # installs smbus2 (only the real driver imports it)
```
If `40` does not show, check SDA/SCL (pins 3/5), VCC, GND and the address jumpers (A0-A5
change the address. Update `motion.i2c_address` if you change them).

### 3.4 What the driver does
* Init (datasheet order): read MODE1 (does anything ACK?), write the full-OFF bit to every
  output (`ALL_LED_OFF_H = 0x10`), set MODE2 = totem-pole, MODE1 = SLEEP, write `PRE_SCALE`
  (121 for 50 Hz: `round(25 MHz / (4096 x 50)) - 1`), wake with auto-increment, wait 1 ms,
  then read back and verify. A device that does not behave like a PCA9685 is refused.
* Each pulse is `ON = 0`, `OFF = round(pulse_us x 4096 / period_us)`. At 50 Hz: 1500 us -> 307
  counts. Pulses are hard-limited to 500-2500 us in the driver and per joint in the joint layer.
* `all_off()` is a **single** I2C write (`ALL_LED_OFF_H = 0x10`). Full-OFF wins over full-ON.
* `close()` switches all outputs off, puts the chip to sleep and closes the bus.

## 4. Power budget

| Item | Typical | Worst case |
|------|---------|-----------|
| MG90S idle (holding, no load) | ~10 mA | |
| MG90S moving (light arm) | 100-250 mA | |
| MG90S **stall** (blocked, or pushed by a child) | | **~0.7-1 A each** |
| 3 servos all stalled | | **~3 A** |
| 4 servos all stalled (with head_tilt) | | **~4 A** |
| PCA9685 logic (from the Pi 3.3 V) | < 10 mA | |

* Use a **regulated 5-6 V supply rated at least 3 A, preferably 4 A** (stall current of all
  servos plus margin; with the tilt servo, 4 A, preferably 5 A). A 5 V / 4 A adapter or a UBEC is fine. At 6 V the MG90S is faster
  and stronger. 5 V is gentler and is recommended here.
* Fit a fuse (~3 A slow-blow or a polyfuse) and an ON/OFF switch in the V+ line. Use wire
  rated for the current (22 AWG or thicker for short runs).
* A 470-1000 uF capacitor across V+/GND at the board absorbs inrush when servos start.
* Measure V+ under load during the test pattern. It must stay within 4.8-6.0 V.
* `motion.release_when_idle_seconds` (default 0 = hold) can de-energise idle servos to cut
  holding current, heat and buzz. Trade-off: a released arm can be pushed by hand, and on
  the next move it is re-energised at its last known angle. If it was moved meanwhile, it
  snaps back at servo speed.

## 5. Calibration procedure

Do this with the child **out of the room**, horns/arms detached for the first run of
every servo, and **LittleBot itself stopped** (`sudo systemctl stop littlebot` if it runs as a
service). Only one program may drive the PCA9685 at a time.

1. Complete checklist parts **A-C** in section 8 (power off while wiring).
2. In `config/local.yaml` (git-ignored) set **only**:
   ```yaml
   motion:
     physical_enabled: true   # hardware verified; calibration + test pattern allowed
     backend: mock            # robot still simulates until calibration is done
   ```
3. Practise in simulation first: `python scripts/calibrate_servos.py --simulate`.
4. Per joint:
   `python scripts/calibrate_servos.py --hardware --i-understand-the-risks --joint head`
   * The script prints the safety checklist and requires you to type `yes`.
   * It energises **only this servo**, at its **configured neutral** pulse. A servo that was
     somewhere else moves there at full servo speed, which is why the horn is off.
   * With power **off** (`o` to go limp, or cut V+), fit the horn/arm so the joint is in its
     resting pose. Re-energise with `r`.
   * Step with `+`/`-` (1-5 degrees; `1`..`5` set the step size). Find each end of travel
     **slowly** and stop **5-10 degrees before** any contact, binding, cable strain or
     mechanical stop. Mark them with `[` (min) and `]` (max).
   * Return with `c` (walks back in small steps). Adjust, then mark neutral with `n`.
   * `d` asks which way is positive: for the head, *+ turns to the robot's LEFT*; for the
     arms, *+ raises the arm*. This sets `positive_toward`.
   * `s` saves the joint (checks `min < neutral < max`). `q` aborts without saving.
     Ctrl-C switches all outputs off.
5. Repeat for every joint. The file `config/calibration.yaml` must cover **all** joints,
   otherwise physical motion stays disabled. Format: `config/calibration.example.yaml`.
6. `python scripts/servo_test_pattern.py --hardware --i-understand-the-risks --speed-factor 0.2`
   sweeps each joint neutral -> min -> max -> neutral slowly. Watch for contact, buzzing
   (stall), binding and supply voltage sag.
7. Complete checklist part **D**, then set `motion.backend: pca9685`, start LittleBot,
   and arm motion from the dashboard (child still absent) for the first gesture trials.

## 6. Stop behaviour

| Action | Triggered by | Motion | Outputs | Latched? | Recovery |
|--------|--------------|--------|---------|----------|----------|
| `cancel()` | activities / dashboard | decelerates smoothly where it is (deceleration <= the joint's acceleration limit) | stay energised (holding) | no | next command |
| **STOP_ALL** (big STOP button) | parent | cancel everything, then return **slowly** to neutral (`homing_speed_factor`, default 40 % of max speed). Only already-energised joints move; `cancel()` right after does not abort this | energised | **no** | next command (it supersedes the return) |
| `emergency_stop(source)` | dashboard e-stop, shutdown, driver errors | immediate halt | **all off**, one I2C write, done synchronously in the caller's thread before anything else | **yes**: every command is refused | `reset_emergency_stop()` once motion is idle. Joints become *unknown*; the next command energises at neutral, settles 0.5 s, then eases to its target |
| Disarm (`motion.physical_armed = false`) | parent | immediate halt | all off | until re-armed | arm again |
| I2C / driver error | automatic | halt | all-off attempted | yes (`source: driver_error`, `system.error` published, health degraded) | fix the wiring; reset re-probes and re-initialises the chip |
| LittleBot shutdown | app | `emergency_stop("shutdown")`, then the driver closes (outputs off, chip asleep) | off | | |
| Pi crash / I2C cable pulled | | the PCA9685 keeps the **last pulses** | energised | | V+ switch / OE (section 3.2) |

STOP_ALL is not latched because it is the routine "calm down" button. Returning slowly to the
resting pose leaves the robot in a known, compact posture. If you prefer the arms to stay
where they are, set `motion.stop_all_returns_to_neutral: false`.

## 7. Mechanical safety

* **Pinch points.** Wherever a moving arm or head approaches the body or another part, a gap
  closes. Make sure no gap that closes can trap a finger. Use generous clearances or fixed
  shrouds, and check them across the whole calibrated range. The relevant toy standard
  (EN 71-1 / ASTM F963) is authoritative on gap sizes; this document gives no numbers.
* **Torque limits.** An MG90S produces ~1.8-2.2 kg-cm stall torque (about 0.4 kgf at the
  tip of a 5 cm arm). That is small but enough to pinch. Prefer soft, light, rounded arms
  (foam or silicone), a friction clutch or a flexible link so an arm yields when held, and
  mechanical end stops just beyond the software limits. Software limits apply only while
  the software works.
* **Software limits** (always on): hard clamps on angles (`min_angle..max_angle`) and pulses
  (per servo and absolute 500-2500 us), max speed (<= 180 deg/s hard ceiling, defaults
  60-90 deg/s), max acceleration (default 200 deg/s^2), minimum-jerk easing, a per-tick
  rate limiter, a pause instead of a jump if the event loop stalls, and gestures with small
  amplitudes at reduced speed that always end at neutral.
* **Small parts.** Servo horns, horn screws, gears, nuts, magnets and any button cells are
  choking hazards. Everything must be inside an enclosure that needs a tool to open, fixed
  with screws (not only glue). Check that nothing can be twisted or chewed off.
* **Enclosure.** Cover the servo gears, the PCB, the wiring and the supply. Use strain relief
  on every cable, no sharp edges, ventilation for the Pi 5, and a stable base that cannot tip
  or be pulled over by its cable. Keep the supply and cables out of the child's reach.
* **Heat and wear.** A stalled servo heats up fast and strips its gears. Watch for buzzing,
  check temperature after 10 minutes of gestures, and replace worn servos.
* **Hair and clothing** can be caught by a horn. Keep the horns covered.

## 8. Verification checklist

### A. Electrical (before `physical_enabled: true`), power OFF while wiring
- [ ] Separate regulated 5-6 V servo supply, rated >= 3 A. Voltage measured.
- [ ] Pi 5 V pins **not** connected to V+ (checked with a continuity tester).
- [ ] Common ground: continuity between supply (-) and Pi GND.
- [ ] Fuse and a reachable ON/OFF switch in the V+ line. Bulk capacitor fitted, polarity OK.
- [ ] VCC from Pi 3.3 V; SDA -> pin 3, SCL -> pin 5.
- [ ] I2C enabled; `i2cdetect -y 1` shows `40`.
- [ ] If OE is wired: pulling it high makes the servos go limp.

### B. Software (on the Pi, still simulated)
- [ ] `.venv/bin/python -m pytest -q tests/motion` passes.
- [ ] `python scripts/servo_test_pattern.py --fast` runs.
- [ ] The dashboard shows motion as *simulation only* with a sensible reason.

### C. Mechanical pre-check
- [ ] Horns and arms detached for each servo's first power-up.
- [ ] Enclosure design reviewed for pinch points and small parts (section 7).

-> Now `physical_enabled: true` (backend still `mock`) and calibrate (section 5).

### D. Before `backend: pca9685` and arming
- [ ] Every joint calibrated, with 5-10 degree margin from any contact.
- [ ] Test pattern at `--speed-factor 0.2`: no contact, no stall buzz, no binding; V+ stays
      within 4.8-6.0 V.
- [ ] After start the dashboard shows *physical, NOT armed*. Nothing moves before arming.
- [ ] Arm, run every gesture once (child absent). Movements are slow and stay in range.
- [ ] **E-stop test**: e-stop during a gesture makes the servos go limp at once; commands are
      refused; reset works; the first move afterwards starts gently from neutral.
- [ ] **STOP test**: the big STOP halts the gesture and returns slowly to neutral.
- [ ] **Disarm test**: switching arming off makes the servos go limp at once.
- [ ] **Driver-error test** (arms detached): unplug SDA during a gesture. The dashboard shows
      a latched e-stop with `driver_error`. Note that the chip holds the last pulse; use the V+
      switch. Reconnect and reset.
- [ ] **Restart test**: after restarting LittleBot, motion is disarmed and nothing moves.
- [ ] **Thermal test**: 10 minutes of gestures, servos only warm.

### E. Final (before the child is present)
- [ ] Enclosure closed with screws, no reachable small parts, pinch points re-checked
      across the full range with the arms fitted.
- [ ] Cables and supply out of reach; base stable.
- [ ] An adult supervises whenever motion is armed and can reach the V+ switch.
- [ ] Start with the child at a distance; watch the reaction (some babies are startled by
      movement and servo noise).

Re-run part D after any change to wiring, servos, horns, arms, calibration or motion code.
