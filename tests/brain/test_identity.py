"""Who LittleBot is (littlebot/brain/identity.py): its creator, its body and its debug log, in every prompt."""

from __future__ import annotations

from littlebot.brain import identity
from littlebot.brain.prompts import build_system_prompt
from littlebot.chatgpt.live import CONTROL_WORDS
from littlebot.testing import make_test_config


def test_the_default_says_ali_made_it() -> None:
    config = make_test_config()
    assert identity.creator(config) == "Ali, the little one's dad"
    text = identity.about(config)
    assert "built and programmed by Ali, the little one's dad" in text and "say Ali did" in text
    assert "read_debug_log" in text and "Raspberry Pi" in text and "ChatGPT" in text


def test_the_voice_delegates_the_debug_log_and_without_tools_it_isnt_mentioned() -> None:
    config = make_test_config()
    assert "check the debug log:" in identity.about(config, debug="voice")
    assert "debug log" not in identity.about(config, debug="")


def test_no_creator_set() -> None:
    config = make_test_config()
    config.ai.creator = None
    assert identity.creator(config) == "" and "built and programmed" not in identity.about(config)
    config.ai.creator, config.ai.creator_relation = "Sam<script>", None
    assert identity.creator(config) == "Samscript"


def test_it_goes_into_the_system_prompt() -> None:
    about = identity.about(make_test_config())
    prompt = build_system_prompt(age_months=10, max_words=60, about=about)
    assert about in prompt and prompt.index(about) < prompt.index("How you talk:")
    assert "Who you are" not in build_system_prompt(age_months=10, max_words=60)


def test_what_went_wrong_hand_offs_go_to_the_tools() -> None:
    for text in ("check the debug log: the video didn't play", "what went wrong with the camera?",
                 "why didn't the game work", "look at your logs", "the music isn't working"):
        assert CONTROL_WORDS.search(text), text
    for text in ("what's a log cabin", "tell me a story"):
        assert not CONTROL_WORDS.search(text), text
