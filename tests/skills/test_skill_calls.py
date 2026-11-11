"""The WhatsApp calls skill (littlebot/skills/calls.py): whatsapp_call / open_whatsapp / close_whatsapp, the words
for the voice, only people on the list, only a grown-up, and the opening waiting for the answer (takeover)."""

from __future__ import annotations

from typing import Any

from littlebot.calls import contacts as C
from littlebot.skills import takeover
from littlebot.skills.calls import Calls
from littlebot.skills.hub import SkillHub
from littlebot.testing import make_test_context


class FakeCalls:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.people = {"Dad": "Ali Khan", "Mum": "Sara"}
        self.why: str | None = None
        self.opened: list[dict[str, Any]] = []
        self.hung: list[str] = []
        self.active = False
        self.call = "none"
        self.result: dict[str, Any] = {"ok": True}
        self.is_offered = True

    def offered(self) -> bool:
        return self.is_offered

    def contacts(self) -> dict[str, str]:
        return dict(self.people)

    def find_contact(self, said: Any) -> tuple[str, str] | None:
        return C.find(self.people, said)

    def check(self, source: str = "voice", *, kind: str | None = None) -> str | None:
        return self.why

    def status(self) -> dict[str, Any]:
        return {"active": self.active, "call": self.call, "state": "ready" if self.active else None}

    async def open(self, **kw: Any) -> dict[str, Any]:
        self.opened.append(kw)
        return self.result

    async def hang_up(self, reason: str = "") -> dict[str, Any]:
        self.hung.append(reason)
        was, self.active = self.active, False
        was_call, self.call = self.call != "none", "none"
        return {"ok": True, "was_call": was_call, "closed": was}


def _skill() -> tuple[Calls, FakeCalls, Any]:
    ctx = make_test_context()
    ctx.calls = FakeCalls(ctx)
    return Calls(ctx), ctx.calls, ctx


async def _run_queued(ctx: Any) -> list[str]:
    items = takeover.claim(ctx)
    for item in items:
        await item.run()
    return [i.label for i in items]


async def test_a_call_is_queued_for_after_the_words() -> None:
    skill, svc, ctx = _skill()
    out = await skill.run("whatsapp_call", {"name": "my dad"})
    assert out == "ok: say: Calling Dad on WhatsApp." and svc.opened == []  # nothing opens under the answer
    queued = takeover.claim(ctx)
    assert [q.label for q in queued] == ["whatsapp call Dad"] and queued[0].takeover is False
    await queued[0].run()
    assert svc.opened == [{"source": "voice", "contact": "Dad", "kind": "video", "answer": False}]
    ctx.db.close()


async def test_voice_call_and_answer() -> None:
    skill, svc, ctx = _skill()
    await skill.run("whatsapp_call", {"name": "Mum", "kind": "voice"})
    await _run_queued(ctx)
    assert svc.opened[-1]["kind"] == "voice" and svc.opened[-1]["contact"] == "Mum"
    assert await skill.run("open_whatsapp", {"answer": True}) == "ok: say: Opening WhatsApp to answer."
    await _run_queued(ctx)
    assert svc.opened[-1] == {"source": "voice", "contact": None, "kind": "video", "answer": True}
    assert await skill.run("open_whatsapp", {}) == "ok: say: Opening WhatsApp."
    ctx.db.close()


async def test_only_people_on_the_list() -> None:
    skill, svc, ctx = _skill()
    assert await skill.run("whatsapp_call", {"name": "Sam"}) == "error: say: I can only call Dad or Mum."
    assert await skill.run("whatsapp_call", {"name": "Ali Khan"}) == "error: say: I can only call Dad or Mum."
    svc.people = {}
    assert "Nobody is on my list" in await skill.run("whatsapp_call", {"name": "Dad"})
    assert takeover.claim(ctx) == [] and svc.opened == []
    ctx.db.close()


async def test_refusals_in_words() -> None:
    skill, svc, ctx = _skill()
    for why, words in (("child", "Only a grown-up can make a call."), ("low_memory", "enough free memory"),
                       ("camera_off", "voice call instead"), ("disabled", "switched off on the dashboard")):
        svc.why = why
        out = await skill.run("whatsapp_call", {"name": "Dad"})
        assert out.startswith("error: say: ") and words in out, why
    assert takeover.claim(ctx) == [] and svc.opened == []
    ctx.db.close()


async def test_open_already_goes_straight_to_it() -> None:
    skill, svc, ctx = _skill()
    svc.active = True
    assert await skill.instant("call", name="Dad") == "Calling Dad on WhatsApp."
    assert svc.opened == [{"source": "voice", "contact": "Dad", "kind": "video", "answer": False}]
    assert takeover.claim(ctx) == []
    svc.result = {"ok": False, "error": "in_call"}
    assert await skill.instant("call", name="Mum") == "A call is on already."
    ctx.db.close()


async def test_hang_up_says_nothing_into_the_end_of_a_call() -> None:
    skill, svc, ctx = _skill()
    svc.active, svc.call = True, "in_call"
    assert await skill.instant("hang_up") == "" and svc.hung == ["asked in the voice chat"]
    svc.active = True
    assert await skill.instant("hang_up") == "WhatsApp is closed."
    assert await skill.instant("hang_up") == "WhatsApp isn't open."
    assert (await skill.run("close_whatsapp", {})).startswith("ok: ")
    ctx.db.close()


async def test_hang_up_drops_a_call_that_has_not_started() -> None:
    skill, svc, ctx = _skill()
    await skill.instant("call", name="Dad")
    assert await skill.instant("hang_up") == "WhatsApp is closed."
    assert takeover.claim(ctx) == []
    ctx.db.close()


async def test_a_failed_start_is_said() -> None:
    skill, svc, ctx = _skill()
    said: list[str] = []
    ctx.bus.subscribe("assistant.announce", lambda e: said.append(e.data["text"]))
    svc.result = {"ok": False, "error": "busy"}
    await skill.instant("open")
    await _run_queued(ctx)
    assert said == ["My screen is busy right now, so I can't open WhatsApp."]
    ctx.db.close()


async def test_through_the_hub_and_hidden_when_not_offered() -> None:
    skill, svc, ctx = _skill()
    hub = SkillHub(ctx, [skill])
    names = [s["name"] for s in hub.specs()]
    assert names == ["whatsapp_call", "open_whatsapp", "close_whatsapp"]
    spec = hub.specs()[0]
    assert "Dad, Mum" in spec["description"] and "Ali" not in str(hub.specs())  # the said names only
    assert "never see, read or send" in hub.prompt_notes()
    assert skill.hearing_words() == ["WhatsApp", "Dad", "Mum"]
    assert hub.context_notes() == ""
    svc.active, svc.call = True, "in_call"
    assert "a call is on" in hub.context_notes()
    assert skill.knows("daddy") and not skill.knows("Sam") and skill.active
    svc.is_offered = False
    assert hub.specs() == [] and hub.prompt_notes() == ""
    ctx.db.close()
