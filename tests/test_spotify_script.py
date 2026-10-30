"""scripts/setup_spotify.sh against fake commands (no apt, no systemd, no Spotify; nothing outside a temp home).

Fakes on PATH: ``librespot`` (its --version / --help / --backend ? answers; run by the launcher it records its
arguments), ``systemctl`` (the user units' and raspotify's state in a JSON file), ``sudo`` (runs the rest),
``curl`` (raspotify's install script: it "installs" the fake librespot and an enabled raspotify.service),
``apt-get``, ``pactl``, ``aplay`` and ``journalctl``. Every call that changes something is logged.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "setup_spotify.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None or sys.platform == "win32", reason="bash not installed")

_LIBRESPOT = r'''
import json, os, pathlib, sys
d = pathlib.Path(os.environ["FAKE_DIR"])
args = sys.argv[1:]
if args == ["--version"]:
    print("librespot 0.6.0 2a1b3c4 (Built on 2024-11-02, Build ID: abc, Profile: release)")
elif args == ["--help"]:
    if os.environ.get("FAKE_OLD_LIBRESPOT"):
        print("    --autoplay          Automatically play similar songs when your music ends.")
    else:
        print("    --autoplay OVERRIDE Explicitly set autoplay {on|off}. Defaults to following the client setting.")
elif args[:2] == ["--backend", "?"]:
    print("Available backends:")
    print("- alsa (default)")
    if not os.environ.get("FAKE_NO_PULSE_BACKEND"):
        print("- pulseaudio")
else:
    (d / "librespot_args.json").write_text(json.dumps(args))
'''

_SYSTEMCTL = r'''
import json, os, pathlib, sys
d = pathlib.Path(os.environ["FAKE_DIR"])
args = sys.argv[1:]
sf = d / "systemd.json"
st = json.loads(sf.read_text()) if sf.exists() else {"raspotify": None, "units": {}}
read_only = args[0] in ("list-unit-files", "is-enabled", "is-active") or args[:2] in (
    ["--user", "is-enabled"], ["--user", "is-active"])
if not read_only:
    with open(d / "calls.log", "a") as f:
        f.write("systemctl " + " ".join(args) + "\n")
if os.environ.get("FAKE_NO_USER_MANAGER") and args[0] == "--user" and not read_only:
    sys.exit("Failed to connect to bus: No medium found")
user = args[0] == "--user"
rest = [a for a in args if a not in ("--user", "--quiet", "--now", "--no-legend")]
cmd, unit = rest[0], (rest[1] if len(rest) > 1 else "")
if not user and unit == "raspotify.service":
    r = st["raspotify"]
    if cmd == "list-unit-files":
        if r:
            print("raspotify.service " + r["state"] + " enabled")
    elif cmd == "is-enabled":
        if not r:
            sys.exit(1)
        print(r["state"])
        sys.exit(0 if r["state"] == "enabled" else 1)
    elif cmd == "is-active":
        sys.exit(0 if r and r.get("running") else 3)
    elif cmd == "disable":
        r["state"], r["running"] = "disabled", False
    elif cmd == "mask":
        r["state"] = "masked"
    elif cmd == "unmask":
        r["state"] = "disabled"
    elif cmd == "enable":
        r["state"], r["running"] = "enabled", True
elif user:
    u = st["units"].setdefault(unit, {"enabled": False, "active": False}) if unit else None
    if cmd == "is-enabled":
        sys.exit(0 if u["enabled"] else 1)
    if cmd == "is-active":
        sys.exit(0 if u["active"] else 3)
    if cmd == "enable":
        u["enabled"] = True
    elif cmd == "restart":
        u["active"] = True
    elif cmd == "disable":
        u["enabled"] = u["active"] = False
sf.write_text(json.dumps(st))
'''

_SUDO = r'''
import os, pathlib, subprocess, sys
d = pathlib.Path(os.environ["FAKE_DIR"])
with open(d / "calls.log", "a") as f:
    f.write("sudo " + " ".join(sys.argv[1:]) + "\n")
sys.exit(subprocess.call(sys.argv[1:]))
'''

# raspotify's install script, as the fake curl serves it: puts librespot on PATH and an enabled raspotify.service
_CURL = r'''
import os, pathlib, sys
d = pathlib.Path(os.environ["FAKE_DIR"])
with open(d / "calls.log", "a") as f:
    f.write("curl " + " ".join(sys.argv[1:]) + "\n")
print("cp \"$FAKE_DIR/librespot.src\" \"$FAKE_BIN/librespot\" && chmod +x \"$FAKE_BIN/librespot\"")
print("echo '{\"raspotify\": {\"state\": \"enabled\", \"running\": true}, \"units\": {}}' > \"$FAKE_DIR/systemd.json\"")
'''

_APT = r'''
import os, pathlib, sys
d = pathlib.Path(os.environ["FAKE_DIR"])
with open(d / "calls.log", "a") as f:
    f.write("apt-get " + " ".join(sys.argv[1:]) + "\n")
'''

_PACTL = r'''
import os, sys
if not os.environ.get("FAKE_PULSE"):
    sys.exit("Connection failure: Connection refused")
if sys.argv[1:] == ["get-default-sink"]:
    print(os.environ.get("FAKE_SINK", "littlebot_ec_speaker"))
'''

_APLAY = 'print("null\\n    Discard all samples\\nlittlebot_ec_speaker\\n    LittleBot speaker (echo cancelled)")\n'
_JOURNALCTL = 'print("-- No entries --")\n'


@pytest.fixture
def fake(tmp_path: Path):
    """run(*args, librespot=True, **env) -> (CompletedProcess, change calls, home, fake dir)."""
    fake_dir, bindir, home = tmp_path / "fake", tmp_path / "bin", tmp_path / "home"
    for p in (fake_dir, bindir, home):
        p.mkdir()
    (fake_dir / "librespot.src").write_text(f"#!{sys.executable}\n{_LIBRESPOT}")
    for name, body in (("systemctl", _SYSTEMCTL), ("sudo", _SUDO), ("curl", _CURL), ("apt-get", _APT),
                       ("pactl", _PACTL), ("aplay", _APLAY), ("journalctl", _JOURNALCTL)):
        (bindir / name).write_text(f"#!{sys.executable}\n{body}")
        (bindir / name).chmod(0o755)
    # the rest of PATH without any real librespot / systemctl / sudo
    keep = tmp_path / "sysbin"
    keep.mkdir()
    for tool in ("sh", "cat", "sed", "grep", "head", "mkdir", "chmod", "rm", "ls", "id", "dirname", "cp", "tr",
                 "env", "printf", "cut", "tail"):
        found = shutil.which(tool)
        if found:
            (keep / tool).symlink_to(found)

    def install_librespot() -> None:
        shutil.copy(fake_dir / "librespot.src", bindir / "librespot")
        (bindir / "librespot").chmod(0o755)

    def run(*args: str, **extra_env: str):
        env = {"PATH": f"{bindir}{os.pathsep}{keep}", "HOME": str(home), "FAKE_DIR": str(fake_dir),
               "FAKE_BIN": str(bindir), "XDG_RUNTIME_DIR": str(tmp_path), "LANG": "C", **extra_env}
        res = subprocess.run([BASH, str(SCRIPT), *args], check=False, capture_output=True, text=True, env=env,
                             timeout=60)
        log = fake_dir / "calls.log"
        calls = log.read_text().splitlines() if log.exists() else []
        log.unlink(missing_ok=True)
        return res, calls, home, fake_dir

    run.install_librespot = install_librespot  # type: ignore[attr-defined]
    run.bindir = bindir  # type: ignore[attr-defined]
    return run


LAUNCHER = Path(".local/bin/littlebot-spotify")
UNIT = Path(".config/systemd/user/littlebot-spotify.service")


def written(out: str, path: Path) -> str:
    """The file content a dry run printed for ``path``."""
    block = out.split(f"[dry-run] write {path}:", 1)[1].split("[dry-run]", 1)[0]
    return "\n".join(line.split("| ", 1)[1] if "| " in line else "" for line in block.splitlines())


def test_help_and_bad_options() -> None:
    res = subprocess.run([BASH, str(SCRIPT), "--help"], check=False, capture_output=True, text=True)
    assert res.returncode == 0
    for want in ("--dry-run", "--check", "--uninstall", "--name", "--bitrate", "Spotify Premium",
                 "Untested on a real Raspberry Pi", "littlebot-spotify"):
        assert want in res.stdout, want
    for bad in (["--bogus"], ["--check", "--uninstall"], ["--bitrate", "128"], ["--volume", "300"],
                ["--name", "Little\"Bot"]):
        assert subprocess.run([BASH, str(SCRIPT), *bad], check=False, capture_output=True).returncode == 2, bad


def test_dry_run_prints_the_launcher_and_the_service_and_changes_nothing(fake) -> None:
    fake.install_librespot()
    res, calls, home, _ = fake("--dry-run")
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    assert "Already installed:" in out and "librespot 0.6.0" in out and "needs Spotify Premium" in out
    launcher = written(out, home / LAUNCHER)
    for want in ('--name "LittleBot" --device-type speaker --bitrate 160 --initial-volume 60',
                 "--disable-audio-cache", "--autoplay off", "set -- --backend pulseaudio",
                 'set -- --backend alsa --device "littlebot_ec_speaker"', 'pulse/native',
                 f'LIBRESPOT="{fake.bindir / "librespot"}"', '"$@"'):
        assert want in launcher, want
    unit = written(out, home / UNIT)
    for want in (f"ExecStart={home / LAUNCHER}", "Restart=always", "WantedBy=default.target",
                 "After=pipewire.service pipewire-pulse.service wireplumber.service",
                 f"Documentation=file://{ROOT}/docs/SPOTIFY.md"):
        assert want in unit, want
    assert "[dry-run] systemctl --user enable littlebot-spotify.service" in out
    assert "[dry-run] systemctl --user restart littlebot-spotify.service" in out
    assert list(home.iterdir()) == [] and calls == []


def test_dry_run_before_raspotify_is_installed(fake) -> None:
    res, calls, home, _ = fake("--dry-run", "--name", "Kitchen Bot", "--bitrate", "320", "--volume", "45")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[dry-run] curl -sSfL https://dtcooper.github.io/raspotify/install.sh | sh" in res.stdout
    assert "[dry-run] sudo systemctl disable --now raspotify.service" in res.stdout
    assert "[dry-run] sudo systemctl mask raspotify.service" in res.stdout
    launcher = written(res.stdout, home / LAUNCHER)
    assert '--name "Kitchen Bot"' in launcher and "--bitrate 320 --initial-volume 45" in launcher
    assert 'LIBRESPOT="/usr/bin/librespot"' in launcher
    assert list(home.iterdir()) == [] and calls == []


def test_setup_installs_is_idempotent_checks_and_uninstalls(fake) -> None:
    res, _, _, _ = fake("--check")  # nothing yet
    assert res.returncode == 1 and "librespot is not installed" in res.stdout

    res, calls, home, fake_dir = fake(FAKE_PULSE="1")
    assert res.returncode == 0, res.stdout + res.stderr
    assert calls == ["curl -sSfL https://dtcooper.github.io/raspotify/install.sh",
                     "sudo systemctl disable --now raspotify.service", "systemctl disable --now raspotify.service",
                     "sudo systemctl mask raspotify.service", "systemctl mask raspotify.service",
                     "systemctl --user daemon-reload", "systemctl --user enable littlebot-spotify.service",
                     "systemctl --user restart littlebot-spotify.service"]
    assert os.access(home / LAUNCHER, os.X_OK) and (home / UNIT).is_file()
    assert (home / ".config/littlebot-spotify/installed-raspotify").exists()
    assert (home / ".config/littlebot-spotify/raspotify-was-enabled").exists()
    st = json.loads((fake_dir / "systemd.json").read_text())
    assert st["raspotify"] == {"state": "masked", "running": False}
    assert "PASS  librespot is installed" in res.stdout and "PASS  it is running" in res.stdout
    assert "WARN  the receiver isn't signed in yet" in res.stdout  # pick LittleBot once in the Spotify app
    assert 'pick "LittleBot"' in res.stdout

    res, calls, home, _ = fake(FAKE_PULSE="1")  # again: nothing to do
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Already installed" in res.stdout and "Already off (masked)" in res.stdout
    assert "unchanged:" in res.stdout and "no restart needed" in res.stdout
    assert calls == ["systemctl --user daemon-reload", "systemctl --user enable littlebot-spotify.service"]

    (home / ".cache/littlebot-spotify").mkdir(parents=True)
    (home / ".cache/littlebot-spotify/credentials.json").write_text("{}")
    res, calls, _, _ = fake("--check", FAKE_PULSE="1")
    assert res.returncode == 0, res.stdout
    for want in ("PASS  raspotify's own system service is off", "PASS  the service and its launcher are installed",
                 "PASS  it starts at login (enabled)", "PASS  PipeWire's PulseAudio server is running",
                 "PASS  the default speaker is littlebot_ec_speaker", "PASS  the receiver is signed in"):
        assert want in res.stdout, want
    assert calls == []
    res, _, _, _ = fake("--check", FAKE_SINK="alsa_output.usb-speaker", FAKE_PULSE="1")
    assert "WARN  the default speaker is alsa_output.usb-speaker" in res.stdout and res.returncode == 0

    res, calls, home, fake_dir = fake("--uninstall")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "systemctl --user disable --now littlebot-spotify.service" in calls
    assert "sudo systemctl unmask raspotify.service" in calls and "sudo systemctl enable --now raspotify.service" in calls
    assert "sudo apt-get remove -y raspotify" in calls
    assert not (home / LAUNCHER).exists() and not (home / UNIT).exists()
    assert not (home / ".cache/littlebot-spotify").exists() and not (home / ".config/littlebot-spotify").exists()
    assert "Disconnect Spotify on the dashboard" in res.stdout

    res, calls, _, _ = fake("--uninstall")  # nothing left
    assert res.returncode == 0 and "The service isn't installed" in res.stdout
    assert "sudo apt-get remove -y raspotify" not in calls


def test_a_raspotify_installed_before_is_left_installed(fake) -> None:
    fake.install_librespot()
    res, calls, home, fake_dir = fake(FAKE_PULSE="1")
    assert res.returncode == 0 and "curl" not in " ".join(calls)
    assert "raspotify's service isn't installed: nothing to switch off" in res.stdout
    res, calls, _, _ = fake("--uninstall")
    assert "sudo apt-get remove -y raspotify" not in calls and "package stays" in res.stdout


def test_no_user_manager_says_what_to_do(fake) -> None:
    fake.install_librespot()
    res, _, _, _ = fake(FAKE_NO_USER_MANAGER="1")
    assert res.returncode == 1 and "systemctl --user daemon-reload && systemctl --user enable --now" in res.stderr


def _launch(home: Path, fake_dir: Path, runtime: Path, bindir: Path) -> list[str]:
    env = {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}", "HOME": str(home),
           "FAKE_DIR": str(fake_dir), "XDG_RUNTIME_DIR": str(runtime)}
    res = subprocess.run(["sh", str(home / LAUNCHER)], check=False, capture_output=True, text=True, env=env,
                         timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads((fake_dir / "librespot_args.json").read_text())


def test_the_launcher_plays_through_pipewire_else_alsa(fake, tmp_path: Path) -> None:
    fake.install_librespot()
    res, _, home, fake_dir = fake(FAKE_PULSE="1")
    assert res.returncode == 0, res.stdout + res.stderr
    runtime = tmp_path / "rt"
    (runtime / "pulse").mkdir(parents=True)
    args = _launch(home, fake_dir, runtime, fake.bindir)  # no PulseAudio socket: ALSA
    assert args[-4:] == ["--backend", "alsa", "--device", "littlebot_ec_speaker"]
    assert args[:10] == ["--name", "LittleBot", "--device-type", "speaker", "--bitrate", "160", "--initial-volume",
                         "60", "--cache", str(home / ".cache/littlebot-spotify")]
    assert "--disable-audio-cache" in args and args[args.index("--autoplay") + 1] == "off"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(runtime / "pulse" / "native"))
        args = _launch(home, fake_dir, runtime, fake.bindir)
    finally:
        sock.close()
    assert args[-2:] == ["--backend", "pulseaudio"]
    assert (home / ".cache/littlebot-spotify").is_dir()


def test_an_older_librespot_gets_no_autoplay_option(fake) -> None:
    fake.install_librespot()
    res, _, home, _ = fake("--dry-run", FAKE_OLD_LIBRESPOT="1")
    assert res.returncode == 0
    assert "--autoplay" not in written(res.stdout, home / LAUNCHER)
