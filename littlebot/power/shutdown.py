"""Switching the Raspberry Pi off cleanly, without sudo.

LittleBot had no power-off of its own (the dashboard's Shutdown only stops the app; ``scripts/safe_shutdown.sh``
uses sudo). When the battery is empty or too hot, ``PowerService`` asks the robot facade
(``LittleBot.request_poweroff``): the app stops every subsystem and closes the database as usual, and only
then ``littlebot.app.main`` runs ``power_off_system()``: ``systemctl --no-ask-password poweroff``.

That needs no sudo for the user logged in on the robot's own screen (polkit lets the active local session
power off: logind's ``org.freedesktop.login1.power-off``). From the headless system service, or over SSH
only, polkit refuses: the log says so, and the battery's BMS cuts the power when it is empty.

Only when ``power.allow_poweroff`` allows it (default: on the Raspberry Pi, never in the desktop
simulation); otherwise it is only logged.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any, Callable, Sequence

log = logging.getLogger("littlebot.power.shutdown")

DEFAULT_COMMAND = ("systemctl", "--no-ask-password", "poweroff")


def poweroff_allowed(config: Any) -> bool:
    """``power.allow_poweroff``, where None means "only on the Raspberry Pi" (mode ``pi``)."""
    allowed = getattr(getattr(config, "power", None), "allow_poweroff", None)
    if allowed is None:
        mode = getattr(getattr(config, "mode", None), "value", getattr(config, "mode", None))
        return mode == "pi"
    return bool(allowed)


def power_off_system(command: Sequence[str] = DEFAULT_COMMAND, *,
                     run: Callable[..., Any] = subprocess.run) -> bool:
    """Run the power-off command (no shell, no sudo). True when it was accepted."""
    cmd = [str(c) for c in command] or list(DEFAULT_COMMAND)
    log.critical("POWERING OFF the Raspberry Pi now: %s", " ".join(cmd))
    try:
        result = run(cmd, timeout=30, check=False, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as e:
        log.critical("power-off failed: %s (the battery's BMS cuts the power when it is empty)", e)
        return False
    code = getattr(result, "returncode", 1)
    if code != 0:
        err = (getattr(result, "stderr", "") or "").strip().splitlines()[-1:] or [""]
        log.critical("power-off was refused (exit %s): %s. Allow it for the robot's user (polkit), or shut "
                     "down with scripts/safe_shutdown.sh", code, err[0][:200])
        return False
    return True
