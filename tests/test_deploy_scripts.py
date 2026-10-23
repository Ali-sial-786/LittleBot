"""Tests for the Raspberry Pi deployment scripts (installer, services, diagnostics).

Everything here runs without real hardware: shell scripts are syntax-checked and run
only in --dry-run mode with stub `sudo` / `shutdown` / `systemctl` / `apt-get`
commands first on PATH, so a test can never install packages or power anything off.
"""

from __future__ import annotations

import configparser
import csv
import importlib.util
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from littlebot.core.config import load_config

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SHELL_SCRIPTS = sorted(SCRIPTS.rglob("*.sh"))
EXPECTED_SECTIONS = {
    "platform", "os", "python", "packages", "config", "power", "memory", "storage", "display",
    "touch", "camera", "audio", "i2c", "network", "secrets", "service", "device_scripts",
}
STATUSES = {"PASS", "WARN", "FAIL", "SKIP", "INFO"}
BASH = shutil.which("bash")


def _load_script_module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_lb_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def diag() -> ModuleType:
    return _load_script_module("diagnose")


@pytest.fixture
def stub_path(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """PATH with logging stubs for every command that could change the system."""
    bindir = tmp_path / "stubs"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("sudo", "shutdown", "reboot", "poweroff", "systemctl", "apt-get", "apt", "usermod", "raspi-config"):
        p = bindir / name
        p.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 3\n')
        p.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    return env, log


def _calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


# ----------------------------------------------------------------- shell scripts
@pytest.mark.skipif(BASH is None, reason="bash not installed")
@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_shell_script_syntax(script: Path) -> None:
    res = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean() -> None:
    res = subprocess.run(["shellcheck", "-S", "warning", "-x", *map(str, SHELL_SCRIPTS)],
                         capture_output=True, text=True, cwd=ROOT)
    assert res.returncode == 0, res.stdout + res.stderr


def test_my_shell_scripts_exist_and_are_executable() -> None:
    for rel in ("install_pi.sh", "safe_shutdown.sh", "set_default_speaker.sh", "systemd/littlebot-launch.sh",
                "autostart/littlebot-session-start.sh"):
        p = SCRIPTS / rel
        assert p.is_file(), rel
        assert p.stat().st_mode & stat.S_IXUSR, f"{rel} is not executable"
        assert p.read_text().startswith("#!/usr/bin/env bash"), rel
        assert "set -euo pipefail" in p.read_text() or "set -uo pipefail" in p.read_text(), rel


# ----------------------------------------------------------------- installer
@pytest.mark.skipif(BASH is None, reason="bash not installed")
def test_install_help() -> None:
    res = subprocess.run([BASH, str(SCRIPTS / "install_pi.sh"), "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    for opt in ("--dry-run", "--with-service", "--enable-i2c", "--system-opencv", "--headless-service", "--no-live",
                "--no-audio-default"):
        assert opt in res.stdout


@pytest.mark.skipif(BASH is None, reason="bash not installed")
def test_install_rejects_unknown_and_conflicting_options() -> None:
    res = subprocess.run([BASH, str(SCRIPTS / "install_pi.sh"), "--bogus"], capture_output=True, text=True)
    assert res.returncode == 2
    res = subprocess.run([BASH, str(SCRIPTS / "install_pi.sh"), "--with-service", "--headless-service", "--dry-run"],
                         capture_output=True, text=True)
    assert res.returncode == 2


@pytest.mark.skipif(BASH is None, reason="bash not installed")
def test_install_dry_run_changes_nothing(stub_path, tmp_path: Path) -> None:
    env, log = stub_path
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    before = {p: p.exists() for p in (ROOT / "config" / "local.yaml", ROOT / ".env", ROOT / "data")}
    res = subprocess.run([BASH, str(SCRIPTS / "install_pi.sh"), "--dry-run", "--allow-non-pi", "--with-service",
                          "--enable-i2c"], capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
    if "This installer is for Raspberry Pi OS / Debian" in res.stderr:
        pytest.skip("not a Debian-family system")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "dry run" in res.stdout.lower()
    assert "[dry-run]" in res.stdout and "pip install" in res.stdout
    assert "physical_enabled: false" in res.stdout  # reminder that motion stays off
    # Only read-only systemctl queries may run in dry-run mode; nothing via sudo/apt.
    calls = _calls(log)
    assert not [c for c in calls if c.split()[0] in ("sudo", "apt-get", "apt", "usermod", "raspi-config", "shutdown")], calls
    assert all(("is-enabled" in c or "is-active" in c) for c in calls if c.startswith("systemctl")), calls
    assert not (tmp_path / "home" / ".config").exists()
    assert {p: p.exists() for p in before} == before


@pytest.mark.skipif(BASH is None, reason="bash not installed")
@pytest.mark.parametrize("no_live", [False, True], ids=["default", "no-live"])
def test_install_live_voice_extra(stub_path, tmp_path: Path, no_live: bool) -> None:
    """Live voice (aiortc) is installed by default in its own, non-fatal pip step; --no-live skips it."""
    env, log = stub_path
    env["HOME"] = str(tmp_path / "home")
    args = ["--dry-run", "--allow-non-pi", "--skip-apt", "--no-chromium", "--no-models"] + (["--no-live"] if no_live else [])
    res = subprocess.run([BASH, str(SCRIPTS / "install_pi.sh"), *args], capture_output=True, text=True, env=env,
                         cwd=ROOT, timeout=120)
    if "This installer is for Raspberry Pi OS / Debian" in res.stderr:
        pytest.skip("not a Debian-family system")
    assert res.returncode == 0, res.stdout + res.stderr
    pip_lines = [line for line in res.stdout.splitlines() if "[dry-run]" in line and "pip install" in line]
    core = [line for line in pip_lines if "[vision,audio,pi]" in line]
    live = [line for line in pip_lines if line.rstrip().endswith("[live]")]
    assert len(core) == 1, pip_lines  # the core extras are unchanged (and never include live)
    assert "live" not in core[0].split("-e", 1)[1]
    if no_live:
        assert not live and "live voice: skipped (--no-live)" in res.stdout
    else:
        assert len(live) == 1 and "--only-binary=av," in live[0], pip_lines
        assert 'live voice: pip install -e ".[live]"' in res.stdout
        assert res.stdout.index("[vision,audio,pi]") < res.stdout.index("Live voice (aiortc)")
    assert not [c for c in _calls(log) if c.split()[0] in ("sudo", "apt-get", "apt")]


@pytest.mark.skipif(BASH is None, reason="bash not installed")
@pytest.mark.parametrize("skip", [False, True], ids=["default", "no-audio-default"])
def test_install_audio_default_step(stub_path, tmp_path: Path, skip: bool) -> None:
    """The USB-speaker default runs by default as a late, non-fatal step; --no-audio-default skips it."""
    env, log = stub_path
    env["HOME"] = str(tmp_path / "home")
    args = ["--dry-run", "--allow-non-pi", "--skip-apt", "--no-chromium", "--no-models", "--no-live"]
    res = subprocess.run([BASH, str(SCRIPTS / "install_pi.sh"), *args, *(["--no-audio-default"] if skip else [])],
                         capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
    if "This installer is for Raspberry Pi OS / Debian" in res.stderr:
        pytest.skip("not a Debian-family system")
    assert res.returncode == 0, res.stdout + res.stderr
    step = [line for line in res.stdout.splitlines() if "[dry-run]" in line and "set_default_speaker.sh" in line]
    if skip:
        assert not step and "audio default: skipped (--no-audio-default)" in res.stdout
    else:
        assert len(step) == 1, res.stdout
        assert "audio default: USB speaker" in res.stdout
        assert res.stdout.index("Default sound devices") < res.stdout.index("Next steps")
    assert not (tmp_path / "home" / ".config").exists()
    assert not [c for c in _calls(log) if c.split()[0] in ("sudo", "apt-get", "apt")]


def test_live_extra_matches_the_installer() -> None:
    """The installer's `[live]` step needs the extra in pyproject.toml (aiortc 1.x)."""
    import tomllib

    extras = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    assert extras["live"] == ["aiortc>=1.10,<2"]
    assert "live" in extras["all"][0]


def test_templates_use_only_known_placeholders() -> None:
    import re

    for rel in ("littlebot.service", "systemd/littlebot.service", "autostart/littlebot.desktop"):
        text = (SCRIPTS / rel).read_text()
        assert "@REPO_DIR@" in text, rel
        assert set(re.findall(r"@[A-Z_]+@", text)) <= {"@REPO_DIR@", "@USER@", "@UID@"}, rel


# ----------------------------------------------------------------- default speaker (PipeWire)
# A fake PipeWire for scripts/set_default_speaker.sh: `pw-dump` prints a Raspberry Pi 5 graph
# (two HDMI outputs, the "UACDemoV1.0" USB speaker - which also has a microphone - and the
# "USB PnP Sound Device" microphone), `wpctl` keeps the default in a JSON file, and
# `systemctl --user restart wireplumber` makes the drop-in's priorities "active".
HDMI0 = "alsa_output.platform-107c701400.hdmi.hdmi-stereo"
USB_SPK = "alsa_output.usb-Jieli_Technology_UACDemoV1.0_4150344B32373916-00.analog-stereo"
USB_SPK_MIC = "alsa_input.usb-Jieli_Technology_UACDemoV1.0_4150344B32373916-00.mono-fallback"
USB_MIC = "alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.mono-fallback"
USB_MIC_OUT = "alsa_output.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-stereo"
FAKE_NODES = {
    40: (HDMI0, "Audio/Sink", "Built-in Audio Digital Stereo (HDMI)", 30, 1500),
    41: ("alsa_output.platform-107c706400.hdmi.hdmi-stereo", "Audio/Sink", "Built-in Audio Digital Stereo (HDMI 2)",
         31, 1500),
    52: (USB_SPK, "Audio/Sink", "UACDemoV1.0 Analog Stereo", 50, 1009),
    53: (USB_SPK_MIC, "Audio/Source", "UACDemoV1.0 Mono", 50, 2009),
    61: (USB_MIC, "Audio/Source", "USB PnP Sound Device Mono", 60, 2009),
    62: (USB_MIC_OUT, "Audio/Sink", "USB PnP Sound Device Analog Stereo", 60, 1009),
}

_FAKE_PW_DUMP = r'''
import json, os, pathlib
d = pathlib.Path(os.environ["FAKE_PW_DIR"])
g = json.loads((d / "graph.json").read_text())
st = json.loads((d / "state.json").read_text()) if (d / "state.json").exists() else {}
active = (d / "restarted").exists()  # WirePlumber restarted -> the drop-in's priorities apply
out = [{"id": 70, "type": "PipeWire:Interface:Node",
        "info": {"props": {"node.name": "Chromium", "media.class": "Stream/Output/Audio"}}}]
for n in g["nodes"]:
    prio = n["prio"]
    if active:
        prio = 1500 if n["name"].startswith("alsa_output.usb-") else 2100 if n["name"].startswith(
            "alsa_input.usb-") else 100 if "hdmi" in n["name"] else prio
    out.append({"id": n["id"], "type": "PipeWire:Interface:Node", "info": {"props": {
        "node.name": n["name"], "media.class": n["cls"], "node.description": n["desc"],
        "device.id": n["device"], "priority.session": prio, "priority.driver": prio}}})
configured = st.get("configured_sink", g.get("configured_sink"))
meta = [{"subject": 0, "key": "default.configured.audio.sink", "type": "Spa:String:JSON",
         "value": {"name": configured}}] if configured else []
out.append({"id": 30, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
            "metadata": meta})
print(json.dumps(out, indent=2))
'''

_FAKE_WPCTL = r'''
import json, os, pathlib, sys
d = pathlib.Path(os.environ["FAKE_PW_DIR"])
args = sys.argv[1:]
with open(d / "calls.log", "a") as f:
    f.write("wpctl " + " ".join(args) + "\n")
if os.environ.get("FAKE_PW_DOWN"):
    sys.exit("Could not connect to PipeWire")
g = json.loads((d / "graph.json").read_text())
sf = d / "state.json"
st = json.loads(sf.read_text()) if sf.exists() else {}
by_id = {str(n["id"]): n for n in g["nodes"]}
if args[0] == "set-default":
    n = by_id[args[1]]
    kind = "sink" if n["cls"] == "Audio/Sink" else "source"
    st["default_" + kind] = st["configured_" + kind] = n["name"]
elif args[0] == "clear-default":
    st["configured_sink"] = None
elif args[0] == "inspect":
    kind = "sink" if args[1] == "@DEFAULT_AUDIO_SINK@" else "source"
    name = st.get("default_" + kind) or g.get("default_" + kind)
    n = next((n for n in g["nodes"] if n["name"] == name), None)
    if n is None:
        sys.exit("Object not found")
    print("id %s, type PipeWire:Interface:Node" % n["id"])
    print('    device.id = "%s"' % n["device"])
    print('  * node.description = "%s"' % n["desc"])
    print('  * node.name = "%s"' % n["name"])
sf.write_text(json.dumps(st))
'''

_FAKE_SYSTEMCTL = r'''
import os, pathlib, sys
d = pathlib.Path(os.environ["FAKE_PW_DIR"])
with open(d / "calls.log", "a") as f:
    f.write("systemctl " + " ".join(sys.argv[1:]) + "\n")
if sys.argv[1:] == ["--user", "restart", "wireplumber"]:
    (d / "restarted").touch()
'''

_FAKE_WIREPLUMBER = r'''
import os
v = os.environ.get("FAKE_WP_VERSION", "0.5.8")
print("wireplumber\nCompiled with libwireplumber %s\nLinked with libwireplumber %s" % (v, v))
'''


@pytest.fixture
def fake_pw(tmp_path: Path):
    """Returns run(node_ids, *args, **env) -> (CompletedProcess, calls, home)."""
    state, bindir, home = tmp_path / "pw", tmp_path / "pwbin", tmp_path / "home"
    for p in (state, bindir, home):
        p.mkdir()
    for name, body in (("pw-dump", _FAKE_PW_DUMP), ("wpctl", _FAKE_WPCTL), ("systemctl", _FAKE_SYSTEMCTL),
                       ("wireplumber", _FAKE_WIREPLUMBER)):
        (bindir / name).write_text(f"#!{sys.executable}\n{body}")
        (bindir / name).chmod(0o755)

    def run(node_ids, *args: str, **extra_env: str):
        nodes = [dict(zip(("id", "name", "cls", "desc", "device", "prio"), (i, *FAKE_NODES[i]))) for i in node_ids]
        (state / "graph.json").write_text(json.dumps({
            "nodes": nodes, "configured_sink": HDMI0, "default_sink": HDMI0, "default_source": USB_SPK_MIC}))
        env = {k: v for k, v in os.environ.items() if k not in ("SUDO_USER", "XDG_CONFIG_HOME")}
        env.update(PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}", HOME=str(home), FAKE_PW_DIR=str(state),
                   XDG_RUNTIME_DIR=str(tmp_path), **extra_env)
        res = subprocess.run([BASH, str(SCRIPTS / "set_default_speaker.sh"), *args], capture_output=True, text=True,
                             env=env, timeout=60)
        calls = _calls(state / "calls.log")
        (state / "calls.log").unlink(missing_ok=True)
        return res, [c for c in calls if not c.startswith(("wpctl status", "wpctl inspect"))], home

    return run


DROPIN_05 = Path(".config/wireplumber/wireplumber.conf.d/51-littlebot-usb-audio.conf")
DROPIN_04 = Path(".config/wireplumber/main.lua.d/51-littlebot-usb-audio.lua")
PI5 = (40, 41, 52, 53, 61)


@pytest.mark.skipif(BASH is None, reason="bash not installed")
def test_set_default_speaker_help() -> None:
    res = subprocess.run([BASH, str(SCRIPTS / "set_default_speaker.sh"), "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    for opt in ("--dry-run", "--speaker", "--mic", "--undo", "wpctl set-default"):
        assert opt in res.stdout
    res = subprocess.run([BASH, str(SCRIPTS / "set_default_speaker.sh"), "--bogus"], capture_output=True, text=True)
    assert res.returncode == 2


@pytest.mark.skipif(BASH is None, reason="bash not installed")
def test_set_default_speaker_dry_run_without_pipewire_changes_nothing(fake_pw) -> None:
    res, calls, home = fake_pw(PI5, "--dry-run", FAKE_PW_DOWN="1")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Cannot reach PipeWire" in res.stderr
    assert f"[dry-run] write {home / DROPIN_05}" in res.stdout
    assert "[dry-run] systemctl --user restart wireplumber" in res.stdout
    assert not (home / ".config").exists() and calls == []


@pytest.mark.skipif(BASH is None, reason="bash not installed")
@pytest.mark.parametrize("nodes,args,speaker,mic", [
    (PI5, [], 52, 61),                        # the speaker's own microphone is not chosen
    ((40, 41, 52, 61, 62), [], 52, 61),       # the microphone's headphone output is not chosen
    ((40, 52, 53), [], 52, 53),               # only the speaker's microphone: better than none
    (PI5, ["--mic", "uacdemo"], 52, 53),      # explicit choice by name (case-insensitive)
    ((40, 41, 52, 53, 61, 62), ["--speaker", "PnP"], 62, 53),
], ids=["pi5", "mic-has-output", "speaker-mic-only", "mic-override", "speaker-override"])
def test_set_default_speaker_dry_run_picks_usb_nodes(fake_pw, nodes, args, speaker: int, mic: int) -> None:
    res, calls, home = fake_pw(nodes, "--dry-run", *args)
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"[dry-run] wpctl set-default {speaker}\n" in res.stdout
    assert f"[dry-run] wpctl set-default {mic}\n" in res.stdout
    assert "[dry-run] systemctl --user restart wireplumber" in res.stdout  # priorities not active yet
    assert not (home / ".config").exists() and calls == []


@pytest.mark.skipif(BASH is None, reason="bash not installed")
def test_set_default_speaker_full_run_is_idempotent_and_undoable(fake_pw) -> None:
    res, calls, home = fake_pw(PI5, "--wait", "2")
    assert res.returncode == 0, res.stdout + res.stderr
    assert calls == ["systemctl --user restart wireplumber", "wpctl set-default 52", "wpctl set-default 61"]
    assert "default speaker:    UACDemoV1.0 Analog Stereo" in res.stdout
    assert "default microphone: USB PnP Sound Device Mono" in res.stdout
    conf = (home / DROPIN_05).read_text()
    assert "monitor.alsa.rules = [" in conf and "update-props = {" in conf
    for rule in ('node.name = "~^alsa_output[.].*hdmi"', "priority.session = 100",
                 'node.name = "~^alsa_output[.]usb-"', "priority.session = 1500",
                 'node.name = "~^alsa_input[.]usb-"', "priority.session = 2100"):
        assert rule in conf
    assert conf.index("hdmi") < conf.index("usb-")  # USB rules come last, so they win
    assert conf.count("{") == conf.count("}") and conf.count("[") == conf.count("]")

    res, calls, _ = fake_pw(PI5)  # second run: rule unchanged and active -> no restart
    assert res.returncode == 0, res.stdout + res.stderr
    assert calls == ["wpctl set-default 52", "wpctl set-default 61"]
    assert "is up to date" in res.stdout and "no restart needed" in res.stdout

    res, calls, _ = fake_pw(PI5, "--undo")
    assert res.returncode == 0, res.stdout + res.stderr
    assert not (home / DROPIN_05).exists()
    assert calls == ["systemctl --user restart wireplumber"]


@pytest.mark.skipif(BASH is None, reason="bash not installed")
def test_set_default_speaker_wireplumber_04_lua(fake_pw) -> None:
    res, calls, home = fake_pw(PI5, "--wait", "2", FAKE_WP_VERSION="0.4.13")
    assert res.returncode == 0, res.stdout + res.stderr
    assert not (home / DROPIN_05).exists()
    lua = (home / DROPIN_04).read_text()
    assert lua.count("table.insert(alsa_monitor.rules, {") == 3
    for rule in ('"alsa_output.*hdmi*"', '"alsa_output.usb-*"', '"alsa_input.usb-*"', '["priority.session"] = 1500'):
        assert rule in lua
    assert "wpctl set-default 52" in calls


@pytest.mark.skipif(BASH is None, reason="bash not installed")
def test_set_default_speaker_without_usb_speaker_warns(fake_pw) -> None:
    res, calls, home = fake_pw((40, 41, 61), "--wait", "1")
    assert res.returncode == 1
    assert "No USB speaker found" in res.stderr and "Sound still goes to HDMI" in res.stderr
    assert "wpctl clear-default 0" in calls  # the saved HDMI choice must not beat the USB speaker later
    assert "wpctl set-default 61" in calls
    assert (home / DROPIN_05).exists()  # the rule still prefers USB once it is plugged in


# ----------------------------------------------------------------- systemd units
def _unit(rel: str) -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser(strict=False, delimiters=("=",), comment_prefixes=("#", ";"))
    cp.optionxform = str  # type: ignore[assignment,method-assign]
    cp.read_string((SCRIPTS / rel).read_text())
    return cp


def test_user_service_unit_has_required_keys() -> None:
    u = _unit("systemd/littlebot.service")
    assert u["Unit"]["Description"]
    assert "graphical-session.target" in u["Unit"]["After"]
    svc = u["Service"]
    assert "littlebot-launch.sh" in svc["ExecStart"]
    assert svc["Restart"] == "on-failure"
    assert float(svc["RestartSec"]) > 0
    assert svc["KillSignal"] == "SIGTERM"
    assert int(svc["TimeoutStopSec"]) >= 10
    assert "WorkingDirectory" in svc and "EnvironmentFile" in svc
    assert "User" not in svc  # a user unit must not set User=
    assert u["Install"]["WantedBy"]
    assert "StartLimitBurst" in u["Unit"]


def test_headless_system_unit_has_required_keys() -> None:
    u = _unit("littlebot.service")
    svc = u["Service"]
    assert svc["User"] == "@USER@"
    assert "--headless" in svc["ExecStart"] and "-m littlebot" in svc["ExecStart"]
    assert svc["Restart"] == "on-failure"
    assert svc["KillSignal"] == "SIGTERM"
    assert u["Install"]["WantedBy"] == "multi-user.target"
    assert "network-online.target" in u["Unit"]["After"]


def test_desktop_autostart_entry() -> None:
    cp = configparser.RawConfigParser(strict=False, delimiters=("=",))
    cp.optionxform = str  # type: ignore[assignment,method-assign]
    cp.read_string((SCRIPTS / "autostart" / "littlebot.desktop").read_text())
    entry = cp["Desktop Entry"]
    assert entry["Type"] == "Application"
    assert entry["Exec"].endswith("littlebot-session-start.sh")


# ----------------------------------------------------------------- launcher
def _stub_python(tmp_path: Path) -> Path:
    stub = tmp_path / "fakepython"
    stub.write_text('#!/bin/sh\necho "ARGS: $*"\necho "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-} DISPLAY=${DISPLAY:-}"\n')
    stub.chmod(0o755)
    return stub


@pytest.mark.skipif(BASH is None or not hasattr(socket, "AF_UNIX"), reason="needs bash + unix sockets")
def test_launcher_finds_wayland_socket(tmp_path: Path) -> None:
    rt = tmp_path / "rt"
    rt.mkdir()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(rt / "wayland-0"))
    try:
        env = {k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY", "DISPLAY")}
        env.update(XDG_RUNTIME_DIR=str(rt), LITTLEBOT_PYTHON=str(_stub_python(tmp_path)), LITTLEBOT_DISPLAY_WAIT="2",
                   LITTLEBOT_ARGS="--debug")
        res = subprocess.run([BASH, str(SCRIPTS / "systemd" / "littlebot-launch.sh")], capture_output=True, text=True,
                             env=env, timeout=30)
    finally:
        sock.close()
    assert res.returncode == 0, res.stderr
    assert "ARGS: -m littlebot --mode pi --debug" in res.stdout
    assert "WAYLAND_DISPLAY=wayland-0" in res.stdout


@pytest.mark.skipif(BASH is None, reason="bash not installed")
def test_launcher_headless_and_no_display(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY", "DISPLAY")}
    env.update(XDG_RUNTIME_DIR=str(tmp_path), LITTLEBOT_PYTHON=str(_stub_python(tmp_path)), LITTLEBOT_HEADLESS="1")
    res = subprocess.run([BASH, str(SCRIPTS / "systemd" / "littlebot-launch.sh")], capture_output=True, text=True,
                         env=env, timeout=30)
    assert res.returncode == 0 and "--headless" in res.stdout
    if os.path.exists("/tmp/.X11-unix/X0"):
        return  # an X server exists on this machine: the no-display path cannot be exercised
    env.pop("LITTLEBOT_HEADLESS")
    env["LITTLEBOT_DISPLAY_WAIT"] = "0"
    res = subprocess.run([BASH, str(SCRIPTS / "systemd" / "littlebot-launch.sh")], capture_output=True, text=True,
                         env=env, timeout=30)
    assert res.returncode == 1
    assert "no desktop session" in res.stderr


# ----------------------------------------------------------------- safe shutdown
@pytest.mark.skipif(BASH is None, reason="bash not installed")
def test_safe_shutdown_dry_run_never_shuts_down(stub_path) -> None:
    env, log = stub_path
    res = subprocess.run([BASH, str(SCRIPTS / "safe_shutdown.sh"), "--dry-run"], capture_output=True, text=True,
                         env=env, timeout=60)
    assert res.returncode == 0, res.stderr
    assert "[dry-run] sudo shutdown -h now" in res.stdout
    calls = _calls(log)
    assert not [c for c in calls if c.split()[0] in ("sudo", "shutdown", "reboot", "poweroff")], calls
    res = subprocess.run([BASH, str(SCRIPTS / "safe_shutdown.sh"), "--dry-run", "--reboot"], capture_output=True,
                         text=True, env=env, timeout=60)
    assert res.returncode == 0 and "[dry-run] sudo shutdown -r now" in res.stdout


# ----------------------------------------------------------------- diagnose.py
def _run_diag(*args: str, timeout: float = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPTS / "diagnose.py"), *args], capture_output=True, text=True,
                          cwd=ROOT, timeout=timeout)


def test_diagnose_json_structure() -> None:
    res = _run_diag("--json", "--skip-scripts")
    assert res.returncode in (0, 1, 2), res.stderr  # missing devices may legitimately FAIL
    data = json.loads(res.stdout)
    assert data["tool"] == "littlebot-diagnose"
    assert set(data["sections"]) == EXPECTED_SECTIONS
    assert data["summary"]["exit_code"] == res.returncode
    assert set(STATUSES) <= set(data["summary"])
    total = 0
    for section, checks in data["sections"].items():
        assert isinstance(checks, list), section
        for c in checks:
            assert c["status"] in STATUSES, (section, c)
            assert c["name"] and isinstance(c["message"], str)
            total += 1
    assert total == sum(data["summary"][s] for s in STATUSES)
    # Things that must always be reported, devices or not.
    names = {(s, c["name"]) for s, checks in data["sections"].items() for c in checks}
    assert ("config", "motion_safety") in names
    assert ("secrets", "ANTHROPIC_API_KEY") in names
    assert ("network", "hostname") in names


def test_diagnose_text_mode_and_sections() -> None:
    res = _run_diag("--section", "platform", "--section", "memory", "--no-color")
    assert res.returncode in (0, 1)
    assert "== platform ==" in res.stdout and "Summary:" in res.stdout
    assert "== camera ==" not in res.stdout


def test_diagnose_never_prints_secret_values(tmp_path: Path) -> None:
    secret = "sk-ant-THIS-IS-A-FAKE-TEST-KEY-1234567890"
    env_file = tmp_path / "test.env"
    env_file.write_text(f"ANTHROPIC_API_KEY={secret}\nOPENAI_API_KEY=\n")
    env_file.chmod(0o644)
    for extra in (["--json"], ["--verbose"]):
        res = _run_diag("--section", "secrets", "--env-file", str(env_file), *extra)
        assert secret not in res.stdout and secret not in res.stderr
        assert "THIS-IS-A-FAKE" not in res.stdout
    data = json.loads(_run_diag("--section", "secrets", "--env-file", str(env_file), "--json").stdout)
    by_name = {c["name"]: c for c in data["sections"]["secrets"]}
    assert by_name["ANTHROPIC_API_KEY"]["message"].startswith("present")
    assert by_name["OPENAI_API_KEY"]["message"] == "absent" or "environment" in by_name["OPENAI_API_KEY"]["message"]
    assert by_name["env_file"]["status"] == "WARN"  # world-readable .env


@pytest.mark.parametrize("backend,physical,calibration,expected", [
    ("mock", False, None, "PASS"),
    ("pca9685", False, None, "PASS"),
    ("mock", True, None, "WARN"),          # calibration stage: physical on, backend still mock
    ("mock", True, "bad", "WARN"),
    ("mock", True, "good", "INFO"),
    ("pca9685", True, None, "FAIL"),
    ("pca9685", True, "bad", "FAIL"),
    ("pca9685", True, "good", "WARN"),     # configured for real motion: allowed but flagged
    ("mock", False, "bad", "WARN"),        # a broken calibration file is worth knowing about
])
def test_diagnose_motion_safety_rules(tmp_path: Path, backend: str, physical: bool, calibration: str | None,
                                      expected: str) -> None:
    calib = tmp_path / "calibration.yaml"
    if calibration == "good":
        calib.write_text((ROOT / "config" / "calibration.example.yaml").read_text())
    elif calibration == "bad":
        calib.write_text("version: 1\nservos:\n  head: {channel: 0}\n")
    env = dict(os.environ, LITTLEBOT__MOTION__BACKEND=backend,
               LITTLEBOT__MOTION__PHYSICAL_ENABLED=str(physical).lower(),
               LITTLEBOT__MOTION__CALIBRATION_FILE=str(calib))
    res = subprocess.run([sys.executable, str(SCRIPTS / "diagnose.py"), "--section", "config", "--json"],
                         capture_output=True, text=True, cwd=ROOT, env=env, timeout=120)
    checks = {c["name"]: c for c in json.loads(res.stdout)["sections"]["config"]}
    assert checks["motion_safety"]["status"] == expected, checks["motion_safety"]
    if expected == "FAIL":
        assert res.returncode == 1


def test_decode_throttled(diag: ModuleType) -> None:
    assert diag.decode_throttled(0) == []
    keys = {d["key"] for d in diag.decode_throttled(0x50005)}
    assert keys == {"under_voltage_now", "throttled_now", "under_voltage_occurred", "throttling_occurred"}
    assert diag.decode_throttled(0x1)[0]["severity"] == "FAIL"
    assert {d["bit"] for d in diag.decode_throttled(0xF000F)} == {0, 1, 2, 3, 16, 17, 18, 19}


def _edid(name: str, h: int, v: int, clock_10khz: int, hblank: int, vblank: int) -> bytes:
    e = bytearray(128)
    e[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    m = ((ord("D") - 64) << 10) | ((ord("O") - 64) << 5) | (ord("R") - 64)
    e[8], e[9] = m >> 8, m & 0xFF
    d = bytearray(18)
    d[0], d[1] = clock_10khz & 0xFF, clock_10khz >> 8
    d[2], d[3], d[4] = h & 0xFF, hblank & 0xFF, ((h >> 8) << 4) | (hblank >> 8)
    d[5], d[6], d[7] = v & 0xFF, vblank & 0xFF, ((v >> 8) << 4) | (vblank >> 8)
    e[54:72] = d
    n = bytearray(18)
    n[3] = 0xFC
    n[5:18] = (name + "\n").encode().ljust(13, b" ")[:13]
    e[72:90] = n
    return bytes(e)


def test_parse_edid(diag: ModuleType) -> None:
    # 480x320, totals 600x340 at 60 Hz -> 12.24 MHz
    info = diag.parse_edid(_edid("MPI3508", 480, 320, 1224, 120, 20))
    assert info["manufacturer"] == "DOR"
    assert info["name"] == "MPI3508"
    assert info["preferred"] == "480x320@60"
    assert diag.parse_edid(b"") == {} and diag.parse_edid(b"\x00" * 128) == {}


APP_ARGVS = [
    ["/home/pi/LittleBot/.venv/bin/python", "-m", "littlebot", "--mode", "pi"],
    ["/home/pi/LittleBot/.venv/bin/python3.13", "-u", "-X", "dev", "-m", "littlebot"],
    ["python3", "-mlittlebot"],
    ["/home/pi/LittleBot/.venv/bin/python", "/home/pi/LittleBot/.venv/bin/littlebot", "--mode", "pi"],
    ["littlebot", "--headless"],
    ["python", "littlebot/app/main.py"],
]
NOT_APP_ARGVS = [
    ["/bin/bash", "-c", "cd ~/LittleBot && .venv/bin/python -m littlebot --mode pi"],
    ["python", "scripts/perf_monitor.py", "--match", "littlebot"],
    ["python", "-c", "import littlebot"],
    ["python", "-m", "littlebotx"],
    ["nano", "littlebot.yaml"],
    ["watch", "python -m littlebot"],
    ["python", "notlittlebot/app/main.py"],
    [],
]


def test_littlebot_process_matching_python(diag: ModuleType) -> None:
    for argv in APP_ARGVS:
        assert diag.is_littlebot_argv(argv), argv
    for argv in NOT_APP_ARGVS:
        assert not diag.is_littlebot_argv(argv), argv


@pytest.mark.skipif(BASH is None, reason="bash not installed")
def test_littlebot_process_matching_bash() -> None:
    """safe_shutdown.sh must agree with diagnose.py - it sends SIGTERM to what it matches."""
    text = (SCRIPTS / "safe_shutdown.sh").read_text()
    func = text[text.index("is_littlebot_argv() {"):text.index("# PIDs of running LittleBot")]
    cases = [(a, 0) for a in APP_ARGVS] + [(a, 1) for a in NOT_APP_ARGVS]
    for argv, expected in cases:
        quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in argv)
        res = subprocess.run([BASH, "-c", f"set -euo pipefail\n{func}\nis_littlebot_argv {quoted}"],
                             capture_output=True, text=True)
        assert res.returncode == expected, (argv, res.stderr)


# ----------------------------------------------------------------- perf_monitor.py
def test_perf_monitor_help() -> None:
    res = subprocess.run([sys.executable, str(SCRIPTS / "perf_monitor.py"), "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    for opt in ("--pid", "--interval", "--duration", "--csv", "--children"):
        assert opt in res.stdout


def test_perf_monitor_one_sample_of_itself(tmp_path: Path) -> None:
    out = tmp_path / "perf.csv"
    res = subprocess.run([sys.executable, str(SCRIPTS / "perf_monitor.py"), "--self", "--samples", "1",
                          "--interval", "0.1", "--csv", str(out), "--json", "--quiet"],
                         capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    summary = json.loads(res.stdout)
    assert summary["samples"] == 1 and summary["ended"] == "completed"
    assert summary["proc_rss_mb"]["max"] > 0
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 1
    assert {"proc_cpu_pct", "proc_rss_mb", "proc_threads", "sys_mem_avail_mb", "temp_c", "throttled"} <= set(rows[0])
    assert json.loads(out.with_suffix(".summary.json").read_text())["samples"] == 1


def test_perf_monitor_reports_missing_process() -> None:
    res = subprocess.run([sys.executable, str(SCRIPTS / "perf_monitor.py"), "--match", "no-such-process-zz9",
                          "--no-csv"], capture_output=True, text=True, timeout=30)
    assert res.returncode == 2
    assert "no process" in res.stderr


# ----------------------------------------------------------------- config template
def test_hardware_example_config_is_valid_and_safe() -> None:
    cfg = load_config("pi", config_file=ROOT / "config" / "hardware.example.yaml", environ={}, use_dotenv=False,
                      include_local=False)
    assert cfg.motion.physical_enabled is False
    assert cfg.motion.backend == "mock"
    assert cfg.dashboard.lan_access is False
    assert cfg.dashboard.host == "127.0.0.1"
    assert cfg.display.fullscreen is True
    assert (cfg.display.width, cfg.display.height) == (480, 320)
    assert cfg.audio.speaker.max_volume <= 0.8


def test_hardware_example_chatgpt_block_is_valid_when_uncommented(tmp_path: Path) -> None:
    """The commented `chatgpt:` example must stay loadable and show the real defaults."""
    import re

    text = (ROOT / "config" / "hardware.example.yaml").read_text()
    uncommented = re.sub(r"(?m)^#   (live_\w+:)", r"  \1", re.sub(r"(?m)^# (chatgpt:)", r"\1", text))
    assert uncommented.count("\n  live_") >= 5
    (tmp_path / "hw.yaml").write_text(uncommented)
    kw = dict(environ={}, use_dotenv=False, include_local=False)
    cfg = load_config("pi", config_file=tmp_path / "hw.yaml", **kw)
    assert cfg.chatgpt == load_config("pi", config_file=ROOT / "config" / "hardware.example.yaml", **kw).chatgpt
