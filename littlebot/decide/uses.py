"""What LittleBot asks the decision model, feature by feature (see littlebot/decide/__init__.py for the list).

Each helper builds the state (only the fields its question needs: Jev reads a large state worse, and nothing
more leaves the robot than the decision needs; the person's words have personal details removed), asks, and
returns the typed answer, or None (off, failed, not sure). The callers decide what to do with it and log the
comparison with LittleBot's own way (``Decider.compare``).
"""

from __future__ import annotations

from typing import Any

from littlebot.decide.base import Answer, Choice, YesNo
from littlebot.decide.service import get_decider

TEXT = 300  # one line of the conversation
STATE_LINES = 6

# ---------------------------------------------------------------- cards (littlebot/screens/director.py)
CARD_NONE = "none"
CARD_CLOSE = "close"
CARD_STEER = "steer"


def _clean(text: Any, limit: int = TEXT) -> str:
    from littlebot.safety.content import sanitize_user_text  # noqa: PLC0415

    return sanitize_user_text(" ".join(str(text or "").split()), max_chars=limit)


def card_options(kinds: dict[str, str], card_up: str | None, actions: list[str]) -> dict[str, str]:
    """"none" first (Jev leans to the first option: the cautious one), then close / steer with a card up, then a
    "show_<kind>" per card kind."""
    opts = {CARD_NONE: "Nothing worth showing on the screen: chit-chat, songs, games, talking to the baby, a "
                       "question with nothing to see, or LittleBot hasn't actually given the answer yet (it only "
                       "says hold on or let me check; an answer cut off because it is still being said counts as "
                       "given)."}
    if card_up:
        opts[CARD_CLOSE] = (f"They want the {card_up} card on the screen gone, or the talk has moved on to something "
                            "it doesn't fit and isn't worth a card. A new question worth a card is a new card "
                            "(show_...), not close.")
        if actions:
            opts[CARD_STEER] = (f"They follow up on the {card_up} card on the screen (it can: "
                                f"{', '.join(actions)[:200]}), e.g. \"and tomorrow?\" or \"next step\".")
    for kind, desc in kinds.items():
        opts[f"show_{kind}"] = f"Show a new {kind} card: {str(desc)[:240]}"
    return opts


async def card_pick(ctx: Any, *, kinds: dict[str, str], card_up: str | None, actions: list[str],
                    lines: list[tuple[str, str]], answer: str | None, partial: bool = False,
                    robot: str = "LittleBot") -> Answer | None:
    """``partial``: ``answer`` is only the first words the voice has said so far (it is still saying the rest)."""
    d = get_decider(ctx)
    convo = [f"{'They said' if who == 'heard' else robot}: {_clean(t) if who == 'heard' else t[:TEXT]}"
             for who, t in lines[-STATE_LINES:]]
    state = {"card_on_screen": card_up or "none", "conversation": convo}
    if answer and partial:  # cut off mid-sentence, it read as "hasn't answered yet" (none, on the robot 2026-10-29)
        state[f"{robot}_answer_first_words"] = (f"{answer[:600]} ... (still being said: {robot} is answering, "
                                                "the rest follows)")
    else:
        state[f"{robot}_answer_being_said"] = (answer or "")[:600] or "(no answer yet)"
    q = Choice(f"{robot} is a small home robot with a screen, answering out loud. Given what they just said and "
               f"{robot}'s answer, what should its screen do? A card shows the gist of {robot}'s answer (a "
               "forecast, a word's meaning, key points, a recipe, a timer, a sum, a fact); most turns show nothing.",
               card_options(kinds, card_up, actions))
    answers = await d.ask("cards", state, {"screen": q})
    return (answers or {}).get("screen")


# ---------------------------------------------------------------- hand-off routing (littlebot/chatgpt/live.py)
ROUTES = {
    "robot": "Anything else: a question to answer, live information (weather, time, news), remembering something, "
             "or doing something with the robot or its app (videos, songs, games, activities, volume, sleep, its "
             "own status, what went wrong, its debug log).",
    "face": "Only make a face or face animation on the robot's screen (happy, sad, surprised, wink, spin eyes, "
            "peekaboo, hearts, confetti), nothing else.",
    "look": "Look through the robot's camera at something now to answer (what do you see, what am I holding, how "
            "many fingers, what colour is this, do you see me).",
    "who": "Say who is here or who is talking, by recognising the family's faces (who am I, who is this, do you "
           "know me).",
}


async def route(ctx: Any, question: str, last_heard: str = "") -> Answer | None:
    state = {"handed_to_the_robot": _clean(question, 600), "they_said_last": _clean(last_heard)}
    q = Choice("A voice assistant handed this request to its robot. Which part of the robot should handle it?",
               ROUTES)
    answers = await get_decider(ctx).ask("routing", state, {"route": q}, timeout=1.5)
    return (answers or {}).get("route")


# ---------------------------------------------------------------- has LittleBot answered? (director)
async def answered(ctx: Any, *, asked: str, said: str, robot: str = "LittleBot") -> Answer | None:
    state = {"they_asked": _clean(asked), f"{robot}_said": said[:TEXT]}
    q = YesNo(f"Does {robot}'s line give them an actual answer or the information they asked for?",
              yes="It answers or tells them something, even briefly.",
              no=f"It only says {robot} is thinking, checking, looking it up or that the answer is coming "
                 "(hold on, one moment, let me see), or it is just a greeting.")
    answers = await get_decider(ctx).ask("answered", state, {"answered": q}, timeout=1.5)
    return (answers or {}).get("answered")


# ---------------------------------------------------------------- still about the browser page? (director)
async def about_the_page(ctx: Any, *, page: str, said: str) -> Answer | None:
    state = {"web_page_on_the_screen": str(page or "a web page")[:200], "they_just_said": _clean(said)}
    q = YesNo("Is what they just said about the web page showing on the robot's screen (what it says, the search "
              "results, scrolling, opening a result, going back, the pictures on it)?",
              yes="It is about the page or steering it.",
              no="It is about something else (a new question, chit-chat, the baby).")
    answers = await get_decider(ctx).ask("browser", state, {"page": q}, timeout=1.5)
    return (answers or {}).get("page")


# ---------------------------------------------------------------- called the grown-up by the baby's name? (live)
async def talked_as_if_baby(ctx: Any, *, grown_up_said: str, reply: str, nick: str,
                            robot: str = "LittleBot") -> Answer | None:
    state = {"grown_up_said": _clean(grown_up_said), f"{robot}_replied": reply[:TEXT],
             "the_baby": f"{nick}, too young to talk"}
    q = YesNo(f"Does {robot}'s reply talk to the grown-up who just spoke as if they were {nick}, the baby: "
              f"calling them {nick}, or talking to them like a baby?",
              yes=f"It talks to the grown-up as {nick} or as a baby.",
              no=f"It talks to the grown-up as a grown-up (it may mention {nick}).")
    answers = await get_decider(ctx).ask("naming", state, {"as_baby": q}, timeout=2.0)
    return (answers or {}).get("as_baby")


# ---------------------------------------------------------------- which video? (littlebot/chatgpt/tools.py)
async def best_video(ctx: Any, *, query: str, results: list[dict[str, Any]], child: str) -> Answer | None:
    opts = {}
    for i, r in enumerate(results[:8]):
        mins = r.get("duration_seconds")
        length = f", {round(mins / 60)} min" if isinstance(mins, (int, float)) and mins else ""
        opts[f"video_{i}"] = f"\"{str(r.get('title') or '')[:120]}\" by {str(r.get('channel') or '?')[:60]}{length}"
    if len(opts) < 2:
        return None
    state = {"they_asked_for": _clean(query, 200), "who_watches": child}
    q = Choice("Which video best matches what they asked for and is the most suitable for this young child to "
               "watch (a real song or show for small children, not a compilation of other things, a reaction video "
               "or something scary)?", opts)
    answers = await get_decider(ctx).ask("videos", state, {"video": q})
    return (answers or {}).get("video")


# ---------------------------------------------------------------- which activity / game did they mean? (tools)
NO_MATCH = "none_of_these"


async def which_one(ctx: Any, *, wanted: str, items: list[dict[str, Any]], what: str) -> Answer | None:
    opts = {}
    for it in items[:60]:
        name = str(it.get("name") or it.get("id") or "")
        if it.get("id"):
            opts[str(it["id"])] = name[:100] + (f" ({', '.join(map(str, it.get('tags') or []))[:80]})"
                                                if it.get("tags") else "")
    if not opts:
        return None
    opts[NO_MATCH] = f"None of these {what}s is what they asked for."
    state = {"they_asked_for": _clean(wanted, 120)}
    q = Choice(f"Which of the robot's {what}s did they ask for?", opts)
    answers = await get_decider(ctx).ask("names", state, {"which": q}, timeout=1.5)
    return (answers or {}).get("which")


# ---------------------------------------------------------------- said to the robot, or room talk? (live)
async def addressed(ctx: Any, *, said: str, robot_said: str = "", robot: str = "LittleBot") -> Answer | None:
    state = {"said_while_it_talked": _clean(said), f"{robot}_was_saying": robot_said[:TEXT]}
    q = YesNo(f"Someone in a family's room talked while {robot}, their home robot, was speaking. Were these words "
              f"said to {robot}?",
              yes=f"Said to {robot}: a question or request for it, an answer to what it said, or telling it to "
                  "stop, wait or change something.",
              no="Said to someone else in the room (family talking to each other, to the baby, on the phone), or "
                 "talking to themselves.")
    answers = await get_decider(ctx).ask("addressed", state, {"to_robot": q}, timeout=1.5)
    return (answers or {}).get("to_robot")


# ---------------------------------------------------------------- someone in danger? (live; raise-only)
CONCERN_AT = 0.85  # only adds an alert (never removes one), and only when this sure


async def concern(ctx: Any, *, said: str) -> Answer | None:
    state = {"heard_in_the_room": _clean(said)}
    q = YesNo("Could a child or anyone in the room be hurt, in danger or in an emergency right now, or need a "
              "grown-up's help urgently?",
              yes="Someone may be hurt, in danger, choking, burned, near water, has swallowed something, can't "
                  "breathe, or there is a fire or other emergency.",
              no="Ordinary talk, play, questions, stories or pretend.")
    answers = await get_decider(ctx).ask("concern", state, {"danger": q}, timeout=2.0)
    return (answers or {}).get("danger")


__all__ = ["CARD_CLOSE", "CARD_NONE", "CARD_STEER", "CONCERN_AT", "NO_MATCH", "ROUTES", "about_the_page", "addressed",
           "answered", "best_video", "card_options", "card_pick", "concern", "route", "talked_as_if_baby", "which_one"]
