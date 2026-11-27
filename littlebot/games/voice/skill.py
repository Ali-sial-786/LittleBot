"""VoiceGames: the voice + screen games as a skill (``ctx.skills.get("voice_games")``).

Two ways in, one engine (littlebot/games/voice/base.py):

* **The fast voice pipeline** (littlebot/assistant) calls :meth:`VoiceGames.handle_utterance` with the person's
  words *before* the brain. A :class:`Reply` comes back for a move ("B", "is there an E", "a towel?") or an explicit
  start ("let's play trivia", "quiz me on history", "play again" after a game): the assistant speaks ``reply.say``
  at once (and may play ``reply.sound`` first). None means "not a game move": the words go to the brain as usual,
  and the brain sees the game in :meth:`context_note`. With no game on, the check is a few regexes: cheap enough
  to call on every turn.
* **GPT-Live** (and the brain, when it decides) plays through the tools: ``start_voice_game``, ``game_answer``,
  ``game_hint``, ``game_skip``, ``stop_voice_game``, ``game_scores``. Their results carry the exact words
  ("ok: say exactly: ..."). On this path the skill plays the reply's sound itself.

The card (quiz / word_game / game_board, littlebot/screens/cards) is put up by the skill and follows the game by
itself (it reads the game's live view); a tap on an option answers, and the reply is spoken with
:meth:`Skill.announce` (kind "game"). Showing ``reply.card`` again is harmless: the card already up is kept.

Scores, recently asked questions and the would-you-rather tally live in SQLite (littlebot/games/voice/scores.py).
Trivia asks the text model for a fresh round (one structured call) when ``voice_games.use_model_questions`` is on,
Claude is set up and the cloud is allowed; otherwise it uses the offline bank.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import random
import re
import time
from typing import Any, Callable

from littlebot.games.voice import answers
from littlebot.games.voice.base import Reply, VoiceGame
from littlebot.games.voice.scores import NAMES, ScoreStore
from littlebot.skills.base import STR, Skill, ToolSpec, err, ok, tool

log = logging.getLogger("littlebot.games.voice")

GAMES: dict[str, tuple[str, str]] = {
    "trivia": ("littlebot.games.voice.trivia", "Trivia"),
    "hangman": ("littlebot.games.voice.word_game", "Hangman"),
    "twenty_questions": ("littlebot.games.voice.twenty_questions", "TwentyQuestions"),
    "riddles": ("littlebot.games.voice.riddles", "Riddles"),
    "would_you_rather": ("littlebot.games.voice.would_you_rather", "WouldYouRather"),
}
# Ways to name a game (normalized words), longest first when searching.
GAME_ALIASES: dict[str, str] = {
    "twenty questions": "twenty_questions", "20 questions": "twenty_questions", "twenty q": "twenty_questions",
    "20q": "twenty_questions", "twenty_questions": "twenty_questions", "20 q": "twenty_questions",
    "would you rather": "would_you_rather", "would you rathers": "would_you_rather", "wyr": "would_you_rather",
    "would_you_rather": "would_you_rather",
    "hangman": "hangman", "hang man": "hangman", "word guess": "hangman", "word guessing": "hangman",
    "guess the word": "hangman", "word game": "hangman", "spelling game": "hangman", "word_game": "hangman",
    "riddles": "riddles", "riddle": "riddles", "riddle game": "riddles",
    "trivia": "trivia", "quiz": "trivia", "pub quiz": "trivia", "quiz show": "trivia", "trivia game": "trivia",
    "quiz game": "trivia",
}
_ALIAS_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(a) for a in GAME_ALIASES), key=len, reverse=True)) + r")\b")
LEVELS = {"easy": "easy", "simple": "easy", "medium": "medium", "normal": "medium", "hard": "hard",
          "difficult": "hard", "tough": "hard", "tricky": "hard", "expert": "hard", "challenging": "hard"}
_LEVEL_RE = re.compile(r"\b(" + "|".join(LEVELS) + r")\b")
_LEAD = (r"^(?:(?:ok|okay|hey|so|um+|uh+|yes|yeah|alright|all right|littlebot|little bot|please|now|well)\s+)*")
_START = re.compile(
    _LEAD + r"(?:(?:lets|let us|can we|could we|can you|could you|i want to|i wanna|wanna|i would like to|"
    r"id like to|shall we|we should|how about we|do you want to|you want to|want to|lets go|time to|i feel like|"
    r"we want to|we wanna)\s+)?(?:play|start|do|have|begin|try|playing|starting)\s+(?P<rest>.+)$")
_HOW_ABOUT = re.compile(_LEAD + r"(?:how about|what about|time for|lets have|lets do|i want|i want some|"
                                r"we want|give me)\s+(?P<rest>.+)$")
_QUIZ_ME = re.compile(_LEAD + r"(?:(?:can you|could you|will you|please)\s+)?(?:quiz|test) me(?:\s+(?:on|about|in)"
                              r"\s+(?P<topic>.+?))?(?:\s+please)?$")
_RIDDLE_ME = re.compile(_LEAD + r"(?:(?:can you|could you|will you|please)\s+)?(?:(?:ask|give|tell) me (?:a|another|"
                                r"some|one|a few) riddles?|riddle me this)(?:\s+please)?$")
_NOT_A_GAME = re.compile(r"\b(?:song|songs|video|videos|youtube|playlist|album|radio|spotify|podcast|on the tv|"
                         r"music by|movie about|show about)\b")
_FILLER_WORDS = frozenset("a an the some of game games round another more with me us together please now please "
                          "lets play quiz trivia questions question a round instead then again too time".split())
_NOT_TOPIC = frozenset("you i we me my your our like love hate know knows think about is are was were what that this "
                       "it how why when where who do does did can could have has had ever played heard any good "
                       "favourite favorite not dont never".split())
_AGAIN = re.compile(_LEAD + r"(?:(?:lets\s+)?play (?:it )?again|again|one more(?: round| game| time)?|"
                            r"another(?: one| round| game)?|lets go again|go again|same again|rematch|"
                            r"lets do (?:it|that|another|one more)(?: again)?|play another(?: one| round)?|"
                            r"(?:i want|can we have|can we do|can we play) (?:another|one more)(?: one| round| game)?)"
                            r"(?:\s+please)?$")
_YES = re.compile(_LEAD + r"(?:yes|yeah|yep|yup|sure|ok|okay|of course|definitely|lets do it|lets go|why not|"
                          r"absolutely|yes please|you bet|go on then|sure thing)(?:\s+please)?$")
AGAIN_SECONDS = 120.0  # "play again" works this long after a game
OFFER_SECONDS = 45.0  # "yes" to "Want another round?" works this long
ENDED_NOTE_SECONDS = 90.0  # the brain hears how the last game ended for this long


def game_id(name: Any) -> str | None:
    """The game ``name`` means ("Trivia", "20 questions", "hangman"), or None."""
    s = answers.normalize(name).replace("_", " ")
    if not s:
        return None
    if s in GAME_ALIASES:
        return GAME_ALIASES[s]
    s2 = s.replace(" ", "_")
    if s2 in GAMES:
        return s2
    m = _ALIAS_RE.search(s)
    return GAME_ALIASES[m.group(1)] if m else None


def parse_start(text: Any) -> tuple[str, str, str] | None:
    """(game, topic, difficulty) when ``text`` clearly asks to start a voice game, else None.

    "let's play trivia", "play hard science trivia", "start a quiz about dinosaurs", "quiz me on history",
    "give me a riddle", "how about twenty questions", "trivia time". Not "play baby shark" or "play a trivia song"."""
    s = answers.normalize(text)
    if not s or len(s) > 120:
        return None
    m = _QUIZ_ME.match(s)
    if m:
        topic, level = _topic_level(m.group("topic") or "")
        return "trivia", topic, level
    if _RIDDLE_ME.match(s):
        return "riddles", "", ""
    rest = None
    for pattern in (_START, _HOW_ABOUT):
        m = pattern.match(s)
        if m:
            rest = m.group("rest")
            break
    if rest is None:
        bare = re.sub(r"\s+(?:time|please|now)$", "", re.sub(_LEAD, "", s))
        if bare in GAME_ALIASES:
            return GAME_ALIASES[bare], "", ""
        return None
    if _NOT_A_GAME.search(rest):
        return None
    m = _ALIAS_RE.search(rest)
    if not m:
        return None
    game = GAME_ALIASES[m.group(1)]
    before, after = rest[: m.start()].strip(), rest[m.end():].strip()
    topic_text = ""
    mm = re.match(r"^(?:game\s+)?(?:about|on|with|in|of|for)\s+(?P<t>.+)$", after)
    if mm:
        topic_text = mm.group("t")
    elif after and not set(after.split()) <= _FILLER_WORDS | set(LEVELS):
        return None  # "play trivia pursuit with dad's friends"... too unsure: let the brain decide
    else:
        topic_text = after
    pre = [w for w in before.split() if w not in _FILLER_WORDS]
    if len(pre) > 2 or any(w in _NOT_TOPIC for w in pre):
        return None  # "do you like trivia?", "have you ever played hangman"
    topic, level = _topic_level(" ".join(pre + topic_text.split()))
    return game, topic, level


def _topic_level(text: str) -> tuple[str, str]:
    s = answers.normalize(text)
    level = ""
    m = _LEVEL_RE.search(s)
    if m:
        level = LEVELS[m.group(1)]
        s = (s[: m.start()] + s[m.end():]).strip()
    words = [w for w in s.split() if w not in _FILLER_WORDS and w not in ("questions", "mode", "level")]
    return " ".join(words)[:40], level


class VoiceGames(Skill):
    """Trivia, hangman, twenty questions, riddles and would you rather, by voice, with a card on the screen."""

    name = "voice_games"

    def __init__(self, ctx: Any, *, now: Callable[[], float] = time.time, rng: random.Random | None = None,
                 model: Any = None, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(ctx, now=now)
        self.rng = rng or random.Random()
        self.clock = clock
        self._model_override = model  # tests: a fake model (False: never use one)
        self._claude: Any = None
        self._store: ScoreStore | None = None
        self._game: VoiceGame | None = None
        self._last: VoiceGame | None = None
        self._last_end = -1e9
        self._offer_until = -1e9
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------ setup
    def _cfg(self) -> Any:
        cfg = getattr(getattr(self.ctx, "config", None), "voice_games", None)
        if cfg is None:
            from littlebot.core.config import VoiceGamesConfig  # noqa: PLC0415

            cfg = VoiceGamesConfig()
        return cfg

    def available(self) -> bool:
        return bool(getattr(self._cfg(), "enabled", True))

    @property
    def store(self) -> ScoreStore | None:
        if self._store is None:
            db = getattr(self.ctx, "db", None)
            if db is None:
                return None
            self._store = ScoreStore(db, now=self.now)
        return self._store

    def _model(self) -> Any:
        """The text model for fresh trivia, or None (switched off, cloud off, Claude not set up)."""
        if not getattr(self._cfg(), "use_model_questions", True):
            return None
        if self._model_override is not None:
            return self._model_override or None
        try:
            from littlebot.brain import text_model  # noqa: PLC0415

            if text_model.cloud_block(self.ctx):
                return None
            if self._claude is None:
                self._claude = text_model.make_claude(self.ctx, purpose="games")
            return None if self._claude.missing() else self._claude
        except Exception:  # noqa: BLE001 - no model: the offline bank
            log.debug("voice games: no text model", exc_info=True)
            return None

    def _remember_loop(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    async def start(self, game_id: str | None = None, **kw: Any) -> Reply | None:  # type: ignore[override]
        """With no arguments: the skill's lifecycle start (the hub). With a game: start that game and return its
        first words (``topic=``, ``difficulty=``)."""
        self._remember_loop()
        if game_id is None:
            return None
        return await self.start_game(game_id, **kw)

    async def stop(self) -> None:  # the hub's lifecycle stop: end a running game quietly (its score is kept)
        g = self._game
        if g is not None and not g.over:
            g.finish("stopped")
        self._game = None
        for t in list(self._tasks):
            t.cancel()

    # ------------------------------------------------------------ the games
    @property
    def active(self) -> VoiceGame | None:
        """The running game, or None."""
        g = self._game
        return g if g is not None and not g.over else None

    def game_for(self, session: Any) -> VoiceGame | None:
        for g in (self._game, self._last):
            if g is not None and g.session == session:
                return g
        return None

    def _make(self, gid: str, topic: str, difficulty: str) -> VoiceGame:
        module, cls = GAMES[gid]
        kind = getattr(importlib.import_module(module), cls)
        cfg = self._cfg()
        kw: dict[str, Any] = {"store": self.store, "rng": random.Random(self.rng.random()), "topic": topic,
                              "difficulty": difficulty, "now": self.now}
        if gid == "trivia":
            kw.update(model=self._model(), questions=getattr(cfg, "trivia_questions", 10))
        elif gid == "hangman":
            kw["words"] = getattr(cfg, "hangman_words", 5)
        elif gid == "riddles":
            kw["riddles"] = getattr(cfg, "riddles_per_round", 5)
        elif gid == "would_you_rather":
            kw["questions"] = getattr(cfg, "would_you_rather_per_round", 5)
        elif gid == "twenty_questions":
            kw["clock"] = self.clock
        return kind(**kw)

    async def start_game(self, game: str, *, topic: str = "", difficulty: str = "") -> Reply:
        """Start ``game`` (any name :func:`game_id` knows), ending one already running. Never raises."""
        self._remember_loop()
        gid = game_id(game)
        if gid is None:
            return Reply("I know trivia, hangman, twenty questions, riddles and would you rather. Which one?")
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            old = self._game
            if old is not None and not old.over:
                old.finish("stopped")
                self._ended(old)
            try:
                g = self._make(gid, str(topic or "")[:60], str(difficulty or "")[:20])
            except Exception:  # noqa: BLE001
                log.exception("voice games: could not make %s", gid)
                return Reply("Hmm, that game isn't working right now.")
            g.on_tap = lambda i, s=g.session: self._tap_soon(s, i)
            self._game = g
            self._offer_until = -1e9
            try:
                reply = await g.start()
            except asyncio.CancelledError:  # the person talked over it, the session ended: no half-started game
                g.finish("cancelled")
                self._game = None
                raise
            except Exception:  # noqa: BLE001
                log.exception("voice games: %s failed to start", gid)
                self._game = None
                return Reply("Hmm, that game isn't working right now.")
            log.info("voice game %s started (%s)", gid, g.status().get("topic") or "-")
        await self._after(g, reply)
        return reply

    async def handle_utterance(self, text: str) -> Reply | None:
        """The person's words, before the brain: a Reply to speak now, or None (not a game move)."""
        if not self.available():
            return None
        self._remember_loop()
        g = self.active
        want = parse_start(text)
        if g is not None:
            if want is not None and want[0] != g.id:  # "let's play hangman instead"
                return await self.start_game(want[0], topic=want[1], difficulty=want[2])
            reply = g.handle(str(text or "")[:300])
            if reply is not None:
                await self._after(g, reply)
            return reply
        if want is not None:
            game, topic, level = want
            return await self.start_game(game, topic=topic, difficulty=level)
        last, now = self._last, self.now()
        offer, self._offer_until = self._offer_until, -1e9  # only the very next words can answer the offer
        if last is not None and now - self._last_end <= AGAIN_SECONDS:
            s = answers.normalize(text)
            if _AGAIN.match(s) or (now <= offer and _YES.match(s)):
                return await self.start_game(last.id, topic=last.topic, difficulty=last.difficulty)
        return None

    async def _after(self, g: VoiceGame, reply: Reply) -> None:
        if g.over:
            self._ended(g)
            if "another round" in reply.say.lower() or "play again" in reply.say.lower() or \
                    "want more" in reply.say.lower():
                self._offer_until = self.now() + OFFER_SECONDS
        await self._ensure_card(g)

    def _ended(self, g: VoiceGame) -> None:
        self._last, self._last_end = g, self.now()
        if self._game is g:
            self._game = None
        g.on_tap = None

    async def _ensure_card(self, g: VoiceGame) -> None:
        """Put the game's card up unless it is already showing (it follows the game by itself)."""
        screens = getattr(self.ctx, "screens", None)
        if screens is None:
            return
        cur = getattr(screens, "current", None)
        if cur is not None and getattr(cur, "session", None) == g.session and not getattr(cur, "done", False):
            return
        if g.over and cur is None:
            return  # the game ended with its card already gone: don't bring it back just to say goodbye
        await self.show(dict(g.view or g.card_spec()))

    # ------------------------------------------------------------ taps (display thread -> the loop)
    def _tap_soon(self, session: str, index: int) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._spawn, session, index)

    def _spawn(self, session: str, index: int) -> None:
        task = asyncio.ensure_future(self._on_tap(session, index))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _on_tap(self, session: str, index: int) -> None:
        g = self.active
        if g is None or g.session != session:
            return
        try:
            reply = g.tap(index)
        except Exception:  # noqa: BLE001
            log.exception("voice game %s: a tap failed", g.id)
            return
        if reply is None:
            return
        await self._after(g, reply)
        self._sound(reply.sound)
        self.announce(reply.say, kind="game", card=reply.card)

    def _sound(self, name: str | None) -> None:
        if not name or not getattr(self._cfg(), "sounds", True):
            return
        audio = getattr(self.ctx, "audio", None)
        if audio is None:
            return
        try:
            task = asyncio.ensure_future(audio.play_sound(name, wait=False))
        except Exception:  # noqa: BLE001
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------ tools
    def tools(self) -> list[ToolSpec]:
        games = list(GAMES)
        return [
            tool("start_voice_game",
                 "Start a voice game with a card on the screen: trivia (a quiz with options A-D; topic: general, "
                 "science, history, geography, sports, movies, music, food, Canada, tech, or any topic; difficulty "
                 "easy, medium or hard), hangman (guess the word), twenty_questions (you think of something, they "
                 "ask yes/no questions), riddles, or would_you_rather. The result has the exact words to say.",
                 {"game": {"type": "string", "enum": games},
                  "topic": {**STR, "description": "Trivia topic or hangman category, optional ('science', "
                                                  "'dinosaurs', 'animals')."},
                  "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]}},
                 ("game",)),
            tool("game_answer",
                 "Pass what the user said as their move in the running voice game: an answer (a letter A-D or the "
                 "option in trivia), a letter or word in hangman, a riddle answer, a would-you-rather pick, or a "
                 "twenty-questions question or guess. The result says exactly what to say.",
                 {"answer": {**STR, "description": "The user's words, as they said them."}}, ("answer",)),
            tool("game_hint", "A hint in the running voice game (trivia: fifty-fifty; hangman: a letter)."),
            tool("game_skip", "Skip the current question, word or riddle in the running voice game (it reveals the "
                              "answer and moves on)."),
            tool("stop_voice_game", "End the running voice game (the score is kept)."),
            tool("game_scores", "The voice games' high scores and best streaks ('what's my high score in trivia').",
                 {"game": {"type": "string", "enum": games}}),
        ]

    async def run(self, tool_name: str, args: dict[str, Any]) -> str:
        args = args if isinstance(args, dict) else {}
        self._remember_loop()
        if tool_name == "start_voice_game":
            gid = game_id(args.get("game"))
            if gid is None:
                return err("unknown game: use trivia, hangman, twenty_questions, riddles or would_you_rather")
            topic = _text_arg(args.get("topic"), 60)
            level = _text_arg(args.get("difficulty"), 12).lower()
            if level and level not in ("easy", "medium", "hard"):
                level = LEVELS.get(level, "")
            reply = await self.start_game(gid, topic=topic, difficulty=level)
            return self._said(reply)
        if tool_name == "game_scores":
            raw = args.get("game")
            gid = game_id(raw) if raw not in (None, "") else None
            if raw not in (None, "") and gid is None:
                return err("unknown game")
            store = self.store
            if store is None:
                return err("scores aren't available")
            return ok(store.summary(gid))
        g = self.active
        if g is None:
            if tool_name == "stop_voice_game":
                return err("no voice game is running")
            return err("no voice game is running: start one with start_voice_game")
        if tool_name == "game_answer":
            text = _text_arg(args.get("answer"), 300)
            if not text:
                return err("say what the user answered")
            if g.id == "twenty_questions" and answers.normalize(text) and _is_20q_move(g, text):
                result = g.answer_tool(text)  # type: ignore[attr-defined]
                if g.over:
                    self._ended(g)
                await self._ensure_card(g)
                return result
            reply = g.handle(text)
            if reply is None:
                return err(f"that isn't a move in {g.title}: {_nudge(g.id)}")
            await self._after(g, reply)
            return self._said(reply)
        if tool_name == "game_hint":
            reply = g.hint()
        elif tool_name == "game_skip":
            reply = g.skip()
        elif tool_name == "stop_voice_game":
            reply = g.stop()
        else:
            return err(f"{self.name} has no tool called {str(tool_name)[:40]}")
        await self._after(g, reply)
        return self._said(reply)

    def _said(self, reply: Reply) -> str:
        self._sound(reply.sound)
        return ok(f"say exactly: {reply.say}")

    # ------------------------------------------------------------ for the brain
    def prompt_note(self) -> str:
        return (
            "Voice games: trivia (topics: general, science, history, geography, sports, movies, music, food, Canada, "
            "tech, or any topic; easy, medium or hard), hangman, twenty questions, riddles and would you rather. "
            "When the user wants to play (\"let's play trivia\", \"quiz me on history\", \"give me a riddle\"), "
            "call start_voice_game and say exactly the words it returns. While a game is on, its answers are usually "
            "judged before you hear them; if one reaches you, pass it to game_answer and say exactly what it returns. "
            "Use game_hint, game_skip or stop_voice_game when they ask for a hint, to skip or to stop, and "
            "game_scores for \"what's my high score\". Keep game chat short and upbeat.")

    def context_note(self) -> str:
        g = self.active
        if g is not None:
            return g.describe()
        last = self._last
        if last is not None and self.now() - self._last_end <= ENDED_NOTE_SECONDS:
            return last.describe()
        return ""

    def status(self) -> dict[str, Any]:
        g = self.active
        return {"name": self.name, "available": self.available(), "active": g.status() if g is not None else None,
                "games": [{"id": k, "name": NAMES.get(k, k)} for k in GAMES]}


def _text_arg(value: Any, limit: int) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return ""
    return " ".join(str(value).split())[:limit]


def _is_20q_move(g: VoiceGame, text: str) -> bool:
    """Twenty questions' tool path takes questions and guesses; commands (hint, stop) go through handle()."""
    from littlebot.games.voice.base import command  # noqa: PLC0415

    return command(text) is None


def _nudge(gid: str) -> str:
    return {
        "trivia": "ask them to answer A, B, C or D (or say hint, skip or stop)",
        "hangman": "ask them for a letter or the whole word",
        "riddles": "ask them for their answer, or offer a hint",
        "would_you_rather": "ask which of the two they'd pick",
        "twenty_questions": "answer it yourself if it's a yes/no question",
    }.get(gid, "steer them back to the game")


__all__ = ["GAMES", "GAME_ALIASES", "VoiceGames", "game_id", "parse_start"]
