"""scripts/setup_mirroring.sh: checks, prints the install command, and only runs sudo after a y on the terminal."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup_mirroring.sh"
BASH = shutil.which("bash")


def test_the_script_is_there_and_asks_before_sudo() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text and "\r\n" not in text
    for pkg in ("uxplay", "adb", "scrcpy", "gstreamer1.0-libav", "gstreamer1.0-plugins-bad",
                "gstreamer1.0-plugins-good", "avahi-daemon", "trixie-backports"):
        assert pkg in text, pkg
    ask = text.index('read -r -p "Run it now?')
    assert text.index('bash -c "$command_line"') > ask  # the only place anything is run
    assert '[ ! -t 0 ]' in text  # not on a terminal: only prints


def test_the_service_names_the_same_install_commands() -> None:
    from littlebot.mirror.service import INSTALL, SETUP_SCRIPT

    assert "trixie-backports" in INSTALL["scrcpy"] and INSTALL["adb"] == "sudo apt install adb"
    assert "gstreamer1.0-libav" in INSTALL["uxplay"] and SETUP_SCRIPT.endswith("scripts/setup_mirroring.sh")


@pytest.mark.skipif(BASH is None, reason="bash not installed")
def test_the_script_parses() -> None:
    res = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
