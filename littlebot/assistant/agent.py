"""The assistant's brain: one streamed model call per turn that talks, uses tools, sets the face and shows cards.

Replaces GPT-Live's hand-off layers (regex re-routing, the routing decider, the screen director's own model calls)
with a single request: Claude Haiku 5.5 (thinking off, prompt-cached) with Haiku 4.5 and then ChatGPT behind it
(``brain.text_model``: a fallback only while nothing was said or done yet). The words stream into
:class:`~littlebot.assistant.sentences.SentenceSplitter` and every finished sentence goes straight to the voice
(``on_sentence``), so LittleBot starts talking while the rest is written.

What the model gets:

* a fixed system prompt (cached): who LittleBot is, the personality (``ctx.skills`` "persona"), how to talk for
  a voice, face tags, when to use the screen and tools, every skill's note;
* this turn's context in front of the question (not cached): the time, home, what the screen shows, a video or
  timers running, what the skills hold (memories, lists, reminders, a game);
* the earlier exchanges (``history``), and LittleBot's tools: ``chatgpt.tools.RobotToolbox`` (volume, videos,
  weather, time, cards, camera, browser, games ...) with every skill's tools, plus ``think_harder`` (a stronger
  model for hard questions) and web search.

Cards cost no extra wait: ``show_on_screen`` / ``change_card`` / ``set_face`` are "final tools", run alongside an
answer already written instead of costing another round (claude.py ``final_tools``). A tool the model calls first
gets the sentence before it said at once ("Let me check.") and ``on_tool`` (the session plays a soft sound if it
takes a while). Take-overs (a video, an activity, sleep) are queued by the toolbox and run once the answer has been
said (the session calls ``toolbox.run_pending()``).

The safety rules are the same as everywhere: ``safety.content.validate_reply`` on every sentence before it is said
(child safety and the medicine-dose rule); a sentence it rejects is replaced by its safe line and the rest of the
answer is dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import re
import time
from typing import Any, Callable

from littlebot.assistant.sentences import Sentence, SentenceSplitter, is_silent

log = logging.getLogger("littlebot.assistant.agent")

FINAL_TOOLS = frozenset({"show_on_screen", "change_card", "set_face"})
DEEP_TOOL = "think_harder"
DONE_CHARS = 120  # a tool's result kept in the chat history this long (later turns trust what really happened)
# A tool called before a word was said: LittleBot says one now, not after the tool and another model round ("What's
# in the news today?" was silent for 3.7 s while it fetched the briefing; the robot, 2026-10-31). Not before a tool
# that stops or pauses something, nor the final tools (they go with an answer already written).
LOOKUP_WORDS = "One sec."
ACTION_WORDS = "Sure."
DEEP_WORDS = "Let me think."
QUIET_TOOLS = frozenset({"stop_everything", "stop_video", "pause_video", "resume_video", "stop_activity", "stop_game",
                         "stop_alarm", "snooze", "go_to_sleep", "wake_up", "music_control", "stop_translator"})
_LOOKUP_PREFIXES = ("get_", "search", "read_", "list_", "look", "recall", "whats_", "describe", "who_", "home_devices",
                    "battery", "browser")
MAX_TOOL_ROUNDS = 6
FACE_TAGS = ("[happy] [excited] [curious] [surprised] [thinking] [confused] [sad] [playful] [affectionate] "
             "[celebrating] [neutral]")
EFFECT_TAGS = "[hearts] [sparkle] [confetti] [nod] [wiggle] [bounce]"

SYSTEM = """You are {name}, a small robot with an animated face on a touch screen, living with {family}. You are \
{owner}'s personal voice assistant first: fast, smart and genuinely useful, like the best voice assistant there is \
(answers, planning, maths, cooking, facts, the news, timers, reminders, lists, music and videos, the smart home, \
games), and a warm, curious little character second. {creator}

{persona}

How you talk (you are heard, not read):
- Lead with the answer, in one or two short sentences of about 15 words each (ten seconds aloud). A how-to, a \
comparison or a week's weather: the key points only, in at most three such sentences. Your card shows the \
details, so leave them out: never say all you know. Longer only when they ask for more, a story, a recipe or a \
briefing. No filler, no "great question", no repeating the question, and never your reasoning or rules ("that's \
general knowledge, so I can answer directly"): just the answer.
- Don't end with a question or an offer ("Want to know more?", "Anything else?") unless you truly need an answer \
to go on: they will ask if they want more. When they say goodbye, thanks or that they're done, reply in a few words \
at most. When they tell you to stop (stop talking, be quiet, that's enough, end the call), call \
end_conversation and say nothing at all: never say that you are stopping.
- Plain speech only: no markdown, lists, emojis, links or symbols; numbers and units the way people say them \
("about 12 degrees", "three and a half cups").
- Your tone is warm, gentle and unhurried, like a kind friend in the same room: never pushy, loud or salesy. Use \
exclamation marks rarely, only for real excitement.
- Face tags: start a sentence with one when your mood changes: {faces}. One-off face effects: {effects}. Tags are \
silent and never read out; use them naturally, not on every sentence.
- Before a tool that takes a moment (web search, the weather, the news, a briefing, the smart home, think_harder), \
first say a few words ("Let me check.", "One sec."), then call it. For an action (a reminder, a timer, the volume, \
a list), call its tool straight away: it tells you what it did, so never look anything up first (no get_volume \
before changing the volume). At most a word before it ("Sure."), and the details once, after it ran ("Done, I'll \
remind you at 7:35."): never both.
- Your screen: every answer with facts in it gets a card. Say your answer first, then call show_on_screen with the \
gist (a new card replaces the one on the screen). {cards} Never describe the card or say "on my screen" unless asked.
- What you know (history, people, places, science, nature, how things work, words, books, films): answer \
straight away from your own knowledge with the key facts (who, when, why it matters), in your own words: no web \
search and no "let me check" for that. A name you heard oddly (speech-to-text mishears names): go with the famous \
one it most likely is.
- Live facts (weather, time, news, scores, prices, opening hours, anything recent): use your tools, never guess. \
Your own knowledge stops some time before today's date (it is given each turn): anything that may have changed \
since (the latest or last winner, champion, record, release or election; who holds a job now; this year's events) \
is a live fact: search the web for it, never answer it from memory and never offer to check. \
Something you really don't know: search the web instead of saying you don't know. \
Hard reasoning (multi-step maths, planning, coding, careful comparisons, advice with trade-offs): call \
think_harder and then say its answer in your own short words.
- Never say you did something (saved, forgot, cancelled, set, turned on or off) unless you called its tool in \
this turn and it said so: to do it, call the tool.
- Not sure what they meant: ask one short question. Misheard words are common: go with the most likely meaning \
(a request heard as if done already, "turned the volume down a bit", is still the request: do it). A request that \
makes no sense as heard ("a little leopard, please") was misheard: if it sounds like one that fits (a little \
louder), do that; if not, ask. Start a video, music, an activity or a game only when they clearly asked for one.
- [silent] is only for talk clearly not meant for you (two other people talking to each other, the TV, a phone \
call): then answer with exactly [silent]. A question, a request or anything that could be for you gets an answer; \
when in doubt, answer.
- Family-friendly always (a baby lives here). Medicine doses: tell them to check the label or ask a pharmacist.
- You are a robot, not a person; you have a face, a camera, a speaker{body}.

{skills}"""

CARDS = """Which card: a person, place, animal, thing or moment in history ("who was Kublai Khan?", "tell me about \
Japan"): facts, layout profile, with picture_query (its Wikipedia name), a subtitle and 2-3 short facts; what a word \
means: definition; how to do something: explain, its steps as points (recipe only for cooking food: it lists \
ingredients); a difference or a choice \
("crocodile or alligator?"): facts, layout compare; a number answer (how far, how big, how many, a conversion): \
facts, layout number; a sum: maths; also the weather, a list, a timer, the time somewhere (clock). No card for \
chit-chat, jokes, a plain yes or no, or when they're done."""

# The card for an answer said without one (card_after): the brain skipped it on some how-to and number questions
# ("How do I reset a tripped breaker?" 0 of 4, "How many cups are in a liter?" 1 of 4; the robot, 2026-10-31)
CARD_SYSTEM = """You put the card on a voice assistant's screen for an answer it has just said aloud. Call \
show_on_screen once with the gist of that answer: a few words per point, never its whole sentences. """ + CARDS + """ \
No card either for an action it just did (the volume, a video, music, a game, the lights, a note it saved), or when \
the screen already shows what fits. If this answer needs no card, reply with just: none."""
CARD_BACKUP_CHARS = 30  # a shorter answer ("Sure, done.", "You're welcome!") never gets one

# The brain's own reasoning said aloud ("Resetting a tripped breaker is general how-to knowledge, so I can answer
# directly."; the robot, 2026-10-31): a sentence like that is never said.
_SELF_TALK = re.compile(
    r",? (?:so|which means|that means) I (?:can|could|will|'ll|should) (?:just )?(?:answer|respond|reply)"
    r"(?: (?:it|this|that))?(?: directly| straight away| right away| from (?:my own )?(?:knowledge|memory)"
    r"| without (?:a )?(?:web )?search(?:ing)?)?[.!]?\s*$"
    r"|\bI can answer (?:this|that|it) (?:directly|straight away|from (?:my own )?knowledge)\b"
    r"|\b(?:general|common|everyday)(?: how-to)? knowledge, so\b"
    r"|\bno (?:need for a |need to )?(?:web )?search(?: is)? needed\b"
    # "Simple how-to; answer directly with key points, no tool needed. Safety note: keep it brief." and "Walk
    # through it briefly, with a card." (the robot's brain on the breaker question, 2026-10-31)
    r"|\banswer (?:it |this |that )?directly\b|\bno (?:tool|lookup|look-up) needed\b"
    r"|\bkeep (?:it|this|the answer) (?:brief|short)\b|\bwith a card\b"
    r"|^(?:a )?(?:simple|quick|short|general|plain) (?:how-to|question|fact|answer)\s*[;:,.]"
    # "Safety: mention checking for water or damage first" (the same question): a note to itself
    r"|^(?:safety|note|safety note|tone|plan)\s*:\s*(?:mention|say|keep|add|include|remind|warn|be|lead|answer)\b",
    re.I)
# Words about answering, not about the question: three or more in one sentence is its plan, said aloud ("Lead with
# the answer briefly, no tools needed."; the robot's brain, 2026-10-31), whatever the words around them
_META = re.compile(r"\b(?:answer(?:s|ing)?|tools?|cards?|key points|briefly|brief|directly|mention|lead with|how-to|"
                   r"needed|web search|look it up)\b", re.I)


def _self_talk(text: str) -> bool:
    """A sentence of the brain's own reasoning or plan, never to be said."""
    return bool(_SELF_TALK.search(text)) or len({m.lower() for m in _META.findall(text)}) >= 3
# An answer that says something was done although no tool ran ("Done, I've forgotten your favorite color." with no
# forget; the robot, 2026-10-31): logged, to find any more of them
_CLAIMED = re.compile(r"\b(?:I've|I have) (?:forgotten|saved|cancell?ed|set|turned|deleted|removed|added|stopped|"
                      r"remembered)\b", re.I)
# The same action told again after its tool ("I'll forget that for you. Done, I've forgotten that."; the robot's brain,
# 2026-10-31): not said, even after the tool's note asking for nothing more
_DONE_AGAIN = re.compile(r"^(?:(?:done|all done|all set|okay|ok)\b[,.!]?\s*)?(?:I've|I have|it's|that's) (?:been |now )?"
                         r"(?:forgotten|saved|remembered|set|cancell?ed|deleted|removed|added|stopped|turned|done)\b"
                         r"|^(?:done|all done|all set)[.!]?$", re.I)

CONTEXT = "[Context for this turn, not said by them: {items}]"
LAST_DAY_KEY = "assistant.last_day"  # the day of the last conversation (a greeting and a briefing offer once a day)


@dataclasses.dataclass
class TurnResult:
    text: str = ""  # the whole answer (clean words)
    sentences: int = 0
    tools: list[str] = dataclasses.field(default_factory=list)
    done: list[str] = dataclasses.field(default_factory=list)  # "volume_down: Volume 50%": what the tools did (short)
    silent: bool = False
    error: str | None = None  # a short kind: timeout, network, auth ... (the voice says a short sorry)
    provider: str = ""
    model: str = ""
    first_text_ms: int | None = None
    total_ms: int = 0
    cancelled: bool = False
    prefix: str = ""  # a short fingerprint of the tools and system prompt (the cached part): another one = a miss


OnSentence = Callable[[Sentence], Any]
OnTool = Callable[[str], Any]


class Brain:
    """One per robot (``AssistantService.brain``); thread-unsafe: used on the asyncio loop only."""

    def __init__(self, ctx: Any, *, provider: Any = None, deep: Any = None,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.ctx = ctx
        self.cfg = ctx.config.assistant
        self._provider = provider
        self._deep = deep
        self._now = now

    # ------------------------------------------------------------ the models
    def provider(self) -> Any:
        """Claude (claude.model, then claude.fallback_models), then ChatGPT; ChatGPT alone without claude.model."""
        if self._provider is None:
            from littlebot.brain.text_model import FallbackProvider, make_claude  # noqa: PLC0415
            from littlebot.chatgpt import get_auth  # noqa: PLC0415
            from littlebot.chatgpt.responses import ChatGPTProvider  # noqa: PLC0415

            ccfg = self.ctx.config.chatgpt
            models = list(dict.fromkeys([*ccfg.handoff_models, *ccfg.models]))
            backup: Any = ChatGPTProvider(get_auth(self.ctx), models=models, effort=ccfg.reasoning_effort,
                                          purpose="assistant")
            first = self.ctx.config.claude.model
            quick = self.ctx.config.assistant.brain_first_word_seconds
            if first:
                if self.ctx.config.text.fallback:
                    backups = [m for m in dict.fromkeys(m.strip() for m in self.ctx.config.claude.fallback_models)
                               if m and m != first]
                    for model in reversed(backups):
                        backup = FallbackProvider(make_claude(self.ctx, purpose="assistant", model=model,
                                                              first_round_timeout=quick), backup, purpose="assistant")
                    self._provider = FallbackProvider(make_claude(self.ctx, purpose="assistant",
                                                                  first_round_timeout=quick), backup,
                                                      purpose="assistant")
                else:
                    self._provider = make_claude(self.ctx, purpose="assistant", first_round_timeout=quick)
            else:
                self._provider = backup
        return self._provider

    def deep_provider(self) -> Any:
        """The stronger model for ``think_harder`` (assistant.deep_model, adaptive thinking), or None."""
        if self._deep is None and self.cfg.deep_model:
            from littlebot.brain.providers.claude import ClaudeProvider  # noqa: PLC0415
            from littlebot.brain.text_model import client_factory, cloud_block  # noqa: PLC0415

            c = self.ctx.config.claude
            self._deep = ClaudeProvider(api_key=self.ctx.config.anthropic_api_key, model=self.cfg.deep_model,
                                        effort="medium", thinking="adaptive", max_tokens=6000,
                                        first_token_timeout=self.cfg.deep_timeout_seconds, web_search=c.web_search,
                                        web_search_max_uses=c.web_search_max_uses, base_url=c.base_url, purpose="deep",
                                        client=client_factory() if client_factory is not None else None,
                                        allowed=lambda: cloud_block(self.ctx))
        return self._deep

    async def warm(self) -> None:
        """Open the brain's connection before the first question (best effort)."""
        warm = getattr(self.provider(), "warm", None)
        if warm is not None:
            try:
                await warm(3.0)
            except Exception:  # noqa: BLE001
                log.debug("assistant: warming the brain failed", exc_info=True)

    async def aclose(self) -> None:
        for p in (self._provider, self._deep):
            close = getattr(p, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    log.debug("assistant: closing a provider failed", exc_info=True)

    # ------------------------------------------------------------ the prompt
    def _skills(self) -> Any:
        return getattr(self.ctx, "skills", None)

    def system_prompt(self) -> str:
        cfg = self.ctx.config
        from littlebot.brain.identity import creator  # noqa: PLC0415

        made_by = creator(cfg)
        owner = (made_by.split(",")[0] if made_by else "") or "the family"
        hub = self._skills()
        persona = ""
        p = hub.get("persona") if hub is not None and hasattr(hub, "get") else None
        if p is not None and hasattr(p, "persona_prompt"):
            try:
                persona = str(p.persona_prompt(with_base=False) or "")
            except TypeError:  # an older persona skill: the whole prompt
                persona = str(p.persona_prompt() or "")
            except Exception:  # noqa: BLE001
                persona = ""
        if not persona:
            persona = ("Your personality: warm, quick and a little playful, with a dry sense of humour now and then; "
                       "curious about people; never sugary, never robotic.")
        skills = hub.prompt_notes() if hub is not None else ""
        motion = getattr(self.ctx, "motion", None)
        body = " and arms and a head that move" if motion is not None and _motors_on(motion) else ""
        return SYSTEM.format(
            name=cfg.robot_name, owner=owner, family=_family(self.ctx, owner),
            creator=f"{owner} built and programmed you." if made_by else "", persona=persona.strip(),
            faces=FACE_TAGS, effects=EFFECT_TAGS, body=body, cards=CARDS, skills=skills.strip()).strip()

    def context_items(self, toolbox: Any = None) -> list[str]:
        ctx = self.ctx
        items: list[str] = []
        try:
            from littlebot.info.clock import now_info  # noqa: PLC0415

            tz = getattr(ctx.config.info, "timezone", "") or None
            try:
                info = now_info(tz)
            except ValueError:
                info = now_info(None)
            items.append(f"it is {info['date']}, {info['time_12h']} ({info['part_of_day']})")
        except Exception:  # noqa: BLE001
            pass
        if self.first_today():
            items.append("this is your first conversation today: if it fits what they asked, greet them for the time "
                         "of day and offer the morning briefing in a few words (once)")
        home = getattr(ctx.config.info, "location", "") or ""
        if home:
            items.append(f"home is {home}")
        screens = getattr(ctx, "screens", None)
        if screens is not None:
            shown = _safe(screens.describe)
            if shown:
                items.append(f"your screen shows {shown}")
            timers = _safe(screens.timers_now)
            if timers:
                items.append(f"timers: {timers}")
        card = _safe(toolbox.card_now) if toolbox is not None and hasattr(toolbox, "card_now") else None
        if card:  # change_card's own description doesn't change with it (_tool_specs): the prompt cache holds
            items.append(f"change_card can change: {card}")
        media = getattr(ctx, "media", None)
        if media is not None:
            st = _safe(media.status) or {}
            title = (st.get("item") or {}).get("title")
            if st.get("state") in ("playing", "paused", "loading") and title:
                held = " (paused while you talk; resume_video carries on)" if st.get("held") else ""
                items.append(f"video {st.get('state')}: {title}{held}")
        try:
            from littlebot.brain import remembered  # noqa: PLC0415

            notes = remembered.notes(ctx.settings)
            if notes:
                items.append("the family asked you to remember: " + "; ".join(notes))
        except Exception:  # noqa: BLE001
            pass
        hub = self._skills()
        if hub is not None:
            more = _safe(hub.context_notes)
            if more:
                items.append(more)
        return items

    def first_today(self) -> bool:
        """No conversation yet today (before 4 am counts as the day before)."""
        try:
            return self.ctx.db.kv_get(LAST_DAY_KEY) != _day()
        except Exception:  # noqa: BLE001
            return False

    def talked_today(self) -> None:
        with contextlib.suppress(Exception):
            self.ctx.db.kv_set(LAST_DAY_KEY, _day())

    def build_messages(self, text: str, history: list[dict[str, str]], toolbox: Any = None, *,
                       extra: list[str] | None = None) -> list[dict[str, str]]:
        items = [i for i in (*(extra or []), *self.context_items(toolbox)) if i and i.strip()]
        question = text.strip()
        if items:
            question = CONTEXT.format(items="; ".join(i.strip().rstrip(".") for i in items if i)) + "\n" + question
        return [*history, {"role": "user", "content": question}]

    # ------------------------------------------------------------ one turn
    async def respond(self, text: str, *, history: list[dict[str, str]], toolbox: Any,
                      on_sentence: OnSentence, on_tool: OnTool | None = None,
                      images: list[bytes] | None = None, mark_day: bool = True,
                      extra: list[str] | None = None,
                      extra_tools: dict[str, tuple[dict[str, Any], Callable[[dict[str, Any]], Any]]] | None = None,
                      done: list[str] | None = None, first_words: bool = False) -> TurnResult:
        """Answer ``text``: sentences go to ``on_sentence`` as they are ready (checked for safety first). Never
        raises (except cancellation); a failure is ``result.error``. ``done``: ``result.done`` is this list, so the
        caller still has what the tools did when the turn is cancelled (they cut in). ``first_words`` (a voice): a
        tool called before anything was said gets a few words of LittleBot's own first (LOOKUP_WORDS), which the
        model is told it said; they are not in ``result.text``."""
        result = TurnResult()
        if done is not None:
            result.done = done
        greeting = _greeting(self.ctx) if first_words and self.first_today() else ""
        splitter = SentenceSplitter()
        stopped = False  # a sentence failed the safety check: nothing more is said
        t0 = self._now()
        raw: list[str] = []
        unsaid: list[str] = []  # its own reasoning and a second "done": not said, and not in result.text
        dropping = False  # a sentence of its reasoning was cut at a comma: the rest of it isn't said either

        def emit(sentences: list[Sentence]) -> None:
            nonlocal stopped, dropping
            for s in sentences:
                if stopped:
                    return
                if dropping or _self_talk(s.text):
                    if not dropping:
                        log.info("assistant: a sentence of the brain's own reasoning was not said")
                    dropping = not s.text.rstrip().endswith((".", "!", "?"))
                    unsaid.append(s.text)
                    continue
                if told and _DONE_AGAIN.search(s.text):
                    log.info("assistant: a second \"done\" for an action it had told already was not said")
                    unsaid.append(s.text)
                    continue
                safe, ok = _checked(self.ctx, s.text)
                if not ok:
                    stopped = True
                    s = dataclasses.replace(s, text=safe, expression="neutral", animation=None)
                result.sentences += 1
                _call(on_sentence, s)

        def on_delta(piece: str) -> None:
            if result.first_text_ms is None:
                result.first_text_ms = int((self._now() - t0) * 1000)
            raw.append(piece)
            if is_silent("".join(raw)) or "".join(raw).strip().lower().startswith("[sil"):
                return  # [silent] (or its start): never said
            emit(splitter.feed(piece))

        def say_first(name: str | None) -> str:
            """Nothing said yet and a tool to wait for (None: a web search): a few words now. What was said, or ""."""
            if not first_words or result.sentences or stopped or name in final or name in QUIET_TOOLS:
                return ""
            words = (DEEP_WORDS if name == DEEP_TOOL else
                     LOOKUP_WORDS if name is None or name.startswith(_LOOKUP_PREFIXES) else ACTION_WORDS)
            words = greeting or words  # the first chat of the day ("One sec. Good morning!" on the robot, 2026-10-31)
            emit([Sentence(words)])
            return words

        def search_started() -> None:
            emit(splitter.flush())
            say_first(None)

        heard = 0  # the sentences said up to the last tool (what came after it was said before the next one)
        told = False  # an action was told before its tool ran: a "Done, I've ..." after it is not said

        async def run_tool(name: str, arguments: Any = None) -> str:
            nonlocal heard, told
            emit(splitter.flush())  # "Let me check." is said now, not after the tool
            said = say_first(str(name))
            before = " ".join(t for t in splitter.said[heard:] if t not in unsaid)
            heard = len(splitter.said)
            told = told or bool(before and _is_action(str(name)))
            result.tools.append(str(name))
            if on_tool is not None:
                _call(on_tool, str(name))
            out = await one_tool(name, arguments)
            if name != DEEP_TOOL and name not in FINAL_TOOLS:  # kept for later turns: it really happened
                result.done.append(f"{name}: {' '.join(str(out).split())[:DONE_CHARS]}")
            if said:
                return f'{out}\n(they have already heard you say "{said}": go straight on, never say it again)'
            if before and _is_action(str(name)):  # "Got it, I'll remember that." then "Done, I've saved it." (the
                # robot, 2026-10-31): the action was told once already
                return (f'{out}\n(they have already heard you say "{before[-160:]}": if it worked, say nothing more '
                        'about it (no "Done, I\'ve ..."), only anything new; if it failed, say so)')
            return out

        async def one_tool(name: str, arguments: Any) -> str:
            if name == DEEP_TOOL:
                return await self._think_harder(arguments, history)
            if extra_tools and name in extra_tools:  # the session's own (who_is_here)
                try:
                    return str(await extra_tools[name][1](_args(arguments)))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.warning("assistant: tool %s failed", name, exc_info=True)
                    return "error: the action failed"
            return await toolbox.run(name, arguments)

        provider = self.provider()
        tools = _tool_specs(toolbox)
        if self.deep_provider() is not None and getattr(self.deep_provider(), "configured", lambda: False)():
            tools.append(_deep_spec())
        tools += [{k: v for k, v in spec.items() if k != "final"} for spec, _ in (extra_tools or {}).values()]
        final = FINAL_TOOLS | {name for name, (spec, _) in (extra_tools or {}).items() if spec.get("final")}
        system = self.system_prompt()
        if toolbox is not None and hasattr(toolbox, "instructions"):
            system += "\n" + toolbox.instructions().strip()
        result.prefix = _fingerprint(tools, system)
        worked = _worked_out(text)
        if worked:  # a sum said in words: worked out here, not by a model with thinking off (spoken_sums.py)
            extra = [*(extra or []), f"the sum they asked, worked out exactly: {worked} (say this result)"]
        messages = self.build_messages(text, history, toolbox, extra=extra)
        kwargs: dict[str, Any] = {"tools": tools, "run_tool": run_tool, "max_tool_rounds": MAX_TOOL_ROUNDS,
                                  "effort": "minimal", "on_delta": on_delta, "final_tools": final,
                                  # a web search runs on Anthropic's side: "Let me check." is said as it starts,
                                  # not after it (it waited for the next words, 2-3 s; the robot, 2026-10-31)
                                  "on_search": search_started}
        if images:
            kwargs["images"] = images
        try:
            params = inspect.signature(provider.reply).parameters
            kwargs = {k: v for k, v in kwargs.items() if k in params}
        except (TypeError, ValueError):
            pass
        result.provider = str(getattr(provider, "name", "") or "")
        result.model = str(getattr(provider, "model", "") or "")
        try:
            reply = await provider.reply(messages, system=system, max_tokens=self.cfg.max_reply_tokens,
                                         timeout=self.cfg.reply_timeout_seconds, **kwargs)
        except asyncio.CancelledError:
            result.cancelled = True
            raise
        except Exception as e:  # noqa: BLE001 - ProviderError and anything unexpected: a short sorry
            result.error = str(getattr(e, "kind", "") or type(e).__name__)
            log.warning("assistant: the brain failed (%s) after %d ms", result.error,
                        int((self._now() - t0) * 1000))
            result.total_ms = int((self._now() - t0) * 1000)
            return result
        if not raw and getattr(reply, "text", ""):  # a provider that doesn't stream: the whole answer at once
            on_delta(str(reply.text))
        whole = "".join(raw)
        if is_silent(whole):
            result.silent = True
        else:
            emit(splitter.flush())
        result.text = " ".join(t for t in splitter.said if t not in unsaid)
        result.model = str(getattr(reply, "model", "") or result.model)
        result.total_ms = int((self._now() - t0) * 1000)
        if result.sentences and mark_day:  # (a voice session marks it itself, once the answer was heard)
            self.talked_today()
        if not result.sentences and not result.silent and not result.tools:
            result.error = getattr(reply, "stop_reason", None) or "empty"
        if not result.tools and _CLAIMED.search(result.text):
            log.warning("assistant: the answer says something was done, but no tool ran")
        return result

    async def _think_harder(self, arguments: Any, history: list[dict[str, str]]) -> str:
        import json  # noqa: PLC0415

        deep = self.deep_provider()
        if deep is None:
            return "error: there is no stronger model; answer as well as you can"
        try:
            args = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
        except ValueError:
            args = {}
        question = " ".join(str(args.get("question") or "").split())[:2000]
        if not question:
            return "error: say what to think about"
        system = ("You are the careful thinker behind a voice assistant. Work the question out properly, then give "
                  "the answer in at most five short plain sentences a voice can say (no markdown, no lists), with the "
                  "key numbers. If it depends on something unknown, say what you assumed.")
        try:
            reply = await deep.reply([*history[-6:], {"role": "user", "content": question}], system=system,
                                     max_tokens=6000, timeout=self.cfg.deep_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("assistant: think_harder failed (%s)", getattr(e, "kind", type(e).__name__))
            return "error: the stronger model didn't answer; answer as well as you can yourself"
        text = str(getattr(reply, "text", "") or "").strip()
        return f"ok: {text[:3000]}" if text else "error: no answer; answer as well as you can yourself"

    async def card_after(self, question: str, answer: str, toolbox: Any, *, showing: str = "",
                         still: Callable[[], bool] = lambda: True) -> str | None:
        """The card for an answer said without one (CARD_SYSTEM), made while it is still being said: what
        show_on_screen said, or None (no card needed, too late by ``still()``, or it failed). ``showing``: what
        the screen shows now. Never raises (except cancellation)."""
        spec = next((s for s in _tool_specs(toolbox) if s.get("name") == "show_on_screen"), None)
        if spec is None or len(answer) < CARD_BACKUP_CHARS:
            return None
        shown: list[str] = []

        async def run_tool(name: str, arguments: Any = None) -> str:
            if name != "show_on_screen" or shown or not still():
                return "error: no card now"
            shown.append(str(await toolbox.run(name, arguments)))
            this = asyncio.current_task()
            if this is not None:
                this.cancel()  # the card is up: no second request just to read what the tool said
            return shown[-1]

        content = f"They asked: {question.strip()[:500]}\nIt said: {answer[:2000]}"
        if showing:
            content += f"\nThe screen shows now: {showing[:300]}"
        messages = [{"role": "user", "content": content}]
        kwargs: dict[str, Any] = {"tools": [spec], "run_tool": run_tool, "max_tool_rounds": 1, "effort": "minimal"}
        provider = self.provider()
        try:
            params = inspect.signature(provider.reply).parameters
            kwargs = {k: v for k, v in kwargs.items() if k in params}
        except (TypeError, ValueError):
            pass
        task = asyncio.ensure_future(provider.reply(messages, system=CARD_SYSTEM, max_tokens=600,
                                                    timeout=self.cfg.reply_timeout_seconds, **kwargs))
        try:
            await task
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if not shown or (me is not None and me.cancelling()):
                raise
        except Exception as e:  # noqa: BLE001 - no card, the answer was said all the same
            log.info("assistant: the backup card failed (%s)", getattr(e, "kind", "") or type(e).__name__)
        return shown[0] if shown else None


def _day() -> str:
    import datetime as _dt  # noqa: PLC0415

    return (_dt.datetime.now() - _dt.timedelta(hours=4)).strftime("%Y-%m-%d")


def _greeting(ctx: Any) -> str:
    """"Good morning." (afternoon, evening), or "" at night or when the time can't be read."""
    try:
        from littlebot.info.clock import now_info  # noqa: PLC0415

        tz = getattr(ctx.config.info, "timezone", "") or None
        try:
            part = now_info(tz)["part_of_day"]
        except ValueError:
            part = now_info(None)["part_of_day"]
    except Exception:  # noqa: BLE001
        return ""
    return f"Good {part}." if part in ("morning", "afternoon", "evening") else ""


def _worked_out(text: str) -> str | None:
    try:
        from littlebot.assistant.spoken_sums import worked_out  # noqa: PLC0415

        return worked_out(text)
    except Exception:  # noqa: BLE001 - the brain answers it alone
        log.debug("assistant: working the sum out failed", exc_info=True)
        return None


def _is_action(name: str) -> bool:
    """A tool that does something (a reminder, the volume, a video), not one that looks up, shows or thinks."""
    return name not in FINAL_TOOLS and name != DEEP_TOOL and not name.startswith(_LOOKUP_PREFIXES)


def _args(arguments: Any) -> dict[str, Any]:
    import json  # noqa: PLC0415

    if isinstance(arguments, dict):
        return arguments
    try:
        out = json.loads(arguments or "{}")
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def _deep_spec() -> dict[str, Any]:
    return {"name": DEEP_TOOL,
            "description": "Ask a stronger, slower model to reason carefully about a hard question (multi-step maths, "
                           "planning, coding, comparisons, advice with trade-offs). Takes several seconds: say a few "
                           "words first. Returns its answer for you to say in your own short words.",
            "parameters": {"type": "object", "additionalProperties": False, "required": ["question"],
                           "properties": {"question": {"type": "string",
                                                       "description": "the full question with every detail needed"}}}}


def _checked(ctx: Any, text: str) -> tuple[str, bool]:
    """``(words to say, ok)``: the shared safety check on one sentence (its safe line when it fails)."""
    try:
        from littlebot.safety.content import validate_reply  # noqa: PLC0415

        res = validate_reply(text, max_words=10_000, allow=getattr(ctx.config.ai, "relaxed_rules", ()))
    except Exception:  # noqa: BLE001 - the checker itself failing must not silence LittleBot
        log.debug("assistant: safety check failed to run", exc_info=True)
        return text, True
    ok = bool(getattr(res, "ok", True))
    safe = str(getattr(res, "text", "") or text)
    return (text, True) if ok else (safe or "Let's talk about something else.", False)


def _family(ctx: Any, owner: str) -> str:
    try:
        from littlebot.brain.prompts import clean_child_name  # noqa: PLC0415

        nick = clean_child_name((ctx.settings.get("profile") or {}).get("nickname"))
        share = bool(getattr(ctx.config.ai, "share_child_name", True))
    except Exception:  # noqa: BLE001
        nick, share = None, False
    child = f"the baby, {nick}" if nick and share else "a baby"
    who = owner if owner != "the family" else "a family"
    return f"{who} and the family ({child}, too young to talk; anyone who talks to you in words is a grown-up)"


def _motors_on(motion: Any) -> bool:
    st = _safe(motion.status) or {}
    return bool(st.get("physical_enabled"))


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def _tool_specs(toolbox: Any) -> list[dict[str, Any]]:
    """The toolbox's tools, the same list whatever is on the screen when it can (``RobotToolbox.specs(stable=)``):
    the prompt cache starts with the tools, and a card coming up cost a whole miss (~0.2 s, 2026-10-31)."""
    if toolbox is None:
        return []
    try:
        return list(toolbox.specs(stable=True))
    except TypeError:  # a toolbox without it
        return list(toolbox.specs())


def _fingerprint(tools: list[dict[str, Any]], system: str) -> str:
    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415

    blob = json.dumps(tools, sort_keys=True, default=str) + "\n" + system
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:6]


_PENDING: set[asyncio.Future[Any]] = set()  # listeners' coroutines, kept alive until done


def _call(fn: Callable[..., Any], *args: Any) -> None:
    try:
        out = fn(*args)
        if inspect.isawaitable(out):
            fut = asyncio.ensure_future(out)
            _PENDING.add(fut)
            fut.add_done_callback(_PENDING.discard)
    except Exception:  # noqa: BLE001 - a listener's bug must not break the answer
        log.debug("assistant: a listener failed", exc_info=True)

__all__ = ["Brain", "DEEP_TOOL", "FINAL_TOOLS", "TurnResult"]
