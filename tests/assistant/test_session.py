"""The voice session end to end with fakes: wake -> words -> brain -> voice -> follow-up -> end."""

from __future__ import annotations

import asyncio
from typing import Any

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

    async def reply(self, messages: list[dict[str, str]], *, system: str, max_tokens: int, timeout: float,
                    tools: Any = None, run_tool: Any = None, max_tool_rounds: int = 6, effort: Any = None,
                    on_delta: Any = None, final_tools: Any = ()) -> ProviderReply:
        self.questions.append(messages[-1]["content"].split("\n")[-1])
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
    svc, stt, tts, _ = _service(brain, follow_up_seconds=0.3)
    await svc.start()
    await svc.start_session()
    await _wait(lambda: stt.stream is not None)
    stt.event("start_of_turn")
    stt.event("eager_end_of_turn", "what's the capital of Peru")
    await _wait(lambda: brain.questions == ["what's the capital of Peru"])  # the brain started early
    await asyncio.sleep(0.1)
    assert tts.said == []  # ...but nothing is said yet
    stt.event("end_of_turn", "What's the capital of Peru?")
    await _wait(lambda: tts.said == ["Lima is the capital of Peru."])
    assert len(brain.questions) == 1  # no second request
    await _wait(lambda: not svc.status()["active"])
    assert svc.last_call["turns"] == 1


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
    assert "The weather today is sunny." not in tts.said
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
    await _wait(lambda: "Hi!" in tts.said)
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
    assert svc.last_call["ended_reason"].startswith("said intent:bye")


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
