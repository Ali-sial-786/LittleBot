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
        if self.delay:
            await asyncio.sleep(self.delay)
        script = self.scripts.pop(0) if self.scripts else ["Okay."]
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

    svc = AssistantService(ctx, audio_factory=lambda: audio, stt_factory=lambda exclude=(): stt,
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
    svc._stt_factory = lambda exclude=(): None
    assert voice_front(svc.ctx) is svc.ctx.live
