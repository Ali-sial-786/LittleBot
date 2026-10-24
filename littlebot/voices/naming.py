"""A grown-up saying who they are in a voice chat: "I'm Ali", "it's Ali", "this is Ali", "I am her dad Ali".

``claimed(text, people)`` looks for a whole sentence that only says who they are (so "I'm tired",
"it's Monday" or "this is Ali's cup" never count) and matches the name (or a role such as "her dad",
when only one person has it) against the people on Who's who. A name that isn't on the list counts
only when it looks like a name (capitalised in the transcript, not a common word).
"""

from __future__ import annotations

import dataclasses
import re
import unicodedata
from collections.abc import Sequence
from typing import Any

_ROLE_GROUPS = {
    "dad": ("dad", "daddy", "papa", "father", "baba"),
    "mum": ("mum", "mummy", "mom", "mommy", "mama", "mother", "ammi"),
    "grandma": ("grandma", "granny", "gran", "nana", "nan", "grandmother"),
    "grandpa": ("grandpa", "grandad", "granddad", "grandfather"),
    "aunt": ("aunt", "auntie", "aunty"),
    "uncle": ("uncle",),
    "nanny": ("nanny", "babysitter", "childminder"),
    "brother": ("brother",),
    "sister": ("sister",),
}
_ROLE_OF = {w: g for g, words in _ROLE_GROUPS.items() for w in words}
_ROLE_RE = "|".join(sorted((re.escape(w) for w in _ROLE_OF), key=len, reverse=True))
_NAME = r"[^\W\d_][\w'-]{1,19}(?: [^\W\d_][\w'-]{1,19})?"
_POSS = r"(?:her|his|their|your|the baby's|the little one's|[^\W\d_][\w-]{0,19}'s)"
_LEAD = r"(?:(?:it'?s|it is) me,?|it'?s|it is|this is|i'?m|i am|my name is|here'?s|here is|you'?re talking to)"
_TAIL = r"(?:,? (?:here|speaking|again|now|talking|by the way|back))?"
_CLAIM = re.compile(
    rf"{_LEAD} (?:(?:{_POSS} )?(?P<role>{_ROLE_RE})(?:,? (?P<name1>{_NAME}))?|(?P<name2>{_NAME})(?:,? (?:{_POSS} )?(?P<role2>{_ROLE_RE}))?){_TAIL}", re.IGNORECASE)
_LEADING = re.compile(r"^(?:(?:hi|hello|hey|oh|okay|ok|yes|yeah|well|so|um+|uh+|littlebot|little bot|robot|"
                      r"good morning|good night|good evening|no)[ ,]+)+", re.IGNORECASE)
# Words that can follow "I'm" / "it's" / "this is" at the start of a sentence but aren't names.
NOT_NAMES = frozenset("""
me you him her them us it that this here there back home fine okay ok good great well ready done sorry tired hungry
busy late early sure going coming leaving sleeping awake hot cold happy sad bored back again now today tomorrow
tonight morning afternoon evening night monday tuesday wednesday thursday friday saturday sunday january february
march april may june july august september october november december christmas easter eid diwali halloween birthday
bedtime lunchtime dinnertime teatime time raining sunny snowing cloudy windy lunch dinner breakfast littlebot robot
nobody somebody someone everyone everybody nothing something everything one two three the a an not just only also
really very so too
""".split())


@dataclasses.dataclass(frozen=True)
class Claim:
    """Who they said they are: ``person`` (a Who's who row) when on the list, else only ``name``."""

    name: str
    person: dict[str, Any] | None = None

    @property
    def known(self) -> bool:
        return self.person is not None


def _sentences(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", str(text or "")).replace("’", "'")
    out = []
    for part in re.split(r"[.!?;]+", text):
        s = " ".join(part.replace(",", ", ").split()).replace(" ,", ",").strip(" ,")
        s = _LEADING.sub("", s).strip(" ,")
        if s:
            out.append(s)
    return out


def _role_group(role_text: str) -> set[str]:
    words = re.findall(r"[a-z']+", role_text.lower())
    return {_ROLE_OF[w] for w in words if w in _ROLE_OF}


def _looks_like_name(word: str, robot_name: str) -> bool:
    """A name LittleBot doesn't know yet: capitalised in the transcript, not a common word, not "Ali's"."""
    w = word.lower()
    return word[:1].isupper() and w not in NOT_NAMES and w != robot_name.lower() and not w.endswith("'s")


def claimed(text: str, people: Sequence[dict[str, Any]], *, robot_name: str = "LittleBot") -> Claim | None:
    """The person a sentence of ``text`` says is talking, or None. ``people``: rows with ``name`` and ``role``."""
    for sentence in _sentences(text):
        m = _CLAIM.fullmatch(sentence)
        if m is None:
            continue
        name = (m.group("name1") or m.group("name2") or "").strip()
        role = (m.group("role") or m.group("role2") or "").lower()
        if name:
            first = name.split()[0]
            same = [p for p in people if p["name"].lower() in (name.lower(), first.lower())]
            if role and len(same) > 1:
                same = [p for p in same if _ROLE_OF.get(role) in _role_group(p.get("role") or "")]
            if len(same) == 1:
                return Claim(same[0]["name"], same[0])
            if same:
                return None  # two people of that name: LittleBot can't tell which
            if _looks_like_name(first, robot_name):
                second = name.split()[1:]  # "Sam Jones" (a second capitalised word), not "Sam cooking"
                return Claim(" ".join([first, *[w for w in second if w[0].isupper()]]))
            if not role:
                continue
        if role:
            group = _ROLE_OF[role]
            same = [p for p in people if group in _role_group(p.get("role") or "")]
            if len(same) == 1:
                return Claim(same[0]["name"], same[0])
    return None


__all__ = ["NOT_NAMES", "Claim", "claimed"]
