"""Routines made by voice (littlebot/skills/routines.py): create, list, delete, run (steps handed back to the
brain), the trigger phrases in the context note, and match_phrase for the assistant."""

from __future__ import annotations

import json

import pytest

from littlebot.skills.hub import SkillHub
from littlebot.skills.routines import MAX_ROUTINES, MAX_STEPS, Routines, key, trimmed
from littlebot.testing import make_test_context

GOOD_NIGHT = {"name": "Good night", "trigger_phrase": "good night",
              "steps": ["turn off the living room lights", "set an alarm for 7 am", "tell me tomorrow's weather"]}


@pytest.fixture
def ctx():
    c = make_test_context()
    yield c
    c.db.close()


@pytest.fixture
def r(ctx):
    return Routines(ctx, now=lambda: 1000.0)


async def test_create_list_run_delete(r, ctx):
    assert r.context_note() == "" and r.match_phrase("good night") is None
    out = await r.run("create_routine", GOOD_NIGHT)
    assert out == "ok: saved the routine 'Good night' (3 steps); it runs when the user says 'good night'"
    assert r.context_note() == "Routines: when the user says 'good night' -> run_routine('Good night')."
    listed = json.loads(await r.run("list_routines", {}))
    assert listed == [{"name": "Good night", "say": "good night", "steps": GOOD_NIGHT["steps"]}]
    run = await r.run("run_routine", {"name": "good night"})
    assert run.startswith("ok: routine 'Good night': run these now, in order, with your tools, then confirm briefly")
    assert run.endswith("1. turn off the living room lights. 2. set an alarm for 7 am. 3. tell me tomorrow's "
                        "weather.")
    assert "still asks first" in run
    row = ctx.db.query_one("SELECT run_count, last_run_at FROM routines")
    assert row == {"run_count": 1, "last_run_at": 1000.0}
    assert await r.run("delete_routine", {"name": "the good night routine"}) == "ok: deleted the routine 'Good night'"
    assert r.context_note() == "" and (await r.run("list_routines", {})).startswith("ok: no routines yet")


async def test_routines_persist_in_the_database(ctx):
    await Routines(ctx).run("create_routine", GOOD_NIGHT)
    again = Routines(ctx)  # e.g. after a restart
    assert again.match_phrase("Good night!") == "Good night" and "good night" in again.context_note()


async def test_saving_a_name_again_replaces_it(r):
    await r.run("create_routine", GOOD_NIGHT)
    out = await r.run("create_routine", {"name": "good night", "trigger_phrase": "lights out",
                                         "steps": ["turn off all the lights"]})
    assert out.startswith("ok: updated the routine 'good night' (1 step)")
    assert r.match_phrase("lights out") == "good night" and r.match_phrase("good night") == "good night"
    assert len(json.loads(await r.run("list_routines", {}))) == 1


async def test_a_phrase_starts_only_one_routine(r):
    await r.run("create_routine", GOOD_NIGHT)
    out = await r.run("create_routine", {"name": "Bedtime", "trigger_phrase": "Good night!", "steps": ["x"]})
    assert out.startswith("error: 'Good night!' already starts the routine 'Good night'")
    out = await r.run("create_routine", {"name": "good morning", "trigger_phrase": "good night", "steps": ["x"]})
    assert out.startswith("error:")


async def test_validation(r):
    for args in ({}, {"name": "x"}, {"name": "x", "steps": []}, {"name": "x", "steps": [None, "", {}]},
                 {"name": "!!!", "steps": ["a"]}, {"name": "x", "steps": ["a"] * (MAX_STEPS + 1)},
                 {"name": "x", "steps": "  "}):
        assert (await r.run("create_routine", args)).startswith("error:"), args
    # steps as one string are split; the phrase defaults to the name
    out = await r.run("create_routine", {"name": "Movie time", "steps": "dim the lights; then turn on the TV"})
    assert out == "ok: saved the routine 'Movie time' (2 steps); it runs when the user says 'Movie time'"
    assert json.loads(await r.run("list_routines", {}))[0]["steps"] == ["dim the lights", "turn on the TV"]
    long = await r.run("create_routine", {"name": "n" * 200, "trigger_phrase": "p" * 200, "steps": ["s" * 500]})
    assert long.startswith("ok:")
    saved = json.loads(await r.run("list_routines", {}))[-1]
    assert len(saved["name"]) == 60 and len(saved["say"]) == 80 and len(saved["steps"][0]) == 200
    assert (await r.run("run_routine", {"name": ""})).startswith("error: say which routine")
    assert (await r.run("delete_routine", {})).startswith("error: say which routine")
    missing = await r.run("run_routine", {"name": "dance party"})
    assert missing.startswith("error: there is no routine called 'dance party'; the routines are: Movie time")


async def test_at_most_max_routines(r):
    for i in range(MAX_ROUTINES):
        assert (await r.run("create_routine", {"name": f"r{i}", "steps": ["x"]})).startswith("ok:")
    assert (await r.run("create_routine", {"name": "one more", "steps": ["x"]})).startswith("error: there are already")
    note = r.context_note()
    assert note.count("run_routine") == 15 and "(+15 more: list_routines)" in note


async def test_match_phrase(r):
    await r.run("create_routine", GOOD_NIGHT)
    await r.run("create_routine", {"name": "Bedtime story", "trigger_phrase": "story time", "steps": ["tell a story"]})
    for heard in ("good night", "Good night!", "hey LittleBot, good night", "good night littlebot", "ok good night "
                  "please", "goodnight"):
        assert r.match_phrase(heard) == "Good night", heard
    assert r.match_phrase("run my bedtime story routine") == "Bedtime story"
    assert r.match_phrase("start the bedtime story") == "Bedtime story"
    assert r.match_phrase("story time") == "Bedtime story"
    for heard in ("good night moon is a book", "what's a good night routine for kids", "", "night", "play jazz"):
        assert r.match_phrase(heard) is None, heard
    assert (await r.run("run_routine", {"name": "hey littlebot good night"})).startswith("ok: routine 'Good night'")


async def test_switched_off_or_broken_database():
    ctx = make_test_context(skills={"routines": False})
    try:
        r = Routines(ctx)
        await r.run("create_routine", GOOD_NIGHT)
        assert not r.available() and r.match_phrase("good night") is None
        assert SkillHub(ctx, [r]).context_notes() == ""
    finally:
        ctx.db.close()

    class BrokenDB:
        def executescript(self, *_a):
            raise RuntimeError("disk on fire")

    class Ctx:
        db = BrokenDB()
        config = ctx.config

    broken = Routines(Ctx())
    assert not broken.available()
    assert (await broken.run("list_routines", {})).startswith("error: routines aren't available")


async def test_through_the_hub(ctx):
    r = Routines(ctx)
    hub = SkillHub(ctx, [r])
    names = [s["name"] for s in hub.specs()]
    assert names == ["create_routine", "list_routines", "delete_routine", "run_routine"]
    steps = next(s for s in hub.specs() if s["name"] == "create_routine")["parameters"]["properties"]["steps"]
    assert steps["type"] == "array" and steps["maxItems"] == MAX_STEPS
    assert (await hub.run("create_routine", json.dumps(GOOD_NIGHT))).startswith("ok:")
    assert "run_routine" in hub.prompt_notes() and "good night" in hub.context_notes()


def test_keys():
    assert key("Good Night!") == "good night" and key("It's  time") == "its time"
    assert trimmed("Hey LittleBot, good night please") == "good night" and trimmed("please") == ""
