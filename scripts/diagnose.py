#!/usr/bin/env python3
"""LittleBot one-stop diagnostics.

Runs anywhere (Raspberry Pi, desktop, CI container) using only the Python
standard library. Optional pieces (the littlebot package, sounddevice, cv2)
are probed with the project's virtualenv interpreter (``.venv/bin/python``)
in a subprocess, so this script also works with the system ``python3`` before
the installer has run.

It only *reads* system state. It never captures images or audio, never plays
sound, never writes to I2C/GPIO, never moves servos and never prints secret
values (API keys are reported as present/absent only).

    python3 scripts/diagnose.py                # human-readable report
    python3 scripts/diagnose.py --json         # machine-readable (paste into a bug report)
    python3 scripts/diagnose.py --verbose      # include details for every check
    python3 scripts/diagnose.py --i2c-scan     # also probe the I2C bus (read-only probes)
    python3 scripts/diagnose.py --skip-scripts # do not run the per-device check scripts

Status words: PASS (good), WARN (works but needs attention), FAIL (broken /
unsafe), SKIP (not applicable here), INFO (facts for the report).
Exit status: 0 = no FAIL, 1 = at least one FAIL, 2 = WARNs present and --strict.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import platform
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
STATUSES = ("PASS", "WARN", "FAIL", "SKIP", "INFO")
SECTIONS = (
    "platform", "os", "python", "packages", "config", "power", "memory", "storage",
    "display", "touch", "camera", "audio", "i2c", "network", "secrets", "service",
    "device_scripts",
)
SECRET_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
BOOT_CONFIG_CANDIDATES = (Path("/boot/firmware/config.txt"), Path("/boot/config.txt"))

# ---------------------------------------------------------------------------
# Small helpers (also imported by scripts/perf_monitor.py)
# ---------------------------------------------------------------------------


def read_text(path: str | Path, default: str | None = None, limit: int = 1_000_000) -> str | None:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", "replace")
    except OSError:
        return default


def run_cmd(cmd: list[str], timeout: float = 8.0, env: dict[str, str] | None = None,
            cwd: Path | None = None) -> tuple[int | None, str, str]:
    """Run a command if it exists. Returns (returncode | None, stdout, stderr)."""
    exe = shutil.which(cmd[0]) if os.sep not in cmd[0] else (cmd[0] if os.path.exists(cmd[0]) else None)
    if exe is None:
        return None, "", f"{cmd[0]}: command not found"
    full_env = dict(os.environ)
    full_env.setdefault("LC_ALL", "C")
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, errors="replace",
                              timeout=timeout, env=full_env, cwd=str(cwd) if cwd else None,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, "", f"{cmd[0]}: timed out after {timeout:.0f}s"
    except OSError as exc:
        return None, "", f"{cmd[0]}: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


def pi_model() -> str | None:
    for p in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        text = read_text(p)
        if text and "raspberry pi" in text.lower():
            return text.strip("\x00\n ")
    return None


def read_cpu_temperature() -> float | None:
    text = read_text("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int((text or "").strip()) / 1000.0, 1)
    except ValueError:
        return None


THROTTLE_BITS: dict[int, tuple[str, str, str]] = {
    0: ("under_voltage_now", "under-voltage detected NOW (supply below ~4.63 V)", "FAIL"),
    1: ("arm_freq_capped_now", "ARM frequency capped now", "WARN"),
    2: ("throttled_now", "CPU currently throttled", "WARN"),
    3: ("soft_temp_limit_now", "soft temperature limit active now", "WARN"),
    16: ("under_voltage_occurred", "under-voltage has occurred since boot", "WARN"),
    17: ("arm_freq_capped_occurred", "ARM frequency capping has occurred since boot", "WARN"),
    18: ("throttling_occurred", "throttling has occurred since boot", "WARN"),
    19: ("soft_temp_limit_occurred", "soft temperature limit has occurred since boot", "WARN"),
}


def decode_throttled(value: int) -> list[dict[str, Any]]:
    """Decode `vcgencmd get_throttled` bits into a list of {bit, key, meaning, severity}."""
    return [
        {"bit": bit, "key": key, "meaning": meaning, "severity": sev}
        for bit, (key, meaning, sev) in sorted(THROTTLE_BITS.items())
        if value & (1 << bit)
    ]


def read_throttled() -> tuple[int | None, str]:
    """Return (flags, source). Uses vcgencmd, then the firmware sysfs node (path differs per model)."""
    rc, out, _ = run_cmd(["vcgencmd", "get_throttled"], timeout=4)
    if rc == 0 and "=" in out:
        try:
            return int(out.strip().split("=", 1)[1], 16), "vcgencmd"
        except ValueError:
            pass
    patterns = ("/sys/devices/platform/soc/soc:firmware/get_throttled",
                "/sys/devices/platform/*/*firmware*/get_throttled",
                "/sys/devices/platform/*/*/*firmware*/get_throttled")
    for pattern in patterns:
        for path in glob.glob(pattern):
            text = (read_text(path) or "").strip()
            try:
                return int(text, 16), path
            except ValueError:
                continue
    return None, "unavailable"


def iter_processes() -> Iterator[tuple[int, str, list[str]]]:
    """Yield (pid, comm, argv) for every readable process (Linux /proc)."""
    for entry in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(entry.rsplit("/", 1)[1])
        except ValueError:
            continue
        comm = (read_text(f"{entry}/comm") or "").strip()
        raw = read_text(f"{entry}/cmdline") or ""
        argv = [a for a in raw.split("\x00") if a]
        yield pid, comm, argv


def is_littlebot_argv(argv: list[str]) -> bool:
    """True for ``python [opts] -m littlebot ...``, the ``littlebot`` console script, or main.py.

    Works on the argv *elements* (not the joined string), so a shell whose ``-c`` command
    text merely mentions "-m littlebot" is never mistaken for the app.
    """
    if not argv:
        return False
    exe = os.path.basename(argv[0])
    if exe == "littlebot":
        return True
    if not exe.startswith("python"):
        return False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "-m":
            return i + 1 < len(argv) and argv[i + 1] == "littlebot"
        if a.startswith("-m"):
            return a[2:] == "littlebot"
        if a.startswith("-c"):
            return False
        if a in ("-X", "-W"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        path = a.replace("\\", "/")
        return os.path.basename(path) == "littlebot" or any(
            path == tail or path.endswith("/" + tail) for tail in ("littlebot/app/main.py", "littlebot/__main__.py"))
    return False


def find_littlebot_pids(exclude: set[int] | None = None) -> list[int]:
    """PIDs of running LittleBot app processes (``python -m littlebot`` or the console script)."""
    skip = {os.getpid()} | (exclude or set())
    return sorted(pid for pid, _comm, argv in iter_processes() if pid not in skip and is_littlebot_argv(argv))


def parse_edid(data: bytes) -> dict[str, Any]:
    """Tiny EDID 1.x parser: manufacturer, monitor name, preferred (first detailed) timing."""
    if len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return {}
    m = (data[8] << 8) | data[9]
    out: dict[str, Any] = {
        "manufacturer": "".join(chr(((m >> s) & 0x1F) + 64) for s in (10, 5, 0)),
        "product_code": data[10] | (data[11] << 8),
        "extensions": data[126],
    }
    for off in (54, 72, 90, 108):
        d = data[off:off + 18]
        clock = d[0] | (d[1] << 8)  # units of 10 kHz
        if clock:
            if "preferred" not in out:
                h = d[2] | ((d[4] & 0xF0) << 4)
                hblank = d[3] | ((d[4] & 0x0F) << 8)
                v = d[5] | ((d[7] & 0xF0) << 4)
                vblank = d[6] | ((d[7] & 0x0F) << 8)
                total = (h + hblank) * (v + vblank)
                refresh = (clock * 10_000) / total if total else 0
                out["preferred"] = f"{h}x{v}@{refresh:.0f}"
                out["pixel_clock_mhz"] = clock / 100
        elif d[3] == 0xFC:
            out["name"] = d[5:18].split(b"\n")[0].decode("ascii", "replace").strip()
    return out


def boot_config_path() -> Path | None:
    for p in BOOT_CONFIG_CANDIDATES:
        if p.is_file():
            return p
    return None


def boot_config_lines() -> list[str]:
    p = boot_config_path()
    if p is None:
        return []
    lines = []
    for raw in (read_text(p) or "").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in (read_text("/proc/meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key.strip()] = int(parts[0])  # kB
    return out


def user_groups() -> set[str]:
    try:
        import grp

        return {grp.getgrgid(g).gr_name for g in os.getgroups()}
    except (ImportError, KeyError, OSError):
        return set()


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    status: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "status": self.status, "message": self.message}
        if self.details:
            d["details"] = self.details
        if self.hint:
            d["hint"] = self.hint
        return d


class Report:
    def __init__(self) -> None:
        self.sections: dict[str, list[Check]] = {s: [] for s in SECTIONS}

    def add(self, section: str, name: str, status: str, message: str,
            details: dict[str, Any] | None = None, hint: str | None = None) -> Check:
        assert status in STATUSES, status
        c = Check(name, status, message, details or {}, hint)
        self.sections.setdefault(section, []).append(c)
        return c

    def counts(self) -> dict[str, int]:
        counts = {s: 0 for s in STATUSES}
        for checks in self.sections.values():
            for c in checks:
                counts[c.status] += 1
        return counts


@dataclass
class Ctx:
    args: argparse.Namespace
    is_pi: bool
    model: str | None
    python: str  # interpreter used for package / config / device probes
    mode: str
    config: dict[str, Any] | None = None

    def sev(self, on_pi: str, elsewhere: str = "WARN") -> str:
        """Severity of a missing Pi device: real problem on the Pi, just a note elsewhere."""
        return on_pi if self.is_pi else elsewhere

    def cfg(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.config or {}
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def probe_json(ctx: Ctx, snippet: str, *argv: str, timeout: float = 30.0) -> dict[str, Any]:
    """Run a Python snippet with the target interpreter and parse its JSON stdout."""
    rc, out, err = run_cmd([ctx.python, "-c", snippet, *argv], timeout=timeout, cwd=REPO_ROOT,
                           env={"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy",
                                "PYGAME_HIDE_SUPPORT_PROMPT": "1"})
    if rc is None:
        return {"error": err.strip() or "could not run probe"}
    lines = [ln for ln in out.strip().splitlines() if ln.startswith("{")]
    if not lines:
        tail = (err.strip().splitlines() or ["no output"])[-1]
        return {"error": f"probe failed (exit {rc}): {tail}"}
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return {"error": f"bad probe output: {exc}"}


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def check_platform(r: Report, ctx: Ctx) -> None:
    S = "platform"
    if ctx.model:
        status = "PASS" if "Raspberry Pi 5" in ctx.model else "WARN"
        r.add(S, "model", status, ctx.model,
              hint=None if status == "PASS" else "LittleBot is developed for the Raspberry Pi 5; other models are untested.")
    else:
        r.add(S, "model", "INFO", f"not a Raspberry Pi ({platform.system()} {platform.machine()}); Pi-specific checks are skipped")

    cpuinfo = read_text("/proc/cpuinfo") or ""
    m = re.search(r"^Revision\s*:\s*([0-9a-fA-F]+)", cpuinfo, re.M)
    if m and ctx.is_pi:
        rev = int(m.group(1), 16)
        if rev & (1 << 23):  # new-style revision code
            mem = {0: "256 MB", 1: "512 MB", 2: "1 GB", 3: "2 GB", 4: "4 GB", 5: "8 GB", 6: "16 GB"}.get((rev >> 20) & 7, "?")
            r.add(S, "board_revision", "INFO", f"revision {m.group(1)} ({mem} RAM)", {"revision": m.group(1), "ram": mem})

    bits = struct.calcsize("P") * 8
    machine = platform.machine()
    if ctx.is_pi:
        if machine == "aarch64" and bits == 64:
            r.add(S, "architecture", "PASS", f"{machine}, {bits}-bit userland")
        else:
            r.add(S, "architecture", "FAIL", f"{machine}, {bits}-bit userland",
                  hint="Flash the 64-bit Raspberry Pi OS: pip wheels for pygame-ce/OpenCV are 64-bit (aarch64).")
    else:
        r.add(S, "architecture", "INFO", f"{machine}, {bits}-bit")

    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else None
    r.add(S, "kernel", "INFO", f"{platform.release()} (page size {page // 1024 if page else '?'} KiB)",
          {"release": platform.release(), "page_size": page},
          hint="Pi 5 kernels use 16 KiB pages; very old binaries built for 4 KiB pages can crash." if page == 16384 else None)

    if ctx.is_pi:
        rc, out, _ = run_cmd(["vcgencmd", "bootloader_version"], timeout=4)
        if rc == 0 and out.strip():
            r.add(S, "bootloader", "INFO", out.strip().splitlines()[0], {"raw": out.strip()})
        else:
            r.add(S, "bootloader", "SKIP", "vcgencmd bootloader_version unavailable")


def check_os(r: Report, ctx: Ctx) -> None:
    S = "os"
    osr: dict[str, str] = {}
    for line in (read_text("/etc/os-release") or "").splitlines():
        k, _, v = line.partition("=")
        if k:
            osr[k.strip()] = v.strip().strip('"')
    pretty = osr.get("PRETTY_NAME", platform.platform())
    codename = osr.get("VERSION_CODENAME", "")
    debian_like = osr.get("ID") in ("debian", "raspbian") or "debian" in osr.get("ID_LIKE", "")
    details = {"id": osr.get("ID"), "version_id": osr.get("VERSION_ID"), "codename": codename}
    if ctx.is_pi:
        if codename in ("bookworm", "trixie"):
            r.add(S, "release", "PASS", f"{pretty} ({codename})", details)
        elif debian_like:
            r.add(S, "release", "WARN", f"{pretty} ({codename or 'unknown codename'})", details,
                  hint="Tested targets are Raspberry Pi OS Bookworm (Debian 12) and Trixie (Debian 13).")
        else:
            r.add(S, "release", "WARN", pretty, details, hint="Use 64-bit Raspberry Pi OS with desktop.")
    else:
        r.add(S, "release", "INFO", pretty, details)

    issue = read_text("/etc/rpi-issue")
    if issue:
        r.add(S, "image", "INFO", issue.strip().splitlines()[0])

    bc = boot_config_path()
    if bc:
        r.add(S, "boot_config", "INFO", str(bc))
    elif ctx.is_pi:
        r.add(S, "boot_config", "WARN", "config.txt not found in /boot/firmware or /boot")

    up = read_text("/proc/uptime")
    if up:
        secs = float(up.split()[0])
        r.add(S, "uptime", "INFO", f"{secs / 3600:.1f} h")


PACKAGE_SPECS: list[tuple[str, list[str], str, str]] = [
    # (import name, distribution names, level when missing, purpose)
    ("pygame", ["pygame-ce", "pygame"], "FAIL", "face display"),
    ("numpy", ["numpy"], "FAIL", "audio/vision maths"),
    ("yaml", ["PyYAML"], "FAIL", "config files"),
    ("pydantic", ["pydantic"], "FAIL", "config validation"),
    ("fastapi", ["fastapi"], "FAIL", "parent dashboard"),
    ("uvicorn", ["uvicorn"], "FAIL", "dashboard web server"),
    ("httpx", ["httpx"], "FAIL", "cloud AI client"),
    ("psutil", ["psutil"], "FAIL", "system monitor"),
    ("cv2", ["opencv-python-headless", "opencv-python", "opencv-contrib-python-headless"], "WARN", "camera + face detection (vision extra or apt python3-opencv)"),
    ("sounddevice", ["sounddevice"], "WARN", "microphone/speaker (audio extra)"),
    ("smbus2", ["smbus2"], "INFO", "PCA9685 servo driver (pi extra)"),
    ("vosk", ["vosk"], "INFO", "optional offline speech recognition"),
    ("littlebot", ["littlebot"], "FAIL", "the app itself (pip install -e .)"),
]

PKG_SNIPPET = r"""
import importlib.metadata as md, importlib.util, json, sys
specs = json.loads(sys.argv[1])
out = {}
for mod, dists in specs:
    try:
        importable = importlib.util.find_spec(mod) is not None
    except Exception:
        importable = False
    info = {"importable": importable, "dist": None, "version": None}
    for d in dists:
        try:
            info["version"] = md.version(d)
            info["dist"] = d
            break
        except md.PackageNotFoundError:
            pass
    out[mod] = info
both = {}
for d in ("pygame", "pygame-ce"):
    try:
        both[d] = md.version(d)
    except md.PackageNotFoundError:
        pass
out["_pygame_dists"] = both
out["_python"] = sys.version.split()[0]
out["_prefix"] = sys.prefix
print(json.dumps(out))
"""


def check_python(r: Report, ctx: Ctx) -> None:
    S = "python"
    v = sys.version_info
    status = "PASS" if v >= (3, 11) else "FAIL"
    r.add(S, "diagnose_interpreter", status if status == "FAIL" else "INFO",
          f"{platform.python_version()} at {sys.executable}",
          hint="LittleBot needs Python >= 3.11." if status == "FAIL" else None)
    venv_dir = REPO_ROOT / ".venv"
    venv_py = venv_dir / "bin" / "python"
    if venv_py.exists():
        cfg = read_text(venv_dir / "pyvenv.cfg") or ""
        sys_site = re.search(r"include-system-site-packages\s*=\s*true", cfg, re.I) is not None
        version = re.search(r"^version(?:_info)?\s*=\s*(\S+)", cfg, re.M)
        rc, out, _ = run_cmd([str(venv_py), "-c", "import sys; print(sys.version.split()[0])"], timeout=15)
        if rc == 0:
            pyv = out.strip()
            ok = tuple(int(x) for x in pyv.split(".")[:2]) >= (3, 11)
            r.add(S, "venv", "PASS" if ok else "FAIL",
                  f".venv works (Python {pyv}{', system site-packages' if sys_site else ''})",
                  {"path": str(venv_dir), "python": pyv, "system_site_packages": sys_site,
                   "created_with": version.group(1) if version else None})
        else:
            r.add(S, "venv", "FAIL", ".venv exists but its python does not run",
                  hint="The venv was probably copied from another machine or Python was upgraded. "
                       "Move it aside (mv .venv .venv.old) and re-run scripts/install_pi.sh.")
    else:
        r.add(S, "venv", ctx.sev("FAIL", "WARN"), ".venv not found",
              hint="Run scripts/install_pi.sh (Pi) or python3 -m venv .venv && .venv/bin/pip install -e '.[vision,audio,dev]'.")
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if not in_venv and venv_py.exists():
        r.add(S, "probe_interpreter", "INFO", f"package/device probes use {ctx.python}")


def check_packages(r: Report, ctx: Ctx) -> None:
    S = "packages"
    specs = [(mod, dists) for mod, dists, _lvl, _p in PACKAGE_SPECS]
    res = probe_json(ctx, PKG_SNIPPET, json.dumps(specs))
    if "error" in res:
        r.add(S, "probe", "FAIL", f"could not inspect packages with {ctx.python}: {res['error']}")
        return
    r.add(S, "interpreter", "INFO", f"Python {res.get('_python')} ({res.get('_prefix')})")
    for mod, _dists, level, purpose in PACKAGE_SPECS:
        info = res.get(mod, {})
        if info.get("importable"):
            ver = info.get("version") or "system/apt package"
            label = f"{info.get('dist') or mod} {ver}"
            r.add(S, mod, "PASS", f"{label} - {purpose}", info)
        else:
            r.add(S, mod, level, f"not installed - {purpose}", info,
                  hint="Re-run scripts/install_pi.sh, or .venv/bin/pip install -e '.[vision,audio,pi]'.")
    both = res.get("_pygame_dists") or {}
    if "pygame" in both and "pygame-ce" in both:
        r.add(S, "pygame_conflict", "FAIL", f"both pygame {both['pygame']} and pygame-ce {both['pygame-ce']} installed",
              hint="They overwrite each other's files: .venv/bin/pip uninstall -y pygame pygame-ce && .venv/bin/pip install pygame-ce")


CONFIG_SNIPPET = r"""
import json, sys
from pathlib import Path
try:
    from littlebot.core.config import load_config
    from littlebot.core.paths import CONFIG_DIR, PROJECT_ROOT
except Exception as e:
    print(json.dumps({"error": "cannot import littlebot: %s: %s" % (type(e).__name__, e)}))
    raise SystemExit(0)
try:
    cfg = load_config(sys.argv[1])
except Exception as e:
    print(json.dumps({"error": "config invalid: %s" % (str(e).splitlines()[0] if str(e) else type(e).__name__,)}))
    raise SystemExit(0)
d = cfg.public_dict()
calib = Path(cfg.motion.calibration_file)
if not calib.is_absolute():
    calib = PROJECT_ROOT / calib
d["_calibration_exists"] = calib.is_file()
d["_calibration_path"] = str(calib)
d["_calibration_valid"] = None
d["_calibration_error"] = None
if calib.is_file():
    try:  # same validation the motion module applies before it will energise anything
        from littlebot.motion.joints import build_joint_specs, load_calibration
    except Exception as e:
        d["_calibration_error"] = "motion module unavailable: %s" % (e,)
    else:
        try:
            cal = load_calibration(calib, known_joints=list(cfg.motion.servos), require_all=True)
            build_joint_specs(cfg.motion, cal)
            d["_calibration_valid"] = True
        except Exception as e:
            d["_calibration_valid"] = False
            d["_calibration_error"] = (str(e).splitlines() or [type(e).__name__])[0][:300]
d["_local_yaml"] = (CONFIG_DIR / "local.yaml").is_file()
d["_data_path"] = str(cfg.data_path)
print(json.dumps(d))
"""


def check_config(r: Report, ctx: Ctx) -> None:
    S = "config"
    res = probe_json(ctx, CONFIG_SNIPPET, ctx.mode)
    if "error" in res:
        r.add(S, "load", "FAIL", f"mode={ctx.mode}: {res['error']}",
              hint="Fix config/local.yaml (compare with config/hardware.example.yaml).")
        return
    ctx.config = res
    local = res.get("_local_yaml")
    r.add(S, "load", "PASS", f"mode={ctx.mode} loads; config/local.yaml {'present' if local else 'absent'}")
    if ctx.is_pi and not local:
        r.add(S, "local_yaml", "WARN", "config/local.yaml missing",
              hint="cp config/hardware.example.yaml config/local.yaml (install_pi.sh does this) and set device names.")

    motion = res.get("motion", {})
    backend = motion.get("backend")
    physical = bool(motion.get("physical_enabled"))
    calib_path = res.get("_calibration_path")
    calib_exists = bool(res.get("_calibration_exists"))
    calib_valid = res.get("_calibration_valid")
    calib_err = res.get("_calibration_error")
    if not calib_exists:
        calib_state = "missing"
    elif calib_valid:
        calib_state = "valid"
    elif calib_valid is False:
        calib_state = f"INVALID ({calib_err})"
    else:
        calib_state = f"present, not validated ({calib_err})"
    details = {"backend": backend, "physical_enabled": physical, "calibration_file": calib_path,
               "calibration": calib_state}
    calib_ok = calib_exists and calib_valid is True
    if physical and backend == "pca9685" and not calib_ok:
        r.add(S, "motion_safety", "FAIL",
              f"backend=pca9685 + physical_enabled=true but calibration is {calib_state}", details,
              hint="Set motion.backend: mock until scripts/calibrate_servos.py has written a valid "
                   "config/calibration.yaml (docs/MOTION_SAFETY.md section 5; format: config/calibration.example.yaml).")
    elif physical and backend == "pca9685":
        r.add(S, "motion_safety", "WARN", "physical servo motion CONFIGURED (backend=pca9685, calibration valid)", details,
              hint="Outputs still need a parent to arm motion in the dashboard. Only with the separate servo supply, "
                   "a reachable power switch and an adult present (docs/MOTION_SAFETY.md section 8).")
    elif physical and not calib_ok:
        r.add(S, "motion_safety", "WARN", f"calibration pending (physical_enabled=true, backend={backend}, calibration {calib_state})",
              details, hint="This is the calibration stage: run scripts/calibrate_servos.py (docs/MOTION_SAFETY.md section 5); "
                            "keep backend: mock until it has written a valid calibration.")
    elif physical:
        r.add(S, "motion_safety", "INFO", f"calibration valid; motion still simulated (backend={backend})", details,
              hint="Switch to backend: pca9685 only after MOTION_SAFETY.md checklist part D.")
    else:
        status = "WARN" if calib_exists and calib_valid is False else "PASS"
        r.add(S, "motion_safety", status, f"physical motion disabled (backend={backend}; calibration {calib_state})", details,
              hint="Re-run scripts/calibrate_servos.py to rewrite config/calibration.yaml." if status == "WARN" else None)

    dash = res.get("dashboard", {})
    host, port, lan = dash.get("host"), dash.get("port"), bool(dash.get("lan_access"))
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if lan:
        r.add(S, "dashboard", "WARN", f"LAN access ENABLED (port {port}): phones on your Wi-Fi can reach the login page",
              hint="Fine at home on a trusted Wi-Fi with a strong dashboard PIN/password. Never port-forward it to the internet.")
    elif loopback:
        r.add(S, "dashboard", "PASS", f"localhost only ({host}:{port}); phone access is off (opt-in)")
    else:
        r.add(S, "dashboard", "WARN", f"host={host} but lan_access=false",
              hint="Leave host at 127.0.0.1 and use dashboard.lan_access: true to opt in to phone access.")

    disp = res.get("display", {})
    msg = f"{disp.get('width')}x{disp.get('height')} fullscreen={disp.get('fullscreen')} fps={disp.get('fps')}/{disp.get('idle_fps')}"
    if ctx.is_pi and ctx.mode == "pi" and not disp.get("fullscreen"):
        r.add(S, "display", "WARN", msg, hint="On the robot set display.fullscreen: true (pi.yaml does).")
    else:
        r.add(S, "display", "INFO", msg)

    spk = res.get("audio", {}).get("speaker", {})
    vol, vmax = spk.get("volume", 0), spk.get("max_volume", 1)
    if vmax is not None and vmax > 0.8:
        r.add(S, "volume_ceiling", "WARN", f"speaker.max_volume={vmax} (default ceiling is 0.8)",
              hint="Small children's hearing is sensitive: keep the ceiling at or below 0.8 and test loudness at ear distance.")
    else:
        r.add(S, "volume_ceiling", "PASS", f"volume={vol} max_volume={vmax}")
    priv = res.get("privacy", {})
    r.add(S, "privacy_defaults", "INFO",
          f"camera={priv.get('camera_enabled')} mic={priv.get('microphone_enabled')} cloud={priv.get('cloud_enabled')} ai.provider={res.get('ai', {}).get('provider')}")


def check_power(r: Report, ctx: Ctx) -> None:
    S = "power"
    temp = read_cpu_temperature()
    if temp is None:
        r.add(S, "cpu_temperature", "SKIP", "no thermal zone readable")
    else:
        status = "PASS" if temp < 70 else ("WARN" if temp < 80 else "FAIL")
        r.add(S, "cpu_temperature", status, f"{temp:.1f} C",
              hint=None if status == "PASS" else "Pi 5 throttles from ~80-85 C: check the active cooler fan and enclosure airflow.")

    if not ctx.is_pi:
        r.add(S, "throttling", "SKIP", "not a Raspberry Pi")
        return

    flags, source = read_throttled()
    if flags is None:
        r.add(S, "throttling", "WARN", "could not read throttling flags (vcgencmd / sysfs unavailable)",
              hint="vcgencmd needs the user to be in the 'video' group.")
    else:
        decoded = decode_throttled(flags)
        if not decoded:
            r.add(S, "throttling", "PASS", "0x0 - no under-voltage or throttling since boot", {"raw": hex(flags), "source": source})
        else:
            worst = "FAIL" if any(d["severity"] == "FAIL" for d in decoded) else "WARN"
            r.add(S, "throttling", worst, f"{hex(flags)}: " + "; ".join(d["meaning"] for d in decoded),
                  {"raw": hex(flags), "source": source, "flags": decoded},
                  hint="Under-voltage: use the official 27 W USB-C PSU and a short good cable (docs/POWER.md). "
                       "Temperature: check the fan.")

    rc, out, _ = run_cmd(["vcgencmd", "pmic_read_adc", "EXT5V_V"], timeout=4)
    m = re.search(r"=\s*([0-9.]+)\s*V", out) if rc == 0 else None
    if m:
        volts = float(m.group(1))
        status = "PASS" if volts >= 4.85 else ("WARN" if volts >= 4.70 else "FAIL")
        r.add(S, "input_voltage", status, f"EXT5V {volts:.2f} V (Pi 5 PMIC)", {"volts": volts},
              hint=None if status == "PASS" else "Supply voltage is sagging; under-voltage threshold is ~4.63 V.")
    else:
        r.add(S, "input_voltage", "SKIP", "vcgencmd pmic_read_adc not available (Pi 5 only)")

    rc, out, _ = run_cmd(["vcgencmd", "get_config", "usb_max_current_enable"], timeout=4)
    m = re.search(r"usb_max_current_enable=(\d+)", out) if rc == 0 else None
    if m:
        if m.group(1) == "1":
            r.add(S, "usb_current", "PASS", "usb_max_current_enable=1 (USB ports may draw up to 1.6 A total)")
        else:
            r.add(S, "usb_current", "WARN", "usb_max_current_enable=0: USB peripherals limited to 600 mA total",
                  hint="The supply did not negotiate 5 V/5 A. Camera + mic + speaker may exceed 600 mA. See docs/POWER.md.")
    else:
        r.add(S, "usb_current", "SKIP", "vcgencmd get_config unavailable")

    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        name = (read_text(f"{hw}/name") or "").strip()
        if name == "rpi_volt":
            alarm = (read_text(f"{hw}/in0_lcrit_alarm") or "").strip()
            if alarm:
                r.add(S, "undervoltage_alarm", "FAIL" if alarm == "1" else "PASS",
                      f"hwmon rpi_volt in0_lcrit_alarm={alarm}")
        if name in ("pwmfan", "cooling_fan"):
            rpm = (read_text(f"{hw}/fan1_input") or "").strip()
            if rpm:
                r.add(S, "fan", "INFO", f"fan {rpm} RPM (0 RPM is normal while the CPU is cool)", {"rpm": rpm})

    rc, out, _ = run_cmd(["journalctl", "-k", "-b", "--no-pager", "-q", "-g", "under-?voltage|undervoltage"], timeout=8)
    if rc in (0, 1):
        n = len([ln for ln in out.splitlines() if ln.strip()])
        r.add(S, "kernel_log", "WARN" if n else "PASS",
              f"{n} under-voltage kernel message(s) this boot" if n else "no under-voltage kernel messages this boot")
    else:
        r.add(S, "kernel_log", "SKIP", "journalctl -k not readable (add user to 'adm' group, or run with sudo)")

    gov = (read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") or "").strip()
    cur = (read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") or "").strip()
    if cur.isdigit():
        r.add(S, "cpu_frequency", "INFO", f"{int(cur) // 1000} MHz (governor {gov or '?'})")


def check_memory(r: Report, ctx: Ctx) -> None:
    S = "memory"
    mi = meminfo()
    if not mi:
        r.add(S, "meminfo", "SKIP", "/proc/meminfo not readable")
        return
    total = mi.get("MemTotal", 0) // 1024
    avail = mi.get("MemAvailable", 0) // 1024
    status = "PASS" if avail >= 400 else ("WARN" if avail >= 200 else "FAIL")
    r.add(S, "ram", status, f"{avail} MB available of {total} MB", {"total_mb": total, "available_mb": avail},
          hint=None if status == "PASS" else "Close other apps (browser tabs). Chromium/YouTube is the heaviest component.")
    swap_total = mi.get("SwapTotal", 0) // 1024
    swap_used = swap_total - mi.get("SwapFree", 0) // 1024
    swaps = []
    for line in (read_text("/proc/swaps") or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            swaps.append({"device": parts[0], "type": parts[1], "size_mb": int(parts[2]) // 1024, "used_mb": int(parts[3]) // 1024})
    kinds = ", ".join(f"{s['device']} {s['size_mb']} MB" for s in swaps) or "none"
    zram = any("zram" in s["device"] for s in swaps)
    if swap_total == 0 and ctx.is_pi and total < 3000:
        r.add(S, "swap", "WARN", "no swap on a 2 GB board", {"swaps": swaps},
              hint="Trixie uses zram via rpi-swap; Bookworm uses dphys-swapfile. Some swap avoids OOM kills when Chromium runs.")
    else:
        r.add(S, "swap", "INFO", f"{swap_total} MB total, {swap_used} MB used ({kinds}){' - zram' if zram else ''}", {"swaps": swaps})
    if ctx.is_pi:
        rc, out, _ = run_cmd(["journalctl", "-k", "-b", "--no-pager", "-q", "-g", "out of memory|oom-kill"], timeout=8)
        if rc in (0, 1):
            n = len([ln for ln in out.splitlines() if "killed process" in ln.lower() or "out of memory" in ln.lower()])
            r.add(S, "oom_kills", "WARN" if n else "PASS", f"{n} out-of-memory kill(s) this boot")


def check_storage(r: Report, ctx: Ctx) -> None:
    S = "storage"
    try:
        du = shutil.disk_usage(REPO_ROOT)
    except OSError as exc:
        r.add(S, "disk", "WARN", f"cannot stat {REPO_ROOT}: {exc}")
        return
    free_gb = du.free / 1e9
    status = "PASS" if free_gb >= 3 else ("WARN" if free_gb >= 1 else "FAIL")
    r.add(S, "disk_free", status, f"{free_gb:.1f} GB free of {du.total / 1e9:.1f} GB", {"free_bytes": du.free, "total_bytes": du.total},
          hint=None if status == "PASS" else "Free space: sudo apt clean; remove old logs in data/logs; journalctl --vacuum-size=100M.")
    root_dev, root_opts = None, ""
    for line in (read_text("/proc/mounts") or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "/":
            root_dev, root_opts = parts[0], parts[3]
    if root_dev:
        kind = "SD card" if "mmcblk" in root_dev else ("NVMe" if "nvme" in root_dev else ("USB/SATA" if "/sd" in root_dev else "other"))
        status = "WARN" if root_opts.split(",")[0] == "ro" else "INFO"
        r.add(S, "root_device", status, f"/ on {root_dev} ({kind}){' READ-ONLY' if status == 'WARN' else ''}")
    for blk in glob.glob("/sys/block/mmcblk*/device"):
        name = (read_text(f"{blk}/name") or "").strip()
        date = (read_text(f"{blk}/date") or "").strip()
        manfid = (read_text(f"{blk}/manfid") or "").strip()
        if name:
            r.add(S, "sd_card", "INFO", f"{name} (manfid {manfid}, date {date})")
    data = Path((ctx.config or {}).get("_data_path") or REPO_ROOT / "data")
    if data.exists():
        ok = os.access(data, os.W_OK)
        r.add(S, "data_dir", "PASS" if ok else "FAIL", f"{data} {'writable' if ok else 'NOT writable'}")
    else:
        r.add(S, "data_dir", "INFO", f"{data} does not exist yet (created on first run / by install_pi.sh)")


def _drm_connectors() -> list[dict[str, Any]]:
    out = []
    for path in sorted(glob.glob("/sys/class/drm/card*-*")):
        name = os.path.basename(path)
        if "Writeback" in name or not os.path.exists(f"{path}/status"):
            continue
        connector = name.split("-", 1)[1]
        modes = [m for m in (read_text(f"{path}/modes") or "").splitlines() if m.strip()]
        edid_raw = b""
        try:
            with open(f"{path}/edid", "rb") as fh:
                edid_raw = fh.read(512)
        except OSError:
            pass
        out.append({
            "connector": connector,
            "card": name.split("-", 1)[0],
            "status": (read_text(f"{path}/status") or "").strip(),
            "enabled": (read_text(f"{path}/enabled") or "").strip(),
            "modes": list(dict.fromkeys(modes))[:15],
            "edid": parse_edid(edid_raw),
        })
    return out


def _compositors() -> list[str]:
    names = {"labwc", "wayfire", "Xorg", "Xwayland", "weston", "sway", "kwin_wayland", "gnome-shell", "mutter"}
    return sorted({comm for _pid, comm, _argv in iter_processes() if comm in names})


def check_display(r: Report, ctx: Ctx) -> None:
    S = "display"
    env = {k: os.environ.get(k) for k in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY", "XDG_CURRENT_DESKTOP")}
    comps = _compositors()
    session = ", ".join(f"{k}={v}" for k, v in env.items() if v) or "no session variables (SSH/service?)"
    if comps:
        r.add(S, "session", "PASS", f"compositor running: {', '.join(comps)}; {session}", {"env": env, "compositors": comps})
    else:
        r.add(S, "session", ctx.sev("WARN", "INFO"), f"no desktop compositor found; {session}", {"env": env},
              hint="The face window needs the desktop session (labwc). Enable Desktop Autologin in raspi-config." if ctx.is_pi else None)

    conns = _drm_connectors()
    if not conns:
        r.add(S, "drm", ctx.sev("FAIL", "SKIP"), "no DRM connectors in /sys/class/drm",
              hint="Pi: make sure dtoverlay=vc4-kms-v3d is in config.txt." if ctx.is_pi else None)
    connected = [c for c in conns if c["status"] == "connected"]
    for c in conns:
        if c["status"] != "connected":
            continue
        edid = c["edid"]
        has_native = "480x320" in c["modes"]
        msg = (f"{c['connector']} connected; modes: {', '.join(c['modes'][:6]) or 'none'}"
               + (f"; EDID '{edid.get('name') or edid.get('manufacturer')}' preferred {edid.get('preferred')}" if edid else "; no EDID"))
        r.add(S, f"connector_{c['connector']}", "PASS", msg, c,
              hint=None if has_native else "480x320 is not advertised by EDID. See docs/DISPLAY_SETUP.md (try 720x480 or a video= mode).")
        if c["connector"] == "HDMI-A-2" and ctx.is_pi:
            r.add(S, "hdmi_port", "INFO", "screen is on HDMI1 (HDMI-A-2)",
                  hint="HDMI0 (next to the USB-C power socket) is HDMI-A-1; video= settings must name the connector in use.")
    if conns and not connected:
        r.add(S, "connected", ctx.sev("FAIL", "INFO"), "no display connected",
              {"connectors": [c["connector"] for c in conns]},
              hint="Use the micro-HDMI port next to the USB-C power socket (HDMI0) and power the screen before booting.")

    cmdline = read_text("/proc/cmdline") or ""
    video = [t for t in cmdline.split() if t.startswith("video=")]
    if video:
        r.add(S, "kernel_video_mode", "INFO", " ".join(video))

    cur = None
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wlr-randr"):
        rc, out, _ = run_cmd(["wlr-randr"], timeout=5)
        if rc == 0:
            current = [ln.strip() for ln in out.splitlines() if "current" in ln]
            transforms = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("Transform")]
            cur = "; ".join(current + transforms) or None
    elif os.environ.get("DISPLAY") and shutil.which("xrandr"):
        rc, out, _ = run_cmd(["xrandr", "--current"], timeout=5)
        if rc == 0:
            cur = "; ".join(ln.strip() for ln in out.splitlines() if "*" in ln) or None
    if cur:
        r.add(S, "current_mode", "INFO", cur)
    elif ctx.is_pi:
        r.add(S, "current_mode", "SKIP", "run from the desktop terminal (needs WAYLAND_DISPLAY + wlr-randr) to see the active mode")

    lines = boot_config_lines()
    if ctx.is_pi and lines:
        if any(ln.startswith("dtoverlay=vc4-kms-v3d") for ln in lines):
            r.add(S, "kms_overlay", "PASS", "dtoverlay=vc4-kms-v3d present")
        elif any("vc4-fkms-v3d" in ln for ln in lines):
            r.add(S, "kms_overlay", "FAIL", "legacy vc4-fkms-v3d configured", hint="Pi 5 needs dtoverlay=vc4-kms-v3d (full KMS).")
        else:
            r.add(S, "kms_overlay", "WARN", "dtoverlay=vc4-kms-v3d not found in config.txt")
        legacy = [ln for ln in lines if re.match(r"(hdmi_(cvt|group|mode|timings|drive)|display_rotate|framebuffer_)", ln)]
        if legacy:
            r.add(S, "legacy_hdmi_settings", "WARN", f"{len(legacy)} legacy line(s): {'; '.join(legacy[:5])}",
                  {"lines": legacy},
                  hint="These firmware settings are ignored by the KMS driver on Pi 5 (probably left by a vendor script). "
                       "Use video=HDMI-A-1:... in cmdline.txt instead (docs/DISPLAY_SETUP.md).")
    fb = read_text("/sys/class/graphics/fb0/virtual_size")
    if fb:
        r.add(S, "console_framebuffer", "INFO", f"fb0 {fb.strip().replace(',', 'x')}")


def _input_devices() -> list[dict[str, str]]:
    devs, cur = [], {}
    for line in (read_text("/proc/bus/input/devices") or "").splitlines() + [""]:
        if not line.strip():
            if cur:
                devs.append(cur)
            cur = {}
            continue
        if line.startswith("N: Name="):
            cur["name"] = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("H: Handlers="):
            cur["handlers"] = line.split("=", 1)[1].strip()
        elif line.startswith("P: Phys="):
            cur["phys"] = line.split("=", 1)[1].strip()
    return devs


def check_touch(r: Report, ctx: Ctx) -> None:
    S = "touch"
    devs = _input_devices()
    touch = [d for d in devs if re.search(r"ads7846|xpt2046|touch", d.get("name", ""), re.I)]
    overlay = [ln for ln in boot_config_lines() if ln.startswith("dtoverlay=ads7846")]
    spi_on = any(re.match(r"dtparam=spi=on", ln) for ln in boot_config_lines())
    if touch:
        for d in touch:
            ev = next((h for h in d.get("handlers", "").split() if h.startswith("event")), None)
            readable = bool(ev) and os.access(f"/dev/input/{ev}", os.R_OK)
            r.add(S, "device", "PASS" if readable else "WARN",
                  f"{d['name']} ({ev or 'no event node'}{'' if readable else ', not readable by this user'})", d,
                  hint=None if readable else "Add the user to the 'input' group (sudo usermod -aG input $USER) and log in again.")
    else:
        hint = ("The touch layer is optional: the app works without it. See docs/DISPLAY_SETUP.md (ads7846 overlay, UNVERIFIED on Pi 5)."
                if ctx.is_pi else None)
        status = "WARN" if overlay else ("INFO" if ctx.is_pi else "SKIP")
        r.add(S, "device", status, "no touchscreen input device found" + (" although the ads7846 overlay is configured" if overlay else ""),
              {"input_devices": [d.get("name") for d in devs][:20]}, hint=hint)
    if ctx.is_pi:
        r.add(S, "boot_config", "INFO", f"spi={'on' if spi_on else 'off'}; ads7846 overlay: {overlay[0] if overlay else 'not configured'}")
        if os.path.isdir("/sys/module/ads7846"):
            r.add(S, "driver", "INFO", "ads7846 kernel module loaded")


CAMERA_SNIPPET = r"""
import json, os, re, sys
try:
    import cv2
except Exception as e:
    print(json.dumps({"error": "%s: %s" % (type(e).__name__, e)}))
    raise SystemExit(0)
want = json.loads(sys.argv[1])
info = {"version": cv2.__version__, "has_cv2_data": hasattr(cv2, "data"),
        "cascade_classifier": hasattr(cv2, "CascadeClassifier"), "face_detector_yn": hasattr(cv2, "FaceDetectorYN")}
cands = [want.get("haar_cascade")] if want.get("haar_cascade") else []
if hasattr(cv2, "data"):
    cands.append(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
cands += ["/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
          "/usr/share/opencv/haarcascades/haarcascade_frontalface_default.xml",
          "models/haarcascade_frontalface_default.xml"]
info["haar_cascade"] = next((c for c in cands if c and os.path.isfile(c)), None)
info["yunet_model"] = want.get("yunet_model") if want.get("yunet_model") and os.path.isfile(want["yunet_model"]) else None
m = re.search(r"v4l/v4l2:\s*(\S+)", cv2.getBuildInformation())
info["v4l2"] = m.group(1) if m else None
print(json.dumps(info))
"""


def _video_nodes() -> list[dict[str, Any]]:
    nodes = []
    for path in sorted(glob.glob("/sys/class/video4linux/video*"), key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0)):
        dev = "/dev/" + os.path.basename(path)
        driver = ""
        try:
            driver = os.path.basename(os.readlink(f"{path}/device/driver"))
        except OSError:
            pass
        nodes.append({
            "dev": dev,
            "name": (read_text(f"{path}/name") or "").strip(),
            "index": (read_text(f"{path}/index") or "").strip(),
            "driver": driver,
            "usb": "usb" in os.path.realpath(f"{path}/device"),
        })
    return nodes


def _device_users(dev: str) -> list[str]:
    users = []
    for pid, comm, _argv in iter_processes():
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    if os.readlink(f"/proc/{pid}/fd/{fd}") == dev:
                        users.append(f"{comm}({pid})")
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return users


def check_camera(r: Report, ctx: Ctx) -> None:
    S = "camera"
    backend = ctx.cfg("camera", "backend", default="auto")
    device = ctx.cfg("camera", "device", default=0)
    nodes = _video_nodes()
    uvc = [n for n in nodes if n["usb"] and n["index"] in ("", "0")]
    other = [n for n in nodes if not n["usb"]]
    if other:
        r.add(S, "platform_nodes", "INFO", f"{len(other)} non-USB video node(s) (ISP/codec on Pi 5 - not cameras)",
              {"nodes": [f"{n['dev']} {n['name']}" for n in other][:12]})
    if backend in ("mock", "none"):
        r.add(S, "config", "INFO", f"camera.backend={backend}: the app will not use a real camera")
    if uvc:
        for n in uvc:
            ok = os.access(n["dev"], os.R_OK | os.W_OK)
            users = _device_users(n["dev"])
            r.add(S, n["dev"], "PASS" if ok else "FAIL",
                  f"{n['name']} (driver {n['driver'] or '?'}){'' if ok else ' - no permission'}{'; in use by ' + ', '.join(users) if users else ''}",
                  {**n, "in_use_by": users},
                  hint=None if ok else "Add the user to the 'video' group and log in again.")
            rc, out, _ = run_cmd(["v4l2-ctl", "-d", n["dev"], "--list-formats-ext"], timeout=6)
            if rc == 0:
                fmts = re.findall(r"'(\w+)'", out)
                sizes = list(dict.fromkeys(re.findall(r"Size: Discrete (\d+x\d+)", out)))
                r.add(S, f"{n['dev']}_formats", "INFO", f"formats {', '.join(dict.fromkeys(fmts))}; sizes {', '.join(sizes[:8])}",
                      {"formats": list(dict.fromkeys(fmts)), "sizes": sizes})
        byid = sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
        if byid:
            r.add(S, "stable_paths", "INFO", "; ".join(byid),
                  hint="A /dev/v4l/by-id path survives re-plugging; it can be used as camera.device if the vision code accepts paths.")
    else:
        status = "SKIP" if backend in ("mock", "none") else (ctx.sev("FAIL" if backend == "real" else "WARN", "WARN"))
        r.add(S, "usb_camera", status, f"no USB (UVC) camera found (camera.backend={backend}, device={device})",
              hint="Plug the camera directly into a blue USB 3 port, then run: v4l2-ctl --list-devices" if ctx.is_pi else None)
    if shutil.which("v4l2-ctl") is None and ctx.is_pi:
        r.add(S, "v4l_utils", "WARN", "v4l2-ctl not installed", hint="sudo apt install v4l-utils")
    if "video" not in user_groups() and ctx.is_pi and os.geteuid() != 0:
        r.add(S, "video_group", "WARN", "user is not in the 'video' group", hint="sudo usermod -aG video $USER, then log out/in.")
    detector = ctx.cfg("vision", "detector", default="haar")
    want = {"haar_cascade": ctx.cfg("vision", "haar_cascade"), "yunet_model": ctx.cfg("vision", "yunet_model")}
    res = probe_json(ctx, CAMERA_SNIPPET, json.dumps(want))
    if "error" in res:
        r.add(S, "opencv", "WARN" if backend in ("mock", "none") else ctx.sev("FAIL", "WARN"), f"cv2 not importable: {res['error']}",
              hint="pip install -e '.[vision]' (or apt python3-opencv with a --system-site-packages venv).")
        return
    r.add(S, "opencv", "PASS", f"OpenCV {res.get('version')} (V4L2 support: {res.get('v4l2')})", res)
    haar_ok = bool(res.get("cascade_classifier") and res.get("haar_cascade"))
    yunet_ok = bool(res.get("face_detector_yn") and res.get("yunet_model"))
    parts = [f"haar {'ready' if haar_ok else 'unavailable'}"
             + ("" if res.get("cascade_classifier") else " (no cv2.CascadeClassifier: removed in OpenCV 5)")
             + ("" if res.get("haar_cascade") or not res.get("cascade_classifier") else " (cascade XML not found)"),
             f"yunet {'ready' if yunet_ok else 'unavailable'}"
             + ("" if res.get("yunet_model") else " (model file missing)")]
    if detector == "none":
        r.add(S, "face_detector", "INFO", "vision.detector=none; " + "; ".join(parts))
    elif haar_ok or yunet_ok:
        preferred_ok = haar_ok if detector == "haar" else yunet_ok
        r.add(S, "face_detector", "PASS" if preferred_ok else "INFO",
              f"vision.detector={detector}: " + "; ".join(parts)
              + ("" if preferred_ok else " - the app falls back to the other detector"))
    else:
        r.add(S, "face_detector", ctx.sev("WARN", "INFO"), f"no face detector usable (vision.detector={detector}): " + "; ".join(parts),
              hint="Run scripts/download_models.py (YuNet), or install OpenCV 4.x / apt opencv-data for the Haar cascade.")


AUDIO_SNIPPET = r"""
import json
try:
    import sounddevice as sd
except Exception as e:
    print(json.dumps({"error": "%s: %s" % (type(e).__name__, e)}))
    raise SystemExit(0)
devs = []
try:
    apis = sd.query_hostapis()
    for i, d in enumerate(sd.query_devices()):
        devs.append({"index": i, "name": d["name"], "in": d["max_input_channels"], "out": d["max_output_channels"],
                     "hostapi": apis[d["hostapi"]]["name"], "rate": d["default_samplerate"]})
    default = list(sd.default.device)
except Exception as e:
    print(json.dumps({"error": "query failed: %s" % (e,)}))
    raise SystemExit(0)
print(json.dumps({"version": sd.__version__, "portaudio": sd.get_portaudio_version()[1], "devices": devs, "default": default}))
"""


def _alsa_list(cmd: str) -> list[dict[str, str]] | None:
    rc, out, _ = run_cmd([cmd, "-l"], timeout=6)
    if rc is None:
        return None
    devs = []
    for m in re.finditer(r"^card (\d+): (\S+) \[([^\]]*)\], device (\d+): ([^\[]*)\[([^\]]*)\]", out, re.M):
        devs.append({"card": m.group(1), "id": m.group(2), "name": m.group(3), "device": m.group(4), "desc": m.group(6)})
    return devs


def _match_device(want: Any, devices: list[dict[str, Any]], key: str) -> bool | None:
    if want in (None, ""):
        return None
    usable = [d for d in devices if d.get(key, 0) > 0]
    if isinstance(want, int):
        return any(d["index"] == want for d in usable)
    return any(str(want).lower() in d["name"].lower() for d in usable)


def check_audio(r: Report, ctx: Ctx) -> None:
    S = "audio"
    mic_backend = ctx.cfg("audio", "microphone", "backend", default="auto")
    spk_backend = ctx.cfg("audio", "speaker", "backend", default="auto")
    cards_text = read_text("/proc/asound/cards") or ""
    cards = re.findall(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s+(\S+)\s+-\s+(.*)$", cards_text, re.M)
    if cards:
        r.add(S, "alsa_cards", "INFO", "; ".join(f"{n}:{cid} ({drv})" for n, cid, drv, _ in cards),
              {"cards": [{"index": n, "id": cid, "driver": drv, "name": name.strip()} for n, cid, drv, name in cards]})
    play = _alsa_list("aplay")
    rec = _alsa_list("arecord")
    usb_cards = {cid for _n, cid, drv, _name in cards if drv == "USB-Audio"}
    if play is None:
        r.add(S, "playback", ctx.sev("WARN", "SKIP"), "aplay not installed", hint="sudo apt install alsa-utils" if ctx.is_pi else None)
    else:
        usb_play = [d for d in play if d["id"] in usb_cards]
        if usb_play:
            r.add(S, "playback", "PASS", "USB playback: " + "; ".join(f"card {d['card']} {d['name']}" for d in usb_play), {"devices": play})
        else:
            sev = "SKIP" if spk_backend in ("mock", "none") else ctx.sev("FAIL", "WARN")
            r.add(S, "playback", sev, f"no USB playback device ({len(play)} other playback device(s))", {"devices": play},
                  hint="Pi 5 has no headphone jack: plug in the USB speaker and check `aplay -l`." if ctx.is_pi else None)
    if rec is not None:
        usb_rec = [d for d in rec if d["id"] in usb_cards]
        if usb_rec:
            r.add(S, "capture", "PASS", "USB capture: " + "; ".join(f"card {d['card']} {d['name']}" for d in usb_rec), {"devices": rec})
        else:
            sev = "SKIP" if mic_backend in ("mock", "none") else ctx.sev("FAIL", "WARN")
            r.add(S, "capture", sev, "no USB microphone found", {"devices": rec},
                  hint="Plug the USB microphone in and check `arecord -l`." if ctx.is_pi else None)

    rc, out, err = run_cmd(["pactl", "info"], timeout=5)
    if rc == 0:
        server = re.search(r"^Server Name:\s*(.*)$", out, re.M)
        sink = re.search(r"^Default Sink:\s*(.*)$", out, re.M)
        source = re.search(r"^Default Source:\s*(.*)$", out, re.M)
        sink_name = sink.group(1).strip() if sink else ""
        r.add(S, "sound_server", "PASS", server.group(1).strip() if server else "pactl info OK")
        hdmi = "hdmi" in sink_name.lower()
        r.add(S, "default_output", "WARN" if hdmi else "INFO", f"default sink: {sink_name or '?'}",
              hint="The default output is HDMI (the screen), not the USB speaker: choose the USB speaker in the volume "
                   "menu, or set audio.speaker.device in config/local.yaml." if hdmi else None)
        r.add(S, "default_input", "INFO", f"default source: {source.group(1).strip() if source else '?'}")
    elif rc is None:
        r.add(S, "sound_server", "SKIP", "pactl not installed" if "not found" in err else err.strip())
    else:
        r.add(S, "sound_server", "SKIP", "no PipeWire/PulseAudio session reachable (run from the desktop session)")

    res = probe_json(ctx, AUDIO_SNIPPET)
    if "error" in res:
        missing_pa = "PortAudio" in res["error"]
        r.add(S, "sounddevice", ctx.sev("FAIL", "WARN"), f"sounddevice unavailable: {res['error']}",
              hint="sudo apt install libportaudio2" if missing_pa else "pip install -e '.[audio]'")
    else:
        devs = res.get("devices", [])
        r.add(S, "sounddevice", "PASS", f"sounddevice {res.get('version')} / {res.get('portaudio')}: {len(devs)} device(s), default in/out {res.get('default')}",
              {"devices": devs})
        for label, key, want in (("microphone", "in", ctx.cfg("audio", "microphone", "device")),
                                 ("speaker", "out", ctx.cfg("audio", "speaker", "device"))):
            found = _match_device(want, devs, key)
            if found is None:
                r.add(S, f"{label}_device", "INFO", f"audio.{label}.device not set (system default is used)",
                      hint="Pin it to a name substring from the list above for robustness." if ctx.is_pi else None)
            else:
                r.add(S, f"{label}_device", "PASS" if found else ctx.sev("FAIL", "WARN"),
                      f"configured {label} device {want!r} {'found' if found else 'NOT found'}")
    if shutil.which("espeak-ng"):
        r.add(S, "tts_espeak", "PASS", "espeak-ng installed (offline voice)")
    else:
        r.add(S, "tts_espeak", ctx.sev("WARN", "INFO"), "espeak-ng not installed (TTS falls back to a silent mock)",
              hint="sudo apt install espeak-ng")


def check_i2c(r: Report, ctx: Ctx) -> None:
    S = "i2c"
    buses = sorted(glob.glob("/dev/i2c-*"))
    backend = ctx.cfg("motion", "backend", default="mock")
    bus_no = ctx.cfg("motion", "i2c_bus", default=1)
    addr = ctx.cfg("motion", "i2c_address", default=0x40)
    enabled = any(re.match(r"dtparam=i2c(_arm)?=on", ln) for ln in boot_config_lines())
    if buses:
        r.add(S, "buses", "PASS" if f"/dev/i2c-{bus_no}" in buses else "INFO", ", ".join(buses))
    elif backend == "pca9685":
        r.add(S, "buses", "FAIL", "no /dev/i2c-* but motion.backend=pca9685",
              hint="Enable I2C: sudo raspi-config nonint do_i2c 0 (or scripts/install_pi.sh --enable-i2c), then reboot.")
    else:
        r.add(S, "buses", "SKIP" if not ctx.is_pi else "INFO",
              "I2C not enabled (fine: servos are not used yet)" if ctx.is_pi else "no I2C buses")
    if ctx.is_pi:
        r.add(S, "boot_config", "INFO", f"dtparam=i2c_arm=on {'present' if enabled else 'absent'}")
    tool = shutil.which("i2cdetect")
    if not ctx.args.i2c_scan:
        r.add(S, "scan", "SKIP", f"bus scan not requested (i2cdetect {'available' if tool else 'not installed'}; use --i2c-scan)")
        return
    if not tool or f"/dev/i2c-{bus_no}" not in buses:
        r.add(S, "scan", "SKIP", f"cannot scan bus {bus_no} (i2cdetect {'ok' if tool else 'missing'}, bus {'ok' if f'/dev/i2c-{bus_no}' in buses else 'missing'})")
        return
    # -r = probe with "read byte" instead of "quick write": never writes to devices.
    rc, out, err = run_cmd(["i2cdetect", "-y", "-r", str(bus_no)], timeout=15)
    if rc != 0:
        r.add(S, "scan", "WARN", f"i2cdetect failed: {err.strip()[:200]}", hint="Is the user in the 'i2c' group?")
        return
    found = sorted({f"0x{h}" for h in re.findall(r"\b([0-7][0-9a-f])\b", "\n".join(ln.split(":", 1)[1] for ln in out.splitlines() if ":" in ln))})
    want = f"0x{addr:02x}"
    r.add(S, "scan", "PASS" if want in found else "WARN",
          f"bus {bus_no}: {', '.join(found) or 'no devices'}; PCA9685 at {want} {'found' if want in found else 'not found'}",
          {"addresses": found})


def _local_ips() -> list[str]:
    ips: list[str] = []
    rc, out, _ = run_cmd(["hostname", "-I"], timeout=3)
    if rc == 0:
        ips = [ip for ip in out.split() if ":" not in ip]
    if not ips:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))  # TEST-NET address: no packet is sent for a UDP connect
                ip = s.getsockname()[0]
                if not ip.startswith("127."):
                    ips = [ip]
        except OSError:
            pass
    return ips


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def check_network(r: Report, ctx: Ctx) -> None:
    S = "network"
    host = socket.gethostname()
    ips = _local_ips()
    r.add(S, "hostname", "INFO", host)
    r.add(S, "lan_ip", "PASS" if ips else ctx.sev("WARN", "INFO"), ", ".join(ips) or "no LAN IPv4 address",
          hint=None if ips else "Connect Wi-Fi (set it in Raspberry Pi Imager, or with the network icon / nmtui).")
    ifaces = []
    for path in sorted(glob.glob("/sys/class/net/*")):
        name = os.path.basename(path)
        if name == "lo":
            continue
        ifaces.append(f"{name}:{(read_text(path + '/operstate') or '?').strip()}")
    if ifaces:
        r.add(S, "interfaces", "INFO", ", ".join(ifaces))
    port = int(ctx.cfg("dashboard", "port", default=8765) or 8765)
    lan = bool(ctx.cfg("dashboard", "lan_access", default=False))
    running = _port_open("127.0.0.1", port)
    r.add(S, "dashboard_local", "PASS" if running else "INFO",
          f"http://127.0.0.1:{port} {'is answering' if running else 'not answering (app not running?)'}")
    if lan:
        urls = [f"http://{ip}:{port}" for ip in ips] + [f"http://{host}.local:{port}"]
        reach = any(_port_open(ip, port) for ip in ips)
        r.add(S, "dashboard_lan", "PASS" if reach else "INFO", "phone URL(s): " + ", ".join(urls),
              hint=None if reach else "Start the app; phone and Pi must be on the same Wi-Fi (no guest/isolated network).")
    else:
        r.add(S, "dashboard_lan", "INFO", "phone access disabled (dashboard.lan_access: false)",
              hint=f"To use your phone: set dashboard.lan_access: true in config/local.yaml (or run with --lan), then open "
                   f"http://{ips[0] if ips else host + '.local'}:{port}")
    avahi = os.path.exists("/run/avahi-daemon/pid") or bool([1 for _p, c, _a in iter_processes() if c == "avahi-daemon"])
    r.add(S, "mdns", "INFO", f"avahi-daemon {'running' if avahi else 'not running'} ({host}.local {'should' if avahi else 'will not'} resolve)")


def _parse_env_file(path: Path) -> dict[str, bool]:
    present: dict[str, bool] = {}
    for raw in (read_text(path) or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        present[key.strip()] = bool(value)
    return present


def check_secrets(r: Report, ctx: Ctx) -> None:
    S = "secrets"
    env_path = Path(ctx.args.env_file) if ctx.args.env_file else REPO_ROOT / ".env"
    if not env_path.is_file():
        r.add(S, "env_file", "INFO", f"{env_path.name} not found (no API keys configured - cloud AI stays off)",
              hint="cp .env.example .env && chmod 600 .env, then add keys only if you plan to use cloud AI.")
        keys: dict[str, bool] = {}
    else:
        mode = env_path.stat().st_mode
        loose = bool(mode & (stat.S_IRGRP | stat.S_IROTH))
        r.add(S, "env_file", "WARN" if loose else "PASS",
              f"{env_path.name} present (permissions {oct(mode & 0o777)})",
              hint=f"chmod 600 {env_path}" if loose else None)
        keys = _parse_env_file(env_path)
    for key in SECRET_KEYS:
        in_file = keys.get(key, False)
        in_env = bool(os.environ.get(key))
        where = [w for w, ok in (("in .env", in_file), ("in environment", in_env)) if ok]
        # Never report the value - not even its length or prefix.
        r.add(S, key, "INFO", ("present " + " and ".join(where)) if where else "absent")
    r.add(S, "note", "INFO", "API keys alone never enable cloud AI: a parent must opt in on the dashboard")


def check_service(r: Report, ctx: Ctx) -> None:
    S = "service"
    states = {}
    for scope, base in (("user", ["systemctl", "--user"]), ("system", ["systemctl"])):
        rc_en, out_en, _ = run_cmd([*base, "is-enabled", "littlebot.service"], timeout=5)
        rc_ac, out_ac, _ = run_cmd([*base, "is-active", "littlebot.service"], timeout=5)
        if rc_en is None and rc_ac is None:
            r.add(S, f"{scope}_service", "SKIP", "systemctl not available")
            continue
        enabled = (out_en.strip() or "unknown").splitlines()[0]
        active = (out_ac.strip() or "unknown").splitlines()[0]
        states[scope] = (enabled, active)
        status = "FAIL" if active == "failed" else ("PASS" if active == "active" else "INFO")
        r.add(S, f"{scope}_service", status, f"littlebot.service ({scope}): enabled={enabled} active={active}",
              hint=(f"journalctl {'--user ' if scope == 'user' else ''}-u littlebot.service -b --no-pager | tail -50"
                    if status == "FAIL" else None))
    if all(states.get(s, ("", ""))[0] == "enabled" for s in ("user", "system")):
        r.add(S, "double_install", "WARN", "both the user and the system littlebot.service are enabled",
              hint="Two copies fight over the camera/microphone. Disable one (docs/PI_SETUP.md, Autostart).")
    xdg = Path.home() / ".config" / "autostart" / "littlebot.desktop"
    labwc = Path.home() / ".config" / "labwc" / "autostart"
    r.add(S, "autostart", "INFO",
          f"XDG autostart {'installed' if xdg.exists() else 'absent'}; labwc autostart "
          f"{'mentions littlebot' if 'littlebot' in (read_text(labwc) or '') else 'has no littlebot entry'}")
    pids = find_littlebot_pids()
    if pids:
        rss = []
        for pid in pids:
            m = re.search(r"VmRSS:\s+(\d+)", read_text(f"/proc/{pid}/status") or "")
            rss.append(f"{pid} ({int(m.group(1)) // 1024} MB)" if m else str(pid))
        r.add(S, "process", "INFO" if len(pids) == 1 else "WARN", "running: " + ", ".join(rss),
              hint=None if len(pids) == 1 else "More than one LittleBot is running.")
    else:
        r.add(S, "process", "INFO", "LittleBot is not running")
    log_rel = ctx.cfg("logging", "file", default="logs/littlebot.log")
    if log_rel:
        log_path = Path(log_rel)
        if not log_path.is_absolute():
            log_path = Path((ctx.config or {}).get("_data_path") or REPO_ROOT / "data") / log_path
        if log_path.exists():
            age = (time.time() - log_path.stat().st_mtime) / 60
            r.add(S, "log_file", "INFO", f"{log_path} ({log_path.stat().st_size // 1024} KB, modified {age:.0f} min ago)")
        else:
            r.add(S, "log_file", "INFO", f"{log_path} (not created yet)")


DEVICE_SCRIPTS: list[tuple[str, str, bool]] = [
    # (file, owner subsystem, safe to run automatically in --list mode)
    ("check_audio.py", "audio", True),
    ("check_camera.py", "vision", True),
    ("download_models.py", "vision", True),  # --list only reports local model files (no download)
    ("benchmark_face.py", "face", False),   # heavy, opens a window
    ("face_preview.py", "face", False),     # opens a window
    ("calibrate_servos.py", "motion", False),   # never run automatically: may move servos
    ("servo_test_pattern.py", "motion", False),  # never run automatically: moves servos
]


def check_device_scripts(r: Report, ctx: Ctx) -> None:
    S = "device_scripts"
    for fname, owner, auto in DEVICE_SCRIPTS:
        path = SCRIPTS_DIR / fname
        rel = f"scripts/{fname}"
        if not path.exists():
            r.add(S, fname, "SKIP", f"{rel} not available yet ({owner} subsystem)")
            continue
        if not auto:
            note = "never run automatically (it can move servos)" if owner == "motion" else "run it manually"
            r.add(S, fname, "INFO", f"{rel} present - {note}: .venv/bin/python {rel} --help")
            continue
        if ctx.args.skip_scripts:
            r.add(S, fname, "SKIP", f"{rel} present (skipped: --skip-scripts)")
            continue
        rc, out, err = run_cmd([ctx.python, str(path), "--help"], timeout=20, cwd=REPO_ROOT)
        if rc != 0 or "--list" not in out:
            r.add(S, fname, "SKIP", f"{rel} has no --list option; run it manually",
                  {"help_exit": rc, "help_tail": (out or err)[-500:]})
            continue
        rc, out, err = run_cmd([ctx.python, str(path), "--list"], timeout=45, cwd=REPO_ROOT,
                               env={"LITTLEBOT_DIAGNOSE": "1"})
        tail = (out + ("\n" + err if err else "")).strip()[-2000:]
        if rc is None:
            r.add(S, fname, "WARN", f"{rel} --list did not finish: {err.strip()}")
        else:
            status = "PASS" if rc == 0 else ctx.sev("FAIL", "WARN")
            first = next((ln for ln in out.splitlines() if ln.strip()), "")[:160]
            r.add(S, fname, status, f"{rel} --list exit {rc}: {first}", {"exit_code": rc, "output_tail": tail})


SECTION_FUNCS: dict[str, Callable[[Report, Ctx], None]] = {
    "platform": check_platform, "os": check_os, "python": check_python, "packages": check_packages,
    "config": check_config, "power": check_power, "memory": check_memory, "storage": check_storage,
    "display": check_display, "touch": check_touch, "camera": check_camera, "audio": check_audio,
    "i2c": check_i2c, "network": check_network, "secrets": check_secrets, "service": check_service,
    "device_scripts": check_device_scripts,
}

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

COLORS = {"PASS": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31;1m", "SKIP": "\033[2m", "INFO": "\033[36m"}


def exit_code_for(counts: dict[str, int], strict: bool) -> int:
    if counts["FAIL"]:
        return 1
    if strict and counts["WARN"]:
        return 2
    return 0


def print_text(report: Report, ctx: Ctx, code: int, color: bool) -> None:
    def tag(status: str) -> str:
        return f"{COLORS[status]}[{status}]\033[0m" if color else f"[{status}]"

    print(f"LittleBot diagnostics - {dt.datetime.now():%Y-%m-%d %H:%M} - host {socket.gethostname()} - mode {ctx.mode}")
    for section, checks in report.sections.items():
        if not checks:
            continue
        print(f"\n== {section} ==")
        for c in checks:
            print(f"  {tag(c.status)} {c.name:<22} {c.message}")
            if c.hint and c.status in ("WARN", "FAIL", "INFO") and (c.status != "INFO" or ctx.args.verbose):
                print(f"         -> {c.hint}")
            if ctx.args.verbose and c.details:
                print("         " + json.dumps(c.details, default=str)[:600])
    counts = report.counts()
    verdict = {0: "OK", 1: "PROBLEMS FOUND", 2: "WARNINGS (strict)"}[code]
    print("\nSummary: " + ", ".join(f"{counts[s]} {s}" for s in STATUSES) + f" -> {verdict} (exit {code})")
    if code:
        print("Fix FAIL lines first; docs/TROUBLESHOOTING.md explains each area.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LittleBot one-stop diagnostics (read-only).")
    p.add_argument("--json", action="store_true", help="print a JSON report instead of text")
    p.add_argument("--mode", choices=["simulation", "pi", "offline"],
                   help="config mode to validate (default: pi on a Raspberry Pi, else LITTLEBOT_MODE or simulation)")
    p.add_argument("--python", help="interpreter for package/config/device probes (default: .venv/bin/python if present)")
    p.add_argument("--section", action="append", choices=SECTIONS, help="only run these sections (repeatable)")
    p.add_argument("--skip-scripts", action="store_true", help="do not run scripts/check_*.py --list")
    p.add_argument("--i2c-scan", action="store_true", help="probe the I2C bus with i2cdetect -r (read-only probes)")
    p.add_argument("--env-file", help=argparse.SUPPRESS)  # tests: alternative .env path
    p.add_argument("--strict", action="store_true", help="exit 2 when there are warnings")
    p.add_argument("--verbose", "-v", action="store_true", help="show details for every check")
    p.add_argument("--no-color", action="store_true", help="disable coloured output")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    model = pi_model()
    venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    python = args.python or (str(venv_py) if venv_py.exists() else sys.executable)
    mode = args.mode or ("pi" if model else (os.environ.get("LITTLEBOT_MODE") or "simulation").lower())
    if mode not in ("simulation", "pi", "offline"):
        mode = "simulation"
    ctx = Ctx(args=args, is_pi=model is not None, model=model, python=python, mode=mode)
    report = Report()
    wanted = set(args.section or SECTIONS)
    if "config" not in wanted and wanted & {"camera", "audio", "i2c", "network", "service", "storage"}:
        wanted.add("config")  # device checks read device names from the config
    started = time.monotonic()
    for name in SECTIONS:
        if name not in wanted:
            continue
        try:
            SECTION_FUNCS[name](report, ctx)
        except Exception as exc:  # noqa: BLE001 - a diagnose bug must not hide other results
            report.add(name, "internal_error", "WARN", f"diagnose could not check this: {type(exc).__name__}: {exc}")
    counts = report.counts()
    code = exit_code_for(counts, args.strict)
    if args.json:
        payload = {
            "tool": "littlebot-diagnose",
            "schema": 1,
            "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "duration_s": round(time.monotonic() - started, 2),
            "repo": str(REPO_ROOT),
            "mode": mode,
            "is_raspberry_pi": ctx.is_pi,
            "probe_python": python,
            "summary": {**counts, "exit_code": code},
            "sections": {s: [c.to_dict() for c in checks] for s, checks in report.sections.items() if s in wanted},
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print_text(report, ctx, code, color=sys.stdout.isatty() and not args.no_color)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
