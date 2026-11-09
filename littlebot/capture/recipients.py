"""Whose phone a photo or video goes to, from the words: "my phone", "my wife's", "Ali's", "dad's", "everyone".

:func:`target` reads what was said against Who's who (``people``: rows with ``id``, ``name`` and ``role``):

* "me", "my phone", "mine": whoever is talking (``speaker_id``; kind "me" while that isn't known);
* a name ("Ali's phone", "to Sara"), or a role only one person has ("dad's", "grandma's"): them;
* "my wife's", "my husband's", "my spouse's", "my partner's", "my other half's": the other one of the two parents.
  "Wife" is the mum on the list and "husband" the dad when the roles say so, whoever is talking; otherwise it is
  the other grown-up than the one talking (kind "needs_speaker" while that isn't known);
* "everyone", "the family", "all our phones", "both": every phone, and only when said.

Anything else is "unknown" (a name not on the list, other words) or "unclear" (two people fit): the camera skill
then asks whose phone (littlebot/skills/camera.py). The little one is never a recipient.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Callable, Sequence

from littlebot.voices.naming import ROLE_OF, role_group

SELF = frozenset({"me", "my", "mine", "myself", "i", "own"})
EVERYONE = frozenset({"everyone", "everybody", "everyones", "all", "family", "families", "both", "us", "our", "ours",
                      "whole", "of", "every", "each", "one"})
_EVERYONE_CORE = frozenset({"everyone", "everybody", "everyones", "all", "family", "both", "us", "our", "ours"})
WIFE = frozenset({"wife", "wifey", "missus", "missis", "mrs"})
HUSBAND = frozenset({"husband", "hubby"})
PARTNER = frozenset({"spouse", "partner", "half"})  # "other half", "better half"
_FEMALE = frozenset({"mum", "wife"})  # role groups (voices/naming.py) and role words for "wife"
_MALE = frozenset({"dad", "husband"})
_PARENT = frozenset({"mum", "dad", "wife", "husband", "spouse", "partner", "parent"})
_FILLER = frozenset({
    "to", "the", "a", "an", "on", "onto", "for", "phone", "phones", "iphone", "iphones", "mobile", "mobiles", "cell",
    "cellphone", "cellphones", "android", "please", "just", "only", "it", "that", "this", "them", "send", "share",
    "sent", "put", "and", "um", "uh", "oh", "okay", "ok", "so", "then", "instead", "other", "better", "dear", "lovely",
    "photo", "picture", "pic", "video", "one", "number", "little", "bot", "littlebot", "hey"})


@dataclasses.dataclass(frozen=True)
class Target:
    kind: str  # "person" | "me" | "needs_speaker" | "everyone" | "unclear" | "unknown"
    person: dict[str, Any] | None = None
    said: str = ""

    @property
    def found(self) -> bool:
        return self.kind in ("person", "everyone")


def words_of(said: str) -> list[str]:
    """The words that can say whose phone ("my wife's phone" -> ["my", "wife"])."""
    text = str(said or "").lower().replace("’", "'")
    out: list[str] = []
    for w in re.findall(r"[^\W\d_][\w'-]*", text):
        w = w.strip("'-")
        if w.endswith("'s"):
            w = w[:-2]
        elif w.endswith("s'"):
            w = w[:-1]
        if w and w not in _FILLER:
            out.append(w)
    return out


def _roles(person: dict[str, Any]) -> set[str]:
    """A person's role groups ("dad", "mum", ...) and the spouse words in their role ("Ali's wife")."""
    role = str(person.get("role") or "")
    words = set(re.findall(r"[a-z]+", role.lower()))
    out = set(role_group(role)) | (words & {"spouse", "partner", "parent"})
    if words & WIFE:
        out.add("wife")
    if words & HUSBAND:
        out.add("husband")
    return out


def _named(person: dict[str, Any], words: list[str]) -> bool:
    name = " ".join(str(person.get("name") or "").lower().split())
    if not name:
        return False
    first = name.split()[0]
    joined = " ".join(words)
    return bool(re.search(rf"\b{re.escape(name)}\b", joined)) or first in words or first + "s" in words


def target(said: str, people: Sequence[dict[str, Any]], *, speaker_id: int | None = None,
           is_child: Callable[[str, str], bool] = lambda _n, _r: False) -> Target:
    """Whose phone ``said`` means (see the module's docstring). ``speaker_id``: who is talking, when known."""
    words = words_of(said)
    ws = set(words)
    grown = [p for p in people if not is_child(str(p.get("name") or ""), str(p.get("role") or ""))]
    speaker = next((p for p in grown if speaker_id is not None and int(p["id"]) == int(speaker_id)), None)

    def one(found: list[dict[str, Any]]) -> Target:
        if len(found) == 1:
            return Target("person", found[0], said)
        return Target("unclear" if found else "unknown", None, said)

    if not ws or ws <= SELF:
        return Target("person", speaker, said) if speaker is not None else Target("me", None, said)
    if ws & _EVERYONE_CORE and ws <= EVERYONE | SELF:
        return Target("everyone", None, said)
    named = [p for p in grown if _named(p, words)]
    if named:
        return one(named)
    others = [p for p in grown if speaker is None or int(p["id"]) != int(speaker["id"])]
    if ws & (WIFE | HUSBAND | PARTNER):
        want = _FEMALE if ws & WIFE else _MALE if ws & HUSBAND else None
        if want is not None:
            gendered = [p for p in others if _roles(p) & want]
            if gendered:
                return one(gendered)
        if speaker is None:
            return Target("needs_speaker", None, said)
        parents = [p for p in others if _roles(p) & _PARENT]
        return one(parents or others)
    groups = {ROLE_OF[w] for w in words if w in ROLE_OF}
    if groups:
        found = [p for p in grown if role_group(str(p.get("role") or "")) & groups]
        if speaker is not None and len(found) > 1:
            found = [p for p in found if int(p["id"]) != int(speaker["id"])] or found
        return one(found)
    return Target("unknown", None, said)


def possessive(name: str) -> str:
    """ "Ali" -> "Ali's"."""
    return f"{name}'s"


__all__ = ["EVERYONE", "SELF", "Target", "possessive", "target", "words_of"]
