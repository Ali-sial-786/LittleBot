"""Instant intents: whole plain utterances only; everything else goes to the brain."""

from __future__ import annotations

import pytest

from littlebot.assistant import intents
from littlebot.chatgpt.tools import RobotToolbox
from littlebot.testing import make_test_context


@pytest.mark.parametrize("text,name", [
    ("Stop.", "stop"), ("never mind", "stop"), ("okay stop please", "stop"), ("Thanks!", "thanks"),
    ("thank you so much", "thanks"), ("bye", "bye"), ("that's all for now", "stop"), ("louder", "volume"),
    ("turn it down please", "volume"), ("volume 40", "volume"), ("set the volume to 70 percent", "volume"),
    ("what time is it?", "time"), ("What's the time", "time"), ("end the call", "stop"), ("hang up", "stop"),
    ("stop listening", "stop"), ("okay end the conversation", "stop"), ("you can go now", "stop"),
    # told to stop in whatever words: the call ends without a word (the robot, 2026-10-31)
    ("Hey LittleBot, stop talking", "stop"), ("stop, stop", "stop"), ("no stop", "stop"), ("I said stop", "stop"),
    ("can you stop talking", "stop"), ("could you please be quiet", "stop"), ("stop stop stop", "stop"),
    ("alright that's enough", "stop"), ("good night", "bye"), ("see you later", "bye"),
    # speech-to-text often ends a sentence after the first word (the robot, 2026-10-31)
    ("Thanks. Bye.", "bye"), ("Thanks! Bye!", "bye"), ("Okay. Stop talking.", "stop"), ("Okay. Thanks.", "thanks"),
    ("Hey. Little bot. Stop.", "stop"),
    # "stop talking" with its "stop" lost under the voice's last words (the robot, 2026-10-31)
    ("Talking.", "stop"), ("talking!", "stop"), ("No talking.", "stop"),
])
def test_matches(text: str, name: str) -> None:
    hit = intents.match(text)
    assert hit is not None and hit.name == name


@pytest.mark.parametrize("text", [
    "stop the war in my head and tell me a story", "what time is it in Tokyo", "thanks, and what's the weather",
    "can you play some jazz", "pause", "skip", "how loud can you go", "",
])
def test_not_instant(text: str) -> None:
    assert intents.match(text) is None


@pytest.mark.parametrize("text", ["stop talking", "end the call", "that's all", "you can stop now", "hang up"])
def test_told_to_stop_ends_without_a_word(text: str) -> None:
    hit = intents.match(text)
    assert hit is not None and hit.end and hit.say == ""


@pytest.mark.parametrize("text", ["no", "hey", "stop the music and play jazz", "no, I meant tomorrow",
                                  "can you stop by the store", "what stops the rain", "talking about dinosaurs",
                                  "who was talking"])
def test_not_a_stop(text: str) -> None:
    hit = intents.match(text)
    assert hit is None or hit.name != "stop"


def test_media_words_only_with_media() -> None:
    assert intents.match("pause") is None
    assert intents.match("pause", media_on=True).name == "pause"
    assert intents.match("next song", media_on=True).name == "skip"
    assert intents.match("stop the music", media_on=True).name == "stop_media"
    assert intents.match("keep playing", media_on=True).name == "resume"


async def test_run_volume_and_time() -> None:
    ctx = make_test_context()
    box = RobotToolbox(ctx, mode="chat")
    said = await intents.run(intents.match("volume 30"), box, ctx)
    assert said == "Volume 30."
    assert "set_volume" in box.calls
    said = await intents.run(intents.match("what time is it"), box, ctx)
    assert said.startswith("It's ") and ("a.m." in said or "p.m." in said)


async def test_voice_can_never_make_it_silent() -> None:
    ctx = make_test_context()
    box = RobotToolbox(ctx, mode="chat")
    assert await intents.run(intents.match("volume 0"), box, ctx) == "Volume 15, as quiet as I go."
    assert ctx.audio.volume == 0.15  # the quietest it goes by voice
    assert await intents.run(intents.match("softer"), box, ctx) == "That's as quiet as I go."
    assert ctx.audio.volume == 0.15
