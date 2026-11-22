# LittleBot v5: wiring the base

Three drawings, in the order you wire them. The RP2040's pins here are the ones its firmware will use: wire exactly these.

| Drawing | What |
|---|---|
| [`wiring/1_power.png`](wiring/1_power.png) | the 6 × AA, the fuse and the drive switch, and the 4 Wagos on the deck that share out battery +, 5 V and ground |
| [`wiring/2_signals.png`](wiring/2_signals.png) | the RP2040's pins: the Pi (UART), the encoders, the motor driver's control pins, the servos' signal wires, the battery sense |
| [`wiring/3_motors.png`](wiring/3_motors.png) | each motor's 6 wires |

![power](wiring/1_power.png)

## 1. Power

6 × AA (in the drawer) + → 5 A fuse → drive switch tab 1. Switch tab 2 → Wago A. The switch's third tab (its light) stays empty.

| Wago | Slot 1 | Slot 2 | Slot 3 | Slot 4 | Slot 5 |
|---|---|---|---|---|---|
| **A: battery +** (after the switch) | from switch tab 2 | MDD3A green + | converter IN + | 22 kΩ to the battery sense (drawing 2) | spare |
| **B: ground** | 6 × AA holder − (with slack: the drawer slides out) | MDD3A green − | converter IN − | RP2040 GND | link to Wago D |
| **C: 5 V** | converter OUT + | head servo red | left arm servo red | right arm servo red | RP2040 VSYS (or its 5V pin) |
| **D: ground** | converter OUT − | head servo brown | left arm servo brown | right arm servo brown | link to Wago B |

- Wago 221-415: strip 11 mm, lift the lever, push the wire in all the way, close it, tug it.
- The servos' plugs don't fit a Wago: put the bare end of a cut jumper (or of the extension) in the Wago and plug the servo's pins onto its other end.
- 20-22 AWG for the battery and the motors; jumper wire for the 5 V to the servos and the RP2040.
- Nothing from this battery or this 5 V goes to the Pi or the UPS. The two systems share only ground, through the UART's GND (drawing 2).

**Before the cells go in:** switch off; on continuity, Wago A to B and Wago C to D must not beep (a short chirp that stops is the capacitors). Then cells in, switch on: A to B reads 7-8.5 V, C to D reads 5 V. Only then plug in the servos and the RP2040.

![signals](wiring/2_signals.png)

## 2. The RP2040's pins

| Pin | Goes to | |
|---|---|---|
| GP0 (TX) | Pi pin 10 (GPIO15, RXD) | the UART to the Pi: it crosses, TX to RX |
| GP1 (RX) | Pi pin 8 (GPIO14, TXD) | |
| GND | Pi pin 6 | |
| GP6, GP7 | left motor's encoder: yellow (A), green (B) | |
| GP8, GP9 | right motor's encoder: yellow (A), green (B) | |
| 3V3 (OUT) | both encoders' blue | never 5 V: the RP2040's pins take 3.3 V |
| GND | both encoders' black | |
| GP10, GP11 | MDD3A control M1A, M1B (left motor) | the MDD3A's 6-pin header; leave its 5V pin empty |
| GP12, GP13 | MDD3A control M2A, M2B (right motor) | |
| GND | MDD3A control GND | |
| GP14 | head servo, orange | the servos' red and brown go to the Wagos |
| GP15 | left arm servo, orange | |
| GP16 | right arm servo, orange | |
| GP26 (ADC0) | battery sense: Wago A slot 4 → 22 kΩ → GP26 → 10 kΩ → GND | 8.5 V reads 2.7 V |
| VSYS (or 5V), GND | Wago C slot 5, Wago B slot 4 | its power (drawing 1) |

The Pi talks to the RP2040 over its UART (the Pi's 4 USB ports go to the speaker, the ReSpeaker, the screen and the camera). The Pi's side needs `dtparam=uart0=on` in `/boot/firmware/config.txt` once the firmware exists.

![motors](wiring/3_motors.png)

## 3. The motors

| Wire | Left motor (the robot's left) | Right motor |
|---|---|---|
| red (motor +) | MDD3A M1A | MDD3A M2A |
| white (motor −) | MDD3A M1B | MDD3A M2B |
| black (encoder GND) | RP2040 GND (breadboard rail) | the same |
| blue (encoder power) | RP2040 3V3 (breadboard rail) | the same |
| yellow (encoder A) | GP6 | GP8 |
| green (encoder B) | GP7 | GP9 |

A wheel that turns backwards: swap that motor's red and white at the MDD3A (or flip it in the firmware).
