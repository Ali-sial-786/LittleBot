"""Who LittleBot is: told to the AI so it knows what it is, who made it and how it works.

``about(config)`` is a short paragraph added to the text model's system prompt (brain/prompts.py
``build_system_prompt(about=...)``: typed chat and live-voice hand-offs) and to the live voice's instructions
(chatgpt/live.py, ``debug="voice"``). It says: the robot's name and version, who made it (``ai.creator``,
``ai.creator_relation``), what it runs on (the Pi model, its screen, camera, microphone and speaker), which parts
run on the robot and which use ChatGPT, and that it keeps a debug log it can read to work out what went wrong
(core/debug_log.py, the ``read_debug_log`` tool). Only names from the config and the Pi's model reach the model.
"""

from __future__ import annotations

import re
from typing import Any

_PLAIN = re.compile(r"[^\w '.,-]+")


def _clean(text: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", _PLAIN.sub("", str(text or ""))).strip()[:limit]


def _version() -> str:
    try:
        from littlebot import __version__  # noqa: PLC0415

        return str(__version__)
    except Exception:  # noqa: BLE001
        return ""


def _pi() -> str | None:
    try:
        from littlebot.hardware.platform import pi_model  # noqa: PLC0415

        model = pi_model()
    except Exception:  # noqa: BLE001
        return None
    return re.sub(r"\s+Rev\s+\S+$", "", model) if model else None  # "Raspberry Pi 5 Model B"


def creator(config: Any) -> str:
    """"Ali, the little one's dad", "Ali", or "" when ``ai.creator`` isn't set."""
    ai = getattr(config, "ai", None)
    who = _clean(getattr(ai, "creator", None), 40)
    relation = _clean(getattr(ai, "creator_relation", None), 60)
    return f"{who}, {relation}" if who and relation else who


def about(config: Any, *, debug: str = "tool") -> str:
    """What LittleBot is, for a system prompt. ``debug``: how it reads its debug log: "tool" (it has
    ``read_debug_log``), "voice" (the live voice, which delegates to the client) or "" (it can't)."""
    name = _clean(getattr(config, "robot_name", None), 40) or "LittleBot"
    version = _version()
    made_by = creator(config)
    who = made_by.split(",")[0] if made_by else ""
    pi = _pi()
    lines = [
        f"Who you are: you are {name}" + (f" (software version {version})" if version else "") + ", a home-made "
        "robot companion for the family's little one" + (f", built and programmed by {made_by}" if made_by else "")
        + ". " + (f"{who} is your creator: when someone asks who made you, built you or programmed you, say {who} "
                  "did. " if who else "")
        + f"Your body: {'a ' + pi if pi else 'a Raspberry Pi'} computer, a small touch screen with your animated "
        "face, a USB camera, a USB microphone and a little speaker. Your face, wake word, voice recognition, "
        "games, activities and screen cards run on the robot itself; your voice chats, answers and look-ups use "
        "ChatGPT (OpenAI) on the family's own plan, and your camera is only used for the AI when a grown-up "
        "allows it.",
    ]
    if debug == "voice":
        lines.append(
            "You keep a debug log of what you do (what happened and when, warnings and errors; never what people "
            "said). When a grown-up asks what went wrong, why something didn't work, or to check your logs, "
            "delegate it to the client as \"check the debug log: <what went wrong, and roughly when>\", wait, then "
            "explain the likely cause simply and what might fix it.")
    elif debug == "tool":
        lines.append(
            "You keep a debug log of what you do (what happened and when, warnings and errors; never what people "
            "said). When a grown-up asks what went wrong, why something didn't work, or to check your logs, call "
            "read_debug_log (with about: a few words of what went wrong, when it helps), work out from it what most "
            "likely happened, and explain it simply: the cause in plain words and what might fix it, without "
            "reading out log lines, codes or times to the second. If the log doesn't show it, say so.")
    return " ".join(lines)


__all__ = ["about", "creator"]
