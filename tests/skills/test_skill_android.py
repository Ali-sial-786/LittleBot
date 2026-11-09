"""The Android skill (littlebot/skills/android.py): open_android / close_android, the words for the voice, and the
start waiting for the answer (takeover, never ending the voice chat)."""

from __future__ import annotations

from typing import Any

from littlebot.skills import takeover
from littlebot.skills.android import Android
from littlebot.skills.hub import SkillHub
from littlebot.testing import make_test_context


class FakeAndroid:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.cfg = ctx.config.android
        self.why: str | None = None
        self.opened: list[tuple[str, str | None]] = []
        self.closed: list[str] = []
        self.on: str | None = None  # None | starting | showing
        self.result: dict[str, Any] = {"ok": True}

    def offered(self) -> bool:
        return True

    def check(self) -> str | None:
        return self.why

    def package_of(self, app: Any) -> str | None:
        return "com.glazero.android" if str(app).lower() == "aosu" else None

    def status(self) -> dict[str, Any]:
        return {"active": self.on is not None, "state": self.on}

    async def open(self, *, source: str = "voice", app: str | None = None) -> dict[str, Any]:
        self.opened.append((source, app))
        return self.result

    async def close(self, reason: str) -> bool:
        self.closed.append(reason)
        was, self.on = self.on is not None, None
        return was


def _skill() -> tuple[Android, FakeAndroid, Any]:
    ctx = make_test_context()
    ctx.android = FakeAndroid(ctx)
    return Android(ctx), ctx.android, ctx


async def test_open_starts_after_the_answer() -> None:
    skill, svc, ctx = _skill()
    out = await skill.run("open_android", {})
    assert out.startswith("ok:") and "starting on your screen" in out
    assert svc.opened == []  # not mid-answer
    items = takeover.claim(ctx)
    assert [i.label for i in items] == ["android"] and items[0].takeover is False  # the chat carries on
    assert await items[0].run() is True and svc.opened == [("voice", None)]
    ctx.db.close()


async def test_an_app() -> None:
    skill, svc, ctx = _skill()
    out = await skill.run("open_android", {"app": "aosu"})
    assert out.startswith("ok:") and "with aosu" in out
    items = takeover.claim(ctx)
    assert [i.label for i in items] == ["android aosu"]
    await items[0].run()
    assert svc.opened == [("voice", "aosu")]
    assert (await skill.run("open_android", {"app": "netflix"})).startswith("error:")
    ctx.db.close()


async def test_refusals_say_why() -> None:
    skill, svc, ctx = _skill()
    svc.why = "not_set_up"
    out = await skill.run("open_android", {})
    assert out.startswith("error:") and "setup_waydroid.sh" in out
    svc.why = "low_memory"
    assert "memory" in await skill.run("open_android", {})
    assert takeover.claim(ctx) == []
    ctx.db.close()


async def test_already_on() -> None:
    skill, svc, ctx = _skill()
    svc.on = "showing"
    assert "already" in await skill.run("open_android", {})
    out = await skill.run("open_android", {"app": "aosu"})
    assert "aosu is opening" in out and svc.opened == [("voice", "aosu")]
    assert skill.context_note() == "Android: on your screen."
    ctx.db.close()


async def test_close() -> None:
    skill, svc, ctx = _skill()
    assert "wasn't open" in await skill.run("close_android", {})
    svc.on = "starting"
    assert "closed" in await skill.run("close_android", {})
    assert svc.closed[-1] == "asked in the voice chat"
    ctx.db.close()


async def test_a_failed_start_is_said() -> None:
    skill, svc, ctx = _skill()
    said: list[str] = []
    skill.announce = lambda text, **_kw: said.append(text)  # type: ignore[method-assign]
    svc.result = {"ok": False, "error": "busy"}
    await skill.run("open_android", {})
    assert await takeover.claim(ctx)[0].run() is False
    assert said == ["My screen is busy right now, so I can't show Android."]
    ctx.db.close()


def test_the_hub_has_it() -> None:
    ctx = make_test_context()
    ctx.android = FakeAndroid(ctx)
    hub = SkillHub(ctx)
    assert hub.get("android") is not None
    names = {t["name"] for t in hub.get("android").tools()}
    assert names == {"open_android", "close_android"}
    assert "aosu" in str(hub.get("android").tools()[0])
    ctx.db.close()
