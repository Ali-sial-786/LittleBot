#!/usr/bin/env python3
"""What LittleBot did lately, as a readable timeline, plus how the Pi is doing now.

For "it stops responding for minutes": paste the output into a bug report or chat.

    python3 scripts/log_timeline.py               # the last 30 minutes
    python3 scripts/log_timeline.py --minutes 90
    python3 scripts/log_timeline.py --since 21:40 # from 21:40 today
    python3 scripts/log_timeline.py --all         # every line, not only the useful ones

It reads data/logs/littlebot.log (and its rotated copies), skipping damaged lines (a power cut
while writing leaves zero bytes). Gaps with nothing logged are marked, the voice's many
"LISTENING -> SPEAKING" turns are folded into one line, and warnings stand out. Standard library
only; it changes nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
GAP_S = 60.0
NOISY_LOGGERS = ("uvicorn.access", "httpx", "httpcore", "aiortc", "aioice")
FLIP = re.compile(r"^state (LISTENING|SPEAKING|THINKING) -> (LISTENING|SPEAKING|THINKING) \(live voice\)$")
CALL_START = re.compile(r"live voice: parent started a conversation")
CALL_END = re.compile(r"live voice: ending \((.*)\)$")


def log_files(path: Path) -> list[Path]:
    """Oldest first: littlebot.log.3, .2, .1, then littlebot.log."""
    rotated = sorted(path.parent.glob(path.name + ".*"), key=lambda p: -int(p.suffix[1:]) if p.suffix[1:].isdigit()
                     else 0)
    return [p for p in rotated if p.suffix[1:].isdigit()] + ([path] if path.exists() else [])


def records(paths: list[Path]) -> Iterator[dict[str, Any]]:
    for p in paths:
        with p.open("rb") as f:
            for raw in f:
                line = raw.replace(b"\x00", b"").decode("utf-8", "replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    d = json.loads(line)
                    d["_t"] = dt.datetime.fromisoformat(str(d["ts"]))
                except (ValueError, KeyError, TypeError):
                    continue
                yield d


def human(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 90:
        return f"{seconds} s"
    m, s = divmod(seconds, 60)
    if m < 90:
        return f"{m} min {s} s"
    h, m = divmod(m, 60)
    return f"{h} h {m} min"


def wanted(d: dict[str, Any], every: bool) -> bool:
    if every:
        return True
    if d.get("level") in ("WARNING", "ERROR", "CRITICAL"):
        return True
    logger = str(d.get("logger") or "")
    return d.get("level") == "INFO" and not logger.startswith(NOISY_LOGGERS)


def timeline(recs: list[dict[str, Any]], start: dt.datetime, every: bool) -> tuple[list[str], dict[str, Any]]:
    out: list[str] = []
    stats: dict[str, Any] = {"calls": 0, "ends": {}, "wakes": 0, "warnings": 0, "errors": 0, "stuck": 0,
                             "gaps_in_call": 0}
    in_call = False
    last: dt.datetime | None = None
    flips = 0
    flip_from: dt.datetime | None = None

    def flush_flips() -> None:
        nonlocal flips, flip_from
        if flips:
            out.append(f"{flip_from:%H:%M:%S}  (the voice took {flips} turns of talking and listening)")
        flips, flip_from = 0, None

    for d in recs:
        t = d["_t"]
        if t < start:
            in_call = bool(CALL_START.search(str(d.get("msg")))) or (in_call and not CALL_END.search(str(d.get("msg"))))
            last = t
            continue
        msg = str(d.get("msg") or "")
        if last is not None and (t - last).total_seconds() >= GAP_S:
            flush_flips()
            gap = (t - last).total_seconds()
            where = "DURING A VOICE CHAT" if in_call else "idle"
            if in_call:
                stats["gaps_in_call"] += 1
            out.append(f"          ... nothing logged for {human(gap)} ({where}) ...")
        last = t
        if CALL_START.search(msg):
            in_call = True
            stats["calls"] += 1
        m = CALL_END.search(msg)
        if m:
            in_call = False
            stats["ends"][m.group(1)] = stats["ends"].get(m.group(1), 0) + 1
        if msg.startswith("wake word heard"):
            stats["wakes"] += 1
        if "main loop has been stuck" in msg:
            stats["stuck"] += 1
        level = str(d.get("level") or "")
        if level == "WARNING":
            stats["warnings"] += 1
        elif level in ("ERROR", "CRITICAL"):
            stats["errors"] += 1
        if not wanted(d, every):
            continue
        if FLIP.match(msg) and not every:
            if not flips:
                flip_from = t
            flips += 1
            continue
        flush_flips()
        tag = {"WARNING": "WARN ", "ERROR": "ERROR", "CRITICAL": "ERROR"}.get(level, "     ")
        out.append(f"{t:%H:%M:%S} {tag} {msg}")
        exc = d.get("exc")
        if exc:
            lines = [ln for ln in str(exc).splitlines() if ln.strip()]
            out.extend(f"                 | {ln.strip()}" for ln in (lines[-4:] if len(lines) > 4 else lines))
    flush_flips()
    stats["in_call_now"] = in_call
    return out, stats


def run(cmd: list[str]) -> str:
    if shutil.which(cmd[0]) is None:
        return ""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def system_now() -> list[str]:
    lines = ["", "== The Pi now =="]
    up = run(["uptime"])
    if up:
        lines.append(up)
    mem = {}
    try:
        for ln in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = ln.partition(":")
            if v.split() and v.split()[0].isdigit():
                mem[k] = int(v.split()[0]) // 1024
    except OSError:
        pass
    if mem:
        swap = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
        lines.append(f"memory: {mem.get('MemAvailable', '?')} MB free of {mem.get('MemTotal', '?')} MB; "
                     f"swap: {swap} MB used of {mem.get('SwapTotal', '?')} MB")
    temp = run(["vcgencmd", "measure_temp"])
    throttled = run(["vcgencmd", "get_throttled"])
    if temp or throttled:
        lines.append(" ".join(x for x in (temp, throttled) if x) + "   (throttled=0x0 is good)")
    top = run(["ps", "-eo", "pid,rss,pcpu,etime,comm", "--sort=-rss"])
    if top:
        rows = top.splitlines()[:8]
        lines.append("biggest programs (RSS in KB):")
        lines.extend("  " + r for r in rows)
    disk = run(["df", "-h", "/"])
    if disk:
        lines.append(disk.splitlines()[-1])
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=30.0, help="how far back (default 30)")
    ap.add_argument("--since", help="start at this time today, HH:MM")
    ap.add_argument("--all", action="store_true", help="every line, debug included, nothing folded")
    ap.add_argument("--log", type=Path, default=ROOT / "data" / "logs" / "littlebot.log")
    args = ap.parse_args(argv)
    paths = log_files(args.log)
    if not paths:
        print(f"No log found at {args.log}.")
        return 1
    recs = list(records(paths))
    if not recs:
        print(f"{args.log} has no readable lines.")
        return 1
    end = recs[-1]["_t"]
    if args.since:
        hh, _, mm = args.since.partition(":")
        start = end.replace(hour=int(hh), minute=int(mm or 0), second=0, microsecond=0)
        if start > end:
            start -= dt.timedelta(days=1)
    else:
        start = end - dt.timedelta(minutes=args.minutes)
    lines, stats = timeline(recs, start, args.all)
    print(f"== LittleBot, {start:%d %b %H:%M} to {end:%H:%M:%S} (the last line logged) ==")
    print("\n".join(lines) if lines else "(nothing logged in that time)")
    now = dt.datetime.now()
    quiet = (now - end).total_seconds()
    if quiet >= GAP_S:
        print(f"          ... nothing logged since, for {human(quiet)} ...")
    ends = ", ".join(f"{n} x {why}" for why, n in stats["ends"].items()) or "none"
    print("")
    print(f"== Summary == voice chats started: {stats['calls']} (ended: {ends}); wake words heard: {stats['wakes']}; "
          f"warnings: {stats['warnings']}; errors: {stats['errors']}; main loop stuck: {stats['stuck']} time(s); "
          f"silent gaps during a chat: {stats['gaps_in_call']}" + ("; a chat is on now" if stats["in_call_now"] else ""))
    print("\n".join(system_now()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
