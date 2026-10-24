"""Running on the battery: the battery monitor, the percentage, and what LittleBot does when it runs low.

The Raspberry Pi can't measure the 2S Li-ion pack itself, so an RP2040-Zero on a USB port does it
(firmware/battery_monitor) and prints one JSON line a second. ``ctx.power`` (``service.PowerService``) finds it,
turns its voltages into a percentage a parent can trust, and acts: a gentle "I'm getting sleepy" when low, a
goodbye and a clean shutdown when empty or too hot, a battery sign on the face when plugged in.

* ``protocol`` - the monitor's line format, parsed defensively
* ``monitor``  - ``SerialMonitor`` (find the port, raw tty, reconnect; standard library only) and
  ``SimulatedMonitor`` (desktop preview, tests)
* ``battery``  - the 2S Li-ion percent curve, smoothing, the time-left estimate (pure)
* ``policy``   - low / critical / hot / cold with sustain timers and hysteresis (pure)
* ``service``  - ``PowerService``: state, ``power.*`` events, speech, the shutdown
* ``shutdown`` - switching the Pi off without sudo (``systemctl poweroff``), only where allowed

Without a monitor (mains power) it is "not fitted" and completely quiet: no events, no speech, no change.
See docs/POWER.md, "Running on the battery".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from littlebot.power.service import PowerService

__all__ = ["create_service", "fitted", "voice_instructions"]


def create_service(ctx: Any) -> PowerService:
    """Factory used by ``app/robot.py``."""
    from littlebot.power.service import PowerService  # noqa: PLC0415

    return PowerService(ctx)


def fitted(ctx: Any) -> bool:
    """True when a battery monitor has been found (the robot runs on the battery)."""
    power = getattr(ctx, "power", None)
    return bool(power is not None and getattr(power, "fitted", False))


def voice_instructions(ctx: Any) -> str:
    """What the live voice is told about the battery ("" on mains power: no monitor fitted)."""
    if not fitted(ctx):
        return ""
    from littlebot.power.service import VOICE_INSTRUCTIONS  # noqa: PLC0415

    return VOICE_INSTRUCTIONS
