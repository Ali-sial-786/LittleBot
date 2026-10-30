"""SkillHub (``ctx.skills``): every skill's tools, prompt notes and background work in one place.

The assistant's brain (littlebot/assistant/agent.py) and GPT-Live's hand-offs (``chatgpt.tools.RobotToolbox``) ask
it for ``specs()`` and hand it the calls it owns (``owns(name)`` / ``run(name, args)``). A skill that fails to
build, start or run is logged and left out; the rest carry on.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from typing import Any

from littlebot.core.service import Service, ServiceStatus
from littlebot.skills.base import Skill, ToolSpec, err

log = logging.getLogger("littlebot.skills")

# (module, class) of every skill, in prompt order. A module that is missing (not written yet) is skipped.
SKILLS: tuple[tuple[str, str], ...] = (
    ("littlebot.skills.memory", "Memory"),
    ("littlebot.skills.reminders", "Reminders"),
    ("littlebot.skills.lists", "Lists"),
    ("littlebot.skills.persona", "Persona"),
    ("littlebot.skills.briefing", "Briefing"),
    ("littlebot.skills.home", "SmartHome"),
    ("littlebot.skills.routines", "Routines"),
    ("littlebot.skills.spotify", "Spotify"),
    ("littlebot.skills.music", "Music"),
    ("littlebot.skills.translator", "Translator"),
    ("littlebot.games.voice.skill", "VoiceGames"),
)


class SkillHub(Service):
    name = "skills"

    def __init__(self, ctx: Any, skills: list[Skill] | None = None) -> None:
        super().__init__()
        self.ctx = ctx
        self.skills: list[Skill] = list(skills) if skills is not None else _build(ctx)
        self._owner: dict[str, Skill] = {}
        for s in self.skills:
            for spec in _safe_list(s.tools):
                self._owner.setdefault(str(spec.get("name")), s)

    def get(self, name: str) -> Skill | None:
        return next((s for s in self.skills if s.name == name), None)

    async def start(self) -> None:
        for s in self.skills:
            try:
                await s.start()
            except Exception:  # noqa: BLE001
                log.exception("skill %s failed to start", s.name)
        self.set_status(ServiceStatus.RUNNING, f"{len(self.skills)} skills")

    async def stop(self) -> None:
        for s in reversed(self.skills):
            try:
                await asyncio.wait_for(s.stop(), 3.0)
            except Exception:  # noqa: BLE001
                log.exception("skill %s failed to stop", s.name)
        self.set_status(ServiceStatus.STOPPED)

    # ------------------------------------------------------------ for the text models
    def _live(self) -> list[Skill]:
        out = []
        for s in self.skills:
            try:
                if s.available():
                    out.append(s)
            except Exception:  # noqa: BLE001
                log.debug("skill %s: available() failed", s.name, exc_info=True)
        return out

    def specs(self) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        seen: set[str] = set()
        for s in self._live():
            for spec in _safe_list(s.tools):
                name = str(spec.get("name") or "")
                if name and name not in seen:
                    seen.add(name)
                    out.append(spec)
        return out

    def owns(self, name: str) -> bool:
        s = self._owner.get(name)
        if s is None:  # a tool a skill offers only sometimes (re-read its list)
            for sk in self.skills:
                if any(spec.get("name") == name for spec in _safe_list(sk.tools)):
                    self._owner[name] = sk
                    return True
            return False
        return True

    async def run(self, name: str, arguments: str | dict[str, Any] | None = None) -> str:
        if not self.owns(name):
            return err(f"there is no tool called {str(name)[:40]}")
        skill = self._owner[name]
        if isinstance(arguments, dict):
            args = arguments
        else:
            try:
                args = json.loads(arguments or "{}")
            except ValueError:
                return err("the arguments were not valid JSON")
            if not isinstance(args, dict):
                args = {}
        try:
            result = await skill.run(name, args)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken skill must never break the conversation
            log.warning("skill tool %s crashed", name, exc_info=True)
            return err("the action failed")
        log.info("skill tool %s -> %s", name, str(result).split(":", 1)[0])
        return str(result if result is not None else "ok")

    def prompt_notes(self) -> str:
        """The skills' fixed instructions, one paragraph each (the cached part of the system prompt)."""
        return "\n".join(n for n in (_safe_str(s.prompt_note) for s in self._live()) if n)

    def context_notes(self) -> str:
        """What the skills hold right now (changes turn to turn: after the cached part)."""
        return "\n".join(n for n in (_safe_str(s.context_note) for s in self._live()) if n)

    def status(self) -> dict[str, Any]:
        return {"skills": [_safe_status(s) for s in self.skills], "tools": len(self.specs())}


def _build(ctx: Any) -> list[Skill]:
    out: list[Skill] = []
    for module_path, cls_name in SKILLS:
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as e:
            if e.name and (module_path == e.name or module_path.startswith(e.name + ".")):
                continue  # not written (yet)
            log.exception("could not import skill %s", module_path)
            continue
        except Exception:  # noqa: BLE001
            log.exception("could not import skill %s", module_path)
            continue
        try:
            out.append(getattr(module, cls_name)(ctx))
        except Exception:  # noqa: BLE001
            log.exception("could not build skill %s", cls_name)
    return out


def _safe_list(fn: Any) -> list[ToolSpec]:
    try:
        return list(fn() or [])
    except Exception:  # noqa: BLE001
        log.debug("skill tools() failed", exc_info=True)
        return []


def _safe_str(fn: Any) -> str:
    try:
        return str(fn() or "").strip()
    except Exception:  # noqa: BLE001
        log.debug("skill note failed", exc_info=True)
        return ""


def _safe_status(s: Skill) -> dict[str, Any]:
    try:
        return s.status()
    except Exception:  # noqa: BLE001
        return {"name": s.name, "available": False}


__all__ = ["SKILLS", "SkillHub"]
