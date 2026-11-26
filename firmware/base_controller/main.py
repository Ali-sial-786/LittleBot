# LittleBot base controller - MicroPython for the RP2040 on the v5 deck (Elecrow RP2040 Core Board; any
# board with the Raspberry Pi Pico's pin layout works).
#
# It drives the 2 wheels (Cytron MDD3A), counts their encoders, moves the 3 servos (head, left arm, right
# arm) and measures the 6 x AA pack. It takes its orders from the Pi over 3 wires (UART0: GP0 TX, GP1 RX,
# GND; 115200 baud), one text line each. No Wi-Fi. The same commands work typed into Thonny's shell
# while the board is on a computer's USB. See README.md for the wiring and the full list:
#
#   PING              -> PONG littlebot-base 1
#   D <l> <r>         wheel speeds, -100..100 (% of the top speed, max_mm_s). Send it at least every
#                     0.5 s: when the orders stop, the wheels ramp to a stop.
#   STOP              both wheels ramp to a stop now
#   S <n> <deg> [dps] servo n (0 head, 1 left arm, 2 right arm) to deg (0..180), dps degrees a second
#   CENTER            all 3 servos to 90 (to fit the pinion and the arms)
#   OFF [n]           servo n (or all 3) limp: no more pulses
#   E                 -> E <left> <right>  encoder counts since start-up (+ = forwards once set up)
#   B                 -> B <volts>  the pack, or B null (nothing on the sense pin)
#   T <hz>            telemetry lines "T <ms> <encL> <encR> <volts>" hz times a second (0 = off, default)
#   GET / SET <name> <value> / SAVE   the settings (SAVE writes cfg.json on the board)
#   PUT <bytes> <crc32>  the next <bytes> bytes are a new main.py (checked, then swapped in); RESET
#
# Safety: the wheels never go faster than max_mm_s (350 mm/s) and speed up and slow down at accel (a
# tall robot pitches over if it stops dead); they stop when the orders stop (0.5 s), when the pack is
# low (low_v) and if this program fails. The servos stay limp until they get a first command.
#
# Everything above run() is plain Python, so the robot's tests import this file in CPython
# (tests/base/test_base_firmware.py) with a fake board.

import json

FW = 1
DEV = "littlebot-base"

# --- pins (hardware/enclosure_v5/WIRING.md) ---
UART_TX, UART_RX = 0, 1
ENC_PINS = ((6, 7), (8, 9))          # (A yellow, B green): left, right
MOTOR_PINS = ((10, 11), (12, 13))    # MDD3A M1A, M1B (left); M2A, M2B (right)
SERVO_PINS = (14, 15, 16)            # head, left arm, right arm (orange wires)
VBAT_PIN = 26                        # ADC0: pack + -> 22 k -> GP26 -> 10 k -> GND
VBAT_RATIO = (22.0 + 10.0) / 10.0
VREF = 3.3
# Nothing on the sense pin? Once a second it's read with its pad's pull-down, then its pull-up (50-80 k inside the
# RP2040): a floating pin follows them rail to rail (~3.3 V apart); with the 22 k / 10 k divider on it (6.9 k) it moves
# under 1 V. A floating ADC pin can otherwise read any steady voltage (10.56 V once, on a bare board).
FLOAT_DIFF_V = 2.2
PADS_BANK0 = 0x4001C000      # RP2040 pad control registers: GPIOn at +4 + 4n; bit 3 = pull-up, bit 2 = pull-down

LOOP_MS = 10
SILENCE_MS = 500
MOTOR_PWM_HZ = 20000                 # the MDD3A takes up to 20 kHz: out of hearing
SERVO_PWM_HZ = 50

DEFAULTS = {
    "max_mm_s": 350,      # top speed (README: about 0.35 m/s, or a sudden stop can pitch it over its caster)
    "accel": 500,         # mm/s per second, up and down: 0 to top speed in 0.7 s
    "wheel_mm": 67,       # the kits' "65 mm" wheels measure about 67
    "rpm6": 170,          # the motors' no-load speed at 6 V (open-loop estimate, scaled by the pack's voltage)
    "kick": 0.06,         # extra duty when moving, to get over the gearbox's friction
    "cpr": 0,             # encoder counts per wheel turn (base_test.py calibrate); 0 = not set: no speed trim
    "ki": 0.0008,         # speed trim (with cpr): duty per (mm/s x s) of error
    "trim_max": 0.25,     # the trim never moves the duty more than this
    "minv_l": 0, "minv_r": 0,     # 1: that motor turns the other way (instead of swapping its red and white)
    "einv_l": 0, "einv_r": 0,     # 1: that encoder counts the other way
    "swap": 0,            # 1: the robot's left wheel is on M2 (encoder GP8/GP9) and its right on M1 (GP6/GP7)
    "drv8871": 0,         # 1: two DRV8871 boards instead of the MDD3A (see motor_pins)
    "low_v": 5.8,         # the wheels stop under this pack voltage (0 = never); the servos keep working
    "us_min": 500, "us_max": 2500,                  # servo pulse at 0 and at 180 degrees
    "s0_min": 0, "s0_max": 180, "s1_min": 0, "s1_max": 180, "s2_min": 0, "s2_max": 180,   # servo limits (deg)
}


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def approach(x, target, step):
    """x moved towards target by at most step."""
    if x < target:
        return min(x + step, target)
    return max(x - step, target)


def servo_us(deg, us_min, us_max):
    """The pulse width (us) for an angle of 0..180 degrees."""
    return us_min + (us_max - us_min) * clamp(deg, 0, 180) / 180.0


def pack_volts(raw_u16):
    """The pack's voltage from a read_u16() of the sense pin, or None when it can't be one: under 3 V (nothing
    there), or pinned at the top of the range (10.3 V and up: more than 6 x AA can give). An RP2040 board whose ADC
    reference is unconnected reads 65535 on every pin: the YD-RP2040 ships with its VREF jumper open."""
    if raw_u16 >= 64000:
        return None
    v = raw_u16 * VREF / 65535 * VBAT_RATIO
    return v if v >= 3.0 else None


def floating(low_raw, high_raw):
    """True when the sense pin, read with the pull-down (low_raw) and then the pull-up (high_raw), follows the pulls:
    nothing is connected to it."""
    return (high_raw - low_raw) * VREF / 65535 > FLOAT_DIFF_V


def free_speed_mm_s(cfg, vbat):
    """The wheels' no-load speed at this pack voltage (unknown pack: assume a fresh 9 V one, the fastest)."""
    v = vbat if vbat else 9.0
    return cfg["rpm6"] * (v / 6.0) / 60.0 * 3.14159 * cfg["wheel_mm"]


def open_loop_duty(cfg, v_mm_s, vbat):
    """-1..1: the duty that turns a wheel at about v_mm_s, before any trim."""
    if v_mm_s == 0:
        return 0.0
    d = abs(v_mm_s) / free_speed_mm_s(cfg, vbat)
    d = clamp(cfg["kick"] + d * (1.0 - cfg["kick"]), 0.0, 1.0)
    return d if v_mm_s > 0 else -d


def motor_pins(duty, drv8871=0):
    """The two inputs' PWM (duty_u16) for one motor at duty -1..1.
    MDD3A: A pulsed, B low (forwards); B pulsed, A low (backwards).
    DRV8871: one input held high and the other pulsed low for the duty's share: drive / brake. Pulsing one input
    with the other low (as for the MDD3A) has it drive / coast, and at 20 kHz its current dies away in the gaps: the
    wheels don't move. Both low (stopped) lets it sleep."""
    if duty == 0:
        return 0, 0
    d = int(min(abs(duty), 1.0) * 65535)
    if drv8871:
        return (65535, 65535 - d) if duty > 0 else (65535 - d, 65535)
    return (d, 0) if duty > 0 else (0, d)


def crc32(data, crc=0):
    """zlib's CRC-32 (binascii.crc32 isn't in every MicroPython build)."""
    crc = ~crc & 0xFFFFFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 & -(crc & 1))
    return ~crc & 0xFFFFFFFF


class Base:
    """Everything but the hardware: the board (or a test's fake) gives it
    set_motor(i, duty -1..1, drv8871), set_servo(i, us or None), counts() -> [l, r], vbat() -> volts or None,
    and write_file(name, data) / swap_main() for PUT."""

    def __init__(self, hw, cfg=None):
        self.hw = hw
        self.cfg = dict(DEFAULTS)
        if cfg:
            self.cfg.update({k: v for k, v in cfg.items() if k in DEFAULTS})
        self.now = 0
        self.last_d = -SILENCE_MS
        self.target = [0.0, 0.0]          # mm/s asked for
        self.v = [0.0, 0.0]               # mm/s now (ramped)
        self.trim = [0.0, 0.0]
        self.prev_counts = [0, 0]
        self.meas = [0.0, 0.0]            # mm/s measured (cpr set)
        self.servo_target = [None, None, None]
        self.servo_pos = [None, None, None]
        self.servo_dps = [120.0, 120.0, 120.0]
        self.vbat = None
        self.low = False
        self.low_said = -10000
        self.tele_ms = 0
        self.tele_last = 0
        self.put = None                   # [size, crc, bytearray] while a PUT is coming in

    # --- the wheels ---
    def drive(self, l_pct, r_pct):
        top = self.cfg["max_mm_s"]
        self.target = [clamp(l_pct, -100, 100) / 100.0 * top, clamp(r_pct, -100, 100) / 100.0 * top]
        self.last_d = self.now

    def stop(self):
        self.target = [0.0, 0.0]

    def signed_counts(self):
        c = self.hw.counts()
        if self.cfg["swap"]:
            c = [c[1], c[0]]
        return [-c[0] if self.cfg["einv_l"] else c[0], -c[1] if self.cfg["einv_r"] else c[1]]

    def step(self, dt_ms):
        """One control period: dt_ms since the last."""
        self.now += dt_ms
        dt = dt_ms / 1000.0
        self.vbat = self.hw.vbat()
        lv = self.cfg["low_v"]
        if self.vbat is not None and lv:
            if self.vbat < lv:
                self.low = True
            elif self.vbat > lv + 0.3:
                self.low = False
        if self.now - self.last_d > SILENCE_MS or self.low:
            self.target = [0.0, 0.0]
        counts = self.signed_counts()
        cpr = self.cfg["cpr"]
        for i in (0, 1):
            self.v[i] = approach(self.v[i], self.target[i], self.cfg["accel"] * dt)
            duty = open_loop_duty(self.cfg, self.v[i], self.vbat)
            if cpr and dt > 0:
                mm = (counts[i] - self.prev_counts[i]) / cpr * 3.14159 * self.cfg["wheel_mm"]
                self.meas[i] = 0.7 * self.meas[i] + 0.3 * (mm / dt)
                if self.v[i] == 0:
                    self.trim[i] = 0.0
                else:
                    tm = self.cfg["trim_max"]
                    self.trim[i] = clamp(self.trim[i] + self.cfg["ki"] * (self.v[i] - self.meas[i]) * dt, -tm, tm)
                    duty = clamp(duty + self.trim[i], -1.0, 1.0)
                    if duty * self.v[i] < 0:          # the trim may slow a wheel, never reverse it
                        duty = 0.0
            if self.cfg["minv_l" if i == 0 else "minv_r"]:
                duty = -duty
            self.hw.set_motor(i ^ 1 if self.cfg["swap"] else i, duty, self.cfg["drv8871"])
        self.prev_counts = counts
        for i in (0, 1, 2):
            t = self.servo_target[i]
            if t is None:
                continue
            p = self.servo_pos[i]
            p = t if p is None else approach(p, t, self.servo_dps[i] * dt)
            self.servo_pos[i] = p
            self.hw.set_servo(i, servo_us(p, self.cfg["us_min"], self.cfg["us_max"]))
        out = []
        if self.low and self.now - self.low_said >= 5000:
            self.low_said = self.now
            out.append("LOWBAT %.2f" % self.vbat)
        if self.tele_ms and self.now - self.tele_last >= self.tele_ms:
            self.tele_last = self.now
            out.append("T %d %d %d %s" % (self.now, counts[0], counts[1], self.volts_text()))
        return out

    def volts_text(self):
        return "null" if self.vbat is None else "%.2f" % self.vbat

    def all_off(self):
        self.target = [0.0, 0.0]
        self.v = [0.0, 0.0]
        for i in (0, 1):
            self.hw.set_motor(i, 0.0, self.cfg["drv8871"])
        for i in (0, 1, 2):
            self.servo_target[i] = None
            self.servo_pos[i] = None
            self.hw.set_servo(i, None)

    # --- the servos ---
    def servo(self, n, deg, dps=None):
        lo, hi = self.cfg["s%d_min" % n], self.cfg["s%d_max" % n]
        self.servo_target[n] = clamp(deg, lo, hi)
        if dps:
            self.servo_dps[n] = clamp(dps, 5, 600)

    # --- PUT: a new main.py, its bytes straight after the PUT line ---
    def feed_put(self, data):
        """Bytes while a PUT is open; returns (bytes not used, replies)."""
        size, crc, buf = self.put
        take = data[: size - len(buf)]
        buf.extend(take)
        rest = data[len(take):]
        if len(buf) < size:
            return rest, []
        self.put = None
        if crc32(buf) != crc:
            return rest, ["ERR put: crc"]
        try:
            src = bytes(buf).decode()
            check = compile  # not in every MicroPython build: then only the CRC checks it
        except NameError:
            check = None
        except Exception as e:  # noqa: BLE001 - not text
            return rest, ["ERR put: %s" % e]
        if check:
            try:
                check(src, "main.py", "exec")
            except Exception as e:  # noqa: BLE001 - any syntax error
                return rest, ["ERR put: %s" % e]
        src = None
        self.hw.write_file("main_new.py", bytes(buf))
        self.hw.swap_main()
        return rest, ["OK put %d (RESET to run it)" % size]

    # --- one line of text ---
    def handle(self, line):
        """Replies to one command line (a list of lines, maybe empty)."""
        parts = line.strip().split()
        if not parts:
            return []
        cmd, args = parts[0].upper(), parts[1:]
        try:
            if cmd == "PING":
                return ["PONG %s %d" % (DEV, FW)]
            if cmd == "D":
                self.drive(float(args[0]), float(args[1]))
                return []
            if cmd == "STOP":
                self.stop()
                return ["OK"]
            if cmd == "S":
                n = int(args[0])
                if n not in (0, 1, 2):
                    return ["ERR servo 0, 1 or 2"]
                self.servo(n, float(args[1]), float(args[2]) if len(args) > 2 else None)
                return []
            if cmd == "CENTER":
                for n in (0, 1, 2):
                    self.servo(n, 90)
                return ["OK"]
            if cmd == "OFF":
                for n in ([int(args[0])] if args else (0, 1, 2)):
                    self.servo_target[n] = None
                    self.servo_pos[n] = None
                    self.hw.set_servo(n, None)
                return ["OK"]
            if cmd == "E":
                c = self.signed_counts()
                return ["E %d %d" % (c[0], c[1])]
            if cmd == "B":
                return ["B " + self.volts_text()]
            if cmd == "T":
                hz = float(args[0])
                self.tele_ms = int(1000 / hz) if hz > 0 else 0
                return ["OK"]
            if cmd == "GET":
                return ["CFG " + json.dumps(self.cfg)]
            if cmd == "SET":
                name = args[0]
                if name not in DEFAULTS:
                    return ["ERR unknown setting " + name]
                val = float(args[1])
                self.cfg[name] = int(val) if val == int(val) and isinstance(DEFAULTS[name], int) else val
                return ["OK %s %s" % (name, self.cfg[name])]
            if cmd == "SAVE":
                self.hw.write_file("cfg.json", json.dumps(self.cfg).encode())
                return ["OK saved"]
            if cmd == "PUT":
                size, crc = int(args[0]), int(args[1], 0)
                if not 0 < size <= 60000:
                    return ["ERR put: size"]
                self.all_off()
                self.put = [size, crc, bytearray()]
                return ["READY %d" % size]
            if cmd == "HELP":
                return ["PING D STOP S CENTER OFF E B T GET SET SAVE PUT RESET"]
        except (IndexError, ValueError) as e:
            return ["ERR %s: %s" % (cmd, e)]
        return ["ERR unknown " + cmd]


class Lines:
    """Bytes in, whole lines out (a PUT's raw bytes go to the Base instead)."""

    def __init__(self, base):
        self.base = base
        self.buf = b""

    def feed(self, data):
        out = []
        while data:
            if self.base.put is not None:
                data, replies = self.base.feed_put(data)
                out.extend(("reply", r) for r in replies)
                continue
            i = data.find(b"\n")
            if i < 0:
                self.buf = (self.buf + data)[-200:]
                break
            line, data = self.buf + data[:i], data[i + 1:]
            self.buf = b""
            text = line.decode("utf-8", "ignore").strip()
            if text.upper() == "RESET":
                out.append(("reset", None))
                continue
            out.extend(("reply", r) for r in self.base.handle(text))
        return out


# ----------------------------------------------------------------------------- the board
def run():
    import os
    import sys
    import time

    import machine
    from machine import ADC, PWM, Pin, UART

    try:
        import select
        poll = select.poll()
        poll.register(sys.stdin, select.POLLIN)
    except Exception:  # noqa: BLE001 - no USB console input: the UART alone
        poll = None

    class Board:
        def __init__(self):
            self.uart = UART(0, baudrate=115200, tx=Pin(UART_TX), rx=Pin(UART_RX), rxbuf=1024, txbuf=1024, timeout_char=20)
            self.motor = []
            for a, b in MOTOR_PINS:
                pa, pb = PWM(Pin(a)), PWM(Pin(b))
                for p in (pa, pb):
                    p.freq(MOTOR_PWM_HZ)
                    p.duty_u16(0)
                self.motor.append((pa, pb))
            self.servo_pins = [Pin(p, Pin.OUT, value=0) for p in SERVO_PINS]
            self.servo_pwm = [None, None, None]
            self.cnt = [0, 0]
            self.enc_b = []
            for i, (a, b) in enumerate(ENC_PINS):
                pin_a = Pin(a, Pin.IN, Pin.PULL_UP)
                self.enc_b.append(Pin(b, Pin.IN, Pin.PULL_UP))
                pin_a.irq(trigger=Pin.IRQ_RISING, handler=self._enc_handler(i))
            self.adc = ADC(Pin(VBAT_PIN))
            self.sense_ok = False
            self.probe_at = 0

        def _enc_handler(self, i):
            b = self.enc_b[i]
            cnt = self.cnt

            def h(_pin):
                cnt[i] += -1 if b.value() else 1
            return h

        def set_motor(self, i, duty, drv8871=0):
            pa, pb = self.motor[i]
            a, b = motor_pins(duty, drv8871)
            for p, v in sorted(((pa, a), (pb, b)), key=lambda t: t[1]):   # the lower one first: never both up on the way
                p.duty_u16(v)

        def set_servo(self, i, us):
            p = self.servo_pwm[i]
            if us is None:
                if p is not None:
                    p.deinit()
                    self.servo_pwm[i] = None
                    self.servo_pins[i] = Pin(SERVO_PINS[i], Pin.OUT, value=0)
                return
            if p is None:
                p = PWM(Pin(SERVO_PINS[i]))
                p.freq(SERVO_PWM_HZ)
                self.servo_pwm[i] = p
            p.duty_ns(int(us * 1000))

        def counts(self):
            return [self.cnt[0], self.cnt[1]]

        def _read(self, n=4):
            s = 0
            for _ in range(n):
                s += self.adc.read_u16()
            return s // n

        def _pulls(self, up, down):
            from machine import mem32
            a = PADS_BANK0 + 4 + 4 * VBAT_PIN
            mem32[a] = (mem32[a] & ~0b1100) | (0b1000 if up else 0) | (0b0100 if down else 0)

        def vbat(self):
            now = time.ticks_ms()
            if time.ticks_diff(now, self.probe_at) >= 1000:      # is anything on the pin?
                self.probe_at = now
                try:
                    self._pulls(False, True)
                    time.sleep_us(300)
                    lo = self._read(8)
                    self._pulls(True, False)
                    time.sleep_us(300)
                    hi = self._read(8)
                finally:
                    self._pulls(False, False)
                time.sleep_us(300)
                self.sense_ok = not floating(lo, hi)
            return pack_volts(self._read()) if self.sense_ok else None

        def write_file(self, name, data):
            with open(name, "wb") as f:
                f.write(data)

        def swap_main(self):
            try:
                os.remove("main_prev.py")
            except OSError:
                pass
            os.rename("main.py", "main_prev.py")
            os.rename("main_new.py", "main.py")

    cfg = None
    try:
        with open("cfg.json") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass

    hw = Board()
    base = Base(hw, cfg)
    uart_lines, usb_lines = Lines(base), Lines(base)

    def say(dest, text):
        if dest == "uart":
            # all of it: a long line (GET's CFG is 300+ bytes) can outgrow the UART's send buffer, and a
            # write that returns early would cut it off before its newline (the Pi then sees no reply)
            data = text.encode() + b"\n"
            sent, t0 = 0, time.ticks_ms()
            while sent < len(data) and time.ticks_diff(time.ticks_ms(), t0) < 500:
                n = hw.uart.write(data[sent:])
                sent += len(data) - sent if n is None else n
                if not n:
                    time.sleep_ms(1)
        else:
            print(text)

    say("uart", "HELLO %s %d" % (DEV, FW))
    print("HELLO %s %d (type HELP)" % (DEV, FW))
    last = time.ticks_ms()
    try:
        while True:
            for dest, lines, data in (("uart", uart_lines, hw.uart.read() if hw.uart.any() else None),
                                      ("usb", usb_lines, sys.stdin.read(1).encode() if poll and poll.poll(0) else None)):
                if not data:
                    continue
                for kind, text in lines.feed(data):
                    if kind == "reset":
                        base.all_off()
                        time.sleep_ms(50)
                        machine.reset()
                    say(dest, text)
            now = time.ticks_ms()
            dt = time.ticks_diff(now, last)
            if dt >= LOOP_MS:
                last = now
                for text in base.step(min(dt, 100)):
                    say("uart", text)
            else:
                time.sleep_ms(1)
    except KeyboardInterrupt:
        base.all_off()
        print("stopped (wheels and servos off)")
    except Exception as e:  # noqa: BLE001 - anything at all: stop the wheels, say why, start again
        base.all_off()
        say("uart", "ERR crash %r" % e)
        sys.print_exception(e)
        time.sleep_ms(1000)
        machine.reset()


if __name__ == "__main__":
    run()
