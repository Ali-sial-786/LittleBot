"""Family messages (littlebot/skills/messages.py): kept for someone, listed in the context until given, marked given,
taken back, tidied away, and the names listened for."""

from __future__ import annotations

import json
from typing import Any

import pytest

from littlebot.skills.messages import GIVEN_KEEP_S, MAX_WAITING, Messages, name_key
from littlebot.testing import make_test_context


@pytest.fixture
def rig():
    ctx = make_test_context()
    t = [1_800_000_000.0]
    yield Messages(ctx, now=lambda: t[0]), t
    ctx.db.close()


async def run(m: Messages, name: str, **args: Any) -> str:
    return await m.run(name, args)


def test_names() -> None:
    assert name_key("Amara") == name_key("AMARA.") == name_key("amara") == "amara"
    assert name_key("my dad") == name_key("Dad") == "dad" and name_key(None) == "" and name_key("Tom") == "tom"


async def test_a_message_waits_is_given_and_goes(rig) -> None:
    m, t = rig
    out = await run(m, "leave_message", to="amara", text="her lunch is in the fridge", sender="Ali")
    assert out == "ok: message #1 kept for Amara; I'll give it the next time Amara talks to me"
    t[0] += 2 * 3600
    assert m.context_note() == ('Messages waiting (give each only to that person): #1 for Amara from Ali, 2 hours ago: '
                                '"her lunch is in the fridge".')
    assert m.hearing_words() == ["Amara"]
    data = json.loads(await run(m, "messages_for", to="AMARA"))
    assert data == {"waiting": [{"id": 1, "for": "Amara", "text": "her lunch is in the fridge", "left": "2 hours ago",
                                 "from": "Ali"}], "given_today": []}
    assert await run(m, "messages_for", to="Dad") == "ok: no messages for Dad"
    assert await run(m, "message_given", ids=[1]) == "ok: marked 1 message as given"
    assert await run(m, "message_given", ids=[1]) == "ok: those were already given (or don't exist)"
    assert m.context_note() == "" and m.hearing_words() == []
    t[0] += 600
    data = json.loads(await run(m, "messages_for"))
    assert data["waiting"] == [] and data["given_today"][0]["given"] == "10 minutes ago"
    t[0] += GIVEN_KEEP_S
    assert await run(m, "messages_for") == "ok: no messages waiting"


async def test_take_back_errors_limits_and_old_ones(rig) -> None:
    m, t = rig
    await run(m, "leave_message", to="Dad", text="I'll be late")
    assert await run(m, "cancel_message", id=1) == "ok: message #1 for Dad taken back"
    assert (await run(m, "cancel_message", id=1)).startswith("error: no message #1")
    assert (await run(m, "leave_message", to="", text="hi")).startswith("error: say who")
    assert (await run(m, "leave_message", to="Dad", text="  ")).startswith("error: give the message")
    assert (await run(m, "message_given", ids=[])).startswith("error:")
    for i in range(MAX_WAITING):
        await run(m, "leave_message", to=f"Person {i}", text="hello")
    assert (await run(m, "leave_message", to="Dad", text="one more")).startswith("error:")
    note = m.context_note()
    assert note.count("#") == 6 and note.endswith(f"(and {MAX_WAITING - 6} more: messages_for).")
    t[0] += 15 * 86400  # never given: dropped after two weeks
    assert m.waiting() == []
    m.ctx.config.skills.messages = False
    assert not m.available() and (await run(m, "messages_for")).startswith("error:")
