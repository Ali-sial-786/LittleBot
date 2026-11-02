"""Words speech-to-text should listen harder for: the family's world, gathered fresh for each chat.

Deepgram Flux and Cartesia Ink take "keyterms" (https://developers.deepgram.com/docs/keyterm,
https://docs.cartesia.ai/use-the-api/stt/keyterms, read 2026-11-02): names they would otherwise mishear ("Amara"
heard as "a mara", a playlist called "Bedtime Bops"). ``hearing_terms(ctx, history)`` gives them in this order, so
the most useful survive the limits:

1. the robot's name and ``assistant.deepgram_keyterms`` (fixed words: "louder", "volume" ...);
2. people: who built it (``ai.creator``), the baby's nickname (only with ``ai.share_child_name``), and everyone in
   the family list (Family & faces);
3. the skills' names (``Skill.hearing_words``): Spotify playlists and most-played artists, smart-home devices, the
   baby cameras;
4. names from the last few turns ("the Great Wall", "Taylor Swift"), so a follow-up question hears them again.

``assistant.personal_keyterms: false`` keeps only the first group (nothing personal goes to the speech service).

Limits: Deepgram takes at most 100 terms and 500 tokens, and advises 20-50; Cartesia at most 100 terms and 1,200
characters. So at most MAX_TERMS terms of MAX_TERM_CHARS characters, MAX_CHARS characters in all.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Sequence

log = logging.getLogger("littlebot.assistant.keyterms")

MAX_TERMS = 50
MAX_TERM_CHARS = 40
MAX_CHARS = 1000  # under Cartesia's 1,200, and about 300 Deepgram tokens (under its 500)
MAX_RECENT = 12  # names taken from the last few turns
MAX_SKILL_TERMS = 24  # the skills' names together (so the recent turns' still fit)
RECENT_MESSAGES = 6  # the last three exchanges

# A run of capitalised words ("Great Wall of China", "Taylor Swift"), "of" / "the" / "de" allowed inside.
_NAME_RUN = re.compile(r"\b[A-Z][\w'’-]*(?:\s+(?:(?:of|the|de|and)\s+)?[A-Z][\w'’-]*)*")
_SENTENCE_START = re.compile(r"(?:^|[.!?:;\"“]\s*|\n\s*)$")
_COMMON = frozenset("""
i i'm i'll i've i'd a an the it it's this that these those there here what who when where why how which yes no
okay ok oh hey hi hello sure well so and but or if then now today tomorrow tonight let's let me my you your
we our they he she his her them sorry thanks thank please wow done here's there's that's
monday tuesday wednesday thursday friday saturday sunday january february march april may june july august
september october november december mom mum dad mommy daddy baby
""".split())


def _clean(term: Any) -> str:
    t = " ".join(str(term or "").split()).strip(" .,!?;:\"'“”‘’()[]")
    return t if 1 < len(t) <= MAX_TERM_CHARS else ""


def recent_names(history: Sequence[dict[str, Any]]) -> list[str]:
    """Names in the last few turns' words, newest first: capitalised words that don't start a sentence, or runs
    of them ("The Great Wall" -> "Great Wall"); common words ("Okay", "Monday") left out."""
    out: list[str] = []
    for m in reversed(list(history)[-RECENT_MESSAGES:]):
        text = str(m.get("content") or "") if isinstance(m, dict) else ""
        for match in _NAME_RUN.finditer(text):
            words = match.group(0).split()
            at_start = bool(_SENTENCE_START.search(text[:match.start()]))
            if at_start and len(words) > 1 and words[0].lower().endswith("ing"):
                words = words[1:]  # "Playing Taylor Swift"
            while words and words[0].lower() in _COMMON:  # "The Great Wall", "Okay Taylor"
                words = words[1:]
            if not words:
                continue
            if len(words) == 1 and (at_start and words[0] == match.group(0).split()[0]):
                continue  # one capitalised word starting a sentence: just a sentence ("Sure", "Rain")
            name = " ".join(words)
            if name.lower() not in _COMMON and name not in out:
                out.append(name)
    return out[:MAX_RECENT]


def fit(terms: Iterable[Any]) -> list[str]:
    """The terms cleaned, without repeats (whatever the case), in order, within the limits."""
    out: list[str] = []
    seen: set[str] = set()
    chars = 0
    for raw in terms:
        t = _clean(raw)
        if not t or t.lower() in seen:
            continue
        if len(out) >= MAX_TERMS or chars + len(t) > MAX_CHARS:
            break
        seen.add(t.lower())
        out.append(t)
        chars += len(t)
    return out


def _people(ctx: Any) -> list[str]:
    out: list[str] = []
    try:
        from littlebot.brain.identity import creator  # noqa: PLC0415

        made_by = creator(ctx.config)
        if made_by:
            out.append(made_by.split(",")[0])
    except Exception:  # noqa: BLE001
        log.debug("keyterms: no creator", exc_info=True)
    try:
        from littlebot.brain.prompts import clean_child_name  # noqa: PLC0415

        nick = clean_child_name((ctx.settings.get("profile") or {}).get("nickname"))
        if nick and bool(getattr(ctx.config.ai, "share_child_name", True)):
            out.append(nick)
    except Exception:  # noqa: BLE001
        log.debug("keyterms: no nickname", exc_info=True)
    try:
        from littlebot.vision.faces import PeopleStore  # noqa: PLC0415

        out += [str(p.get("name") or "") for p in PeopleStore(ctx.db).people()]
    except Exception:  # noqa: BLE001 - no family list (no database, a test)
        log.debug("keyterms: no family list", exc_info=True)
    return out


def _skills(ctx: Any) -> list[str]:
    words = getattr(getattr(ctx, "skills", None), "hearing_words", None)
    try:
        return list(words()) if words is not None else []
    except Exception:  # noqa: BLE001
        log.debug("keyterms: a skill's words failed", exc_info=True)
        return []


def hearing_terms(ctx: Any, history: Sequence[dict[str, Any]] = ()) -> list[str]:
    """The keyterms for speech-to-text now (see the module's docstring), within the limits."""
    a = ctx.config.assistant
    fixed = [ctx.config.robot_name, *getattr(a, "deepgram_keyterms", ())]
    if not getattr(a, "personal_keyterms", True):
        return fit(fixed)
    return fit([*fixed, *_people(ctx), *fit(_skills(ctx))[:MAX_SKILL_TERMS], *recent_names(history)])


__all__ = ["MAX_CHARS", "MAX_TERMS", "fit", "hearing_terms", "recent_names"]
