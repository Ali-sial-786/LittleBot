"""The voice session end to end with fakes: wake -> words -> brain -> voice -> follow-up -> end."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from littlebot.assistant import events as ev
from littlebot.assistant.service import AssistantService
from littlebot.brain.providers.base import ProviderReply
from littlebot.core.state import RobotState
from littlebot.testing import FakeCallAudio, FakeSTT, FakeTTS, make_test_context


class Scripted:
    """A brain provider: answers each question with the next script (strings stream; tuples run a tool)."""

    name = "claude"
    model = "fake"

    def __init__(self, *scripts: list[Any], delay: float = 0.0) -> None:
        self.scripts = list(scripts)
        self.delay = delay
        self.questions: list[str] = []
        self.asked: list[str] = []  # the whole last message (with its context note)

    async def reply(self, messages: list[dict[str, str]], *, system: str, max_tokens: int, timeout: float,
                    tools: Any = None, run_tool: Any = None, max_tool_rounds: int = 6, effort: Any = None,
                    on_delta: Any = None, final_tools: Any = ()) -> ProviderReply:
        self.questions.append(messages[-1]["content"].split("\n")[-1])
        self.asked.append(messages[-1]["content"])
        script = self.scripts.pop(0) if self.scripts else ["Okay."]
        if self.delay:
            await asyncio.sleep(self.delay)
        for step in script:
            if isinstance(step, tuple):
                await run_tool(step[0], step[1])
            elif on_delta is not None:
                on_delta(step)
        return ProviderReply(text="".join(s for s in script if isinstance(s, str)))


def _service(provider: Any, **cfg: Any) -> tuple[AssistantService, FakeSTT, FakeTTS, FakeCallAudio]:
    assistant = {"pipeline": "cascade", "follow_up_seconds": 0.4, "first_words_seconds": 2.0, **cfg}
    ctx = make_test_context(assistant=assistant)
    ctx.privacy.set(cloud_enabled=True)
    stt, tts, audio = FakeSTT(), FakeTTS(), FakeCallAudio()
    from littlebot.assistant.agent import Brain

    svc = AssistantService(ctx, audio_factory=lambda: audio, stt_factory=lambda: stt,
                           tts_factory=lambda: tts, brain=Brain(ctx, provider=provider))
    ctx.assistant = svc
    return svc, stt, tts, audio


async def _wait(pred: Any, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


async def test_a_question_answered_then_follow_up_then_end() -> None:
    brain = Scripted(["[happy] Sunny and 12 degrees.", " Want tomorrow too"], ["Tomorrow is rainy."])
    svc, stt, tts, audio = _service(brain, follow_up_seconds=0.5)
    await svc.start()
    phases: list[str] = []
    svc.ctx.bus.subscribe(ev.PHASE, lambda e: phases.append(e.get("phase")))
    res = await svc.start_session(parent_present=True)
    assert res["ok"] and res["active"]
    await _wait(lambda: stt.stream is not None and svc.status().get("phase") == "listening")
    assert svc.ctx.state.state == RobotState.LISTENING
    stt.say("what's the weather")
    await _wait(lambda: len(tts.said) == 2)
    assert tts.said == ["Sunny and 12 degrees.", "Want tomorrow too"]
    await _wait(lambda: any(c[0] == "set_expression" and c[1][0] == "happy" for c in svc.ctx.face.calls))
    await _wait(lambda: svc.status().get("phase") == "listening" and len(svc.history) == 1)
    stt.say("and tomorrow")  # a follow-up: no wake word
    await _wait(lambda: "Tomorrow is rainy." in tts.said)
    assert brain.questions == ["what's the weather", "and tomorrow"]
    await _wait(lambda: not svc.status()["active"], timeout=3)
    assert svc.last_call["turns"] == 2 and svc.last_call["ended_reason"] == "no follow-up"
    assert audio.opened and audio.closed and stt.stream.closed
    assert {"thinking", "speaking", "listening"} <= set(phases)
    assert svc.ctx.state.state == RobotState.IDLE
    msgs = svc.history.messages()
    assert msgs[0] == {"role": "user", "content": "what's the weather"}
    await svc.stop()


async def test_the_next_turn_knows_what_the_tools_really_did() -> None:
    # The history kept only the words, so a turn later the brain said "I should correct myself: I said the volume
    # was at 60 percent, but I never actually checked it" (the robot, 2026-10-31).
    brain = Scripted([("set_volume", '{"percent": 60}'), "Done, it's at 60 percent."], ["Sure."])
    svc, stt, tts, _ = _service(brain, follow_up_seconds=0.5)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("turn the volume down a bit")
    await _wait(lambda: "Done, it's at 60 percent." in tts.said and len(svc.history) == 1)
    assert "really did" not in brain.asked[0]
    stt.say("and tell me a joke")
    await _wait(lambda: len(brain.asked) == 2)
    note = brain.asked[1].split("\n")[0]
    assert note.startswith("[Context for this turn") and "really did" in note
    assert 'after "turn the volume down a bit": set_volume:' in note
    assert [m["content"] for m in svc.history.messages()][:2] == ["turn the volume down a bit",
                                                                  "Done, it's at 60 percent."]  # words only
    await svc.stop()


def test_the_tools_note_covers_the_recent_turns_and_goes_with_them() -> None:
    from littlebot.assistant.service import DONE_TURNS, History

    clock = [0.0]
    h = History(turns=10, minutes=5, now=lambda: clock[0])
    assert h.done_note() == ""
    h.add("hi", "Hello!")
    assert h.done_note() == ""  # no tools: no note
    for i in range(DONE_TURNS + 1):
        h.add(f"q{i}", "Done.", [f"tool{i}: ok"])
    note = h.done_note()
    assert "tool0" not in note and f"tool{DONE_TURNS}: ok" in note
    clock[0] = 6 * 60
    assert h.done_note() == "" and h.messages() == []  # forgotten with the words


async def test_thanks_ends_the_session_without_the_brain() -> None:
    brain = Scripted()
    svc, stt, tts, _ = _service(brain, follow_up_seconds=5)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("thank you")
    await _wait(lambda: not svc.status()["active"])
    assert brain.questions == [] and len(tts.said) == 1
    assert svc.last_call["ended_reason"].startswith("said intent:thanks")


async def test_the_rest_of_thanks_bye_does_not_cut_the_goodbye_off() -> None:
    # "Thanks, bye" came as "Thanks." and "bought.": the second cut the goodbye off and the brain answered it
    # (the robot, 2026-10-31).
    brain = Scripted()
    svc, stt, tts, _ = _service(brain, follow_up_seconds=5)
    svc._tts = SlowTTS()
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("Thanks.")
    await asyncio.sleep(0.05)
    stt.say("bought.")
    await _wait(lambda: not svc.status()["active"])
    assert brain.questions == [] and svc.last_call["ended_reason"].startswith("said intent:thanks")


async def test_a_real_question_while_it_says_goodbye_is_answered() -> None:
    brain = Scripted(["Rain tomorrow."])
    svc, stt, tts, _ = _service(brain, follow_up_seconds=0.3)
    svc._tts = SlowTTS()
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("Thanks.")
    await asyncio.sleep(0.05)
    stt.say("and what about tomorrow")
    await _wait(lambda: bool(brain.questions))
    assert brain.questions[0].endswith("and what about tomorrow")  # (with "Thanks." if the goodbye hadn't started)
    await _wait(lambda: not svc.status()["active"])
    assert not svc.last_call["ended_reason"].startswith("said intent")


@pytest.mark.parametrize("log_words", [False, True])
async def test_the_words_are_logged_only_when_asked_for_testing(log_words: bool, caplog: Any) -> None:
    caplog.set_level(logging.INFO, logger="littlebot.assistant")
    svc, stt, _tts, _ = _service(Scripted(["Paris."]), follow_up_seconds=5, log_words=log_words)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("what is the capital of france")
    await _wait(lambda: "assistant turn:" in caplog.text)
    assert ("what is the capital of france" in caplog.text) is log_words
    assert ("said 'Paris.'" in caplog.text) is log_words
    await svc.stop_session("test")


async def test_stop_ends_quietly() -> None:
    svc, stt, tts, _ = _service(Scripted(), follow_up_seconds=5)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("never mind")
    await _wait(lambda: not svc.status()["active"])
    assert tts.said == []


async def test_more_words_while_thinking_join_the_question() -> None:
    brain = Scripted(["first"], ["Both at once."], delay=0.3)
    svc, stt, tts, _ = _service(brain, follow_up_seconds=0.3)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("what's the capital of")
    await asyncio.sleep(0.05)
    stt.say("Australia")
    await _wait(lambda: "Both at once." in tts.said or "first" in tts.said)
    assert brain.questions[-1] == "what's the capital of Australia"
    await _wait(lambda: not svc.status()["active"])


async def test_barge_in_cuts_the_answer() -> None:
    brain = Scripted(["One. ", "Two. ", "Three."], ["Okay, new topic."], delay=0.0)
    svc, stt, tts, audio = _service(brain, follow_up_seconds=0.4)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("count")
    await _wait(lambda: len(tts.said) >= 1)
    audio.interrupted = True
    audio.on_change("interrupted", {})
    stt.say("actually stop counting and change topic")
    await _wait(lambda: audio.heard_words)  # their words confirm the barge-in
    assert audio.heard_words[0][0].startswith("actually")
    audio.on_change("heard", {})
    audio.interrupted = False
    await _wait(lambda: "Okay, new topic." in tts.said)
    await _wait(lambda: not svc.status()["active"])


async def test_a_word_or_two_of_its_own_voice_heard_back_do_not_cut_it_off() -> None:
    # "One sec." came back through the microphone, the cut-in detector fired on it and its words confirmed the
    # cut-in: the weather answer was never said (the robot, 2026-10-31).
    brain = Scripted(["One sec. ", "Sunny all day."])
    svc, stt, tts, audio = _service(brain, follow_up_seconds=0.4)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("what's the weather")
    await _wait(lambda: len(tts.said) >= 1)
    audio.interrupted = True
    audio.on_change("interrupted", {})
    stt.event("partial", "One sec.")  # its own voice
    await asyncio.sleep(0.1)
    assert audio.heard_words == []
    stt.event("partial", "Stop.")  # theirs, even if the answer had said "stop"
    await _wait(lambda: audio.heard_words == [("Stop.", False)])
    session = svc._session
    assert session is not None and session._echo_of_me("sec", short=True)
    assert not session._echo_of_me("sec")  # a turn of theirs: "and tomorrow" after "... and 12 degrees" is theirs
    assert not session._echo_of_me("cloudy", short=True) and not session._echo_of_me("Yes.", short=True)
    assert not session._echo_of_me("one cloud", short=True)
    await svc.stop_session("test")


async def test_a_takeover_ends_the_session_and_runs_after() -> None:
    brain = Scripted(["Going to sleep now.", ("go_to_sleep", "{}")])
    svc, stt, tts, _ = _service(brain, follow_up_seconds=5)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("go to sleep for a bit")
    await _wait(lambda: not svc.status()["active"])
    assert svc.last_call["ended_reason"].startswith("handing over")
    await _wait(lambda: svc.ctx.state.state == RobotState.SLEEPING)


async def test_no_words_after_the_wake_word() -> None:
    svc, stt, _, _ = _service(Scripted(), first_words_seconds=2.0)
    clock = [0.0]
    svc.now = lambda: clock[0]
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    clock[0] = 3.0
    await _wait(lambda: not svc.status()["active"])
    assert svc.last_call["ended_reason"] == "no words after the wake word"
    assert svc.last_call["heard_user"] is False


async def test_announce_in_a_session_and_outside() -> None:
    svc, stt, tts, _ = _service(Scripted(), follow_up_seconds=1.0)
    await svc.start()
    from littlebot.skills.base import announce

    announce(svc.ctx, "The egg timer is done.")
    await _wait(lambda: "The egg timer is done." in svc.ctx.audio.spoken)  # no session: the speaker
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    announce(svc.ctx, "Time to call mom.")
    await _wait(lambda: "Time to call mom." in tts.said)
    await svc.stop_session("test")


async def test_pipeline_choice() -> None:
    ctx = make_test_context()
    svc = AssistantService(ctx)
    assert ctx.config.assistant.pipeline == "auto" and not svc.use_cascade()
    assert "GPT-Live until DEEPGRAM_API_KEY" in svc.pipeline_reason()
    from pydantic import SecretStr

    ctx.config.deepgram_api_key = SecretStr("dg")
    ctx.config.cartesia_api_key = SecretStr("ca")
    assert svc.use_cascade() and "Cartesia" in svc.pipeline_reason()
    assert "dg" not in str(svc.status()) and "ca\"" not in str(svc.status())


async def test_the_first_session_is_made_ready_in_the_background() -> None:
    # On the robot the first session after a restart imported anthropic on the event loop (~1.2 s): the question
    # said right after "Hey LittleBot" was lost meanwhile. Start-up now loads it all in the background.
    import sys

    from littlebot.assistant import service as service_mod

    svc, _stt, tts, _audio = _service(Scripted())
    await svc.preload()
    assert svc._tts is tts
    assert all(m in sys.modules for m in ("littlebot.brain.text_model", "littlebot.assistant.session"))
    await svc.start()
    task = svc._preloading
    assert task is not None and not task.done()
    await svc.stop()
    await asyncio.sleep(0)
    assert task.cancelled()
    assert service_mod._import_all(("littlebot.no_such_module",)) == ["littlebot.no_such_module"]


async def test_the_call_audio_opens_before_the_wake_word_lets_go_of_the_microphone() -> None:
    # The wake word's carry ends when it lets go of the microphone; the call's microphone used to open ~150 ms
    # later on the robot, and words said in between were lost. Now it opens first, and what it heard before the
    # wake word let go (already in the carry) is dropped.
    import numpy as np

    from littlebot.audio.carry import SpeechCarry

    svc, stt, _tts, audio = _service(Scripted())
    order: list[str] = []
    opened = audio.open
    audio.open = lambda loop: (order.append("call audio open"), opened(loop))[-1]
    kept: list[float] = []
    audio.flush_input = lambda keep_s=0.0: (order.append("flush"), kept.append(keep_s))
    let_go_at_once: list[bool] = []

    async def suspend(owner: str) -> None:
        let_go_at_once.append(carry.taker_listens)  # the call listens already: no waiting for a pause
        order.append("wake word lets go")

    svc.ctx.audio.suspend = suspend
    svc.ctx.audio.resume = lambda owner: order.append("back")
    carry = SpeechCarry(16000, phrase="hey little bot")
    carry.add(np.full(1600, 0.1, dtype=np.float32))
    carry.seal("test")
    await svc.start()
    await svc.start_session(carry=carry)
    await _wait(lambda: stt.stream is not None and stt.stream.fed >= 1600)
    assert order[:3] == ["call audio open", "wake word lets go", "flush"] and let_go_at_once == [True]
    assert 0.0 < kept[0] < 5.0  # what the call heard since the carry ended is kept
    await svc.stop_session("test")


async def test_started_before_the_earcon_it_tells_the_wake_word_how_loud_they_are_and_can_play_it() -> None:
    # wakeword EARLY_START: the session listens at once; the wake word decides on its earcon from the call's
    # microphone, and the session plays it if they wait (leaving the microphone open).
    import numpy as np

    from littlebot.audio.carry import SpeechCarry

    svc, _stt, _tts, _ = _service(Scripted([]), follow_up_seconds=5)
    carry = SpeechCarry(16000, phrase="hey little bot")
    carry.add(np.full(1600, 0.1, dtype=np.float32))
    carry.seal("test")
    carry.levels_wanted = True
    await svc.start()
    assert svc.play_earcon("listen") is False  # no session yet
    await svc.start_session(carry=carry)
    await _wait(lambda: carry.quiet_s >= 0.1)  # the call's (quiet) microphone
    speech = carry.speech_seconds
    assert svc.play_earcon("listen", volume=0.6) is True
    audio = svc._session.audio
    assert audio.cues and audio.played == []  # a cue, not the voice: the microphone stays open
    carry.levels_wanted = False
    quiet = carry.quiet_s
    await asyncio.sleep(0.1)
    assert carry.quiet_s == quiet and carry.speech_seconds == speech  # only while the wake word wants them
    await svc.stop_session("test")


async def test_the_wake_phrase_goes_first_and_is_taken_off_the_words() -> None:
    # Decided early, the carry started in the middle of "bot": "Hey little bot, who was Kublai Khan?" was heard as
    # "not who was Kublai Khan." and answered as a correction (the robot, 2026-10-31).
    import numpy as np

    from littlebot.audio.carry import SpeechCarry

    brain = Scripted(["He was a Mongol emperor."])
    svc, stt, _tts, _ = _service(brain, follow_up_seconds=5)
    carry = SpeechCarry(16000, phrase="hey little bot")
    carry.keep_phrase([np.full(8000, 0.2, dtype=np.float32)])
    carry.add(np.full(1600, 0.1, dtype=np.float32))
    carry.seal("test")
    await svc.start()
    await svc.start_session(carry=carry)
    await _wait(lambda: stt.stream is not None and stt.stream.fed >= 9600)  # the wake phrase, then the rest
    assert carry.take_phrase().size == 0  # handed over once
    stt.say("Hey, little bot.")  # their pause after it: a turn of its own, and nothing to answer
    await asyncio.sleep(0.1)
    assert brain.questions == [] and svc.status()["active"]
    stt.say("Hey, little bot. Who was Kublai Khan?")
    await _wait(lambda: bool(brain.questions))
    assert brain.questions == ["Who was Kublai Khan?"]
    await svc.stop_session("test")


@pytest.mark.parametrize("said,question", [
    ("Hey, little bot. What time is it?", "What time is it?"), ("hey little bot what's the weather", "What's the weather"),
    ("Little bot, stop.", "Stop."), ("Hey Littlebot!", ""), ("Hey lil bot.", ""), ("Okay, little bot, next song", "Next song"),
    ("What's a little bot?", "What's a little bot?"), ("a little bit louder", "a little bit louder"),
    ("bot. Who was Kublai Khan?", "Who was Kublai Khan?"), ("Hey bot, what time is it", "What time is it"),
    ("Bottles of water", "Bottles of water"), ("a little bot who was Kublai Khan.", "Who was Kublai Khan."),
    ("a little bit more", "a little bit more"),
    # misheard after "hey" (the robot, 2026-10-31), but never in a sentence of theirs
    ("Hey, little bug.", ""), ("Hey little bought, what time is it", "What time is it"),
    ("A little bug is on the wall.", "A little bug is on the wall."), ("Hey, a little bug!", "Hey, a little bug!"),
])
def test_the_wake_phrase_is_no_part_of_the_question(said: str, question: str) -> None:
    from littlebot.assistant.session import _heard

    assert _heard(said) == question


def test_what_was_said_with_the_wake_word_goes_without_its_long_pauses() -> None:
    # Speech-to-text works through it at about real time: 2.5 s of it delayed the end of their turn by ~2 s.
    import numpy as np

    from littlebot.assistant.session import CARRY_PAUSE_S, _squeeze_quiet

    rng = np.random.default_rng(3)
    word = (0.2 * rng.standard_normal(4800)).astype(np.float32)  # 0.3 s
    quiet = np.full(16000, 0.001, dtype=np.float32)  # 1 s
    out = _squeeze_quiet(np.concatenate([word, quiet, word, quiet[:1600], word]), 16000, 0.015)
    # the long pause down to CARRY_PAUSE_S (give or take a 20 ms frame), the short one (0.1 s) as it was
    assert abs(len(out) - (3 * 4800 + round(CARRY_PAUSE_S * 16000) + 1600)) <= 320
    assert np.array_equal(out[:4800], word) and np.array_equal(out[-4800:], word)


async def test_typed_chat_uses_the_same_brain() -> None:
    svc, _, _, _ = _service(Scripted(["Paris is the capital of France."]))
    out = await svc.handle_text("capital of france?")
    assert out["reply"] == "Paris is the capital of France." and out["error"] is None
    out = await svc.handle_text("volume 20")
    assert out["provider"] == "intent" and out["reply"] == "Volume 20."


async def test_voice_front_falls_back_to_gpt_live_without_speech_to_text() -> None:
    from littlebot.assistant import voice_front

    svc, stt, _, _ = _service(Scripted())
    svc.ctx.live = object()
    assert voice_front(svc.ctx) is svc
    svc._stt_factory = lambda: None
    assert voice_front(svc.ctx) is svc.ctx.live


async def test_an_early_answer_is_said_only_once_the_turn_really_ends() -> None:
    brain = Scripted(["Lima is the capital of Peru."])
    svc, stt, tts, audio = _service(brain, follow_up_seconds=0.3)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.event("start_of_turn")
    stt.event("eager_end_of_turn", "what's the capital of Peru")
    await _wait(lambda: brain.questions == ["what's the capital of Peru"])  # the brain started early
    await asyncio.sleep(0.1)
    # ...and its voice is made already (heard the moment the turn ends), but nothing reached the speaker
    assert tts.said == ["Lima is the capital of Peru."] and audio.played == []
    stt.event("end_of_turn", "What's the capital of Peru?")
    await _wait(lambda: len(audio.played) > 0)
    assert len(brain.questions) == 1 and tts.said == ["Lima is the capital of Peru."]  # no second request
    await _wait(lambda: not svc.status()["active"])
    assert svc.last_call["turns"] == 1


async def test_quiet_after_probably_done_ends_their_turn_at_once() -> None:
    # Flux alone waited 1.6 s after "who was Kublai Khan" with the answer ready (the robot, 2026-10-31).
    brain = Scripted(["He was a Mongol emperor."], ["Lima."])
    svc, stt, tts, audio = _service(brain, follow_up_seconds=5)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.event("start_of_turn")
    stt.event("partial", "who was Kublai Khan")
    stt.event("eager_end_of_turn", "who was Kublai Khan")
    await _wait(lambda: stt.stream.finished == 1, timeout=1.5)  # asked to end the turn now
    stt.event("end_of_turn", "Who was Kublai Khan?")
    await _wait(lambda: len(audio.played) > 0)
    # more words after "probably done": no forcing (they went on)
    stt.event("start_of_turn")
    stt.event("partial", "what's the capital of")
    stt.event("eager_end_of_turn", "what's the capital of")
    await asyncio.sleep(0.1)
    stt.event("partial", "what's the capital of Peru")
    await asyncio.sleep(0.6)
    assert stt.stream.finished == 1
    await svc.stop_session("test")


async def test_an_early_answer_is_dropped_when_they_go_on() -> None:
    brain = Scripted(["The weather today is sunny.", ("set_volume", '{"percent": 10}')],
                     ["Tomorrow it rains."], delay=0.05)
    svc, stt, tts, _ = _service(brain, follow_up_seconds=0.3)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.event("start_of_turn")
    stt.event("eager_end_of_turn", "what's the weather")
    await _wait(lambda: len(brain.questions) == 1)
    stt.event("turn_resumed")
    stt.event("end_of_turn", "what's the weather tomorrow")
    await _wait(lambda: "Tomorrow it rains." in tts.said)
    await _wait(lambda: len(svc.history.messages()) >= 2)
    said = [m["content"] for m in svc.history.messages() if m["role"] == "assistant"]
    assert said == ["Tomorrow it rains."]  # the dropped answer was never heard (its voice may have been made)
    assert brain.questions[-1] == "what's the weather tomorrow"
    assert svc.ctx.settings.get("audio.volume") != 0.1  # the dropped answer's tool never ran


async def test_stop_or_snooze_while_an_alarm_rings() -> None:
    brain = Scripted()
    svc, stt, tts, _ = _service(brain, follow_up_seconds=0.5)

    class Ringing:
        name = "reminders"

        def __init__(self) -> None:
            self.rings = [1]
            self.calls: list[str] = []

        def ringing(self) -> list[int]:
            return self.rings

        def snooze(self) -> list[int]:
            self.calls.append("snooze")
            return self.rings

        def snooze_minutes(self) -> int:
            return 9

        def stop_ringing(self) -> list[int]:
            self.calls.append("stop")
            return self.rings

    class Hub:
        def __init__(self, skill: Ringing) -> None:
            self.skill = skill

        def get(self, name: str) -> Ringing | None:
            return self.skill if name == "reminders" else None

        def specs(self) -> list:
            return []

        def prompt_notes(self) -> str:
            return ""

        def context_notes(self) -> str:
            return ""

    rem = Ringing()
    svc.ctx.skills = Hub(rem)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("snooze")
    await _wait(lambda: "Snoozed for 9 minutes." in tts.said)
    stt.say("I'm up")
    await _wait(lambda: "Okay, it's off." in tts.said)
    assert rem.calls == ["snooze", "stop"] and brain.questions == []
    await svc.stop_session("test")


async def test_an_alarm_wakes_littlebot_up() -> None:
    svc, _, _, _ = _service(Scripted())
    await svc.start()
    svc.ctx.state.transition(RobotState.SLEEPING, "test")
    from littlebot.skills.base import announce

    announce(svc.ctx, "Good morning! It's 7.", kind="alarm", urgent=True)
    assert svc.ctx.state.state == RobotState.IDLE
    await _wait(lambda: "Good morning! It's 7." in svc.ctx.audio.spoken)


async def test_voice_games_are_played_without_the_brain() -> None:
    from littlebot.games.voice.skill import VoiceGames
    from littlebot.skills.hub import SkillHub

    brain = Scripted()
    svc, stt, tts, _ = _service(brain, follow_up_seconds=3)
    svc.ctx.skills = SkillHub(svc.ctx, [VoiceGames(svc.ctx)])
    await svc.ctx.skills.start()
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("let's play trivia")
    await _wait(lambda: len(tts.said) >= 1)
    assert svc.ctx.skills.get("voice_games").active is not None
    said = len(tts.said)
    stt.say("B")
    await _wait(lambda: len(tts.said) > said)
    assert brain.questions == []  # the start and the answer were handled on the robot
    await svc.stop_session("test")


class SlowTTS(FakeTTS):
    """Two seconds of voice per sentence, streamed in 50 ms pieces (a real answer takes a while to say)."""

    async def stream(self, text: str) -> Any:
        import numpy as np

        self.said.append(text)
        for _ in range(40):
            await asyncio.sleep(0.05)
            yield np.ones(2400, dtype=np.float32) * 0.1


async def test_a_barge_in_mid_sentence_cancels_the_turns_take_over_and_records_it_once() -> None:
    brain = Scripted(["Going to sleep now, goodnight.", ("go_to_sleep", "{}")])
    svc, stt, _, audio = _service(brain, follow_up_seconds=5)
    svc._tts = SlowTTS()
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("could you have a little nap please")
    await _wait(lambda: len(audio.played) >= 3)
    audio.on_change("heard", {})  # what LiveAudio does once their words confirm the barge-in
    await asyncio.sleep(0.5)
    assert svc.status()["active"] is True
    assert svc.ctx.state.state != RobotState.SLEEPING
    assert svc.history.messages()[-1]["content"].endswith("(interrupted)")
    assert len(svc.history) == 1
    note = svc.history.done_note()  # the next answer knows the nap was asked for but never happened
    assert 'after "could you have a little nap please": go_to_sleep:' in note and "did NOT start" in note
    assert not svc.brain.first_today()  # an answer heard in part was talk today: no second "Good morning"
    await svc.stop_session("test")


async def test_a_long_answer_is_not_cut_by_a_timeout_and_unsaid_news_is_kept() -> None:
    brain = Scripted(["One. ", "Two. ", "Three."])
    svc, stt, _, audio = _service(brain, follow_up_seconds=0.3)
    svc._tts = SlowTTS()
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("count to three slowly")
    from littlebot.skills.base import announce

    await _wait(lambda: len(audio.played) >= 2)
    announce(svc.ctx, "The egg timer is done.")
    await _wait(lambda: not svc.status()["active"], timeout=15)
    assert svc._tts.said == ["One.", "Two.", "Three.", "The egg timer is done."]


async def test_the_brain_is_told_who_is_talking_by_voice() -> None:
    from littlebot.voices.service import Heard, VoiceMatch

    class Voices:
        threshold = 0.5
        improved = 0

        def blocked_reason(self) -> str | None:
            return None

        async def analyse(self, samples: Any, rate: int, **kw: Any) -> Heard:
            assert rate == 16000 and len(samples) > 0
            m = VoiceMatch(person_id=1, name="Ali", role="dad", score=0.8, is_child=False, margin=0.3)
            return Heard("ok", 2.5, m, 1, 0.8, 0.4, True)

        def improve(self, heard: Heard) -> bool:
            self.improved += 1
            return True

    brain = Scripted(["Hi!"], ["Sure."])
    svc, stt, tts, _ = _service(brain, follow_up_seconds=2)
    svc.ctx.voices = Voices()
    seen: list[str] = []
    real = svc.brain.build_messages

    def spy(text: str, history: Any, toolbox: Any = None, *, extra: Any = None) -> Any:
        seen.append(" ".join(extra or []))
        return real(text, history, toolbox, extra=extra)

    svc.brain.build_messages = spy
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    await asyncio.sleep(0.2)  # some microphone audio to recognise
    stt.say("hello")
    await _wait(lambda: "Hi." in tts.said)  # "!" is softened to "." for the voice
    await _wait(lambda: svc._session is not None and svc._session.speaker is not None)
    stt.say("what's up")
    await _wait(lambda: "Sure." in tts.said)
    assert "the person talking is Ali (dad)" in seen[1]  # recognised in the background: known by the next turn
    assert svc.ctx.voices.improved >= 1  # a sure turn sharpens their voiceprint
    await svc.stop_session("test")


async def test_saying_who_you_are_teaches_littlebot_your_voice() -> None:
    from littlebot.voices.service import Heard

    class People:
        def people(self) -> list[dict[str, Any]]:
            return [{"id": 2, "name": "Sara", "role": "mum"}]

        def person(self, pid: int) -> dict[str, Any] | None:
            return {"id": 2, "name": "Sara", "role": "mum"} if pid == 2 else None

    class Store:
        people = People()

    class Voices:
        threshold = 0.5
        store = Store()
        claimed: list[int] = []

        def blocked_reason(self) -> str | None:
            return None

        async def analyse(self, samples: Any, rate: int, **kw: Any) -> Heard:
            return Heard("nobody", 2.2, one_voice=True)

        def keep_claimed(self, person_id: int, heard: Heard) -> dict[str, Any]:
            self.claimed.append(person_id)
            return {"ok": True, "reason": "claimed", "first": True}

        def improve(self, heard: Heard) -> bool:
            return False

    svc, stt, tts, _ = _service(Scripted(["Nice to meet you, Sara!"], ["Sure."]), follow_up_seconds=2)
    svc.ctx.voices = Voices()
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    await asyncio.sleep(0.2)
    stt.say("Hi, I'm Sara")
    await _wait(lambda: svc.ctx.voices.claimed == [2])
    who = svc._session.speaker
    assert who is not None and who.name == "Sara" and who.sure
    await svc.stop_session("test")


async def test_who_is_here_tool_says_what_it_knows() -> None:
    brain = Scripted([("who_is_here", "{}"), "I can't tell yet."])
    svc, stt, tts, _ = _service(brain, follow_up_seconds=1)
    results: list[str] = []
    real = svc.brain.respond

    async def spy(*a: Any, **kw: Any) -> Any:
        tools = kw.get("extra_tools") or {}
        assert "who_is_here" in tools
        results.append(await tools["who_is_here"][1]({}))
        return await real(*a, **kw)

    svc.brain.respond = spy
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("do you know who I am")
    await _wait(lambda: "I can't tell yet." in tts.said)
    assert "voice recognition is off" in results[0] and "Camera:" in results[0]
    await svc.stop_session("test")


async def test_pause_and_next_go_to_spotify_when_it_plays() -> None:
    class Spotify:
        name = "spotify"
        actions: list[str] = []

        def playing(self) -> bool:
            return True

        def paused(self) -> bool:
            return False

        async def command(self, action: str, percent: Any = None) -> tuple[bool, str]:
            self.actions.append(action)
            return True, "Done."

    class Hub:
        sp = Spotify()

        def get(self, name: str) -> Any:
            return self.sp if name == "spotify" else None

        def specs(self) -> list:
            return []

        def prompt_notes(self) -> str:
            return ""

        def context_notes(self) -> str:
            return ""

    brain = Scripted()
    svc, stt, tts, _ = _service(brain, follow_up_seconds=3)
    svc.ctx.skills = Hub()
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("next song")
    await _wait(lambda: Hub.sp.actions == ["next"])
    assert brain.questions == [] and tts.said == []  # the music changing is the answer
    await _wait(lambda: not svc.status()["active"])


async def test_a_turn_without_words_does_not_keep_the_session_open() -> None:
    svc, stt, _, _ = _service(Scripted(), first_words_seconds=2.0)
    clock = [0.0]
    svc.now = lambda: clock[0]
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.event("start_of_turn")  # a noise: speech-to-text starts a turn and never ends it
    await asyncio.sleep(0.1)
    clock[0] = 3.0
    await asyncio.sleep(0.3)
    assert svc.status()["active"]  # still within the no-words grace
    clock[0] = 5.0
    await _wait(lambda: not svc.status()["active"])
    assert svc.last_call["ended_reason"] == "no words after the wake word"


async def test_its_own_voice_heard_back_is_not_a_turn() -> None:
    brain = Scripted(["The Roman Empire lasted for centuries."], ["Sure."])
    svc, stt, tts, _ = _service(brain, follow_up_seconds=3)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("tell me about rome")
    await _wait(lambda: len(tts.said) == 1)
    await asyncio.sleep(0.1)
    stt.say("the Roman Empire lasted for centuries")  # its own voice leaking back
    await asyncio.sleep(0.3)
    assert brain.questions == ["tell me about rome"]
    stt.say("end the call")
    await _wait(lambda: not svc.status()["active"])
    assert svc.last_call["ended_reason"] == "said stop"  # without a word
    assert len(tts.said) == 1


async def test_its_own_words_said_well_before_their_turn_began_are_theirs() -> None:
    # "Turn the volume down a bit" said 1.3 s after LittleBot's "Turning the volume down a bit." had ended was
    # ignored as its own voice heard back (the robot, 2026-10-31).
    brain = Scripted(["Done."])
    svc, stt, _tts, _ = _service(brain, follow_up_seconds=3)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    session = svc._session
    now = session._now()
    session._said.append((now - 5.0, "Turning the volume down a bit."))  # began 5 s ago, about 2 s long
    assert session._echo_of_me("turn the volume down a bit")  # when their turn began isn't known: as before
    assert session._echo_of_me("turning the volume down a bit", since=now - 4.0)  # began while it was said
    assert not session._echo_of_me("turn the volume down a bit", since=now - 0.5)  # began well after it ended
    stt.say("turn the volume down a bit")
    await _wait(lambda: brain.questions == ["turn the volume down a bit"])
    await svc.stop_session("test")


async def test_stop_word_ends_wait_word_only_quiets() -> None:
    svc, stt, tts, audio = _service(Scripted(["A long answer."]), follow_up_seconds=3)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    audio.on_change("stop_word", {"said": "wait"})
    await asyncio.sleep(0.2)
    assert svc.status()["active"] and audio.dropped == 1
    audio.on_change("stop_word", {"said": "stop"})
    await _wait(lambda: not svc.status()["active"])
    assert svc.last_call["ended_reason"] == "they said stop"


async def test_only_one_voice_ever_talks() -> None:
    svc, stt, _, audio = _service(Scripted(["The answer."]), follow_up_seconds=3)
    svc._tts = SlowTTS()
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    session = svc._session
    from littlebot.skills.base import announce

    announce(svc.ctx, "Your timer is done. It rang a minute ago.")
    await _wait(lambda: len(audio.played) >= 2)  # the announcement is talking
    stt.say("what was the answer")  # they start a new question over it
    await _wait(lambda: "The answer." in svc._tts.said)
    talking = [v for v in session._mouths if v.busy]
    assert len(talking) <= 1  # the announcement was stopped for the answer
    await svc.stop_session("test")


async def test_the_brain_is_told_the_first_turn_and_a_cut_in_are_for_it() -> None:
    """On the robot words said right after cutting in were twice answered with [silent] (2026-10-31)."""
    brain = Scripted(["Hi there."], ["One. ", "Two. ", "Three."], ["Okay."], ["Sure."])
    svc, stt, _, audio = _service(brain, follow_up_seconds=5)
    seen: list[str] = []
    real = svc.brain.build_messages

    def spy(text: str, history: Any, toolbox: Any = None, *, extra: Any = None) -> Any:
        seen.append(" ".join(extra or []))
        return real(text, history, toolbox, extra=extra)

    svc.brain.build_messages = spy
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("hello")
    await _wait(lambda: len(seen) == 1)
    assert "it is for you" in seen[0] and "woke you" in seen[0]  # the first turn after the wake word
    await _wait(lambda: svc._session is not None and not svc._session._answering())
    svc._tts = SlowTTS()
    stt.say("count to three")
    await _wait(lambda: len(seen) == 2)
    assert "it is for you" not in seen[1]  # an ordinary follow-up: the brain decides
    await _wait(lambda: svc._session._voice is not None and bool(svc._session._voice.heard))
    audio.on_change("heard", {})  # they talked over it
    stt.say("what about tomorrow")
    await _wait(lambda: len(seen) == 3)
    assert "talked over you" in seen[2]
    await svc.stop_session("test")


async def test_told_to_stop_in_its_own_words_the_brain_ends_the_call_without_a_word() -> None:
    """"Please don't talk anymore" isn't an instant intent: the brain calls end_conversation, and whatever it
    writes after that is never said (asked for on the robot, 2026-10-31)."""
    brain = Scripted([("end_conversation", "{}"), "Okay, I'll stop talking now."])
    svc, stt, tts, _ = _service(brain, follow_up_seconds=5)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.say("please don't talk anymore")
    await _wait(lambda: not svc.status()["active"])
    assert svc.last_call["ended_reason"] == "asked to stop"
    assert tts.said == []
