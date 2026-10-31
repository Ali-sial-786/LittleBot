"""Instant intents: whole plain utterances only; everything else goes to the brain."""

from __future__ import annotations

import pytest

from littlebot.assistant import intents
from littlebot.chatgpt.tools import RobotToolbox
from littlebot.testing import make_test_context


@pytest.mark.parametrize("text,name", [
    ("Stop.", "stop"), ("never mind", "stop"), ("okay stop please", "stop"), ("Thanks!", "thanks"),
    ("thank you so much", "thanks"), ("bye", "bye"), ("that's all for now", "bye"), ("louder", "volume"),
    ("turn it down please", "volume"), ("volume 40", "volume"), ("set the volume to 70 percent", "volume"),
    ("what time is it?", "time"), ("What's the time", "time"), ("end the call", "bye"), ("hang up", "bye"),
    ("stop listening", "bye"), ("okay end the conversation", "bye"), ("you can go now", "bye"),
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
