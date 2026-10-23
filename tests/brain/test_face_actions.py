"""Face actions: the menu, live-voice request parsing, hidden [face: NAME] tags and playing them."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from brain_helpers import FakeAPI, anthropic_response, cloud_context

from littlebot.brain import face_actions
from littlebot.brain.face_actions import FACE_MENU, FaceAction, extract_tags, menu_text, parse_request, perform
from littlebot.brain.prompts import build_system_prompt
from littlebot.core.events import Topics, collect
from littlebot.core.interfaces import EXPRESSIONS, FACE_ANIMATIONS
from littlebot.testing import FakeFace


# --------------------------------------------------------------------- menu
def test_menu_offers_expressions_animations_and_combos():
    required = ("happy surprised sleepy curious excited sad playful celebrating affectionate confused thinking "
                "neutral wink spin_eyes peekaboo nod shake bounce wiggle sparkle hearts confetti").split()
    assert set(required) <= set(FACE_MENU)
    assert not {"listening", "speaking", "zzz", "peek"} & set(FACE_MENU)
    for name, description in FACE_MENU.items():
        assert name in EXPRESSIONS or name in FACE_ANIMATIONS or name in ("wink", "peekaboo")
        assert description and len(description) < 60
    assert menu_text() == ", ".join(FACE_MENU)
    assert "happy" in menu_text() and "spin_eyes" in menu_text() and "\n" not in menu_text()


def test_prompt_face_section_is_opt_in():
    plain = build_system_prompt(age_months=40, max_words=20)
    assert "[face:" not in plain and "Your face" not in plain
    p = build_system_prompt(age_months=40, max_words=20, face_tags=True)
    assert "Your face:" in p and "animated face on a screen" in p and "[face: happy]" in p
    assert menu_text() in p and "never spoken" in p and "at most one" in p


# ---------------------------------------------------------- parse_request
@pytest.mark.parametrize(("text", "name"), [
    ("make a happy face", "happy"),
    ("Make a happy face.", "happy"),
    ("Wink at her", "wink"),
    ("Wink!", "wink"),
    ("spin your eyes", "spin_eyes"),
    ("Hey LittleBot, spin your eyes!", "spin_eyes"),
    ("close your eyes and play peekaboo", "peekaboo"),
    ("Let's play peek-a-boo", "peekaboo"),
    ("Peekaboo!", "peekaboo"),
    ("look surprised", "surprised"),
    ("look very sleepy", "sleepy"),
    ("do a sad face", "sad"),
    ("show hearts", "hearts"),
    ("face: happy", "happy"),
    ("Face: Spin_Eyes", "spin_eyes"),
    ("[face: wink]", "wink"),
    ("Can you make a happy face?", "happy"),
    ("Please make LittleBot smile", "happy"),
    ("Tell LittleBot to wink", "wink"),
    ("Look at her and smile", "happy"),
    ("give her a big smile", "happy"),
    ("make a silly face", "playful"),
    ("blow her a kiss", "affectionate"),
    ("make an excited face", "excited"),
    ("make a curious face", "curious"),
    ("make a confused face", "confused"),
    ("look like you're thinking", "thinking"),
    ("go back to a normal face", "neutral"),
    ("celebrate!", "celebrating"),
    ("Throw some confetti", "confetti"),
    ("show sparkles", "sparkle"),
    ("nod yes", "nod"),
    ("shake your head no", "shake"),
    ("bounce", "bounce"),
    ("do a little wiggle", "wiggle"),
    ("act sleepy", "sleepy"),
    ("happy face", "happy"),
])
def test_parse_request_recognises_face_requests(text, name):
    assert parse_request(text) == FaceAction(name)


@pytest.mark.parametrize("text", [
    "what's the weather",
    "how many fingers am I holding?",
    "How many fingers am I holding up?",
    "Is the baby smiling?",
    "Does she look happy?",
    "Is she happy?",
    "she is happy",
    "Look at her and tell me if she is smiling",
    "Can you see what she's holding?",
    "Describe what you see",
    "What color is her shirt?",
    "look at the camera",
    "Look around the room",
    "Look at the picture and tell me what animal it is",
    "Tell me a joke",
    "What does a cow say?",
    "Play a happy song",
    "Sing twinkle twinkle little star",
    "Count her fingers",
    "don't make a sad face",
    "Did you wink?",
    "",
    "   ",
])
def test_parse_request_ignores_questions_and_look_ups(text):
    assert parse_request(text) is None


# ------------------------------------------------------------ extract_tags
@pytest.mark.parametrize(("text", "clean", "names"), [
    ("Moo! The cow says moo. [face: happy]", "Moo! The cow says moo.", ["happy"]),
    ("Moo! [face:happy]", "Moo!", ["happy"]),
    ("[Face: Happy] Hi there!", "Hi there!", ["happy"]),
    ("Peekaboo! [ FACE :  peekaboo ]", "Peekaboo!", ["peekaboo"]),
    ("Round and round! [face: spin_eyes]", "Round and round!", ["spin_eyes"]),
    ("Whee [face: spin eyes]!", "Whee!", ["spin_eyes"]),
    ("Hi [face: blorp] there [face: wink].", "Hi there.", ["wink"]),
    ("Yay! [face: hearts] [face: confetti]", "Yay!", ["hearts", "confetti"]),
    ("[face: spin_eyes]", "", ["spin_eyes"]),
    ("Moo! [face: hap", "Moo!", []),
    ("No tags here.", "No tags here.", []),
])
def test_extract_tags(text, clean, names):
    out, actions = extract_tags(text)
    assert out == clean
    assert [a.name for a in actions] == names
    assert "face" not in out.lower() and "[" not in out


# ----------------------------------------------------------------- perform
@pytest.fixture
def face_ctx():
    return SimpleNamespace(face=FakeFace())


def _calls(face: FakeFace) -> list[tuple]:
    return [(n, *a, *k.values()) for n, a, k in face.calls]


async def test_perform_expression_is_held(face_ctx):
    assert await perform(face_ctx, FaceAction("surprised")) is True
    assert _calls(face_ctx.face) == [("set_expression", "surprised", face_actions.EXPRESSION_HOLD)]
    assert 3.0 <= face_actions.EXPRESSION_HOLD <= 5.0 and face_ctx.face.expression == "surprised"


@pytest.mark.parametrize("name", ["spin_eyes", "hearts", "confetti", "nod", "shake", "bounce", "wiggle", "sparkle"])
async def test_perform_animation(face_ctx, name):
    assert await perform(face_ctx, FaceAction(name)) is True
    assert _calls(face_ctx.face) == [("play_animation", name)]


async def test_perform_wink_is_a_brief_playful_face(face_ctx):
    assert await perform(face_ctx, FaceAction("wink")) is True
    assert _calls(face_ctx.face) == [("set_expression", "playful", face_actions.WINK_HOLD)]
    assert 0.8 <= face_actions.WINK_HOLD <= 1.5


async def test_perform_peekaboo_closes_then_peeks(face_ctx, monkeypatch):
    assert face_actions.PEEKABOO_CLOSED < 3.0
    monkeypatch.setattr(face_actions, "PEEKABOO_CLOSED", 0.05)
    face = face_ctx.face
    task = asyncio.create_task(perform(face_ctx, FaceAction("peekaboo")))
    await asyncio.sleep(0.01)
    assert face.eyes_closed and _calls(face) == [("close_eyes",)]
    assert await task is True
    assert _calls(face) == [("close_eyes",), ("open_eyes",), ("play_animation", "peek"),
                            ("set_expression", "happy", face_actions.PEEK_HOLD)]
    assert not face.eyes_closed and face.expression == "happy"


async def test_cancelled_peekaboo_never_leaves_the_eyes_shut(face_ctx):
    face = face_ctx.face
    task = asyncio.create_task(perform(face_ctx, FaceAction("peekaboo")))
    await asyncio.sleep(0.01)
    assert face.eyes_closed
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not face.eyes_closed


async def test_perform_unknown_or_no_face_is_false(face_ctx):
    assert await perform(face_ctx, FaceAction("moonwalk")) is False
    assert await perform(face_ctx, FaceAction("listening")) is False
    assert face_ctx.face.calls == []
    assert await perform(SimpleNamespace(face=None), FaceAction("happy")) is False
    assert await perform(SimpleNamespace(), FaceAction("happy")) is False


async def test_perform_never_raises():
    class BrokenFace(FakeFace):
        def set_expression(self, name, *, hold=None):
            raise RuntimeError("render thread gone")

        def play_animation(self, name):
            raise RuntimeError("render thread gone")

        def close_eyes(self):
            super().close_eyes()
            raise RuntimeError("render thread gone")

    ctx = SimpleNamespace(face=BrokenFace())
    for name in ("happy", "wink", "hearts", "peekaboo"):
        assert await perform(ctx, FaceAction(name)) is False
    assert not ctx.face.eyes_closed  # a failed peekaboo still reopens the eyes


# ------------------------------------------------------- typed chat (engine)
def _shown(ctx) -> list[str]:
    return [a[0] for a, _k in ctx.face.called("set_expression")]


async def test_engine_strips_tag_and_shows_the_face(make_brain):
    ctx = cloud_context(age_months=40)
    events = collect(ctx.bus, [Topics.BRAIN_REPLY])
    api = FakeAPI(lambda req: anthropic_response("Moo! The cow says moo. [face: happy]"))
    brain = await make_brain(ctx, api)
    r = await brain.handle_utterance("what does the cow say")
    assert r["reply"] == "Moo! The cow says moo." and r["filtered"] is False and r["error"] is None
    assert ctx.audio.spoken == ["Moo! The cow says moo."]
    assert r["expression"] == "happy" and r["face_action"] == "happy"
    assert ctx.face.expression == "happy"
    assert ctx.face.called("set_expression")[-1] == (("happy",), {"hold": face_actions.EXPRESSION_HOLD})
    assert [e.get("text") for e in events] == ["Moo! The cow says moo."]
    system = api.body()["system"]
    assert "[face: happy]" in system and menu_text() in system


async def test_engine_requested_expression_wins_over_the_guess(make_brain):
    ctx = cloud_context(age_months=40)
    brain = await make_brain(ctx, FakeAPI(lambda req: anthropic_response("Can you moo? [face: surprised]")))
    r = await brain.handle_utterance("what does the cow say")
    assert r["reply"] == "Can you moo?" and r["expression"] == "surprised"  # the guess would be "curious"
    assert "curious" not in _shown(ctx) and _shown(ctx)[-1] == "surprised"


async def test_engine_plays_an_animation_tag(make_brain):
    ctx = cloud_context(age_months=40)
    brain = await make_brain(ctx, FakeAPI(lambda req: anthropic_response("Round and round! [face: spin_eyes]")))
    r = await brain.handle_utterance("spin your eyes")
    assert r["reply"] == "Round and round!" and r["face_action"] == "spin_eyes"
    assert ctx.face.called("play_animation") == [(("spin_eyes",), {})]
    assert ctx.audio.spoken == ["Round and round!"]


async def test_engine_tag_only_reply_is_an_empty_reply_but_still_plays(make_brain):
    ctx = cloud_context(age_months=40)
    brain = await make_brain(ctx, FakeAPI(lambda req: anthropic_response("[face: spin_eyes]")))
    r = await brain.handle_utterance("spin your eyes")
    assert r["filtered"] is True and r["reply"] and "face" not in r["reply"]  # the usual safe fallback line
    assert ctx.audio.spoken == [r["reply"]]
    assert ctx.face.called("play_animation") == [(("spin_eyes",), {})]
    assert brain.status()["context_turns"] == 0


async def test_engine_wink_and_peekaboo(make_brain, monkeypatch):
    monkeypatch.setattr(face_actions, "PEEKABOO_CLOSED", 0.01)
    ctx = cloud_context(age_months=40)
    replies = iter(["Hello friend! [face: wink]", "Where did I go? Peekaboo! [face: peekaboo]"])
    brain = await make_brain(ctx, FakeAPI(lambda req: anthropic_response(next(replies))))
    await brain.handle_utterance("wink at me")
    assert ctx.face.called("set_expression")[-1] == (("playful",), {"hold": face_actions.WINK_HOLD})
    ctx.face.calls.clear()
    r = await brain.handle_utterance("play peekaboo")
    assert r["reply"] == "Where did I go? Peekaboo!" and r["face_action"] == "peekaboo"
    names = [n for n, _a, _k in ctx.face.calls]
    assert names.index("close_eyes") < names.index("open_eyes")
    assert ctx.face.called("play_animation") == [(("peek",), {})]
    assert not ctx.face.eyes_closed and ctx.face.expression == "happy"


async def test_engine_tag_never_reaches_context_or_transcript(make_brain):
    ctx = cloud_context(age_months=40, ai={"store_transcripts": True})
    ctx.privacy.set(record_history=True)
    api = FakeAPI(lambda req: anthropic_response("Moo! The cow says moo. [face: happy]"))
    brain = await make_brain(ctx, api)
    await brain.handle_utterance("what does the cow say")
    await brain.handle_utterance("again")
    assistant = [m["content"] for m in api.body()["messages"] if m["role"] == "assistant"]
    assert assistant == ["Moo! The cow says moo."]
    rows = [r["text"] for r in ctx.db.query("SELECT text FROM brain_transcript ORDER BY id")]
    assert rows and all("face" not in t and "[" not in t for t in rows)
    assert "Moo! The cow says moo." in rows


async def test_engine_ignores_tag_on_a_filtered_reply(make_brain):
    ctx = cloud_context(age_months=40)
    ctx.config.ai.relaxed_rules = []  # every rule group on: the link reply is filtered
    brain = await make_brain(ctx, FakeAPI(lambda req: anthropic_response(
        "Visit www.example.com for fun! [face: hearts]")))
    r = await brain.handle_utterance("what can we do")
    assert r["filtered"] is True and "face_action" not in r and r["expression"] == "playful"
    assert ctx.face.called("play_animation") == []
    assert all("face" not in s for s in ctx.audio.spoken)


async def test_engine_without_face_still_speaks_clean_text(make_brain):
    ctx = cloud_context(age_months=40)
    ctx.face = None
    brain = await make_brain(ctx, FakeAPI(lambda req: anthropic_response("Yay! [face: confetti]")))
    r = await brain.handle_utterance("hooray")
    assert r["reply"] == "Yay!" and ctx.audio.spoken == ["Yay!"] and r["error"] is None
