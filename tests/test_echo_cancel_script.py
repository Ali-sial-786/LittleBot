"""scripts/setup_echo_cancel.sh against a fake PipeWire (no real sound server, nothing outside a temp home).

The fake `pw-dump` prints a Raspberry Pi 5 graph (HDMI, the "UACDemoV1.0" USB speaker - which also has a
microphone - and the "USB PnP Sound Device" microphone); once `systemctl --user restart pipewire ...` ran
with the script's PipeWire drop-in in place, it also lists the echo canceller's nodes. `wpctl` keeps the
defaults in a JSON file; `arecord -L` / `aplay -L` list the pcm names in the temp home's ~/.asoundrc.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "setup_echo_cancel.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not installed")

HDMI0 = "alsa_output.platform-107c701400.hdmi.hdmi-stereo"
USB_SPK = "alsa_output.usb-Jieli_Technology_UACDemoV1.0_4150344B32373916-00.analog-stereo"
USB_SPK_MIC = "alsa_input.usb-Jieli_Technology_UACDemoV1.0_4150344B32373916-00.mono-fallback"
USB_MIC = "alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.mono-fallback"
NODES = [
    (40, HDMI0, "Audio/Sink", "Built-in Audio Digital Stereo (HDMI)", 30),
    (52, USB_SPK, "Audio/Sink", "UACDemoV1.0 Analog Stereo", 50),
    (53, USB_SPK_MIC, "Audio/Source", "UACDemoV1.0 Mono", 50),
    (61, USB_MIC, "Audio/Source", "USB PnP Sound Device Mono", 60),
]
EC_NODES = [
    (80, "littlebot_ec_speaker", "Audio/Sink", "LittleBot speaker (echo cancelled)", 0),
    (81, "littlebot_ec_mic", "Audio/Source", "LittleBot microphone (echo cancelled)", 0),
    (82, "littlebot_ec_capture", "Stream/Input/Audio", "LittleBot echo canceller", 0),
    (83, "littlebot_ec_playback", "Stream/Output/Audio", "LittleBot echo canceller", 0),
]
CONF = Path(".config/pipewire/pipewire.conf.d/60-littlebot-echo-cancel.conf")
ALSA_RC = Path(".config/alsa/asoundrc")

_PW_DUMP = r'''
import json, os, pathlib
d = pathlib.Path(os.environ["FAKE_PW_DIR"])
g = json.loads((d / "graph.json").read_text())
nodes = g["nodes"] + (g["ec_nodes"] if (d / "ec_loaded").exists() else [])
out = [{"id": i, "type": "PipeWire:Interface:Node", "info": {"props": {
    "node.name": name, "media.class": cls, "node.description": desc, "device.id": dev,
    "priority.session": 1000}}} for i, name, cls, desc, dev in nodes]
print(json.dumps(out))
'''

_WPCTL = r'''
import json, os, pathlib, sys
d = pathlib.Path(os.environ["FAKE_PW_DIR"])
args = sys.argv[1:]
if args[0] != "status" and args[0] != "inspect":
    with open(d / "calls.log", "a") as f:
        f.write("wpctl " + " ".join(args) + "\n")
if os.environ.get("FAKE_PW_DOWN"):
    sys.exit("Could not connect to PipeWire")
g = json.loads((d / "graph.json").read_text())
nodes = g["nodes"] + (g["ec_nodes"] if (d / "ec_loaded").exists() else [])
sf = d / "state.json"
st = json.loads(sf.read_text()) if sf.exists() else {"sink": g["default_sink"], "source": g["default_source"]}
by_id = {str(n[0]): n for n in nodes}
if args[0] == "set-default":
    n = by_id[args[1]]
    st["sink" if n[2] == "Audio/Sink" else "source"] = n[1]
elif args[0] == "inspect":
    name = st["sink" if args[1] == "@DEFAULT_AUDIO_SINK@" else "source"]
    n = next((n for n in nodes if n[1] == name), None)
    if n is None:
        sys.exit("Object not found")
    print("id %s, type PipeWire:Interface:Node" % n[0])
    print('  * node.description = "%s"' % n[3])
    print('  * node.name = "%s"' % n[1])
sf.write_text(json.dumps(st))
'''

_SYSTEMCTL = r'''
import os, pathlib, sys
d = pathlib.Path(os.environ["FAKE_PW_DIR"])
args = sys.argv[1:]
with open(d / "calls.log", "a") as f:
    f.write("systemctl " + " ".join(args) + "\n")
if args[:2] == ["--user", "is-active"]:
    sys.exit(0 if os.environ.get("FAKE_LITTLEBOT_RUNNING") else 3)
if args[:2] == ["--user", "restart"] and "pipewire" in args:
    conf = pathlib.Path(os.environ["HOME"]) / ".config/pipewire/pipewire.conf.d/60-littlebot-echo-cancel.conf"
    marker = d / "ec_loaded"
    if conf.exists():
        marker.touch()
    elif marker.exists():
        marker.unlink()
'''

# arecord / aplay: -L lists the pcm.<name> entries of ~/.asoundrc and ~/.config/alsa/asoundrc (as ALSA
# would); recording "works".
_ALSA = r'''
import os, pathlib, re, sys
home = pathlib.Path(os.environ["HOME"])
names = [n for rc in (home / ".asoundrc", home / ".config" / "alsa" / "asoundrc") if rc.exists()
         for n in re.findall(r"^pcm\.(\w+)", rc.read_text(), re.M)]
if "-L" in sys.argv[1:]:
    print("null\n    Discard all samples")
    for n in names:
        print(n + "\n    LittleBot")
    sys.exit(0)
dev = sys.argv[sys.argv.index("-D") + 1] if "-D" in sys.argv else "default"
sys.exit(0 if dev in names or dev == "default" else 1)
'''

_WIREPLUMBER = 'print("wireplumber\\nCompiled with libwireplumber 0.4.13\\nLinked with libwireplumber 0.4.13")\n'
_PIPEWIRE = 'print("pipewire\\nCompiled with libpipewire 0.3.65\\nLinked with libpipewire 0.3.65")\n'


@pytest.fixture
def fake_pw(tmp_path: Path):
    """run(*args, asoundrc=None, **env) -> (CompletedProcess, calls, home)."""
    state, bindir, home, libs = tmp_path / "pw", tmp_path / "bin", tmp_path / "home", tmp_path / "lib"
    for p in (state, bindir, home):
        p.mkdir()
    for rel in ("aarch64-linux-gnu/pipewire-0.3/libpipewire-module-echo-cancel.so",
                "aarch64-linux-gnu/spa-0.2/aec/libspa-aec-webrtc.so",
                "aarch64-linux-gnu/alsa-lib/libasound_module_pcm_pipewire.so"):
        (libs / rel).parent.mkdir(parents=True, exist_ok=True)
        (libs / rel).write_text("")
    for name, body in (("pw-dump", _PW_DUMP), ("wpctl", _WPCTL), ("systemctl", _SYSTEMCTL), ("arecord", _ALSA),
                       ("aplay", _ALSA), ("wireplumber", _WIREPLUMBER), ("pipewire", _PIPEWIRE)):
        (bindir / name).write_text(f"#!{sys.executable}\n{body}")
        (bindir / name).chmod(0o755)
    fake_python = bindir / "lb-python"  # "PortAudio lists both" (the real check imports littlebot.audio.devices)
    fake_python.write_text("#!/bin/sh\nexit 0\n")
    fake_python.chmod(0o755)
    (state / "graph.json").write_text(json.dumps({
        "nodes": [[i, n, c, d, dev] for i, n, c, d, dev in NODES],
        "ec_nodes": [[i, n, c, d, dev] for i, n, c, d, dev in EC_NODES],
        "default_sink": USB_SPK, "default_source": USB_MIC}))

    def run(*args: str, **extra_env: str):
        env = {k: v for k, v in os.environ.items() if k not in ("SUDO_USER", "XDG_CONFIG_HOME")}
        env.update(PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}", HOME=str(home), FAKE_PW_DIR=str(state),
                   XDG_RUNTIME_DIR=str(tmp_path), LITTLEBOT_EC_LIB_DIRS=str(libs),
                   LITTLEBOT_EC_PYTHON=str(fake_python), **extra_env)
        res = subprocess.run([BASH, str(SCRIPT), *args], check=False, capture_output=True, text=True, env=env, timeout=90)
        log = state / "calls.log"
        calls = log.read_text().splitlines() if log.exists() else []
        log.unlink(missing_ok=True)
        return res, calls, home

    return run


def _changes(calls: list[str]) -> list[str]:
    """The calls that change something (not the read-only systemctl is-active)."""
    return [c for c in calls if "is-active" not in c]


def test_help_and_bad_options() -> None:
    res = subprocess.run([BASH, str(SCRIPT), "--help"], check=False, capture_output=True, text=True)
    assert res.returncode == 0
    for opt in ("--dry-run", "--check", "--remove", "--speaker", "--mic", "--no-default", "littlebot_ec_mic",
                "littlebot_ec_speaker", "Untested on a real Raspberry Pi"):
        assert opt in res.stdout
    assert subprocess.run([BASH, str(SCRIPT), "--bogus"], check=False, capture_output=True).returncode == 2
    assert subprocess.run([BASH, str(SCRIPT), "--remove", "--check"], check=False, capture_output=True).returncode == 2


def test_dry_run_prints_the_config_it_would_write_and_changes_nothing(fake_pw) -> None:
    res, calls, home = fake_pw("--dry-run")
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    assert f"[dry-run] write {home / CONF}:" in out
    conf = "\n".join(line.split("| ", 1)[1] if "| " in line else "" for line in
                     out.split(f"write {home / CONF}:", 1)[1].split("[dry-run] write", 1)[0].splitlines())
    for want in ("name = libpipewire-module-echo-cancel", "library.name = aec/libspa-aec-webrtc",
                 'node.name = "littlebot_ec_mic"', 'node.name = "littlebot_ec_speaker"',
                 'node.name = "littlebot_ec_capture"', 'node.name = "littlebot_ec_playback"',
                 f'target.object = "{USB_MIC}"', f'target.object = "{USB_SPK}"',  # not the speaker's own mic
                 "node.passive = true", "webrtc.gain_control = false", "audio.channels = 1",
                 "priority.session = 1600", "priority.session = 2200"):
        assert want in conf, want
    assert USB_SPK_MIC not in conf
    assert conf.count("{") == conf.count("}") and conf.count("[") == conf.count("]")
    assert f"[dry-run] write {home / ALSA_RC}:" in out
    for want in ("pcm.littlebot_ec_mic {", "pcm.littlebot_ec_speaker {", "type pipewire",
                 'capture_node "littlebot_ec_mic"', 'playback_node "littlebot_ec_speaker"', "show on"):
        assert want in out, want
    assert "[dry-run] systemctl --user restart pipewire pipewire-pulse wireplumber" in out
    assert "[dry-run] wpctl set-default" in out
    assert list(home.iterdir()) == [] and _changes(calls) == []


def test_dry_run_without_pipewire_follows_the_default_devices(fake_pw) -> None:
    res, calls, home = fake_pw("--dry-run", FAKE_PW_DOWN="1")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Cannot reach PipeWire" in res.stderr
    assert "no USB microphone found when this was written" in res.stdout and "target.object" not in res.stdout
    assert list(home.iterdir()) == [] and _changes(calls) == []
    res, _, _ = fake_pw(FAKE_PW_DOWN="1")  # for real it refuses
    assert res.returncode == 1 and "Cannot reach PipeWire" in res.stderr


def test_setup_is_idempotent_checks_and_removes_cleanly(fake_pw) -> None:
    res, calls, home = fake_pw("--check")  # not set up yet
    assert res.returncode == 1 and "not set up" in res.stdout
    original = "# my own ALSA settings\ndefaults.pcm.rate_converter \"speexrate\"\n"
    (home / ".asoundrc").write_text(original)

    res, calls, home = fake_pw("--wait", "2")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _changes(calls) == ["systemctl --user restart pipewire pipewire-pulse wireplumber",
                               "wpctl set-default 80", "wpctl set-default 81"]
    assert (home / CONF).is_file()
    assert (home / ".asoundrc").read_text() == original  # their own file is left alone
    assert "pcm.littlebot_ec_mic {" in (home / ALSA_RC).read_text()
    assert "Echo cancelling looks set up" in res.stdout  # it runs the check at the end
    assert "FAIL" not in res.stdout
    assert "LittleBot's service is not running" in res.stdout

    res, calls, _ = fake_pw("--wait", "2", FAKE_LITTLEBOT_RUNNING="1")  # again: nothing to do but the defaults
    assert res.returncode == 0, res.stdout + res.stderr
    assert "is up to date" in res.stdout and "no restart needed" in res.stdout
    assert _changes(calls) == ["wpctl set-default 80", "wpctl set-default 81", "systemctl --user restart littlebot"]

    res, calls, _ = fake_pw("--check")
    assert res.returncode == 0, res.stdout
    for want in ("PASS  PipeWire is running", "(version 0.3.65)", "PASS  PipeWire has littlebot_ec_speaker (id 80)",
                 "PASS  default speaker: littlebot_ec_speaker", "PASS  default microphone: littlebot_ec_mic",
                 "PASS  arecord -L lists littlebot_ec_mic", "PASS  aplay -L lists littlebot_ec_speaker",
                 "PASS  littlebot_ec_mic opens through ALSA", "PASS  PortAudio (LittleBot) sees",
                 f"PASS  USB microphone: USB PnP Sound Device Mono ({USB_MIC})"):
        assert want in res.stdout, want
    assert _changes(calls) == []

    res, calls, _ = fake_pw("--remove", "--wait", "1")
    assert res.returncode == 0, res.stdout + res.stderr
    assert not (home / CONF).exists()
    assert (home / ".asoundrc").read_text() == original  # exactly as it was
    assert "systemctl --user restart pipewire pipewire-pulse wireplumber" in calls
    assert "wpctl set-default 52" in calls and "wpctl set-default 61" in calls  # set_default_speaker.sh: USB again

    res, calls, _ = fake_pw("--remove")  # nothing left to remove
    assert res.returncode == 0 and "no LittleBot block" in res.stdout and _changes(calls) == []


def test_an_asoundrc_of_our_own_is_deleted_on_remove(fake_pw) -> None:
    res, _, home = fake_pw("--wait", "2", "--no-default")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "WARN  default speaker is" in res.stdout  # --no-default: a video's sound isn't cancelled
    res, _, _ = fake_pw("--remove", "--wait", "1")
    assert res.returncode == 0 and not (home / ALSA_RC).exists()


def test_a_block_left_in_the_old_asoundrc_is_moved(fake_pw) -> None:
    """Earlier versions wrote ~/.asoundrc, which something on the robot deleted at boot."""
    res, _, home = fake_pw("--wait", "2")
    assert res.returncode == 0, res.stdout + res.stderr
    block = (home / ALSA_RC).read_text()
    (home / ".asoundrc").write_text("# mine\n\n" + block)
    res, _, _ = fake_pw("--check")
    assert "WARN  LittleBot's block is in" in res.stdout
    res, _, _ = fake_pw("--wait", "2")
    assert res.returncode == 0, res.stdout + res.stderr
    assert (home / ".asoundrc").read_text().strip() == "# mine" and "pcm.littlebot_ec_mic {" in (home / ALSA_RC).read_text()
    (home / ".asoundrc").unlink()  # deleted at boot: nothing is lost
    res, _, _ = fake_pw("--check")
    assert res.returncode == 0 and "FAIL" not in res.stdout, res.stdout


def test_check_reports_missing_plugins(fake_pw, tmp_path: Path) -> None:
    fake_pw("--wait", "2")
    shutil.rmtree(tmp_path / "lib" / "aarch64-linux-gnu" / "alsa-lib")
    res, _, _ = fake_pw("--check")
    assert res.returncode == 1
    assert "FAIL  the PipeWire ALSA plugin is missing: sudo apt install pipewire-alsa" in res.stdout


def test_the_installer_runs_it_only_when_asked(tmp_path: Path) -> None:
    res = subprocess.run([BASH, str(ROOT / "scripts" / "install_pi.sh"), "--help"], check=False, capture_output=True, text=True)
    assert "--echo-cancel" in res.stdout
    env = dict(os.environ, HOME=str(tmp_path))
    base = ["--dry-run", "--allow-non-pi", "--skip-apt", "--no-chromium", "--no-models", "--no-live", "--no-wake-word"]
    for extra, want in (([], False), (["--echo-cancel"], True)):
        res = subprocess.run([BASH, str(ROOT / "scripts" / "install_pi.sh"), *base, *extra], check=False, capture_output=True,
                             text=True, env=env, cwd=ROOT, timeout=120)
        if "This installer is for Raspberry Pi OS / Debian" in res.stderr:
            pytest.skip("not a Debian-family system")
        assert res.returncode == 0, res.stdout + res.stderr
        step = [line for line in res.stdout.splitlines() if "[dry-run]" in line and "setup_echo_cancel.sh" in line]
        assert bool(step) == want, res.stdout
        if want:
            assert res.stdout.index("set_default_speaker.sh") < res.stdout.index("setup_echo_cancel.sh")
            assert "echo cancelling: PipeWire's echo canceller" in res.stdout
