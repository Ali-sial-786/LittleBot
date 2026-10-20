#!/usr/bin/env python3
"""Sample a running LittleBot process (CPU, memory, threads) plus system health.

    .venv/bin/python scripts/perf_monitor.py                     # find the app, 5 min every 2 s
    .venv/bin/python scripts/perf_monitor.py --duration 15 --interval 5
    .venv/bin/python scripts/perf_monitor.py --pid 1234 --children
    .venv/bin/python scripts/perf_monitor.py --self --samples 1    # smoke test against itself

Every sample prints one line and is appended to a CSV file (default
``data/perf/perf-YYYYmmdd-HHMMSS.csv``). At the end (or on Ctrl-C) an
average/maximum summary is printed and written next to the CSV as
``*.summary.json``. Columns:

  proc_cpu_pct     CPU of the process (100 = one full core; the Pi 5 has 4 cores)
  proc_rss_mb      resident memory of the process (+ children with --children)
  chromium_rss_mb  total RSS of all Chromium processes (the YouTube player)
  sys_cpu_pct      whole-system CPU (100 = all cores busy)
  sys_mem_avail_mb / sys_mem_used_pct / swap_used_mb
  temp_c, arm_mhz, throttled (vcgencmd get_throttled flags, Pi only)

Uses psutil when available (it is a LittleBot dependency); falls back to
/proc on Linux so it also runs with a bare system python3. Read-only.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagnose as _diag  # noqa: E402  (sibling script: shared Pi helpers)

try:
    import psutil
except ImportError:  # pragma: no cover - exercised only without the venv
    psutil = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
COLUMNS = [
    "timestamp", "elapsed_s", "pid", "proc_cpu_pct", "proc_rss_mb", "proc_threads",
    "children", "chromium_rss_mb", "sys_cpu_pct", "sys_mem_avail_mb", "sys_mem_used_pct",
    "swap_used_mb", "temp_c", "arm_mhz", "throttled",
]
NUMERIC = [c for c in COLUMNS if c not in ("timestamp", "pid", "throttled", "elapsed_s")]


class ProcessGone(Exception):
    pass


# ---------------------------------------------------------------------------
# Samplers
# ---------------------------------------------------------------------------


class PsutilSampler:
    def __init__(self, pid: int, children: bool) -> None:
        self.proc = psutil.Process(pid)
        self.children = children
        self._child_cache: dict[int, Any] = {}
        self.proc.cpu_percent(None)
        psutil.cpu_percent(None)

    def _procs(self) -> list[Any]:
        procs = [self.proc]
        if self.children:
            try:
                for c in self.proc.children(recursive=True):
                    if c.pid not in self._child_cache:
                        c.cpu_percent(None)  # prime; first reading of a new child counts as 0
                        self._child_cache[c.pid] = c
                    procs.append(self._child_cache[c.pid])
            except psutil.Error:
                pass
        return procs

    def sample(self) -> dict[str, Any]:
        try:
            if not self.proc.is_running() or self.proc.status() == psutil.STATUS_ZOMBIE:
                raise ProcessGone()
            cpu = rss = 0.0
            threads = 0
            procs = self._procs()
            for p in procs:
                try:
                    with p.oneshot():
                        cpu += p.cpu_percent(None)
                        rss += p.memory_info().rss
                        threads += p.num_threads()
                except psutil.Error:
                    if p is self.proc:
                        raise
        except psutil.NoSuchProcess as exc:
            raise ProcessGone() from exc
        chromium = 0
        for p in psutil.process_iter(["name", "memory_info"]):
            name = (p.info.get("name") or "").lower()
            mem = p.info.get("memory_info")
            if "chrom" in name and mem is not None:
                chromium += mem.rss
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        return {
            "proc_cpu_pct": round(cpu, 1),
            "proc_rss_mb": round(rss / 1_048_576, 1),
            "proc_threads": threads,
            "children": len(procs) - 1,
            "chromium_rss_mb": round(chromium / 1_048_576, 1),
            "sys_cpu_pct": round(psutil.cpu_percent(None), 1),
            "sys_mem_avail_mb": round(vm.available / 1_048_576),
            "sys_mem_used_pct": round(vm.percent, 1),
            "swap_used_mb": round(sw.used / 1_048_576),
        }


class ProcSampler:
    """Minimal Linux /proc fallback (no children / Chromium totals)."""

    def __init__(self, pid: int, children: bool) -> None:
        self.pid = pid
        self.ticks = os.sysconf("SC_CLK_TCK")
        self._last = (self._proc_ticks(), self._sys_ticks(), time.monotonic())

    def _proc_ticks(self) -> int:
        text = _diag.read_text(f"/proc/{self.pid}/stat")
        if not text:
            raise ProcessGone()
        fields = text.rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])  # utime + stime

    @staticmethod
    def _sys_ticks() -> tuple[int, int]:
        parts = [int(x) for x in (_diag.read_text("/proc/stat") or "cpu 0 0 0 0").splitlines()[0].split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return sum(parts), idle

    def sample(self) -> dict[str, Any]:
        pticks, (stotal, sidle), now = self._proc_ticks(), self._sys_ticks(), time.monotonic()
        lp, (lt, li), lnow = self._last
        self._last = (pticks, (stotal, sidle), now)
        wall = max(now - lnow, 1e-6)
        status = _diag.read_text(f"/proc/{self.pid}/status") or ""
        rss = threads = 0
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
            elif line.startswith("Threads:"):
                threads = int(line.split()[1])
        mi = _diag.meminfo()
        total, avail = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
        dtot = max(stotal - lt, 1)
        return {
            "proc_cpu_pct": round(100.0 * (pticks - lp) / self.ticks / wall, 1),
            "proc_rss_mb": round(rss / 1024, 1),
            "proc_threads": threads,
            "children": 0,
            "chromium_rss_mb": None,
            "sys_cpu_pct": round(100.0 * (1 - (sidle - li) / dtot), 1),
            "sys_mem_avail_mb": avail // 1024,
            "sys_mem_used_pct": round(100.0 * (1 - avail / total), 1) if total else None,
            "swap_used_mb": (mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) // 1024,
        }


def system_extras() -> dict[str, Any]:
    flags, _src = _diag.read_throttled() if _diag.pi_model() else (None, "")
    freq = (_diag.read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") or "").strip()
    return {
        "temp_c": _diag.read_cpu_temperature(),
        "arm_mhz": int(freq) // 1000 if freq.isdigit() else None,
        "throttled": hex(flags) if flags is not None else "",
    }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def find_target(args: argparse.Namespace) -> int | None:
    if args.self:
        return os.getpid()
    if args.pid:
        return args.pid
    deadline = time.monotonic() + max(0.0, args.wait)
    while True:
        if args.match:
            me = os.getpid()
            pids = [pid for pid, _c, argv in _diag.iter_processes()
                    if pid != me and argv and args.match in " ".join(argv) and "perf_monitor.py" not in " ".join(argv)]
        else:
            pids = _diag.find_littlebot_pids()
        if pids:
            if len(pids) > 1:
                print(f"note: several matching processes {pids}; monitoring the oldest ({pids[0]})", file=sys.stderr)
            return pids[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(1.0)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"samples": len(rows)}
    if not rows:
        return out
    out["duration_s"] = rows[-1]["elapsed_s"]
    for col in NUMERIC:
        vals = [float(r[col]) for r in rows if isinstance(r.get(col), (int, float))]
        if vals:
            out[col] = {"avg": round(sum(vals) / len(vals), 1), "max": round(max(vals), 1), "min": round(min(vals), 1)}
    combined = 0
    for r in rows:
        if r.get("throttled"):
            combined |= int(str(r["throttled"]), 16)
    out["throttled_any"] = hex(combined)
    out["throttled_flags"] = [d["meaning"] for d in _diag.decode_throttled(combined)]
    return out


def fmt_line(row: dict[str, Any]) -> str:
    def f(key: str, unit: str = "", spec: str = ".1f") -> str:
        v = row.get(key)
        return "-" if v is None or v == "" else f"{v:{spec}}{unit}"

    return (f"{row['timestamp'][11:19]}  cpu {f('proc_cpu_pct', '%'):>7}  rss {f('proc_rss_mb', 'MB'):>9}  "
            f"thr {row.get('proc_threads', '-'):>3}  chromium {f('chromium_rss_mb', 'MB'):>8}  "
            f"sys {f('sys_cpu_pct', '%'):>6}  avail {f('sys_mem_avail_mb', 'MB', '.0f'):>7}  "
            f"swap {f('swap_used_mb', 'MB', '.0f'):>6}  temp {f('temp_c', 'C'):>6}  "
            f"{f('arm_mhz', 'MHz', '.0f'):>8}  thr-flags {row.get('throttled') or '-'}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sample LittleBot CPU/memory/temperature and write a CSV + summary.")
    target = p.add_mutually_exclusive_group()
    target.add_argument("--pid", type=int, help="process id to monitor")
    target.add_argument("--match", help="monitor the first process whose command line contains this text")
    target.add_argument("--self", action="store_true", help="monitor this script itself (smoke test)")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between samples (default 2)")
    p.add_argument("--duration", type=float, default=5.0, help="minutes to run (default 5)")
    p.add_argument("--samples", type=int, help="stop after N samples (overrides --duration)")
    p.add_argument("--wait", type=float, default=0.0, help="wait up to N seconds for the process to appear")
    p.add_argument("--children", action="store_true", help="include child processes (e.g. a Chromium kiosk started by the app)")
    p.add_argument("--csv", help="CSV output path (default data/perf/perf-<timestamp>.csv)")
    p.add_argument("--no-csv", action="store_true", help="do not write CSV/summary files")
    p.add_argument("--json", action="store_true", help="print the summary as JSON at the end")
    p.add_argument("--quiet", "-q", action="store_true", help="no per-sample lines")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.interval <= 0:
        print("--interval must be > 0", file=sys.stderr)
        return 2
    pid = find_target(args)
    if pid is None:
        what = f"matching {args.match!r}" if args.match else "running LittleBot (python -m littlebot)"
        print(f"no process {what} found. Start the app first, or pass --pid / --wait.", file=sys.stderr)
        return 2
    sampler_cls = PsutilSampler if psutil is not None else (ProcSampler if os.path.isdir("/proc") else None)
    if sampler_cls is None:
        print("psutil is required on this platform: pip install psutil", file=sys.stderr)
        return 2
    try:
        sampler = sampler_cls(pid, args.children)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot monitor pid {pid}: {exc}", file=sys.stderr)
        return 2

    csv_path: Path | None = None
    if not args.no_csv:
        csv_path = Path(args.csv) if args.csv else REPO_ROOT / "data" / "perf" / f"perf-{dt.datetime.now():%Y%m%d-%H%M%S}.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
    total = args.samples if args.samples else max(1, int(round(args.duration * 60 / args.interval)))
    cmd = " ".join(next((a for p_, _c, a in _diag.iter_processes() if p_ == pid), [])) or "?"
    if not args.quiet:
        print(f"monitoring pid {pid} ({cmd[:100]}) - {total} sample(s) every {args.interval:g}s"
              + (f" -> {csv_path}" if csv_path else ""))

    stop = {"flag": False}

    def _stop(_sig: int, _frm: Any) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    fh = csv_path.open("w", newline="", encoding="utf-8") if csv_path else None
    writer = csv.DictWriter(fh, fieldnames=COLUMNS) if fh else None
    if writer:
        writer.writeheader()
    ended = "completed"
    try:
        next_t = started
        for _ in range(total):
            next_t += args.interval
            while not stop["flag"] and time.monotonic() < next_t:
                time.sleep(min(0.2, max(0.0, next_t - time.monotonic())))
            if stop["flag"]:
                ended = "interrupted"
                break
            try:
                data = sampler.sample()
            except ProcessGone:
                ended = "process exited"
                break
            row = {"timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                   "elapsed_s": round(time.monotonic() - started, 1), "pid": pid, **data, **system_extras()}
            rows.append(row)
            if writer and fh:
                writer.writerow(row)
                fh.flush()
            if not args.quiet:
                print(fmt_line(row), flush=True)
    finally:
        if fh:
            fh.close()

    summary = {"pid": pid, "command": cmd, "ended": ended, "csv": str(csv_path) if csv_path else None, **summarize(rows)}
    if csv_path:
        csv_path.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"\nsummary ({summary['samples']} samples, {ended}):")
        for col in ("proc_cpu_pct", "proc_rss_mb", "proc_threads", "chromium_rss_mb", "sys_cpu_pct",
                    "sys_mem_avail_mb", "swap_used_mb", "temp_c", "arm_mhz"):
            if col in summary:
                s = summary[col]
                print(f"  {col:<17} avg {s['avg']:>8}  max {s['max']:>8}  min {s['min']:>8}")
        print(f"  throttled_any     {summary.get('throttled_any', '0x0')} {'; '.join(summary.get('throttled_flags', []))}")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
