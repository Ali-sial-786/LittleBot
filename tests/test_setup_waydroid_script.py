"""scripts/setup_waydroid.sh: every change is printed and asked for first, step 1 is marked so undo can remove it,
and the kernel step needs the Pi's own 4K-page kernel."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup_waydroid.sh"
BASH = shutil.which("bash")


def test_the_script_asks_before_changing_anything() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text and "\r\n" not in text
    ask = text.index('read -r -p "Run it now?')
    assert text.index('bash -c "$2"') > ask  # the only place a change is run
    assert '[ ! -t 0 ]' in text  # not on a terminal: only prints
    for step in ("kernel", "install", "start", "play-id", "aosu", "type", "stop", "undo"):
        assert f"\n  {step}) " in text, step


def test_typing_keeps_the_text_off_command_lines() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    body = text[text.index("cmd_type() {"):text.index("cmd_stop() {")]
    assert 'read -r -s -p' in body  # not shown while typed
    assert """printf '%s\\n' "$text" | sudo waydroid shell -- sh -c 'IFS= read -r t; input text "$t"'""" in body
    assert body.count("$text") == 2  # tested, piped: never an argument of a program


def test_step_one_is_backed_up_marked_and_undone() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "kernel=kernel8.img" in text and "psi=1" in text and "BACKUP_SUFFIX=.littlebot-16k" in text
    assert text.count("$MARK_START") >= 3 and "$MARK_END" in text  # added between the marks, removed by them
    assert "trixie-backports waydroid" in text and "waydroid init -s GAPPS" in text
    # the container service runs at every boot, so LittleBot starts Android without a password (littlebot/android)
    assert "sudo systemctl enable --now waydroid-container" in text


@pytest.mark.skipif(BASH is None, reason="bash not installed")
def test_the_script_parses() -> None:
    res = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
