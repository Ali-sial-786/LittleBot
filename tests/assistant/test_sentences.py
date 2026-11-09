"""SentenceSplitter: streamed words -> clean sentences with face tags, first one out early."""

from __future__ import annotations

from littlebot.assistant.sentences import SentenceSplitter, clean, is_silent, split_tags


def _all(deltas: list[str]) -> list[tuple[str, str | None, str | None]]:
    sp = SentenceSplitter()
    out = []
    for d in deltas:
        out += sp.feed(d)
    out += sp.flush()
    return [(s.text, s.expression, s.animation) for s in out]


def test_sentences_come_out_as_soon_as_they_end() -> None:
    sp = SentenceSplitter()
    assert sp.feed("[happy] It's sunny") == []
    got = sp.feed(" today. Highs of 12")
    assert [(s.text, s.expression) for s in got] == [("It's sunny today.", "happy")]
    got = sp.feed(" degrees! [curious] Want the week?")
    assert [s.text for s in got] == ["Highs of 12 degrees!"]
    rest = sp.flush()
    assert [(s.text, s.expression) for s in rest] == [("Want the week?", "curious")]
    assert sp.said == ["It's sunny today.", "Highs of 12 degrees!", "Want the week?"]


def test_abbreviations_decimals_and_initials_do_not_split() -> None:
    got = _all(["Dr. Smith said 3.5 percent, e.g. ", "a lot. J. K. Rowling agreed."])
    assert [g[0] for g in got] == ["Dr. Smith said 3.5 percent, e.g. a lot.", "J. K. Rowling agreed."]


def test_long_first_sentence_cut_at_a_comma() -> None:
    sp = SentenceSplitter(first_cut_chars=40)
    got = sp.feed("Well, the capital of Australia is Canberra, which surprises many people because")
    assert got and got[0].text == "Well, the capital of Australia is Canberra,"  # never a tiny first piece
    assert sp.count == 1


def test_markdown_links_emoji_and_bullets_are_cleaned() -> None:
    assert clean("**Bold** and `code` 😀 see https://x.io/a [docs](https://d.io)") == "Bold and code see docs"
    got = _all(["- milk\n- eggs\n"])
    assert [g[0] for g in got] == ["milk", "eggs"]


def test_tags_alone_go_with_the_next_sentence_and_animations() -> None:
    got = _all(["[celebrating] [confetti]", " You won!"])
    assert got == [("You won!", "celebrating", "confetti")]
    text, expr, anim = split_tags("[Happy] hi [hearts] there [note]")
    assert (expr, anim) == ("happy", "hearts") and "[" not in text


def test_silent() -> None:
    assert is_silent("[silent]") and is_silent(" [SILENT]. ")
    assert not is_silent("[silent] but then words")
    assert _all(["[silent]"]) == []


def test_very_long_text_without_punctuation_is_cut() -> None:
    sp = SentenceSplitter(max_chars=60)
    sp.feed("x")  # first sentence done: the next uses max_chars
    sp._count = 1
    got = sp.feed("word " * 30)
    assert got and all(len(s.text) <= 60 for s in got)
