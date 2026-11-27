"""Translator: an interpreter mode ("be my interpreter in Spanish", "translate everything into French").

``start_translator(language, both_ways)`` switches it on; while it is on, the context note tells the brain to
translate everything the user says into that language (and, both ways, that language back into English), to
speak only the translation and to put it on the screen when that helps. "Stop translating" (``stop_translator``)
ends it, and so does ``skills.translator_idle_minutes`` (15) without a turn. The state lives in memory only:
a restart ends it, and nothing that was said is kept.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

from littlebot.skills.base import BOOL, STR, Skill, ToolSpec, err, ok, tool

log = logging.getLogger("littlebot.skills.translator")

IDLE_MINUTES = 15.0
HEARD_TOPICS = ("assistant.heard", "assistant.turn_done")  # a turn in the fast pipeline keeps it alive
# Common names and codes -> the language's English name (anything else made of letters is taken as said).
LANGUAGES = {
    "ar": "Arabic", "arabic": "Arabic", "bn": "Bengali", "bengali": "Bengali", "bangla": "Bengali",
    "zh": "Mandarin Chinese", "chinese": "Mandarin Chinese", "mandarin": "Mandarin Chinese",
    "mandarin chinese": "Mandarin Chinese", "cantonese": "Cantonese", "nl": "Dutch", "dutch": "Dutch",
    "en": "English", "english": "English", "tl": "Tagalog", "filipino": "Filipino", "tagalog": "Tagalog",
    "fr": "French", "french": "French", "de": "German", "german": "German", "el": "Greek", "greek": "Greek",
    "gu": "Gujarati", "gujarati": "Gujarati", "he": "Hebrew", "hebrew": "Hebrew", "hi": "Hindi", "hindi": "Hindi",
    "it": "Italian", "italian": "Italian", "ja": "Japanese", "japanese": "Japanese", "ko": "Korean",
    "korean": "Korean", "fa": "Persian", "farsi": "Persian", "persian": "Persian", "pl": "Polish", "polish": "Polish",
    "pt": "Portuguese", "portuguese": "Portuguese", "brazilian portuguese": "Brazilian Portuguese",
    "pa": "Punjabi", "punjabi": "Punjabi", "ro": "Romanian", "romanian": "Romanian", "ru": "Russian",
    "russian": "Russian", "es": "Spanish", "spanish": "Spanish", "sw": "Swahili", "swahili": "Swahili",
    "sv": "Swedish", "swedish": "Swedish", "ta": "Tamil", "tamil": "Tamil", "th": "Thai", "thai": "Thai",
    "tr": "Turkish", "turkish": "Turkish", "uk": "Ukrainian", "ukrainian": "Ukrainian", "ur": "Urdu", "urdu": "Urdu",
    "vi": "Vietnamese", "vietnamese": "Vietnamese", "somali": "Somali", "amharic": "Amharic", "tigrinya": "Tigrinya",
    "croatian": "Croatian", "serbian": "Serbian", "hungarian": "Hungarian", "czech": "Czech", "malay": "Malay",
    "indonesian": "Indonesian", "nepali": "Nepali", "sinhala": "Sinhala", "kurdish": "Kurdish", "pashto": "Pashto",
    "dari": "Dari", "yoruba": "Yoruba", "igbo": "Igbo", "hausa": "Hausa", "irish": "Irish", "welsh": "Welsh",
    "asl": "", "sign": "", "sign language": "", "american sign language": "",  # not something LittleBot can speak
}
_LANG = re.compile(r"^[A-Za-z][A-Za-z .()'-]{1,38}[A-Za-z)]$")


def language_name(raw: Any) -> str | None:
    """ "french" / "fr" / "French" -> "French"; "" or nonsense -> None."""
    s = " ".join(str(raw or "").split()).strip(" .,!?")
    s = re.sub(r"(?i)^(?:in|into|to)\s+", "", s)
    if s.lower() in LANGUAGES:
        return LANGUAGES[s.lower()] or None
    s = re.sub(r"(?i)\s+language$", "", s)
    if not s:
        return None
    known = LANGUAGES.get(s.lower())
    if known is not None:
        return known or None  # "" = not something to speak (sign language)
    if not _LANG.match(s) or len(s.split()) > 3:
        return None
    return " ".join(w[:1].upper() + w[1:] for w in s.split())


class Translator(Skill):
    name = "translator"

    def __init__(self, ctx: Any, *, now: Callable[[], float] = time.time) -> None:
        super().__init__(ctx, now=now)
        self.language: str | None = None
        self.both_ways = False
        self.since: float | None = None
        self.last_turn: float | None = None
        self._subs: list[Any] = []

    # ------------------------------------------------------------ state
    @property
    def on(self) -> bool:
        self._expire()
        return self.language is not None

    def idle_seconds(self) -> float:
        minutes = getattr(self.ctx.config.skills, "translator_idle_minutes", IDLE_MINUTES)
        try:
            return max(60.0, float(minutes) * 60.0)
        except (TypeError, ValueError):
            return IDLE_MINUTES * 60.0

    def _expire(self) -> None:
        if self.language is not None and self.last_turn is not None \
                and float(self.now()) - self.last_turn >= self.idle_seconds():
            log.info("translator: off after %.0f idle minutes", self.idle_seconds() / 60)
            self._off()

    def _off(self) -> None:
        self.language, self.both_ways, self.since, self.last_turn = None, False, None, None

    def touch(self) -> None:
        """A turn happened (keeps interpreter mode from timing out)."""
        if self.language is not None:
            self._expire()
            if self.language is not None:
                self.last_turn = float(self.now())

    # ------------------------------------------------------------ the skill
    def available(self) -> bool:
        return bool(getattr(self.ctx.config.skills, "translator", True))

    async def start(self) -> None:
        bus = getattr(self.ctx, "bus", None)
        if bus is None:
            return
        for topic in HEARD_TOPICS:
            try:
                self._subs.append(bus.subscribe(topic, lambda _e: self.touch()))
            except Exception:  # noqa: BLE001
                log.debug("translator: couldn't subscribe to %s", topic, exc_info=True)

    async def stop(self) -> None:
        for s in self._subs:
            try:
                s.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._subs.clear()

    def tools(self) -> list[ToolSpec]:
        return [
            tool("start_translator", "Interpreter mode: from now on translate what the user says into a language "
                 "(and, both ways, that language back into English for a conversation between two people), until "
                 "they say stop translating.",
                 {"language": {**STR, "description": "the other language, e.g. 'Spanish', 'Urdu', 'French'"},
                  "both_ways": {**BOOL, "description": "true when two people talk to each other through LittleBot "
                                                       "(the default); false to translate only into the language"}},
                 ("language",)),
            tool("stop_translator", "End interpreter mode (stop translating)."),
        ]

    def prompt_note(self) -> str:
        return ("Translator: for 'be my interpreter', 'translate what I say into Spanish' or 'help me talk to my "
                "neighbour in Urdu', call start_translator (both_ways for a conversation); 'stop translating' ends "
                "it with stop_translator.")

    def context_note(self) -> str:
        if not self.on:
            return ""
        self.touch()
        lang = self.language
        if self.both_ways:
            what = f"translate everything the user says into {lang}, and {lang} into English"
        else:
            what = f"translate everything the user says into {lang}"
        return (f"Interpreter mode: {what}; speak only the translation, no commentary; show the translation on "
                f"screen with show_on_screen (an explain or definition card) if useful; 'stop translating' ends it "
                f"(stop_translator).")

    def status(self) -> dict[str, Any]:
        return {**super().status(), "on": self.on, "language": self.language, "both_ways": self.both_ways}

    async def run(self, name: str, args: dict[str, Any]) -> str:
        if name == "start_translator":
            return self._start(args)
        if name == "stop_translator":
            if not self.on:
                return ok("interpreter mode wasn't on")
            lang = self.language
            self._off()
            log.info("translator: off")
            return ok(f"interpreter mode is off (no more translating into {lang}); say so in one short sentence")
        return await super().run(name, args)

    def _start(self, args: dict[str, Any]) -> str:
        lang = language_name(args.get("language"))
        if lang is None:
            return err("say which language to translate into, e.g. Spanish or Urdu")
        if lang == "English":
            return err("say which other language: LittleBot translates between English and it")
        both = args.get("both_ways")
        self.both_ways = True if both is None else both is True
        self.language = lang
        self.since = self.last_turn = float(self.now())
        log.info("translator: on (%s, %s)", lang, "both ways" if self.both_ways else "one way")
        how = f"between English and {lang}" if self.both_ways else f"into {lang}"
        return ok(f"interpreter mode is on, translating {how}; from the next thing they say, speak only the "
                  f"translation. Say you're ready in one short sentence, in English and in {lang}.")


__all__ = ["Translator", "language_name"]
