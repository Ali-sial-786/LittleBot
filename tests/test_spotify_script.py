"""scripts/setup_spotify.sh against fake commands (no download, no systemd, no Spotify; nothing outside a temp home).

Fakes on PATH: ``curl`` (Spotify's download server: it serves ``served.tar.gz`` from the fake dir, with its MD5 as
the ETag), ``soloist`` (inside that archive: its --version names a build date; ``ctl now --json`` answers as
signed in / playing or not; run by the launcher it records its arguments), ``systemctl`` (the user units',
raspotify's and avahi-daemon's state in a JSON file), ``sudo`` (runs the rest), ``apt-get``, ``dpkg-query``
(raspotify's package), ``pactl``, ``journalctl``, ``pgrep`` and ``ss``. Every call that changes something is
logged. The API key typed in the tests must never show up in what the script prints.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "setup_spotify.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None or sys.platform == "win32", reason="bash not installed")

KEY = "sk-Test0123456789abcdefXYZ"  # a made-up key: it must never be printed

_SOLOIST = r'''
import datetime, json, os, pathlib, sys, time
AGE = __AGE__
MARK = "__MARK__"
d = pathlib.Path(os.environ["FAKE_DIR"])
args = sys.argv[1:]
if args == ["--version"]:
    built = datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=AGE)
    print("soloist 1.3.10.31 build %s (linux/aarch64) %s" % (built.strftime("%Y%m%d"), MARK))
elif args[:1] == ["ctl"]:
    with open(d / "ctl.log", "a") as f:
        f.write(" ".join(args) + "\n")
    if not os.environ.get("FAKE_SIGNED_IN"):
        sys.exit(3)
    print(json.dumps({"type": "playback_state", "status": "playing" if os.environ.get("FAKE_PLAYING") else "paused"}))
elif "--pair" in args:
    with open(d / "calls.log", "a") as f:
        f.write("soloist --pair\n")
    pf = d / "pair_fails"
    left = int(pf.read_text()) if pf.exists() else 0
    if left > 0:
        pf.write_text(str(left - 1))
        sys.exit(1)
else:
    (d / "soloist_args.json").write_text(json.dumps(args))
'''


def soloist_source(age: int = 5, mark: str = "first") -> str:
    return f"#!{sys.executable}\n" + _SOLOIST.replace("__AGE__", str(age)).replace("__MARK__", mark)


def make_archive(path: Path, *, age: int = 5, mark: str = "first", evil: bool = False) -> None:
    """Spotify's archive: the soloist program (and a licence file) in it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text, mode in (("soloist", soloist_source(age, mark), 0o755), ("LICENSE.txt", "licence\n", 0o644)):
            data = text.encode()
            info = tarfile.TarInfo(("../" if evil and name == "soloist" else "") + name)
            info.size, info.mode, info.mtime = len(data), mode, 1_700_000_000
            tar.addfile(info, io.BytesIO(data))
    path.write_bytes(buf.getvalue())


_CURL = r'''
import hashlib, os, pathlib, shutil, sys
d = pathlib.Path(os.environ["FAKE_DIR"])
args = sys.argv[1:]
head = "--head" in args
url = args[-1]
with open(d / "calls.log", "a") as f:
    f.write("curl %s %s\n" % ("HEAD" if head else "GET", url))
if os.environ.get("FAKE_OFFLINE"):
    sys.exit("curl: (6) Could not resolve host")
served = d / "served.tar.gz"
etag = hashlib.md5(served.read_bytes()).hexdigest()
if os.environ.get("FAKE_BAD_ETAG"):
    etag = "0" * 32
hdr = "HTTP/2 200\r\ncontent-type: application/x-tar\r\netag: \"%s\"\r\n\r\n" % etag
if "-D" in args:
    pathlib.Path(args[args.index("-D") + 1]).write_text(hdr)
if not head and "-o" in args:
    shutil.copy(served, args[args.index("-o") + 1])
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
cmd, units = rest[0], rest[1:]
unit = units[0] if units else ""
if not user and unit.startswith("avahi-daemon"):
    if cmd == "is-active":  # running when the test says so (FAKE_AVAHI), unless the script paused it
        sys.exit(0 if os.environ.get("FAKE_AVAHI") and not st.get("avahi_stopped") else 3)
    st["avahi_stopped"] = cmd == "stop"
elif not user and unit == "raspotify.service":
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
elif user:
    for name in units or [""]:
        u = st["units"].setdefault(name, {"enabled": False, "active": False}) if name else None
        if cmd == "is-enabled":
            sys.exit(0 if u["enabled"] else 1)
        if cmd == "is-active":
            sys.exit(0 if u["active"] else 3)
        if cmd == "enable":
            u["enabled"] = True
        elif cmd in ("restart", "start"):
            u["active"] = True
        elif cmd == "stop":
            u["active"] = False
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

_APT = r'''
import json, os, pathlib, sys
d = pathlib.Path(os.environ["FAKE_DIR"])
with open(d / "calls.log", "a") as f:
    f.write("apt-get " + " ".join(sys.argv[1:]) + "\n")
(d / "raspotify_pkg").unlink(missing_ok=True)
sf = d / "systemd.json"
if sf.exists():
    st = json.loads(sf.read_text())
    st["raspotify"] = None
    sf.write_text(json.dumps(st))
'''

_DPKG_QUERY = r'''
import os, pathlib, sys
if not (pathlib.Path(os.environ["FAKE_DIR"]) / "raspotify_pkg").exists():
    sys.exit(1)
print("install ok installed", end="")
'''

_PACTL = r'''
import os, sys
if not os.environ.get("FAKE_PULSE"):
    sys.exit("Connection failure: Connection refused")
if sys.argv[1:] == ["get-default-sink"]:
    print(os.environ.get("FAKE_SINK", "littlebot_ec_speaker"))
'''

_JOURNALCTL = 'print("-- No entries --")\n'
_PGREP = 'import os, sys\nsys.exit(0 if os.environ.get("FAKE_LIBRESPOT_RUNNING") else 1)\n'
_SS = r'''
import os
if os.environ.get("FAKE_MDNS"):
    print('UNCONN 0 0 0.0.0.0:5353 0.0.0.0:* users:(("soloist",pid=42,fd=9))')
'''


@pytest.fixture
def fake(tmp_path: Path):
    """run(*args, stdin=None, **env) -> (CompletedProcess, change calls, home, fake dir)."""
    fake_dir, bindir, home = tmp_path / "fake", tmp_path / "bin", tmp_path / "home"
    for p in (fake_dir, bindir, home):
        p.mkdir()
    make_archive(fake_dir / "served.tar.gz")
    for name, body in (("systemctl", _SYSTEMCTL), ("sudo", _SUDO), ("curl", _CURL), ("apt-get", _APT),
                       ("dpkg-query", _DPKG_QUERY), ("pactl", _PACTL), ("journalctl", _JOURNALCTL),
                       ("pgrep", _PGREP), ("ss", _SS)):
        (bindir / name).write_text(f"#!{sys.executable}\n{body}")
        (bindir / name).chmod(0o755)
    # the rest of PATH without any real soloist / curl / systemctl / sudo
    keep = tmp_path / "sysbin"
    keep.mkdir()
    for tool in ("sh", "cat", "sed", "grep", "head", "mkdir", "chmod", "rm", "ls", "id", "dirname", "cp", "tr",
                 "env", "printf", "cut", "tail", "date", "mktemp", "tar", "gzip", "md5sum", "sha256sum", "find",
                 "install", "mv", "cmp", "timeout", "sleep", "uname"):
        found = shutil.which(tool)
        if found:
            (keep / tool).symlink_to(found)

    def run(*args: str, stdin: str | None = None, **extra_env: str):
        env = {"PATH": f"{bindir}{os.pathsep}{keep}", "HOME": str(home), "FAKE_DIR": str(fake_dir),
               "XDG_RUNTIME_DIR": str(tmp_path), "LANG": "C", "LITTLEBOT_SPOTIFY_ARCH": "aarch64",
               "LITTLEBOT_SPOTIFY_SETTLE_S": "0", **extra_env}
        if stdin is not None:
            env["LITTLEBOT_SPOTIFY_INTERACTIVE"] = "1"
        res = subprocess.run([BASH, str(SCRIPT), *args], check=False, capture_output=True, text=True, env=env,
                             input=stdin if stdin is not None else "", timeout=60)
        assert KEY not in res.stdout + res.stderr  # the key is never printed
        log = fake_dir / "calls.log"
        calls = log.read_text().splitlines() if log.exists() else []
        log.unlink(missing_ok=True)
        return res, calls, home, fake_dir

    def install_soloist(age: int = 5, mark: str = "first") -> Path:
        """A soloist somewhere on PATH (installed before, not by this script)."""
        (bindir / "soloist").write_text(soloist_source(age, mark))
        (bindir / "soloist").chmod(0o755)
        return bindir / "soloist"

    def save_key() -> None:
        (home / ".config/littlebot-spotify").mkdir(parents=True, exist_ok=True)
        (home / KEY_FILE).write_text(f"SOLOIST_API_KEY={KEY}\n")

    run.install_soloist = install_soloist  # type: ignore[attr-defined]
    run.save_key = save_key  # type: ignore[attr-defined]
    run.bindir = bindir  # type: ignore[attr-defined]
    run.fake_dir = fake_dir  # type: ignore[attr-defined]
    run.home = home  # type: ignore[attr-defined]
    return run


LAUNCHER = Path(".local/bin/littlebot-spotify")
SOLOIST_BIN = Path(".local/bin/soloist")
UNIT = Path(".config/systemd/user/littlebot-spotify.service")
UPDATE_UNIT = Path(".config/systemd/user/littlebot-spotify-update.service")
TIMER = Path(".config/systemd/user/littlebot-spotify-update.timer")
KEY_FILE = Path(".config/littlebot-spotify/api-key.env")
URL = "https://soloist-builds.spotifycdn.com/soloist_release_arm64.tar.gz"


def written(out: str, path: Path) -> str:
    """The file content a dry run printed for ``path``."""
    block = out.split(f"[dry-run] write {path}:", 1)[1].split("[dry-run]", 1)[0]
    return "\n".join(line.split("| ", 1)[1] if "| " in line else "" for line in block.splitlines())


def _launch(home: Path, fake_dir: Path, bindir: Path, *extra: str) -> list[str]:
    env = {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}", "HOME": str(home), "FAKE_DIR": str(fake_dir)}
    res = subprocess.run(["sh", str(home / LAUNCHER), *extra], check=False, capture_output=True, text=True, env=env,
                         timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads((fake_dir / "soloist_args.json").read_text())


def test_help_and_bad_options() -> None:
    res = subprocess.run([BASH, str(SCRIPT), "--help"], check=False, capture_output=True, text=True)
    assert res.returncode == 0
    for want in ("--dry-run", "--check", "--update", "--pair", "--new-key", "--uninstall", "--name", "--volume",
                 "Spotify Premium", "Soloist", "Untested on a real Raspberry Pi", "littlebot-spotify"):
        assert want in res.stdout, want
    for bad in (["--bogus"], ["--check", "--uninstall"], ["--check", "--update"], ["--pair", "--update"],
                ["--bitrate", "160"], ["--volume", "300"], ["--name", "Little\"Bot"]):
        assert subprocess.run([BASH, str(SCRIPT), *bad], check=False, capture_output=True).returncode == 2, bad


def test_dry_run_prints_the_launcher_the_service_and_the_timer_and_changes_nothing(fake) -> None:
    soloist = fake.install_soloist()
    res, calls, home, _ = fake("--dry-run")
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    assert "Already installed:" in out and "soloist 1.3.10.31 build" in out and "85 day(s) left" in out
    assert "needs Spotify Premium" in out and "would ask for your Soloist API key" in out
    launcher = written(out, home / LAUNCHER)
    for want in (f'SOLOIST="{soloist}"', f'KEY_FILE="{home / KEY_FILE}"', '--device-name "LittleBot"',
                 '--api-key "$SOLOIST_API_KEY"', f'--data-dir "{home / ".local/share/soloist"}"',
                 "--initial-volume 60", "--ws 127.0.0.1:0", '"$@"', "exit 3"):
        assert want in launcher, want
    assert KEY not in launcher and "librespot" not in launcher.lower()
    unit = written(out, home / UNIT)
    for want in (f"ExecStart={home / LAUNCHER}", "Restart=always", "RestartPreventExitStatus=3 10",
                 "WantedBy=default.target", "After=pipewire.service pipewire-pulse.service wireplumber.service",
                 f"Documentation=file://{ROOT}/docs/SPOTIFY.md"):
        assert want in unit, want
    assert f"ExecStart=/bin/bash {ROOT}/scripts/setup_spotify.sh --update" in written(out, home / UPDATE_UNIT)
    timer = written(out, home / TIMER)
    assert "OnCalendar=Mon *-*-* 04:20" in timer and "Persistent=true" in timer
    assert ("[dry-run] systemctl --user enable littlebot-spotify.service littlebot-spotify-update.timer" in out)
    assert "[dry-run] systemctl --user restart littlebot-spotify.service" in out
    assert list(home.iterdir()) == [] and calls == []


def test_dry_run_before_soloist_is_installed(fake) -> None:
    res, calls, home, _ = fake("--dry-run", "--name", "Kitchen Bot", "--volume", "45")
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"[dry-run] curl --fail --location -o soloist.tar.gz {URL}" in res.stdout
    assert f"[dry-run] install -m 755 soloist {home / SOLOIST_BIN}" in res.stdout
    launcher = written(res.stdout, home / LAUNCHER)
    assert '--device-name "Kitchen Bot"' in launcher and "--initial-volume 45" in launcher
    assert f'SOLOIST="{home / SOLOIST_BIN}"' in launcher
    assert list(home.iterdir()) == [] and calls == []
    res, calls, _, _ = fake("--dry-run", "--update")
    assert res.returncode == 0 and f"[dry-run] curl --fail --location -o soloist.tar.gz {URL}" in res.stdout
    assert calls == [] and list(home.iterdir()) == []


def test_setup_asks_for_the_key_installs_checks_updates_and_uninstalls(fake) -> None:
    res, _, _, _ = fake("--check")  # nothing yet
    assert res.returncode == 1 and "Soloist is not installed" in res.stdout

    res, calls, home, fake_dir = fake(stdin=KEY + "\n", FAKE_PULSE="1")
    assert res.returncode == 0, res.stdout + res.stderr
    assert calls == [f"curl HEAD {URL}", f"curl GET {URL}", "systemctl --user daemon-reload",
                     "systemctl --user enable littlebot-spotify.service littlebot-spotify-update.timer",
                     "systemctl --user start littlebot-spotify-update.timer",
                     "systemctl --user restart littlebot-spotify.service"]
    assert "developer.spotify.com/dashboard/soloist" in res.stdout and "matches the server's checksum" in res.stdout
    assert os.access(home / SOLOIST_BIN, os.X_OK) and os.access(home / LAUNCHER, os.X_OK)
    for p in (UNIT, UPDATE_UNIT, TIMER):
        assert (home / p).is_file(), p
    assert (home / KEY_FILE).read_text() == f"SOLOIST_API_KEY={KEY}\n"
    assert stat.S_IMODE((home / KEY_FILE).stat().st_mode) == 0o600
    sha = hashlib.sha256((fake_dir / "served.tar.gz").read_bytes()).hexdigest()
    assert (home / ".config/littlebot-spotify/soloist-sha256").read_text().startswith(sha)
    for want in ("PASS  Soloist is installed", "PASS  the build works for 85 more day(s)",
                 "PASS  the Soloist API key is saved", "PASS  it is running", "PASS  the weekly update timer is on",
                 "PASS  the old receiver (librespot) isn't running", "WARN  the receiver isn't signed in yet"):
        assert want in res.stdout, want
    assert 'pick "LittleBot"' in res.stdout

    args = _launch(home, fake_dir, fake.bindir)
    assert args == ["--device-name", "LittleBot", "--api-key", KEY, "--data-dir", str(home / ".local/share/soloist"),
                    "--cache-dir", str(home / ".cache/soloist"), "--cache-size", "100", "--initial-volume", "60",
                    "--ws", "127.0.0.1:0"]

    res, calls, _, _ = fake(FAKE_PULSE="1")  # again: nothing to do, and the key isn't asked for again
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Already installed" in res.stdout and "Saved already" in res.stdout
    assert "unchanged:" in res.stdout and "no restart needed" in res.stdout
    assert calls == ["systemctl --user daemon-reload",
                     "systemctl --user enable littlebot-spotify.service littlebot-spotify-update.timer"]

    res, calls, _, _ = fake("--check", FAKE_PULSE="1", FAKE_SIGNED_IN="1")
    assert res.returncode == 0, res.stdout
    for want in ("PASS  the service and its launcher are installed", "PASS  it starts at login (enabled)",
                 "PASS  PipeWire is running", "PASS  the default speaker is littlebot_ec_speaker",
                 "PASS  the receiver is signed in"):
        assert want in res.stdout, want
    assert calls == []
    res, _, _, _ = fake("--check", FAKE_SINK="alsa_output.usb-speaker", FAKE_PULSE="1", FAKE_SIGNED_IN="1")
    assert "WARN  the default speaker is alsa_output.usb-speaker" in res.stdout and res.returncode == 0

    res, calls, _, _ = fake("--update")  # the same build on the server: only asks
    assert res.returncode == 0 and "Up to date" in res.stdout and calls == [f"curl HEAD {URL}"]

    make_archive(fake_dir / "served.tar.gz", mark="second")  # a new build, while music plays: no restart yet
    res, calls, _, _ = fake("--update", FAKE_SIGNED_IN="1", FAKE_PLAYING="1")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "second" in (home / SOLOIST_BIN).read_text() and "first" in (home / ".local/bin/soloist.old").read_text()
    assert "Music is playing on it" in res.stdout and calls == [f"curl HEAD {URL}", f"curl GET {URL}"]
    assert (home / ".config/littlebot-spotify/restart-pending").exists()
    res, calls, _, _ = fake("--update", FAKE_SIGNED_IN="1")  # nothing plays now: the waiting build starts
    assert calls == [f"curl HEAD {URL}", "systemctl --user restart littlebot-spotify.service"], res.stdout
    assert not (home / ".config/littlebot-spotify/restart-pending").exists()

    make_archive(fake_dir / "served.tar.gz", mark="third")
    res, calls, _, _ = fake("--update")
    assert calls[-1] == "systemctl --user restart littlebot-spotify.service" and "third" in (home / SOLOIST_BIN).read_text()

    res, calls, home, fake_dir = fake("--uninstall")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "systemctl --user disable --now littlebot-spotify.service" in calls
    assert "systemctl --user disable --now littlebot-spotify-update.timer" in calls
    assert not [c for c in calls if c.startswith("sudo")]
    for p in (LAUNCHER, SOLOIST_BIN, UNIT, UPDATE_UNIT, TIMER, KEY_FILE):
        assert not (home / p).exists(), p
    assert not (home / ".local/share/soloist").exists() and not (home / ".config/littlebot-spotify").exists()
    assert "Disconnect Spotify on the dashboard" in res.stdout

    res, calls, _, _ = fake("--uninstall")  # nothing left
    assert res.returncode == 0 and "The service isn't installed" in res.stdout


def test_moving_from_librespot_asks_before_any_sudo(fake) -> None:
    home, fake_dir = fake.home, fake.fake_dir
    (home / LAUNCHER).parent.mkdir(parents=True)
    (home / LAUNCHER).write_text('#!/bin/sh\nexec /usr/bin/librespot --name "LittleBot"\n')
    (home / ".cache/littlebot-spotify").mkdir(parents=True)
    (home / ".cache/littlebot-spotify/credentials.json").write_text("{}")
    (fake_dir / "raspotify_pkg").write_text("")
    (fake_dir / "systemd.json").write_text(json.dumps({"raspotify": {"state": "enabled", "running": True},
                                                       "avahi": False, "units": {}}))
    fake.save_key()

    res, calls, _, _ = fake()  # no terminal: never sudo, the commands are only shown
    assert res.returncode == 0, res.stdout + res.stderr
    assert not [c for c in calls if c.startswith(("sudo", "apt-get"))]
    assert "sudo apt-get remove raspotify" in res.stdout and "not asked: no terminal" in res.stdout
    assert "The service ran librespot" in res.stdout and not (home / ".cache/littlebot-spotify").exists()
    assert "/usr/bin/librespot" not in (home / LAUNCHER).read_text()
    assert "PASS  the service and its launcher are installed" in res.stdout
    assert "FAIL  raspotify's own system service is running" in res.stdout

    res, calls, _, _ = fake(stdin="n\nn\n")  # asked, and no: still no sudo
    assert not [c for c in calls if c.startswith(("sudo", "apt-get"))]
    assert "Remove the raspotify package now" in res.stdout and "Switch it off" in res.stdout

    res, calls, _, _ = fake(stdin="y\n")  # yes: the package goes (and its service with it)
    assert "sudo apt-get remove -y raspotify" in calls
    assert not [c for c in calls if c.startswith("sudo systemctl")]
    res, _, _, _ = fake("--check")
    assert "raspotify" not in res.stdout


def test_no_key_and_no_terminal_says_what_to_do(fake) -> None:
    fake.install_soloist()
    res, calls, home, _ = fake()
    assert res.returncode == 1 and "run this in a terminal" in res.stderr
    assert not (home / KEY_FILE).exists() and not (home / LAUNCHER).exists()

    res, _, home, _ = fake(stdin="not a key\n")  # spaces: refused, nothing saved
    assert res.returncode == 1 and "doesn't look like a Soloist API key" in res.stderr
    assert not (home / KEY_FILE).exists()

    res, _, home, _ = fake(SOLOIST_API_KEY=KEY)  # from the environment
    assert res.returncode == 0, res.stdout + res.stderr
    assert (home / KEY_FILE).read_text() == f"SOLOIST_API_KEY={KEY}\n"

    res, _, home, _ = fake("--new-key", stdin="sk-Another0123456789abcdef\n")
    assert res.returncode == 0 and "sk-Another" not in res.stdout + res.stderr
    assert (home / KEY_FILE).read_text() == "SOLOIST_API_KEY=sk-Another0123456789abcdef\n"


def test_a_damaged_or_unexpected_download_is_not_installed(fake) -> None:
    fake.save_key()
    res, _, home, _ = fake(FAKE_BAD_ETAG="1")
    assert res.returncode == 1 and "the download is damaged" in res.stderr and not (home / SOLOIST_BIN).exists()

    res, _, home, _ = fake(LITTLEBOT_SOLOIST_SHA256="ab" * 32)
    assert res.returncode == 1 and "not installed" in res.stderr and not (home / SOLOIST_BIN).exists()

    make_archive(fake.fake_dir / "served.tar.gz", evil=True)
    res, _, home, _ = fake()
    assert res.returncode == 1 and "outside its own folder" in res.stderr and not (home / SOLOIST_BIN).exists()

    make_archive(fake.fake_dir / "served.tar.gz", age=95)  # Spotify's newest build already expired
    res, _, home, _ = fake()
    assert res.returncode == 1 and "has already expired" in res.stderr and not (home / SOLOIST_BIN).exists()

    res, _, home, _ = fake("--update", FAKE_OFFLINE="1")
    assert res.returncode == 1 and "the download failed" in res.stderr


def test_an_old_build_is_renewed_and_an_expired_one_fails_the_check(fake) -> None:
    fake.save_key()
    fake.install_soloist(age=95)
    res, _, _, _ = fake("--check")
    assert res.returncode == 1 and "FAIL  this Soloist build expired 5 day(s) ago" in res.stdout

    fake.install_soloist(age=80)  # 10 days left: setup fetches the newest one
    res, calls, home, _ = fake()
    assert res.returncode == 0, res.stdout + res.stderr
    assert "It stops working soon" in res.stdout and f"curl GET {URL}" in calls
    assert f'SOLOIST="{home / SOLOIST_BIN}"' in (home / LAUNCHER).read_text()
    res, _, _, _ = fake("--check")
    assert "PASS  the build works for 85 more day(s)" in res.stdout


def test_pair_signs_in_again_and_offers_to_pause_avahi(fake) -> None:
    fake.save_key()
    res, _, _, _ = fake(FAKE_PULSE="1")
    assert res.returncode == 0, res.stdout + res.stderr

    res, _, _, _ = fake("--pair")  # needs a person
    assert res.returncode == 1 and "needs you at the terminal" in res.stderr

    res, calls, _, _ = fake("--pair", stdin="")
    assert res.returncode == 0, res.stdout + res.stderr
    assert calls[:3] == ["systemctl --user stop littlebot-spotify.service", "soloist --pair",
                         "systemctl --user start littlebot-spotify.service"]
    assert 'pick "LittleBot"' in res.stdout and "Signed in" in res.stdout

    (fake.fake_dir / "pair_fails").write_text("1")  # not found the first time; avahi-daemon is running
    res, calls, _, _ = fake("--pair", stdin="y\n", FAKE_AVAHI="1")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Pause avahi-daemon during the pairing" in res.stdout
    assert "sudo systemctl stop avahi-daemon.socket avahi-daemon.service" in calls
    assert "sudo systemctl start avahi-daemon.socket avahi-daemon.service" in calls
    assert calls.count("soloist --pair") == 2

    (fake.fake_dir / "pair_fails").write_text("1")
    res, calls, _, _ = fake("--pair", stdin="n\n", FAKE_AVAHI="1")  # no: no sudo
    assert res.returncode == 1 and not [c for c in calls if c.startswith("sudo")]
    assert calls[-1] == "systemctl --user start littlebot-spotify.service"


def test_check_finds_a_second_receiver_and_a_blocked_mdns_port(fake) -> None:
    fake.save_key()
    res, _, _, _ = fake(FAKE_PULSE="1")
    assert res.returncode == 0, res.stdout + res.stderr
    res, _, _, _ = fake("--check", FAKE_PULSE="1", FAKE_LIBRESPOT_RUNNING="1", FAKE_AVAHI="1")
    assert res.returncode == 1 and "FAIL  librespot (the old receiver) is still running" in res.stdout
    assert "WARN  Soloist doesn't have the mDNS port" in res.stdout
    res, _, _, _ = fake("--check", FAKE_PULSE="1", FAKE_AVAHI="1", FAKE_MDNS="1")
    assert res.returncode == 0 and "mDNS" not in res.stdout


def test_no_user_manager_says_what_to_do(fake) -> None:
    fake.install_soloist()
    fake.save_key()
    res, _, _, _ = fake(FAKE_NO_USER_MANAGER="1")
    assert res.returncode == 1 and "systemctl --user daemon-reload && systemctl --user enable --now" in res.stderr


def test_the_launcher_needs_the_key_and_passes_extra_options(fake) -> None:
    fake.install_soloist()
    fake.save_key()
    res, _, home, fake_dir = fake()
    assert res.returncode == 0, res.stdout + res.stderr
    args = _launch(home, fake_dir, fake.bindir, "--verbose")
    assert args[args.index("--api-key") + 1] == KEY and args[-1] == "--verbose"
    (home / KEY_FILE).unlink()
    env = {"PATH": f"{fake.bindir}{os.pathsep}{os.environ.get('PATH', '')}", "HOME": str(home),
           "FAKE_DIR": str(fake_dir)}
    out = subprocess.run(["sh", str(home / LAUNCHER)], check=False, capture_output=True, text=True, env=env,
                         timeout=30)
    assert out.returncode == 3 and "no Soloist API key" in out.stderr
