"""A sum asked by voice, worked out here so the brain says the exact result.

Speech-to-text writes numbers as words ("What is the square root of seventeen sixty four?"), and the brain,
answering with thinking off, said "about forty-one point nine" for it (the robot, 2026-10-31; 42 is right). The
words become digits ("seventeen sixty four" -> 1764, "three hundred and forty two" -> 342, "three point five"
-> 3.5) and the maths card's own parser (no eval) works the sum out; the brain gets the result as a note.
"""

from __future__ import annotations

import re

_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine".split())}
_TEENS = {w: i + 10 for i, w in enumerate(
    "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: (i + 2) * 10 for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
_VALUES = {**_UNITS, **_TEENS, **_TENS}
_LEAD = re.compile(r"^(?:hey |okay |so )?(?:what(?:'s| is| are| does)|how much (?:is|are)|calculate|work out|"
                   r"(?:can|could) you (?:tell me|work out)|tell me) ", re.I)
_SUM = re.compile(r"[-+*/^%√]|\b(?:times|plus|minus|divided|multiplied|squared|cubed|percent|power|square root)\b|×|÷")


def _value(words: list[str]) -> int | None:
    """The number some number words say: "twenty three" 23, "seventeen sixty four" 1764 (said like a year),
    "two thousand and six" 2006 ("and" already left out); None when they aren't one number ("three four")."""
    if any(w in _SCALES for w in words):
        total = cur = 0
        for w in words:
            if w == "hundred":
                cur = (cur or 1) * 100
            elif w in _SCALES:
                total, cur = total + (cur or 1) * _SCALES[w], 0
            else:
                cur += _VALUES[w]
        return total + cur
    groups: list[int] = []  # numbers of up to two digits, each said whole: [17, 64]
    tens: int | None = None
    for w in words:
        if tens is not None and w in _UNITS:
            groups.append(tens + _UNITS[w])
            tens = None
            continue
        if tens is not None:
            groups.append(tens)
            tens = None
        if w in _TENS:
            tens = _TENS[w]
        else:
            groups.append(_VALUES[w])
    if tens is not None:
        groups.append(tens)
    if len(groups) == 1:
        return groups[0]
    if len(groups) == 2 and groups[0] >= 10 and groups[1] >= 10:
        return groups[0] * 100 + groups[1]
    return None


def digits(text: str) -> str:
    """``text`` with its spoken numbers as digits, in lower case, without end punctuation."""
    s = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", str(text or "").lower())
    words = [w.strip("?.!,") for w in s.split()]
    out: list[str] = []
    i = 0
    while i < len(words):
        run: list[str] = []
        j = i
        while j < len(words) and (words[j] in _VALUES or words[j] in _SCALES or
                                  (words[j] == "and" and run and j + 1 < len(words) and words[j + 1] in _VALUES)):
            if words[j] != "and":
                run.append(words[j])
            j += 1
        number = _value(run) if run else None
        if number is None:  # not a number ("three four"): the words as they were
            out.extend(words[i:j] if run else words[i:i + 1])
            i = j if run else i + 1
            continue
        said = str(number)
        if j + 1 < len(words) and words[j] == "point" and words[j + 1] in _UNITS:  # "three point five"
            j += 1
            decimals = ""
            while j < len(words) and words[j] in _UNITS:
                decimals += str(_UNITS[words[j]])
                j += 1
            said += "." + decimals
        out.append(said)
        i = j
    return " ".join(w for w in out if w)


def worked_out(question: str) -> str | None:
    """A plain sum in what they asked, worked out ("square root of 1764 = 42", "23 times 14 = 322"); None when
    it isn't one."""
    from littlebot.screens.cards.maths import evaluate, format_number  # noqa: PLC0415 - pygame

    s = _LEAD.sub("", digits(question).strip(), count=1)
    s = re.sub(r"\bthe\b|\bplease\b", " ", s)
    s = " ".join(s.split())
    if not s or not re.search(r"\d", s) or not _SUM.search(s):
        return None
    value = evaluate(s)
    if value is None:
        return None
    return f"{s} = {format_number(value)}"


__all__ = ["digits", "worked_out"]
