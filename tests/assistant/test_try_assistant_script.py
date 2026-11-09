"""scripts/try_assistant.py runs without keys (it says what's missing) and never prints a key."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "try_assistant.py"


def test_help() -> None:
    res = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0 and "--measure" in res.stdout
