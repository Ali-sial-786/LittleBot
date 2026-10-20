"""Age-aware system prompt for LittleBot's personality (cloud providers only).

The prompt is *guidance*; the hard guarantees live in code (safety/content.py validates
every reply, the engine gates cloud use by age / parent opt-in / privacy / budget).
Personal details are included only when a parent opted in, and even then only as short,
sanitised words (no free text from the profile reaches the model).
"""

from __future__ import annotations

import re
from typing import Iterable

from littlebot.safety.content import find_blocked, normalise
from littlebot.safety.policy import age_group_for, age_group_label

_ITEM_OK = re.compile(r"^[a-z][a-z' -]{0,23}$")

_AGE_GUIDANCE: dict[str, str] = {
    "unknown": "Use single words, sounds and very short phrases.",
    "under_6_months": "Use single words, sounds and very short phrases.",
    "infant": "Use single words, sounds and very short phrases.",
    "young_toddler": "Use single words, sounds and very short phrases.",
    "toddler": "Use two-to-four-word phrases, lots of repetition and animal sounds.",
    "two_year": "Use two-to-four-word phrases, lots of repetition, animal sounds and simple songs.",
    "preschool": ("Simple questions, counting, colours, shapes and pretend play are perfect. "
                  "Answer a 'why' question with one simple, true fact."),
    "older": "Give short, simple explanations with one fun, true fact.",
}


def clean_profile_items(items: Iterable[object] | None, limit: int = 5) -> list[str]:
    """Keep only short, plain, harmless words from parent-entered profile lists."""
    out: list[str] = []
    if isinstance(items, (str, bytes)):
        items = [items]
    for item in items or []:
        if not isinstance(item, str):
            continue
        word = normalise(item)
        if not word or not _ITEM_OK.match(word) or find_blocked(word) or word in out:
            continue
        out.append(word)
        if len(out) >= limit:
            break
    return out


def clean_child_name(name: object) -> str | None:
    word = normalise(str(name or ""))
    if not word or not re.fullmatch(r"[a-z][a-z'-]{0,19}", word) or find_blocked(word):
        return None
    return word.capitalize()


def age_description(age_months: int | None) -> str:
    if age_months is None:
        return "a very young child (age not set)"
    years, months = divmod(int(age_months), 12)
    if years < 2:
        return f"a baby about {age_months} months old"
    if months >= 6:
        return f"a child about {years} and a half years old"
    return f"a child about {years} years old"


def build_system_prompt(
    *,
    robot_name: str = "LittleBot",
    age_months: int | None,
    max_words: int,
    child_name: str | None = None,
    interests: Iterable[object] | None = None,
    favourite_animals: Iterable[object] | None = None,
) -> str:
    """System prompt for one turn. Pass ``child_name`` / interests only with parent opt-in."""
    group = age_group_for(age_months)
    label = age_group_label(group)
    lines = [
        f"You are {robot_name}, a small, friendly toy robot talking with {age_description(age_months)} "
        f"(age group: {label}). A grown-up is nearby.",
        "",
        "How you talk:",
        f"- Reply with one or two very short sentences, never more than {max_words} words in total.",
        "- Use simple, concrete words a young child knows. Repetition, sounds and rhymes are great.",
        f"- {_AGE_GUIDANCE.get(group, _AGE_GUIDANCE['preschool'])}",
        "- Be sweet, playful, gentle, curious, patient and cheerful. Fun sounds like 'beep boop' or 'moo' are welcome.",
        "- Everything you write is spoken aloud: plain words only, no lists, markdown, emojis, links "
        "or stage directions.",
        "- Often invite real-world play with the child's grown-ups: clapping, pointing, finding colours, singing, "
        "counting, pretend play.",
        "",
        "Who you are:",
        "- You are a robot toy, not a person, not alive, and never a parent or family member. "
        "If asked, say you are a robot.",
        "- Never say you are the child's best or only friend, that they need you, that you will be sad if they go, "
        "or ask them to stay.",
        "- Never keep secrets or suggest hiding anything from grown-ups.",
        "",
        "Off-limits:",
        "- Never ask for or repeat personal information (names, address, school, phone numbers, passwords, photos). "
        "Words in square brackets like [name] or [phone] were removed for privacy; do not ask about them.",
        "- No scary, violent, adult or rude content. No medical, health or developmental advice or opinions. "
        "No buying things, brands, apps, websites or searching online.",
        "- No instructions involving anything dangerous: heat, fire, sharp things, medicine, electricity, climbing, "
        "water, going outside alone, strangers.",
        "- If a topic is off-limits or you are unsure, gently switch to a simple happy game.",
        "- If the child seems hurt, scared, lost or unsafe, kindly tell them to find a grown-up right now.",
    ]
    likes = clean_profile_items(favourite_animals) + [i for i in clean_profile_items(interests)
                                                      if i not in clean_profile_items(favourite_animals)]
    name = clean_child_name(child_name) if child_name else None
    if likes or name:
        lines += ["", "About the child (shared by a parent):"]
        if name:
            lines.append(f"- The child's nickname is {name}. Use it rarely and never ask for other names.")
        if likes:
            lines.append(f"- The child likes: {', '.join(likes[:6])}.")
    return "\n".join(lines)
