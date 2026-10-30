"""Persona: how LittleBot talks. warm | witty | calm | concise | playful, or "custom: <the user's own words>".

The style is a runtime setting (``assistant.persona``, registered in core/settings.py; its default is config
``skills.persona``), so the dashboard can change it too; "be more concise" / "be funnier" changes it by voice
(``set_personality``). The assistant's brain puts :meth:`Persona.persona_prompt` at the top of its system prompt::

    persona = ctx.skills.get("persona")
    system = persona.persona_prompt() if persona else ""
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from littlebot.skills.base import Skill, ToolSpec, err, ok, tool

log = logging.getLogger("littlebot.skills.persona")

SETTING = "assistant.persona"
STYLES = ("warm", "witty", "calm", "concise", "playful")
DEFAULT = "warm"
CUSTOM = "custom:"
MAX_CUSTOM = 200

# Who LittleBot is, whatever the style (spoken answers: short, natural, never robotic).
BASE = ("You are LittleBot, a small, friendly and curious robot with an animated face, living with a family at home. "
        "You're a grown-up's assistant first and always family-friendly. "
        "You speak out loud, so talk naturally and briefly: one to three short sentences unless asked for more, no "
        "lists or markdown, never robotic.")
STYLE_TEXT = {
    "warm": "Your style is warm: friendly, encouraging and genuinely interested, like a kind friend who is happy to "
            "help. Show a little personality and curiosity, but get to the point.",
    "witty": "Your style is witty: dry, quick humour and the odd playful aside, never at anyone's expense. The answer "
             "comes first; the joke is a light touch, not every time.",
    "calm": "Your style is calm: gentle, unhurried and soothing, with a soft, steady way of speaking. Keep things "
            "simple and reassuring, with no exclamation marks or hype.",
    "concise": "Your style is concise: give the shortest useful answer, often one sentence or just the fact. Skip "
               "greetings, filler and offers to help more unless they matter.",
    "playful": "Your style is playful: bubbly, silly and full of fun, with happy sounds and gentle jokes that work with "
               "a baby around. Still answer properly, and keep it short.",
}
SHORT = {  # for the tool's answer
    "warm": "warm and friendly", "witty": "dry, quick humour", "calm": "gentle and unhurried",
    "concise": "the shortest useful answers", "playful": "bubbly and silly",
}
# What people say -> a style.
ALIASES = {
    "warm": "warm", "friendly": "warm", "kind": "warm", "nice": "warm", "normal": "warm", "default": "warm",
    "usual": "warm", "regular": "warm", "standard": "warm", "sweet": "warm", "caring": "warm", "reset": "warm",
    "warmer": "warm", "nicer": "warm", "kinder": "warm", "wittier": "witty", "gentler": "calm", "calmer": "calm",
    "witty": "witty", "funny": "witty", "funnier": "witty", "humorous": "witty", "humour": "witty", "humor": "witty",
    "sarcastic": "witty", "dry": "witty", "clever": "witty", "jokey": "witty", "cheeky": "witty", "sassy": "witty",
    "calm": "calm", "calmer": "calm", "gentle": "calm", "relaxed": "calm", "chill": "calm", "soothing": "calm",
    "quiet": "calm", "soft": "calm", "peaceful": "calm", "zen": "calm", "slow": "calm", "slower": "calm",
    "concise": "concise", "brief": "concise", "briefer": "concise", "short": "concise", "shorter": "concise",
    "terse": "concise", "succinct": "concise", "direct": "concise", "to the point": "concise",
    "less chatty": "concise", "less talkative": "concise", "quick": "concise", "efficient": "concise",
    "playful": "playful", "silly": "playful", "sillier": "playful", "fun": "playful", "bubbly": "playful",
    "cheerful": "playful", "goofy": "playful", "excited": "playful", "energetic": "playful", "happy": "playful",
    "childlike": "playful", "kid friendly": "playful", "baby friendly": "playful",
}


def clean_style(value: Any) -> str | None:
    """ "Witty", "be more concise", "funny", "custom: talk like a pirate" -> the style to keep; None if unknown."""
    if not isinstance(value, str):
        return None
    raw = " ".join(value.split()).strip(" .!\"'")
    low = raw.lower()
    if low.startswith(CUSTOM):
        text = raw[len(CUSTOM):].strip(" :\"'")
        text = re.sub(r"[<>{}\[\]`]", "", text)[:MAX_CUSTOM].strip()
        return f"{CUSTOM} {text}" if text else None
    low = re.sub(r"^(?:be|talk|sound|speak|act|get|a bit|a little|bit|more|less of a|much|way|so|be a bit|be more)\s+",
                 "", low)
    low = re.sub(r"^(?:more|a bit|a little|bit)\s+", "", low)
    low = re.sub(r"\s+(?:please|style|mode|personality|again|now|one)$", "", low).strip(" -")
    if low in ALIASES:
        return ALIASES[low]
    for key in sorted(ALIASES, key=len, reverse=True):  # "a calm voice", "the funny one"
        if re.search(rf"\b{re.escape(key)}\b", low):
            return ALIASES[key]
    return None


class Persona(Skill):
    name = "persona"

    def __init__(self, ctx: Any, *, now: Callable[[], float] | None = None) -> None:
        super().__init__(ctx, **({"now": now} if now is not None else {}))
        self._fallback: str | None = None  # the style when the setting can't be stored (an old core)
        try:  # the dashboard's changes are checked the same way as the voice's
            ctx.settings.register_validator(SETTING, _validate)
        except Exception:  # noqa: BLE001 - a context without settings (some tests)
            log.debug("persona: no settings to validate", exc_info=True)

    def default(self) -> str:
        return clean_style(getattr(getattr(self.ctx.config, "skills", None), "persona", DEFAULT)) or DEFAULT

    def style(self) -> str:
        """The style now: one of STYLES, or "custom: <text>"."""
        try:
            value = self.ctx.settings.get(SETTING)
        except Exception:  # noqa: BLE001
            value = None
        return clean_style(value) or self._fallback or self.default()

    def set_style(self, value: Any, *, source: str = "voice") -> str | None:
        style = clean_style(value)
        if style is None:
            return None
        try:
            self.ctx.settings.set(SETTING, style, source=source)
        except KeyError:  # the setting isn't registered (an old core): keep it for this run only
            log.warning("persona: %s isn't a setting; the style lasts until restart", SETTING)
            self._fallback = style
        return style

    def persona_prompt(self) -> str:
        """3-6 sentences for the top of the brain's system prompt: who LittleBot is and the current style."""
        style = self.style()
        if style.startswith(CUSTOM):
            text = style[len(CUSTOM):].strip()
            return (f"{BASE} The style the user asked for: {text}. Keep it kind and family-friendly whatever the "
                    "style, and keep answers short.")
        return f"{BASE} {STYLE_TEXT.get(style, STYLE_TEXT[DEFAULT])}"

    # ------------------------------------------------------------ for the model
    def tools(self) -> list[ToolSpec]:
        return [tool("set_personality", "Change how LittleBot talks, when the user asks ('be more concise', 'be "
                     "funnier', 'calm down', 'back to normal'): warm (the usual), witty, calm, concise, playful, or "
                     "'custom: <their description>'.",
                     {"style": {"type": "string", "description": "warm | witty | calm | concise | playful | "
                                                                 "custom: <a short description>"}}, ("style",))]

    def prompt_note(self) -> str:
        return "Personality: set_personality changes your speaking style when asked; it applies from the next answer."

    def context_note(self) -> str:
        return ""

    def status(self) -> dict[str, Any]:
        return {**super().status(), "style": self.style()}

    async def run(self, name: str, args: dict[str, Any]) -> str:
        if name != "set_personality":
            return err(f"persona has no tool called {str(name)[:40]}")
        args = args if isinstance(args, dict) else {}
        raw = args.get("style")
        if raw in (None, ""):
            return ok(f"the style is {self.style()}; the choices: {', '.join(STYLES)}, or custom: <description>")
        style = self.set_style(raw)
        if style is None:
            return err(f"unknown style; choose {', '.join(STYLES)}, or 'custom: <a short description>'")
        log.info("persona: the style is now %s", "custom" if style.startswith(CUSTOM) else style)
        if style.startswith(CUSTOM):
            return ok(f"style set to {style}. Answer that way from now on (kind and family-friendly).")
        return ok(f"style set to {style}: {SHORT[style]}. Answer that way from now on.")


def _validate(value: Any) -> str:
    style = clean_style(value)
    if style is None:
        raise ValueError(f"{SETTING}: one of {', '.join(STYLES)} or 'custom: <text>'")
    return style


__all__ = ["ALIASES", "BASE", "SETTING", "STYLES", "STYLE_TEXT", "Persona", "clean_style"]
