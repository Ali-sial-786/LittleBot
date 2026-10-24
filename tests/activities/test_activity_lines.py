"""The fixed lines the activities speak (littlebot.activities.lines): what gets recorded in the ChatGPT voice."""

from __future__ import annotations

import random

import pytest

from activity_helpers import activity_yaml, auto_answer, shared_data
from littlebot.activities.engine import ActivityEngine, profile_values
from littlebot.activities.lines import spoken_lines
from littlebot.activities.model import ActivityLibrary
from littlebot.core.paths import ACTIVITY_SCRIPTS_DIR
from littlebot.testing import make_test_context


@pytest.fixture(scope="module")
def library() -> ActivityLibrary:
    lib = ActivityLibrary([ACTIVITY_SCRIPTS_DIR])
    assert lib.load() == []
    return lib


def texts(lines) -> list[str]:
    return [x.text for x in lines]


def test_the_built_in_activities_lines(library):
    lines = spoken_lines(library, profile_values({"nickname": "Amara"}, "LittleBot"))
    by_act = {}
    for x in lines:
        by_act.setdefault(x.activity, []).append(x.text)
    assert by_act["again_again"] == ["Tap again, and I'll do my trick!", "Boing!", "All done! That was fun!",
                                     "Boing! Bye-bye for now!", "Boing, boing! What a fun game!"]
    assert "Hi Amara! Let's play peekaboo!" in by_act["peekaboo"]  # the child's name, from the profile
    assert "Where did LittleBot go?" in by_act["peekaboo"]  # every choice of a list, the robot's name
    animals = [a["name"] for a in shared_data()["animals"]]
    assert all(f"The {a} says" in " ".join(by_act["animal_sounds"]) for a in animals)  # one line per animal
    assert "The cow says moo! Moo!" in by_act["animal_sounds"]
    assert "Twinkle, twinkle, little star," in by_act["song_twinkle_twinkle"]  # sung words
    named = [x for x in lines if x.uses_name]
    assert named and all("Amara" in x.text or x.activity == "song_pat_a_cake" for x in named)
    assert all(len(said) == len(set(said)) for said in by_act.values())  # (activities share some lines)
    assert all("{" not in t and "  " not in t for t in texts(lines))


def test_without_a_nickname_the_fallback_is_said(library):
    lines = spoken_lines(library, profile_values({}, "LittleBot"), only=["peekaboo"])
    assert "Hi friend! Let's play peekaboo!" in texts(lines) and not any(x.uses_name for x in lines)


def test_placeholders_follow_the_script(library):
    yaml = activity_yaml("""
  - say: "Hi {child_name|little one}! Round {round}."
  - repeat: 2
    steps:
      - say: "Round {round}!"
  - pick: {var: animal, from: animals}
  - say: "{animal.sound}! {animal.sound}!"
  - pick: {var: two, from: animals, count: 2}
  - say: "Remember {two}?"
  - listen: 3
    store_as: heard
    on_heard:
      - say: "You said {heard}!"
      - say: "Lovely!"
  - prompt: "Where? (not spoken)"
    options: ["Park", "Beach"]
    branches:
      "Park": [{set: {place: park}}]
      "Beach": [{set: {place: "sandy {child_name} beach"}}]
  - say: "To the {place}!"
  - note: "A tip for you (not spoken)"
  - song: Hum
    style: play_only
    lines: [{words: "Hum hum", notes: "C4"}]
  - song: Sing
    lines: [{words: "La la {child_name}", notes: "C4"}]
""")
    lib = ActivityLibrary.from_yaml(yaml, shared_data=shared_data())
    lines = spoken_lines(lib, profile_values({"nickname": "Amara"}, "LittleBot"))
    said = texts(lines)
    assert said[:3] == ["Hi Amara! Round 1.", "Round 1!", "Round 2!"]
    assert "Moo! Moo!" in said and "Quack! Quack!" in said
    assert not any(t.startswith("Remember") or t.startswith("You said") for t in said)  # not known in advance
    assert "Lovely!" in said
    assert "To the park!" in said and "To the sandy Amara beach!" in said
    assert "To the !" not in said and not any("not spoken" in t for t in said)
    assert "Hum hum" not in said and "La la Amara" in said
    assert {x.text for x in lines if x.uses_name} == {"Hi Amara! Round 1.", "To the sandy Amara beach!", "La la Amara"}


def test_too_many_combinations_stay_text_to_speech():
    yaml = activity_yaml("""
  - pick: {var: a, from: animals}
  - pick: {var: b, from: animals}
  - say: "{a.name} and {b.name}"
  - say: "Just {a.name}"
""")
    lib = ActivityLibrary.from_yaml(yaml, shared_data=shared_data())
    said = texts(spoken_lines(lib, profile_values({}, "LittleBot"), max_variants=40))
    assert not any(" and " in t for t in said) and "Just cow" in said


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_everything_the_engine_says_is_a_known_line(library, seed):
    """Run every built-in activity with random answers: each sentence it speaks (bar the ones that
    can't be known in advance) is one of the lines worked out from the scripts. (Skipping a prompt
    can leave a value unset: "at the  until..." - such a line isn't worth recording.)"""
    ctx = make_test_context()
    try:
        ctx.settings.set("profile", {**ctx.settings.get("profile"), "nickname": "Amara",
                                     "favourite_animals": ["ducks"]})
        known = set(texts(spoken_lines(library, profile_values(ctx.settings.get("profile"), "LittleBot"))))
        engine = ActivityEngine(ctx, library, seed=seed)
        ctx.activities = engine
        rng = random.Random(seed)
        auto_answer(ctx, lambda options: rng.choice(options + [None]))  # None: no answer (its timeout)
        unknown_ok = {"memory_game", "who_says_that", "question_time", "animal_sounds", "first_words"}
        for act in library.all():
            before = len(ctx.audio.spoken)
            await engine.start(act.id)
            assert await engine.wait_until_finished(10)
            spoken = ctx.audio.spoken[before:]
            missing = [t for t in spoken if t not in known]
            if act.id in unknown_ok:  # these say what the child said, or several animals at once
                continue
            assert missing == [], (act.id, missing)
        assert len(ctx.audio.spoken) > 300
    finally:
        ctx.db.close()
