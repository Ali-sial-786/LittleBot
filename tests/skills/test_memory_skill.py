"""The Memory skill (littlebot/skills/memory.py): remember, recall, forget, the facts in the prompt, safety rules."""

from __future__ import annotations

from typing import Any

import pytest

from littlebot.skills import memory as M
from littlebot.skills.hub import SkillHub
from littlebot.skills.memory import Memory, clean_about, clean_fact, norm, tokens
from littlebot.testing import make_test_context


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        self.t += 1.0  # every fact a second after the last: "most recent" is well defined
        return self.t


@pytest.fixture
def mem():
    ctx = make_test_context()
    yield Memory(ctx, now=Clock())
    ctx.db.close()


async def run(m: Memory, name: str, **args: Any) -> str:
    return await m.run(name, args)


async def test_remember_and_the_prompt(mem: Memory) -> None:
    assert await run(mem, "remember", fact="Remember that Ali takes his coffee black.") == \
        "ok: remembered: Ali takes his coffee black"
    assert await run(mem, "remember", fact="birthday is March 3", about="amara") == \
        "ok: remembered: Birthday is March 3"
    assert await run(mem, "remember", fact="Amara's mom is allergic to peanuts", about="Amara's mom") == \
        "ok: remembered: Amara's mom is allergic to peanuts"
    assert mem.context_note() == ("Things you know about the family:\n"
                                  "- Amara's mom is allergic to peanuts\n"
                                  "- Amara: birthday is March 3\n"
                                  "- Ali takes his coffee black")
    assert mem.facts(2) == ["Amara's mom is allergic to peanuts", "Amara: birthday is March 3"]
    assert mem.status()["facts"] == 3


async def test_the_prompt_keeps_the_most_recent_facts_only() -> None:
    ctx = make_test_context(skills={"memory_prompt_facts": 2})
    m = Memory(ctx, now=Clock())
    for f in ("Ali likes jazz", "Ali runs on Sundays", "The car is a blue Honda"):
        await run(m, "remember", fact=f)
    assert m.context_note().splitlines()[1:] == ["- The car is a blue Honda", "- Ali runs on Sundays"]
    ctx.config.skills.memory_prompt_facts = 0
    assert m.context_note() == ""


async def test_saying_it_again_refreshes_and_rewording_replaces(mem: Memory) -> None:
    await run(mem, "remember", fact="Ali loves sushi")
    await run(mem, "remember", fact="The wifi router is in the hall closet")
    assert await run(mem, "remember", fact="ali LOVES sushi!") == "ok: already knew that; kept: Ali LOVES sushi"
    assert mem.facts()[0] == "Ali LOVES sushi"  # most recent again, one copy
    out = await run(mem, "remember", fact="Ali likes sushi")  # likes / loves: the same fact
    assert out.startswith("ok: already knew that")
    out = await run(mem, "remember", fact="Ali's favourite food is sushi")
    assert out == "ok: remembered: Ali's favourite food is sushi"
    out = await run(mem, "remember", fact="Ali's favorite food is ramen")  # one value changed: an update
    assert out == "ok: updated: Ali's favorite food is ramen (was: Ali's favourite food is sushi)"
    out = await run(mem, "remember", fact="Amara's favorite food is bananas")  # another person: a new fact
    assert out.startswith("ok: remembered:")
    assert len(mem.facts()) == 4


async def test_replaces_drops_the_old_fact(mem: Memory) -> None:
    await run(mem, "remember", fact="Ali works at the hospital")
    out = await run(mem, "remember", fact="Ali now works from home", replaces="works at the hospital")
    assert out == "ok: remembered: Ali now works from home (forgot the old one: Ali works at the hospital)"
    assert mem.facts() == ["Ali now works from home"]


@pytest.mark.parametrize("fact, words", [
    ("", "short sentence"),
    ("hi", "short sentence"),
    (None, "short sentence"),
    (["a list"], "short sentence"),
    ("Ali's wifi password is hunter2", "passwords"),
    ("my PIN is 4321", "passwords"),
    ("Ali's credit card number is 4111 1111 1111 1111", "passwords"),
    ("the account is 123456789012345", "passwords"),
    ("Remember what Ali looks like on the camera", "camera"),
    ("You saw a red car through the camera", "camera"),
    ("Keep this photo of Amara", "camera"),
])
async def test_what_is_never_kept(mem: Memory, fact: Any, words: str) -> None:
    out = await run(mem, "remember", fact=fact)
    assert out.startswith("error:") and words in out
    assert mem.facts() == []


async def test_recall_by_keyword_best_then_newest(mem: Memory) -> None:
    for f, about in (("Amara's birthday is March 3", ""), ("Ali's mother lives in Lahore", ""),
                     ("Ali's birthday is June 12", ""), ("prefers oat milk in tea", "Amara's mom"),
                     ("The spare key is under the blue pot", "")):
        await run(mem, "remember", fact=f, about=about)
    out = await run(mem, "recall", query="birthdays")
    assert out == "ok: 2 found: #3 Ali's birthday is June 12; #1 Amara's birthday is March 3"
    assert await run(mem, "recall", query="When is Amara's bday?") == "ok: 1 found: #1 Amara's birthday is March 3"
    assert await run(mem, "recall", query="mom") == ("ok: 2 found: #4 Amara's mom: prefers oat milk in tea; "
                                                     "#2 Ali's mother lives in Lahore")
    assert await run(mem, "recall", query="favorite color") == "ok: nothing remembered about 'favorite color'"
    assert (await run(mem, "recall", query="spare keys")).endswith("#5 The spare key is under the blue pot")
    assert (await run(mem, "recall", query="lahor")).endswith("Ali's mother lives in Lahore")  # a typo
    out = await run(mem, "recall")
    assert out.startswith("ok: most recent: #5 The spare key")
    assert (await run(mem, "recall", query="everything")).startswith("ok: most recent:")


async def test_forget_one_several_and_everything(mem: Memory) -> None:
    for f in ("Ali takes his coffee black", "Amara's mom takes her coffee with oat milk", "Ali likes jazz",
              "Amara naps at 1 pm"):
        await run(mem, "remember", fact=f)
    out = await run(mem, "forget", query="coffee")
    assert out.startswith("ok: 2 facts match: #2 Amara's mom takes her coffee") and "Ask which one" in out
    assert len(mem.facts()) == 4
    assert await run(mem, "forget", id=1) == "ok: forgot: Ali takes his coffee black"
    assert await run(mem, "forget", query="coffee") == "ok: forgot: Amara's mom takes her coffee with oat milk"
    assert await run(mem, "forget", query="#3") == "ok: forgot: Ali likes jazz"
    assert await run(mem, "forget", query="the weather") == "ok: nothing remembered matches 'the weather'"
    assert (await run(mem, "forget", id=42)).startswith("error: there is no fact #42")
    assert (await run(mem, "forget", id="x")).startswith("error:")
    assert (await run(mem, "forget")).startswith("error:")
    out = await run(mem, "forget", query="everything")
    assert out.startswith("ok: that would forget all 1 facts") and len(mem.facts()) == 1
    assert await run(mem, "forget", query="everything", all=True) == "ok: forgot all 1 facts"
    assert mem.facts() == [] and await run(mem, "forget", query="all") == "ok: nothing remembered to forget"


async def test_forget_the_exact_words_or_all_that_match(mem: Memory) -> None:
    for f in ("Ali plays football on Saturdays", "Ali plays the guitar", "Amara plays with blocks"):
        await run(mem, "remember", fact=f)
    assert await run(mem, "forget", query="Ali plays the guitar") == "ok: forgot: Ali plays the guitar"
    out = await run(mem, "forget", query="plays", all=True)
    assert out == "ok: forgot: Amara plays with blocks; Ali plays football on Saturdays"
    assert mem.facts() == []


async def test_list_memories_and_limits(mem: Memory, monkeypatch: pytest.MonkeyPatch) -> None:
    assert await run(mem, "list_memories") == "ok: nothing remembered yet"
    assert await run(mem, "recall") == "ok: nothing remembered yet"
    monkeypatch.setattr(M, "MAX_FACTS", 3)
    for f in ("Fact number one is here", "Fact number two is here", "Ali likes jazz", "Amara likes ducks"):
        await run(mem, "remember", fact=f)
    out = await run(mem, "list_memories")
    assert out == "ok: 3 facts: #4 Amara likes ducks; #3 Ali likes jazz; #2 Fact number two is here"
    long = "Ali " + "very " * 100 + "much likes long facts"
    await run(mem, "remember", fact=long)
    assert len(mem.facts()[0]) <= M.MAX_FACT


async def test_the_old_profile_notes_come_in_once() -> None:
    ctx = make_test_context()
    prof = dict(ctx.settings.get("profile"))
    prof["remembered"] = ["Ali is Amara's dad", "Ali's password is x", "Amara loves ducks"]
    ctx.settings.set("profile", prof)
    m = Memory(ctx, now=Clock())
    assert sorted(m.facts()) == ["Ali is Amara's dad", "Amara loves ducks"]
    await run(m, "forget", query="ducks")
    assert m.facts() == ["Ali is Amara's dad"]
    assert Memory(ctx, now=Clock()).facts() == ["Ali is Amara's dad"]  # not copied again


async def test_switched_off_and_the_hub() -> None:
    ctx = make_test_context(skills={"memory": False})
    m = Memory(ctx)
    assert SkillHub(ctx, [m]).specs() == [] and m.context_note() == ""
    assert (await run(m, "remember", fact="Ali likes jazz")).startswith("error: memory is switched off")
    ctx = make_test_context()
    hub = SkillHub(ctx, [Memory(ctx)])
    assert [s["name"] for s in hub.specs()] == ["remember", "recall", "forget", "list_memories"]
    assert "remember" in hub.prompt_notes() and "chit-chat" in hub.prompt_notes()
    assert (await hub.run("remember", '{"fact": "Ali likes jazz", "about": "Ali"}')) == "ok: remembered: Ali likes jazz"
    assert hub.context_notes() == "Things you know about the family:\n- Ali likes jazz"
    assert (await hub.run("nope_tool", "{}")).startswith("error:")
    assert (await Memory(ctx).run("nope", {})).startswith("error: memory has no tool")


def test_helpers() -> None:
    assert clean_fact('  "please remember that the bins go out Tuesday."  ') == "The bins go out Tuesday"
    assert clean_fact({"x": 1}) == "" and clean_fact(True) == ""
    assert clean_about("  amara ") == "Amara" and clean_about("the user") == "" and clean_about(3) == "3"
    assert clean_about(None) == "" and clean_about(["Ali"]) == ""
    assert norm("Ali's favourite colour is BLUE!") == "ali favorite color is blue"
    assert tokens("Amara's birthdays are fun") == {"amara", "birthday", "fun"}
    assert tokens("What does Mum like?") == {"mom"}
