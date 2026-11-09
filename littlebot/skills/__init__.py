"""LittleBot's skills (memory, reminders, lists, briefings, smart home, games ...): see littlebot/skills/base.py."""

from __future__ import annotations

from typing import Any


def create_service(ctx: Any) -> Any:
    from littlebot.skills.hub import SkillHub

    return SkillHub(ctx)


def get_skills(ctx: Any) -> Any:
    """``ctx.skills`` or None (a context without the field: some tests)."""
    return getattr(ctx, "skills", None)
