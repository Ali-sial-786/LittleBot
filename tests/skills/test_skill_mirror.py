"""The mirroring skill (littlebot/skills/mirror.py): mirror_phone / stop_mirroring, the words for the voice, and the
start waiting for the answer (takeover, never ending the voice chat)."""

from __future__ import annotations

from typing import Any

from littlebot.mirror.service import MirrorService
from littlebot.skills import takeover
from littlebot.skills.base import ANNOUNCE
from littlebot.skills.hub import SkillHub
from littlebot.skills.mirror import Mirror
from littlebot.testing import make_test_context


class FakeMirror:
    kind_of = staticmethod(MirrorService.kind_of)

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.why: str | None = None
        self.paired: list[dict[str, Any]] = [{"guid": "adb-x", "address": "192.168.1.23"}]
        self.started: list[tuple[str, str]] = []
        self.stopped: list[str] = []
        self.active_kind: str | None = None
        self.result: dict[str, Any] = {"ok": True}
        self.on = True

    def offered(self) -> bool:
        return self.on

    def check(self, kind: str) -> str | None:
        return "bad_kind" if kind not in ("iphone", "android") else self.why

    def phones(self) -> list[dict[str, Any]]:
        return self.paired

    def status(self) -> dict[str, Any]:
        return {"active": self.active_kind is not None, "kind": self.active_kind, "state": "waiting",
                "airplay_name": "LittleBot", "wait_seconds": 90}

    async def start(self, kind: str, *, source: str = "voice") -> dict[str, Any]:
        self.started.append((kind, source))
        return self.result

    async def stop(self, reason: str) -> bool:
        self.stopped.append(reason)
        was, self.active_kind = self.active_kind is not None, None
        return was


def _skill() -> tuple[Mirror, FakeMirror, Any]:
    ctx = make_test_context()
    ctx.mirror = FakeMirror(ctx)
    return Mirror(ctx), ctx.mirror, ctx


async def test_iphone_says_the_three_steps_and_starts_after_the_answer() -> None:
    skill, svc, ctx = _skill()
    out = await skill.run("mirror_phone", {"phone": "iPhone"})
    assert out.startswith("ok:") and "Control Center" in out and "Screen Mirroring" in out and "LittleBot" in out
    assert svc.started == []  # not mid-answer
    items = takeover.claim(ctx)
    assert [i.label for i in items] == ["mirror iphone"] and items[0].takeover is False  # the chat carries on
    assert await items[0].run() is True and svc.started == [("iphone", "voice")]
    ctx.db.close()


async def test_android_needs_a_pairing_first() -> None:
    skill, svc, ctx = _skill()
    svc.paired = []
    out = await skill.run("mirror_phone", {"phone": "android"})
    assert out.startswith("error:") and "paired once on the dashboard" in out and takeover.pending(ctx) == []
    svc.paired = [{"guid": "adb-x"}]
    out = await skill.run("mirror_phone", {"phone": "android"})
    assert out.startswith("ok:") and "dashboard" in out
    takeover.claim(ctx)
    ctx.db.close()


async def test_not_installed_switched_off_and_unknown_phones() -> None:
    skill, svc, ctx = _skill()
    svc.why = "not_installed"
    assert "isn't set up on LittleBot yet" in await skill.run("mirror_phone", {"phone": "iphone"})
    svc.why = "disabled"
    assert "switched off" in await skill.run("mirror_phone", {"phone": "iphone"})
    svc.why = None
    assert "iPhone or an Android" in await skill.run("mirror_phone", {"phone": "nokia"})
    assert takeover.pending(ctx) == []
    ctx.db.close()


async def test_a_failed_start_is_said() -> None:
    skill, svc, ctx = _skill()
    said: list[str] = []
    ctx.bus.subscribe(ANNOUNCE, lambda e: said.append(e.data["text"]))
    svc.result = {"ok": False, "error": "no_phone"}
    await skill.run("mirror_phone", {"phone": "android"})
    (item,) = takeover.claim(ctx)
    assert await item.run() is False
    assert said and "Wireless debugging" in said[0]
    ctx.db.close()


async def test_stop_mirroring() -> None:
    skill, svc, ctx = _skill()
    assert "nothing was being mirrored" in await skill.run("stop_mirroring", {})
    svc.active_kind = "android"
    assert "face is back" in await skill.run("stop_mirroring", {})
    assert svc.stopped[-1] == "asked in the voice chat"
    await skill.run("mirror_phone", {"phone": "iphone"})  # asked for, then "no, stop" before it started
    assert "face is back" in await skill.run("stop_mirroring", {}) and takeover.pending(ctx) == []
    ctx.db.close()


async def test_offered_only_when_the_service_is() -> None:
    skill, svc, ctx = _skill()
    hub = SkillHub(ctx, [skill])
    names = {s["name"] for s in hub.specs()}
    assert {"mirror_phone", "stop_mirroring"} <= names and "mirror, cast or show" in hub.prompt_notes()
    svc.on = False
    assert not {"mirror_phone", "stop_mirroring"} & {s["name"] for s in hub.specs()}
    ctx.mirror = None
    assert skill.available() is False and (await skill.run("mirror_phone", {"phone": "iphone"})).startswith("error:")
    ctx.db.close()


async def test_the_hub_builds_it() -> None:
    ctx = make_test_context()
    hub = SkillHub(ctx)
    assert hub.get("mirror") is not None
    ctx.db.close()


async def test_context_note() -> None:
    skill, svc, ctx = _skill()
    assert skill.context_note() == ""
    svc.active_kind = "iphone"
    assert "waiting for an iPhone" in skill.context_note()
    ctx.db.close()
