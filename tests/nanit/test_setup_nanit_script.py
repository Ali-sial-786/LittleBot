"""scripts/setup_nanit.sh and scripts/nanit_bridge.sh: pinned versions, no sudo, --check changes nothing, --remove
removes everything, a bad Go download installs nothing, and the user unit (memory cap, restarts, IPv4 only). Run
with fake programs (systemctl, curl, git, python3, ip, uname); never the real bridge, never Nanit."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SETUP = SCRIPTS / "setup_nanit.sh"
RUNNER = SCRIPTS / "nanit_bridge.sh"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not installed")


def _posix(p: Path) -> str:
    return str(p).replace("\\", "/")


def _fake(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_bytes(f"#!/usr/bin/env bash\n{body}\n".encode())
    p.chmod(0o755)


class Box:
    """A home folder and fake programs; ``run`` the setup script in it."""

    def __init__(self, tmp: Path, arch: str = "aarch64") -> None:
        self.home = tmp / "home"
        self.bin = tmp / "fakebin"
        self.log = tmp / "calls.log"
        self.home.mkdir()
        self.bin.mkdir()
        log = _posix(self.log)
        _fake(self.bin, "uname", f'[ "${{1:-}}" = -m ] && echo {arch} || echo Linux')
        _fake(self.bin, "systemctl", f'echo "systemctl $*" >> "{log}"; [ "${{2:-}}" = is-active ] && echo inactive; '
                                     f'[ "${{2:-}}" = is-enabled ] && echo disabled; exit 0')
        _fake(self.bin, "curl", f'echo "curl $*" >> "{log}"; out=""; while [ $# -gt 0 ]; do '
                                f'[ "$1" = -o ] && out=$2; shift; done; echo "not go" > "$out"')
        _fake(self.bin, "git", f'echo "git $*" >> "{log}"; exit 1')
        _fake(self.bin, "python3", f'echo "python3 $*" >> "{log}"; exit 1')
        _fake(self.bin, "ip", 'echo "1.1.1.1 via 192.168.2.1 dev wlan0 src 192.168.2.112 uid 1000"')
        _fake(self.bin, "ffplay", "exit 0")
        _fake(self.bin, "systemd-run", "exit 1")
        self.go = self.home / ".local" / "nanit-go"
        self.bridge = self.home / ".local" / "nanit-bridge"
        self.data = self.home / "LittleBot" / "data" / "nanit"
        self.units = self.home / ".config" / "systemd" / "user"

    def run(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        assert BASH is not None
        env = {"HOME": _posix(self.home), "PATH": f"{_posix(self.bin)}:/usr/bin:/bin", "LANG": "C",
               "LB_NANIT_GO_DIR": _posix(self.go), "LB_NANIT_BRIDGE_DIR": _posix(self.bridge),
               "LB_NANIT_DATA_DIR": _posix(self.data), "LB_NANIT_UNIT_DIR": _posix(self.units)}
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        env.update(extra_env or {})
        return subprocess.run([BASH, _posix(SETUP), *args], capture_output=True, text=True, env=env,
                              stdin=subprocess.DEVNULL, timeout=60)

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def files(self) -> list[str]:
        return sorted(_posix(p.relative_to(self.home)) for p in self.home.rglob("*"))


# ---------------------------------------------------------------- the text
def test_the_scripts_are_pinned_and_never_use_sudo() -> None:
    text = SETUP.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text and "\r\n" not in text
    commit = re.search(r"^BRIDGE_COMMIT=([0-9a-f]+)$", text, re.M)
    assert commit and len(commit.group(1)) == 40
    assert re.search(r"^GO_SHA256=[0-9a-f]{64}$", text, re.M) and re.search(r"^GO_VERSION=go1\.\d+\.\d+$", text, re.M)
    assert "https://go.dev/dl/${GO_VERSION}.linux-arm64.tar.gz" in text and "indiefan/home_assistant_nanit" in text
    for line in text.splitlines():  # sudo only in advice printed to the user, never run
        if "sudo" in line and not line.lstrip().startswith("#"):
            assert line.lstrip().startswith("echo "), line
    for words in ("MemoryMax=200M", "Restart=on-failure", "RestrictAddressFamilies=AF_INET AF_UNIX AF_NETLINK",
                  "systemctl --user", "WantedBy=default.target", "ExecStart=/bin/bash $RUNNER", "--login-again",
                  "--remove", "--check", "chmod 600", "LB_NANIT_PORT=$PORT", "PORT=1935"):
        assert words in text, words
    run = RUNNER.read_text(encoding="utf-8")
    assert run.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in run and "\r\n" not in run
    for words in ("NANIT_RTMP_ADDR=", "NANIT_SESSION_FILE=", "NANIT_DATA_DIR=", 'cd "$DATA"', 'exec "$BIN"',
                  "is_private", "nanit_helper.py", "cameras"):
        assert words in run, words


@needs_bash
@pytest.mark.parametrize("script", [SETUP, RUNNER])
def test_the_scripts_parse(script: Path) -> None:
    res = subprocess.run([BASH, "-n", _posix(script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


# ---------------------------------------------------------------- running it with fakes
@needs_bash
def test_check_changes_nothing(tmp_path: Path) -> None:
    box = Box(tmp_path)
    before = box.files()
    res = box.run("--check")
    assert res.returncode == 0, res.stderr
    for words in ("missing", "Go go1.", "the bridge program", "the Nanit sign-in", "littlebot-nanit.service",
                  "setup_nanit.sh"):
        assert words in res.stdout, words
    assert box.files() == before
    calls = box.calls()
    assert "curl" not in calls and "git" not in calls.replace("systemctl", "")
    assert all(c.split()[2] in ("is-active", "is-enabled") for c in calls.splitlines() if c.startswith("systemctl"))


@needs_bash
def test_help_and_bad_options(tmp_path: Path) -> None:
    box = Box(tmp_path)
    res = box.run("--help")
    assert res.returncode == 0 and "--check" in res.stdout and "--remove" in res.stdout and "set -e" not in res.stdout
    assert box.run("--bogus").returncode == 2


@needs_bash
def test_setup_needs_a_terminal(tmp_path: Path) -> None:
    box = Box(tmp_path)
    res = box.run()
    assert res.returncode == 1 and "terminal" in res.stderr and box.files() == []


@needs_bash
def test_remove_removes_everything(tmp_path: Path) -> None:
    box = Box(tmp_path)
    for d in (box.go / "go" / "bin", box.bridge / "bin", box.data / "video", box.units):
        d.mkdir(parents=True)
    (box.bridge / "bin" / "nanit").write_text("x")
    (box.data / "session.json").write_text("{}")
    (box.data / "cameras.json").write_text("{}")
    (box.units / "littlebot-nanit.service").write_text("[Unit]\n")
    (box.units / "other.service").write_text("[Unit]\n")
    keep = box.home / "LittleBot" / "data" / "littlebot.db"
    keep.write_text("db")
    res = box.run("--remove")
    assert res.returncode == 0, res.stderr
    assert not box.go.exists() and not box.bridge.exists() and not box.data.exists()
    assert not (box.units / "littlebot-nanit.service").exists() and (box.units / "other.service").exists()
    assert keep.exists()
    assert "systemctl --user disable --now littlebot-nanit.service" in box.calls()


@needs_bash
def test_a_bad_go_download_installs_nothing(tmp_path: Path) -> None:
    box = Box(tmp_path)
    res = box.run(extra_env={"LB_NANIT_ASSUME_TERMINAL": "1"})
    assert res.returncode == 1 and "checksum is wrong" in res.stderr, res.stdout + res.stderr
    assert "https://go.dev/dl/go1." in box.calls()
    assert not box.go.exists() and not list((box.home / ".local").glob(".nanit-go-download.*"))


def _run_bridge(box: Box, route: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    log = _posix(box.log)
    _fake(box.bin, "ip", f'echo "{route}"')
    _fake(box.bin, "python3", f'echo "python3 $*" >> "{log}"; exit 0')
    nanit = box.home / "nanit"
    _fake(box.bin, "nanit", f'{{ echo "pwd=$(pwd)"; env | grep "^NANIT_" | sort; }} >> "{log}"')
    box.data.mkdir(parents=True)
    env = {"HOME": _posix(box.home), "PATH": f"{_posix(box.bin)}:/usr/bin:/bin", "LB_NANIT_BIN": _posix(nanit),
           "LB_NANIT_DATA": _posix(box.data), "NANIT_EMAIL": "should-be-removed"}
    shutil.copy(box.bin / "nanit", nanit)
    return subprocess.run([BASH, _posix(RUNNER)], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
                          timeout=60)


@needs_bash
def test_the_bridge_starts_with_the_lan_address(tmp_path: Path) -> None:
    box = Box(tmp_path)
    res = _run_bridge(box, "1.1.1.1 via 192.168.2.1 dev wlan0 src 192.168.2.112 uid 1000")
    assert res.returncode == 0, res.stderr
    calls = box.calls()
    assert "NANIT_RTMP_ADDR=192.168.2.112:1935" in calls and "NANIT_MQTT_ENABLED=false" in calls
    assert f"NANIT_SESSION_FILE={_posix(box.data)}/session.json" in calls and "NANIT_EMAIL" not in calls
    assert re.search(r"^pwd=.*/LittleBot/data/nanit$", calls, re.M)  # run from the data folder: no .env there
    assert "nanit_helper.py tidy" in calls and "nanit_helper.py cameras" in calls


@needs_bash
def test_the_bridge_refuses_a_public_address(tmp_path: Path) -> None:
    box = Box(tmp_path)
    res = _run_bridge(box, "1.1.1.1 via 203.0.113.1 dev eth0 src 203.0.113.7 uid 1000")
    assert res.returncode == 1 and "not a home-network address" in res.stderr
    assert "NANIT_RTMP_ADDR" not in box.calls()


@needs_bash
def test_not_on_the_robot(tmp_path: Path) -> None:
    box = Box(tmp_path, arch="x86_64")
    res = box.run(extra_env={"LB_NANIT_ASSUME_TERMINAL": "1"})
    assert res.returncode == 1 and "run it on LittleBot" in res.stderr and "curl" not in box.calls()
