"""The shipped activity scripts: they load, use only valid vocabulary, are age-banded
sensibly, read kindly, and every one runs end to end with fakes without crashing or hanging."""

from __future__ import annotations

import asyncio
import re

import pytest

from activity_helpers import auto_answer
from littlebot.activities import create_service
from littlebot.activities import model as m
from littlebot.activities.model import ActivityLibrary, load_directories
from littlebot.core.arbiter import Resource
from littlebot.core.events import Topics
from littlebot.core.interfaces import ACTIVITY_CATEGORIES, AGE_GROUPS, EXPRESSIONS, FACE_ANIMATIONS, SOUND_NAMES
from littlebot.core.paths import ACTIVITY_SCRIPTS_DIR
from littlebot.core.state import RobotState
from littlebot.testing import make_test_context

REPORT = load_directories([ACTIVITY_SCRIPTS_DIR])
ACTIVITIES = REPORT.activities
IDS = sorted(ACTIVITIES)

GESTURES = set(m.GESTURES)

REQUIRED_IDS = {
    # baby
    "peekaboo", "clapping_game", "wave_hello", "bye_bye", "animal_sounds", "first_words", "music_turns",
    "copy_me", "hide_the_toy", "again_again", "touch_and_feel",
    # songs (traditional / public domain)
    "song_twinkle_twinkle", "song_row_your_boat", "song_old_macdonald", "song_wheels_on_the_bus",
    "song_if_youre_happy", "song_itsy_bitsy_spider", "song_baa_baa_black_sheep", "song_head_shoulders",
    "song_pat_a_cake", "song_abc",
    # toddler / preschool
    "colour_hunt", "shape_spotting", "count_to_three", "count_to_ten", "letter_sounds", "who_says_that",
    "body_parts", "matching_pairs", "memory_game", "freeze_dance", "animal_moves", "dance_party",
    "story_pip_the_puppy", "feelings_faces", "teddy_problem", "opposites", "i_spy", "question_time",
    "tea_party", "zoo_keeper",
}

PUBLIC_DOMAIN_SONGS = {
    "Twinkle, Twinkle, Little Star", "Twinkle, Twinkle (lullaby)", "Row, Row, Row Your Boat", "Old MacDonald",
    "The Wheels on the Bus", "If You're Happy and You Know It", "Itsy Bitsy Spider", "Baa, Baa, Black Sheep",
    "Head, Shoulders, Knees and Toes", "Pat-a-Cake", "The Alphabet Song", "Tune",
}

# Words that would turn play into judgement or diagnosis. LittleBot never says them.
BANNED = re.compile(r"\b(wrong|incorrect|fail(ed|ure)?|score[sd]?|(earn|get|got|win|won|\d+) points?|bad job|try harder|delay(ed)?|falling behind|"
                    r"normal for|should be able|test(ed|ing)?|assessment|diagnos\w*)\b", re.I)


def all_texts(act: m.Activity) -> list[str]:
    texts = [act.name, act.description]
    for _path, step in act.all_steps():
        texts.extend(step.templates())
    return texts


# ----------------------------------------------------------------- loading
def test_every_script_file_loads_without_errors():
    assert REPORT.errors == []
    files = {p.split("/")[-1] for p in REPORT.files}
    assert "_data.yaml" in files and len(files) >= 8
    lib = ActivityLibrary([ACTIVITY_SCRIPTS_DIR])
    lib.load(strict=True)  # raises on any problem
    assert len(lib) == len(ACTIVITIES)


def test_enough_activities_and_the_required_ones_exist():
    assert len(ACTIVITIES) >= 25
    missing = REQUIRED_IDS - set(ACTIVITIES)
    assert not missing, f"missing activities: {sorted(missing)}"


def test_every_age_group_has_plenty_to_do():
    for _key, lo, hi, label in AGE_GROUPS:
        n = sum(1 for a in ACTIVITIES.values() if a.min_age_months < hi and lo < a.max_age_months)
        assert n >= 10, f"only {n} activities for {label}"
    for age in (6, 10, 14, 20, 30, 42, 59, 66):
        assert sum(1 for a in ACTIVITIES.values() if a.suitable_for(age)) >= 8, age


def test_gesture_vocabulary_matches_the_motion_contract():
    from littlebot.testing import FakeMotion

    assert {"wave", "look_left", "look_right", "raise_arms", "celebrate", "dance", "neutral"} <= GESTURES
    assert GESTURES <= set(FakeMotion().gestures())


def test_categories_are_valid_and_varied():
    used = {a.category for a in ACTIVITIES.values()}
    assert used <= set(ACTIVITY_CATEGORIES)
    assert len(used) >= 9


# ----------------------------------------------------------------- metadata quality
@pytest.mark.parametrize("activity_id", IDS)
def test_metadata_is_complete(activity_id):
    a = ACTIVITIES[activity_id]
    assert len(a.parent_instructions) >= 80, "parent instructions should really help the caregiver"
    assert a.safety_notes and all(len(n) >= 10 for n in a.safety_notes)
    assert a.participation in m.PARTICIPATION
    assert 30 <= a.duration_seconds <= 600
    assert 0 <= a.min_age_months < a.max_age_months <= 72
    assert a.age_groups(), "range must overlap at least one age group"
    assert len(a.description) <= 140, "keep descriptions short for the dashboard"


@pytest.mark.parametrize("activity_id", IDS)
def test_only_valid_vocabulary(activity_id):
    a = ACTIVITIES[activity_id]
    for path, step in a.all_steps():
        where = f"{activity_id} {path}"
        if isinstance(step, m.SayStep):
            assert step.expression in (None, *EXPRESSIONS), where
            assert step.animation in (None, *FACE_ANIMATIONS), where
        elif isinstance(step, m.ExpressionStep):
            assert step.name in EXPRESSIONS, where
        elif isinstance(step, m.AnimationStep):
            assert step.name in FACE_ANIMATIONS, where
        elif isinstance(step, m.SoundStep):
            assert step.name in SOUND_NAMES, where
        elif isinstance(step, m.GestureStep):
            assert step.name in GESTURES, where
        elif isinstance(step, (m.MelodyStep, m.SongStep)):
            notes = step.notes if isinstance(step, m.MelodyStep) else [n for ln in step.lines for n in ln.notes]
            tempo = step.tempo
            assert notes or isinstance(step, m.SongStep), where
            assert m.melody_seconds(notes, tempo) < 90, f"{where}: melody too long"
            for note, _beats in notes:
                assert note == "R" or 2 <= int(note[-1]) <= 6, f"{where}: {note} is outside a comfortable range"
    assert a.finish_expression in EXPRESSIONS


@pytest.mark.parametrize("activity_id", IDS)
def test_language_is_kind_and_never_judging(activity_id):
    for text in all_texts(ACTIVITIES[activity_id]):
        assert not BANNED.search(text), f"{activity_id}: {text!r}"
    for note in ACTIVITIES[activity_id].safety_notes + [ACTIVITIES[activity_id].parent_instructions]:
        assert not re.search(r"\b(score|diagnos\w*|assessment)\b", note, re.I), note


@pytest.mark.parametrize("activity_id", IDS)
def test_spoken_lines_are_short(activity_id):
    for _path, step in ACTIVITIES[activity_id].all_steps():
        if isinstance(step, m.SayStep):
            for t in step.templates():
                assert len(t.split()) <= 26, f"{activity_id}: long sentence {t!r}"
        if isinstance(step, m.PromptStep):
            assert all(len(o) <= 24 for o in step.options), f"{activity_id}: long button label"


def test_songs_are_traditional_public_domain():
    titles = {s.title for a in ACTIVITIES.values() for _p, s in a.all_steps() if isinstance(s, m.SongStep)}
    assert titles <= PUBLIC_DOMAIN_SONGS, titles - PUBLIC_DOMAIN_SONGS
    songs = [a for a in ACTIVITIES.values() if a.id.startswith("song_")]
    assert len(songs) >= 9
    for a in songs:
        assert any(isinstance(s, m.SongStep) and all(ln.words and ln.notes for ln in s.lines)
                   for _p, s in a.all_steps()), f"{a.id}: songs need words AND notes on every line"


def test_baby_activities_lean_on_caregivers_and_are_gentle():
    babies = [a for a in ACTIVITIES.values() if a.min_age_months <= 9]
    assert len(babies) >= 10
    for a in babies:
        assert a.participation in ("required", "recommended"), a.id
        assert not any(isinstance(s, m.ListenStep) and not (s.on_timeout or s.on_heard)
                       for _p, s in a.all_steps()), a.id


def test_movement_activities_warn_about_space():
    for a in ACTIVITIES.values():
        if a.category == "movement" or "movement" in a.tags or "dance" in a.tags:
            notes = " ".join(a.safety_notes).lower()
            assert any(w in notes for w in ("space", "clear", "gently", "gentle", "supervise", "stay with")), a.id


# ----------------------------------------------------------------- run everything end to end
STRATEGIES = {
    "first": lambda options: options[0],
    "last": lambda options: options[-1],
    "skip": lambda options: "skip",
    "silent": lambda options: None,  # nobody answers: every prompt times out
}


async def _run_activity(activity_id: str, strategy: str, *, seed: int, devices: bool = True,
                        utterance: str | None = None) -> tuple[dict, object]:
    ctx = make_test_context()
    try:
        if not devices:
            ctx.face = ctx.audio = ctx.motion = None
        ctx.settings.set("vision.gestures_enabled", True)
        engine = create_service(ctx)
        engine._seed = seed
        ctx.activities = engine
        prompts = auto_answer(ctx, STRATEGIES[strategy])
        if utterance and ctx.audio is not None:
            for _ in range(10):
                ctx.audio.inject_utterance(utterance)
        finished: list[dict] = []
        ctx.bus.subscribe(Topics.ACTIVITY_FINISHED, lambda e: finished.append(dict(e.data)))
        await engine.start()
        await engine.start(activity_id, source="test")
        assert await asyncio.wait_for(engine.wait_until_finished(), 10), f"{activity_id} hung"
        assert len(finished) == 1, finished
        assert finished[0]["completed"] is True, finished[0]
        assert ctx.state.state == RobotState.IDLE
        assert ctx.arbiter.is_free(Resource.FACE) and ctx.arbiter.is_free(Resource.AUDIO_OUT)
        assert engine.status()["running"] is False and engine.status()["prompt"] is None
        if devices:
            assert not ctx.face.eyes_closed
            spoken = ctx.audio.spoken
            assert spoken, "an activity should say something"
            assert all("{" not in t and "}" not in t for t in spoken), spoken
            assert all(t.strip() for t in spoken)
        return {"prompts": prompts, "spoken": list(ctx.audio.spoken) if devices else []}, engine
    finally:
        ctx.db.close()


@pytest.mark.parametrize("strategy", sorted(STRATEGIES))
@pytest.mark.parametrize("activity_id", IDS)
async def test_every_activity_runs_to_completion(activity_id, strategy):
    await _run_activity(activity_id, strategy, seed=7)


@pytest.mark.parametrize("activity_id", IDS)
async def test_every_activity_runs_without_devices(activity_id):
    await _run_activity(activity_id, "first", seed=3, devices=False)


@pytest.mark.parametrize("activity_id", IDS)
async def test_every_activity_runs_with_speech_heard(activity_id):
    """Listen steps hear something (a simulated utterance); other seeds vary random picks."""
    for seed in (1, 2):
        await _run_activity(activity_id, "first", seed=seed, utterance="cow moo yes")


async def test_favourite_animal_comes_first_in_animal_sounds():
    ctx = make_test_context()
    ctx.settings.set("profile", {"nickname": "Mia", "favourite_animals": ["Horses"]})
    engine = create_service(ctx)
    ctx.activities = engine
    await engine.start("animal_sounds")
    assert await engine.wait_until_finished(10)
    first_animal_line = next(t for t in ctx.audio.spoken if "says" in t)
    assert first_animal_line.startswith("The horse says neigh")
    ctx.db.close()


async def test_personalised_pat_a_cake():
    ctx = make_test_context()
    ctx.settings.set("profile", {"nickname": "Ava"})
    engine = create_service(ctx)
    ctx.activities = engine
    await engine.start("song_pat_a_cake")
    assert await engine.wait_until_finished(10)
    lines = " ".join(ctx.audio.spoken)
    assert "mark it with A" in lines and "for Ava and me" in lines
    ctx.db.close()
