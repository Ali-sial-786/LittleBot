"""Every fixed line LittleBot speaks through ``AudioService.say()``: what gets recorded in the ChatGPT voice.

``scripts/record_voice_lines.py`` and the dashboard's "Record activity voices" button record these
once (``littlebot.chatgpt.voice_record``); ``AudioService.say()`` then plays the recording of a line
instead of text-to-speech. Sources:

* ``activity:<id>`` - the activities' spoken lines, placeholders filled with the profile's values
  (the child's nickname, favourite animal...) and every item of the data lists they use
  (``littlebot.activities.lines``);
* ``game:<id>`` - what the screen games say (``host.say``: the peekaboo animals' noises);
* ``replies`` - the scripted chat replies and greetings (the offline conversation provider, for the
  child's age now) and the fixed fallback / safety lines of the conversation;
* ``robot`` - the "test speech" phrase.

Lines that can't be known in advance (an AI's answer, what the child said, a video title) are not
here: they keep the text-to-speech voice. Activities that suit the child's age now come first.
"""

from __future__ import annotations

import logging
import os
import types
from dataclasses import dataclass
from typing import Any, Iterable

from littlebot.audio.voice_lines import line_hash

log = logging.getLogger("littlebot.spoken_lines")

GROUPS = ("activities", "games", "replies", "robot")  # --only also takes these (besides activity ids)
WORDS_PER_SECOND = 2.4  # the voice's pace for these lines (slow and warm, for a little one)
SECONDS_PER_LINE = 2.5  # asking, the voice's reply delay, the quiet tail after each line (voice_record.TAIL_QUIET_S)
SECONDS_PER_CALL = 6.0  # connecting a call (a new call every lines_per_call lines)


@dataclass(frozen=True)
class SpokenLine:
    text: str
    source: str  # "activity:<id>" | "game:<id>" | "replies" | "robot"
    uses_name: bool = False  # has the child's nickname in it (only recorded with the parent's OK)
    suitable: bool = True  # an activity that suits the child's age now (recorded first)

    @property
    def hash(self) -> str:
        return line_hash(self.text)

    @property
    def group(self) -> str:
        kind = self.source.split(":", 1)[0]
        return {"activity": "activities", "game": "games"}.get(kind, kind)


def _activity_library(config: Any, library: Any = None) -> Any:
    if library is not None:
        return library
    from littlebot.activities.engine import default_script_dirs  # noqa: PLC0415
    from littlebot.activities.model import ActivityLibrary  # noqa: PLC0415

    lib = ActivityLibrary(default_script_dirs(types.SimpleNamespace(config=config)))
    lib.load()
    return lib


def activity_ids(config: Any, library: Any = None) -> list[str]:
    return list(_activity_library(config, library).ids())


def collect(config: Any, settings: Any, *, library: Any = None, only: Iterable[str] | None = None,
            all_ages: bool = False, for_age: bool = False) -> list[SpokenLine]:
    """The fixed lines, without repeats (by normalised text), activities for the child's age first.

    ``only``: activity ids and / or groups (``GROUPS``); unknown names raise ``KeyError``.
    ``all_ages``: the scripted replies of every age, not just the child's age now.
    ``for_age``: only the activities that suit the child's age now (plus games, replies, robot)."""
    from littlebot.activities.engine import profile_values  # noqa: PLC0415
    from littlebot.activities.lines import spoken_lines  # noqa: PLC0415

    lib = _activity_library(config, library)
    wanted = {str(w).strip() for w in only or () if str(w).strip()}
    unknown = sorted(w for w in wanted if w not in GROUPS and w not in lib)
    if unknown:
        raise KeyError(", ".join(unknown))
    profile = _profile(settings)
    age = _child_age(settings)
    robot_name = str(getattr(config, "robot_name", "") or "LittleBot")

    def want(group: str, name: str = "") -> bool:
        return not wanted or group in wanted or (bool(name) and name in wanted)

    acts = [a for a in lib.all() if want("activities", a.id)]
    suits = {a.id: age is None or a.suitable_for(age) for a in acts}
    if for_age:
        acts = [a for a in acts if suits[a.id]]
    act_lines = spoken_lines(lib, profile_values(profile, robot_name), only=[a.id for a in acts])
    lines = [SpokenLine(x.text, f"activity:{x.activity}", x.uses_name, suits[x.activity]) for x in act_lines]
    first = [x for x in lines if x.suitable]
    later = [x for x in lines if not x.suitable]
    rest: list[SpokenLine] = []
    if want("games"):
        rest += [SpokenLine(t, f"game:{gid}") for gid, texts in _game_lines() for t in texts]
    if want("replies"):
        rest += [SpokenLine(t, "replies") for t in _reply_lines(config, profile, age, robot_name, all_ages)]
    if want("robot"):
        rest += [SpokenLine(t, "robot") for t in _robot_lines()]
    out: dict[str, SpokenLine] = {}
    for line in [*first, *rest, *later]:
        out.setdefault(line.hash, line)
    return list(out.values())


def estimate_seconds(lines: list[SpokenLine], lines_per_call: int = 40) -> float:
    """About how long recording ``lines`` takes (one call per ``lines_per_call`` lines)."""
    if not lines:
        return 0.0
    words = sum(len(x.text.split()) for x in lines)
    calls = -(-len(lines) // max(1, lines_per_call))
    return words / WORDS_PER_SECOND + len(lines) * SECONDS_PER_LINE + calls * SECONDS_PER_CALL


# ------------------------------------------------------------ sources
def _profile(settings: Any) -> dict[str, Any]:
    try:
        p = settings.get("profile") or {}
    except Exception:  # noqa: BLE001
        return {}
    return p if isinstance(p, dict) else {}


def _child_age(settings: Any) -> int | None:
    try:
        return settings.child_age_months()
    except Exception:  # noqa: BLE001
        return None


def _game_lines() -> list[tuple[str, list[str]]]:
    """(game id, what it says with ``host.say``) for the screen games."""
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    try:
        from littlebot.games.peekaboo import PHRASES  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - no pygame: no games either
        log.debug("games unavailable", exc_info=True)
        return []
    return [("peekaboo", list(dict.fromkeys(PHRASES.values())))]


def _reply_lines(config: Any, profile: dict[str, Any], age: int | None, robot_name: str, all_ages: bool) -> list[str]:
    from littlebot.brain.conversation import DIDNT_HEAR, ERROR_FALLBACKS  # noqa: PLC0415
    from littlebot.brain.providers.mock import MockProvider  # noqa: PLC0415
    from littlebot.safety.content import fixed_lines  # noqa: PLC0415

    favourites = [str(a) for a in profile.get("favourite_animals") or []]
    mock = MockProvider(age_months=lambda: age, robot_name=robot_name, favourite_animals=lambda: favourites)
    scripted = mock.scripted_lines(("baby", "toddler", "preschool") if all_ages else None)
    return list(dict.fromkeys([*scripted, *DIDNT_HEAR, *ERROR_FALLBACKS, *fixed_lines()]))


def _robot_lines() -> list[str]:
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    try:
        from littlebot.face.display import TEST_PHRASE  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []
    return [TEST_PHRASE]
