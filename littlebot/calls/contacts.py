"""Who can be called: the name as it is said ("Dad") -> the name as it appears in WhatsApp ("Ali Khan").

The parent keeps the list on the dashboard (setting ``calls.contacts``; the config's ``calls.contacts`` is where it
starts). Only people on it can be called, and only their said names are ever logged or handed to the voice: the
WhatsApp names are typed into WhatsApp's search box and nowhere else.

* ``clean(raw)``           - a list from the dashboard, tidied (raises ``ValueError`` with words for the parent)
* ``find(contacts, said)`` - the (said name, WhatsApp name) for what was said ("my dad", "Dad's"), or None
* ``spoken(contacts)``     - the said names, for "you can call ..."
"""

from __future__ import annotations

import re
from typing import Any

MAX_CONTACTS = 30
MAX_NAME = 40
_CONTROL = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]")
_LEAD = re.compile(r"^(?:(?:my|our|the|to|with|up)\s+)+", re.I)
_TAIL = re.compile(r"(?:'s|’s)?(?:\s+(?:please|now|on whatsapp|whatsapp|phone|number|mobile))*$", re.I)
# The same person said another way ("mom" for a contact kept as "Mum").
_SAME = (frozenset({"mum", "mom", "mummy", "mommy", "mama", "mother"}),
         frozenset({"dad", "daddy", "papa", "father"}),
         frozenset({"grandma", "granny", "nana", "nan", "grandmother", "gran"}),
         frozenset({"grandpa", "grandad", "granddad", "grandfather", "gramps"}))


def tidy(name: Any) -> str:
    """One line, single spaces, no control characters."""
    return " ".join(_CONTROL.sub(" ", str(name or "")).split())


def key(name: Any) -> str:
    """How a said name is compared: lower case, without "my", "'s", "please", punctuation."""
    t = tidy(name).lower().strip(" .,!?")
    t = _TAIL.sub("", _LEAD.sub("", t)).strip(" .,!?")
    return re.sub(r"[^\w\s-]", "", t).strip()


def clean(raw: Any) -> dict[str, str]:
    """The dashboard's list as it is kept: ``{said name: WhatsApp name}``. ``raw`` is that dict, or a list of
    ``{"name", "whatsapp"}``. An empty WhatsApp name means it is the same as the said one."""
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, list):
        pairs = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("Each contact needs a name.")
            pairs.append((item.get("name"), item.get("whatsapp")))
    else:
        raise ValueError("The contacts weren't understood.")
    out: dict[str, str] = {}
    seen: set[str] = set()
    for said, whatsapp in pairs:
        said_t, wa_t = tidy(said), tidy(whatsapp)
        if not said_t and not wa_t:
            continue
        if not said_t:
            raise ValueError("Each contact needs the name you say.")
        if len(said_t) > MAX_NAME or len(wa_t) > MAX_NAME:
            raise ValueError(f"Names can be up to {MAX_NAME} characters.")
        k = key(said_t)
        if not k:
            raise ValueError("A name needs letters in it.")
        if k in seen:
            raise ValueError(f"“{said_t}” is on the list twice.")
        seen.add(k)
        out[said_t] = wa_t or said_t
    if len(out) > MAX_CONTACTS:
        raise ValueError(f"There can be up to {MAX_CONTACTS} contacts.")
    return out


def find(contacts: Any, said: Any) -> tuple[str, str] | None:
    """The (said name, WhatsApp name) on the list for ``said``, or None (nobody else can be called)."""
    wanted = key(said)
    if not wanted or not isinstance(contacts, dict):
        return None
    same = next((group for group in _SAME if wanted in group), frozenset({wanted}))
    for name, whatsapp in contacts.items():
        if key(name) == wanted:
            return str(name), tidy(whatsapp) or str(name)
    for name, whatsapp in contacts.items():
        if key(name) in same:
            return str(name), tidy(whatsapp) or str(name)
    return None


def spoken(contacts: Any) -> list[str]:
    """The said names, in the list's order."""
    return [str(k) for k in contacts] if isinstance(contacts, dict) else []


def say_who(contacts: Any) -> str:
    """"Dad, Mum or Grandma" (for "you can call ..."); "" when the list is empty."""
    names = spoken(contacts)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " or " + names[-1]


__all__ = ["MAX_CONTACTS", "MAX_NAME", "clean", "find", "key", "say_who", "spoken", "tidy"]
