"""scripts/check_audio.py and scripts/generate_sounds.py run anywhere (no devices needed)."""

from __future__ import annotations

import os
import subprocess
import sys

from littlebot.core.paths import PROJECT_ROOT

SCRIPTS = PROJECT_ROOT / "scripts"


def _run(*args: str, timeout: float = 90) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "SDL_AUDIODRIVER": "dummy", "PYGAME_HIDE_SUPPORT_PROMPT": "1"}
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=timeout,
                          cwd=PROJECT_ROOT, env=env, check=False)


def test_check_audio_help_and_list():
    help_ = _run(str(SCRIPTS / "check_audio.py"), "--help")
    assert help_.returncode == 0 and "--list" in help_.stdout and "--record" in help_.stdout
    res = _run(str(SCRIPTS / "check_audio.py"), "--list")
    assert res.returncode == 0, res.stderr[-2000:]
    out = res.stdout
    assert "PortAudio devices" in out and "Outputs (speakers)" in out and "Result" in out
    assert "Traceback" not in res.stderr


def test_check_audio_save_requires_record():
    res = _run(str(SCRIPTS / "check_audio.py"), "--save", "/tmp/x.wav")
    assert res.returncode == 2 and "--save only makes sense with --record" in res.stderr


def test_generated_sounds_are_up_to_date():
    res = _run(str(SCRIPTS / "generate_sounds.py"), "--check")
    assert res.returncode == 0, res.stdout[-2000:]
    from littlebot.core.interfaces import SOUND_NAMES

    assert f"{len(SOUND_NAMES)} sound(s)" in res.stdout
