#!/usr/bin/env python3
"""LittleBot battery monitor check: find the RP2040 battery monitor and print its live readings.

Uses the robot's own reader (littlebot/power: the same port search, raw tty, parsing and percentage),
so what you see here is what LittleBot sees. It only reads the port; it never writes to it.
Stop LittleBot first (``systemctl --user stop littlebot``): two programs reading one serial port each
get only some of the lines.

    .venv/bin/python scripts/check_battery.py                 # find the monitor, print readings until Ctrl-C
    .venv/bin/python scripts/check_battery.py --seconds 20    # ... for 20 s, then a summary
    .venv/bin/python scripts/check_battery.py --list          # the candidate ports only (never opens one)
    .venv/bin/python scripts/check_battery.py --port /dev/ttyACM0
    .venv/bin/python scripts/check_battery.py --any-port      # also try ports that don't look like an RP2040
    .venv/bin/python scripts/check_battery.py --raw           # also the JSON line itself
    .venv/bin/python scripts/check_battery.py --simulate      # no hardware: the simulated pack
    .venv/bin/python scripts/check_battery.py --json          # one JSON object per reading

Exit status: 0 = readings received, 1 = a monitor was found but stopped sending, 2 = no monitor found.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from littlebot.power.battery import BatteryGauge, percent_for_pack  # noqa: E402
from littlebot.power.monitor import DEFAULT_GLOBS, SerialMonitor, SimulatedMonitor, candidate_ports  # noqa: E402
from littlebot.power.protocol import Reading  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Print the LittleBot battery monitor's live readings.")
    p.add_argument("--port", help="the serial port (default: search /dev/serial/by-id and /dev/ttyACM*)")
    p.add_argument("--any-port", action="store_true", help="also try serial ports that don't look like an RP2040")
    p.add_argument("--list", action="store_true", help="list the candidate ports and exit")
    p.add_argument("--seconds", type=float, default=0.0, help="stop after this long (default: until Ctrl-C)")
    p.add_argument("--probe", type=float, default=3.0, help="seconds to wait for a line from each port")
    p.add_argument("--simulate", action="store_true", help="no hardware: read the simulated pack")
    p.add_argument("--percent", type=float, default=80.0, help="--simulate: the pack's starting percent")
    p.add_argument("--raw", action="store_true", help="also print each reading as the monitor sent it")
    p.add_argument("--json", action="store_true", help="one JSON object per reading")
    return p


def fmt(value: Any, unit: str, digits: int = 2) -> str:
    return "  --  " if value is None else f"{value:.{digits}f}{unit}"


def flag(value: bool | None) -> str:
    return "?" if value is None else ("yes" if value else "no")


def raw_line(r: Reading) -> str:
    def num(v: float | None, d: int) -> str:
        return "null" if v is None else f"{v:.{d}f}"

    def b(v: bool | None) -> str:
        return "null" if v is None else ("true" if v else "false")

    return (f'{{"dev":"littlebot-battery","fw":{r.fw if r.fw is not None else 1},"v":{num(r.voltage, 2)},'
            f'"cell1":{num(r.cell1, 2)},"t":{num(r.temperature_c, 1)},"chg":{b(r.charging)},"full":{b(r.charged)},'
            f'"up":{r.uptime_s if r.uptime_s is not None else 0}}}')


def show(r: Reading, gauge: BatteryGauge, args: argparse.Namespace) -> None:
    pct = gauge.update(r, time.monotonic())
    if args.json:
        print(json.dumps({"voltage": r.voltage, "cell1": r.cell1, "cell2": r.cell2, "temperature_c": r.temperature_c,
                          "charging": r.charging, "charged": r.charged, "uptime_s": r.uptime_s,
                          "percent_now": None if r.voltage is None else round(percent_for_pack(r.voltage), 1),
                          "percent_smoothed": None if pct is None else round(pct, 1)}), flush=True)
        return
    now_pct = "  --" if r.voltage is None else f"{percent_for_pack(r.voltage):4.0f}"
    smooth = "  --" if pct is None else f"{pct:4.0f}"
    print(f"{time.strftime('%H:%M:%S')}  pack {fmt(r.voltage, ' V')}  cell1 {fmt(r.cell1, ' V')}  "
          f"cell2 {fmt(r.cell2, ' V')}  temp {fmt(r.temperature_c, ' C', 1)}  charging {flag(r.charging):>3}  "
          f"charged {flag(r.charged):>3}  {now_pct} % (smoothed {smooth} %)  up {r.uptime_s} s", flush=True)
    if args.raw:
        print(f"          {raw_line(r)}", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        ports = [args.port] if args.port else candidate_ports(DEFAULT_GLOBS, match_any=args.any_port)
        print("\n".join(ports) if ports else "no candidate ports (is the monitor plugged in? try --any-port)")
        return 0 if ports else 2
    gauge = BatteryGauge()
    count = 0
    deadline = time.monotonic() + args.seconds if args.seconds > 0 else None
    if args.simulate:
        sim = SimulatedMonitor(percent=args.percent, interval_s=0)
        print("simulated battery monitor (no hardware)", file=sys.stderr)
        try:
            while deadline is None or time.monotonic() < deadline:
                show(sim.reading(), gauge, args)
                count += 1
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        return 0
    mon = SerialMonitor(port=args.port, match_any=args.any_port, probe_s=args.probe, scan_s=1.0)
    print("looking for the battery monitor...", file=sys.stderr)
    found = mon.find()
    if found is None:
        tried = [args.port] if args.port else candidate_ports(DEFAULT_GLOBS, match_any=args.any_port)
        print(f"no battery monitor found (tried: {', '.join(tried) or 'no candidate ports'}). Is it plugged in and "
              "flashed (firmware/battery_monitor/README.md)? Is LittleBot still running and reading it?", file=sys.stderr)
        return 2
    port, path, first = found
    print(f"battery monitor on {path}", file=sys.stderr)
    try:
        show(first, gauge, args)
        count += 1

        def on_reading(r: Reading) -> None:
            nonlocal count
            show(r, gauge, args)
            count += 1

        if deadline is not None:
            import threading

            timer = threading.Timer(max(0.0, deadline - time.monotonic()), mon.stop)
            timer.daemon = True
            timer.start()
        why = mon.read_until_lost(port, on_reading)
    except KeyboardInterrupt:
        why = "stopped"
    finally:
        port.close()
    print(f"{count} readings; {why}", file=sys.stderr)
    return 0 if why == "stopped" else 1


if __name__ == "__main__":
    raise SystemExit(main())
