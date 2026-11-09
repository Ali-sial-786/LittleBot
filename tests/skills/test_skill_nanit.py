"""The baby camera skill (littlebot/skills/nanit.py): show_baby_camera / close_baby_camera, the words for the voice,
the view opening after the answer (takeover, never ending the voice chat) and a picture that never came being said."""

from __future__ import annotations

from typing import Any

from littlebot.nanit.service import NanitService
from littlebot.skills import takeover
from littlebot.skills.base import ANNOUNCE
from littlebot.skills.hub import SkillHub
from littlebot.skills.nanit import NanitCamera
from littlebot.testing import make_test_context

CAMS = [{"uid": "a1", "name": "Nursery"}, {"uid": "b2", "name": "Guest Room"}]


class FakeNanit:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.why: str | None = None
        self.cams = [dict(c) for c in CAMS]
        self.shown: list[tuple[Any, str]] = []
        self.closed: list[str] = []
        self.showing: str | None = None
        self.result: dict[str, Any] = {"ok": True}
        self.on = True

    def offered(self) -> bool:
        return self.on

    def check(self) -> str | None:
        return self.why

    def cameras(self) -> list[dict[str, str]]:
        return self.cams

    def pick(self, camera: Any = None) -> dict[str, str] | None:
        svc = NanitService.__new__(NanitService)
        svc.cameras = self.cameras  # type: ignore[method-assign]
        return NanitService.pick(svc, camera)

    def status(self) -> dict[str, Any]:
        return {"active": self.showing is not None, "camera": self.showing, "state": "showing"}

    async def show(self, camera: Any = None, *, source: str = "voice") -> dict[str, Any]:
        self.shown.append((camera, source))
        return self.result

    async def close(self, reason: str) -> bool:
        self.closed.append(reason)
        was, self.showing = self.showing is not None, None
        return was


def _skill() -> tuple[NanitCamera, FakeNanit, Any]:
    ctx = make_test_context()
    ctx.nanit = FakeNanit(ctx)
    return NanitCamera(ctx), ctx.nanit, ctx


async def test_show_answers_then_opens_after_the_answer() -> None:
    skill, svc, ctx = _skill()
    out = await skill.run("show_baby_camera", {})
    assert out.startswith("ok:") and "Nursery camera is coming up" in out and "Guest Room" in out
    assert svc.shown == []  # not mid-answer
    items = takeover.claim(ctx)
    assert [i.label for i in items] == ["nanit a1"] and items[0].takeover is False  # the chat carries on
    assert await items[0].run() is True and svc.shown == [("Nursery", "voice")]
    ctx.db.close()


async def test_a_camera_by_name_and_an_unknown_name() -> None:
    skill, svc, ctx = _skill()
    out = await skill.run("show_baby_camera", {"camera": "guest room"})
    assert out.startswith("ok:") and "Guest Room camera" in out
    (item,) = takeover.claim(ctx)
    await item.run()
    assert svc.shown == [("Guest Room", "voice")]
    out = await skill.run("show_baby_camera", {"camera": "garage"})
    assert out.startswith("error:") and "Nursery, Guest Room" in out and takeover.pending(ctx) == []
    ctx.db.close()


async def test_not_set_up_switched_off_and_not_running() -> None:
    skill, svc, ctx = _skill()
    svc.why = "not_set_up"
    out = await skill.run("show_baby_camera", {})
    assert out.startswith("error:") and "needs setting up on the dashboard" in out
    svc.why = "disabled"
    assert "switched off" in await skill.run("show_baby_camera", {})
    svc.why = None
    svc.cams = []
    assert "hasn't found the Nanit camera" in await skill.run("show_baby_camera", {})
    assert takeover.pending(ctx) == []
    ctx.db.close()


async def test_a_failed_start_and_a_picture_that_never_came_are_said() -> None:
    skill, svc, ctx = _skill()
    said: list[str] = []
    ctx.bus.subscribe(ANNOUNCE, lambda e: said.append(e.data["text"]))
    svc.result = {"ok": False, "error": "not_running"}
    await skill.run("show_baby_camera", {})
    (item,) = takeover.claim(ctx)
    assert await item.run() is False and "isn't running" in said[-1]
    svc.result = {"ok": True}
    await skill.run("show_baby_camera", {})
    (item,) = takeover.claim(ctx)
    assert await item.run() is True
    ctx.bus.publish("nanit.stopped", {"camera": "Nursery", "reason": "the camera isn't sending its picture",
                                      "code": "no_stream"})
    assert "isn't sending its picture" in said[-1]
    n = len(said)
    ctx.bus.publish("nanit.stopped", {"camera": "Nursery", "reason": "parent", "code": None})
    assert len(said) == n  # only once, and only for no picture
    ctx.db.close()


async def test_close_baby_camera() -> None:
    skill, svc, ctx = _skill()
    assert "wasn't showing" in await skill.run("close_baby_camera", {})
    svc.showing = "Nursery"
    assert "face is back" in await skill.run("close_baby_camera", {})
    assert svc.closed[-1] == "asked in the voice chat"
    await skill.run("show_baby_camera", {})  # asked for, then "no, stop" before it opened
    assert "face is back" in await skill.run("close_baby_camera", {}) and takeover.pending(ctx) == []
    ctx.db.close()


async def test_offered_only_when_set_up_and_switched_on() -> None:
    skill, svc, ctx = _skill()
    hub = SkillHub(ctx, [skill])
    names = {s["name"] for s in hub.specs()}
    assert {"show_baby_camera", "close_baby_camera"} <= names and "the nursery, the Nanit" in hub.prompt_notes()
    svc.on = False
    assert not {"show_baby_camera", "close_baby_camera"} & {s["name"] for s in hub.specs()}
    ctx.nanit = None
    assert skill.available() is False and (await skill.run("show_baby_camera", {})).startswith("error:")
    ctx.db.close()


async def test_the_hub_builds_it_and_hides_it_when_not_set_up() -> None:
    ctx = make_test_context()
    ctx.nanit = NanitService(ctx, screen=True)  # nothing set up in the test folder
    hub = SkillHub(ctx)
    assert hub.get("nanit") is not None
    assert "show_baby_camera" not in {s["name"] for s in hub.specs()}
    ctx.db.close()


async def test_context_note() -> None:
    skill, svc, ctx = _skill()
    assert skill.context_note() == ""
    svc.showing = "Nursery"
    assert "Nursery is on your screen" in skill.context_note()
    ctx.db.close()
