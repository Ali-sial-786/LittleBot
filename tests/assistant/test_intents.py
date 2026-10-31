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


@pytest.mark.parametrize("text,step", [
    # these went to the brain, which looked the volume up first: 2-2.3 s to the first word (the robot, 2026-10-31)
    ("Turn the volume down a bit.", -1), ("A little louder, please.", 1), ("turn it up a little", 1),
    ("can you turn the volume down a little bit", -1), ("Make it a bit quieter.", -1), ("It's too loud.", -1),
    ("lower the volume", -1), ("Turn up the volume please.", 1), ("a bit louder", 1), ("Louder, please.", 1),
    ("could you speak up", 1), ("a little bit softer", -1), ("turn it down", -1),
    ("Turned the volume down a bit.", -1),  # how speech-to-text wrote "turn the volume down a bit" (the robot)
])
def test_volume_in_everyday_words(text: str, step: int) -> None:
    hit = intents.match(text)
    assert hit is not None and hit.name == "volume" and hit.args == {"step": step}


@pytest.mark.parametrize("text,percent", [
    ("set the volume to one hundred percent", 100), ("Volume to fifty.", 50), ("Set the volume to seventy-five "
     "percent.", 75), ("volume 40", 40), ("set the volume to max", 100), ("turn the volume to 30", 30),
    ("volume to twenty five", 25), ("volume seven", 7), ("can you set the volume to sixty percent", 60),
])
def test_volume_to_a_number_in_words_or_digits(text: str, percent: int) -> None:
    hit = intents.match(text)
    assert hit is not None and hit.name == "volume" and hit.args == {"percent": percent}


@pytest.mark.parametrize("text", ["a little leopard, please", "turn up the heat", "turn the lights down a bit",
                                  "is it too loud?", "volume of a sphere", "a little later please",
                                  "how loud is a jet engine", "turn it up to eleven, like in the film"])
def test_not_the_volume(text: str) -> None:
    hit = intents.match(text)
    assert hit is None or hit.name != "volume"


async def test_at_the_ceiling_it_says_so_and_never_a_level_it_did_not_set() -> None:
    ctx = make_test_context()  # audio.speaker.max_volume 0.8
    box = RobotToolbox(ctx, mode="chat")
    assert await intents.run(intents.match("set the volume to one hundred percent"), box, ctx) == \
        "Volume 80, as loud as I go."
    assert await intents.run(intents.match("a little louder please"), box, ctx) == "That's as loud as I go."
    await box.run("set_volume", {"percent": 75})
    assert await intents.run(intents.match("louder"), box, ctx) == "Louder. That's as loud as I go."
    assert ctx.audio.volume == pytest.approx(0.8)
    assert await intents.run(intents.match("turn the volume down a bit"), box, ctx) == "Quieter."
    assert ctx.audio.volume == pytest.approx(0.7)
    ctx.audio.set_volume = lambda v: min(float(v), 0.6)  # the speaker itself stops lower than the config's ceiling
    assert await intents.run(intents.match("volume 100"), box, ctx) == "Volume 60, as loud as I go."


async def test_voice_can_never_make_it_silent() -> None:
    ctx = make_test_context()
    box = RobotToolbox(ctx, mode="chat")
    assert await intents.run(intents.match("volume 0"), box, ctx) == "Volume 15, as quiet as I go."
    assert ctx.audio.volume == 0.15  # the quietest it goes by voice
    assert await intents.run(intents.match("softer"), box, ctx) == "That's as quiet as I go."
    assert ctx.audio.volume == 0.15
