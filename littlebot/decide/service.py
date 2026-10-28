"""The decider (``get_decider(ctx)``): asks the chosen decision model, within LittleBot's rules, and keeps score.

``mode(feature)``: what a feature does now: "off" (nothing is asked), "shadow" (asked alongside LittleBot's own
way, only logged) or "on" (its answer is used when sure enough). It is "off" whatever the setting while the
provider is off or has no key, the Privacy page's cloud switch is off, LittleBot is in offline mode, the hour's
or the day's requests are used up, or the provider failed ``failures_to_pause`` times in a row (paused for
``pause_seconds``).

``ask(feature, state, questions)`` never raises: None when it can't (the caller then does what it always did).
``compare(feature, mine=, theirs=, used=, answer=)`` logs one line for each decision and counts how often the two
agree (the dashboard's Decisions panel shows it). With ``decide.log_context`` on (the default; the dashboard can
switch it off) the line also says what the decider was shown (what LittleBot heard and said, the card on the
screen...) and, during a voice call, who face and voice recognition said was there (``sensors``), so a parent can
judge who was right. After a decision whose line doesn't hold LittleBot's reply (routing, concern, browser,
videos, names), what it said next is logged too (``ai_replied``). Only while a provider is chosen: nothing is
logged with the provider off.

Settings (dashboard): ``decide.provider`` and ``decide.modes`` ({feature: mode}) override the config
(``decide.provider``, ``decide.modes``, ``decide.default_mode``).
"""

from __future__ import annotations

import collections
import datetime as dt
import logging
import time
from typing import Any

from littlebot.decide.base import FEATURES, MODES, PROVIDERS, Answer, DecideError, Question

log = logging.getLogger("littlebot.decide")

STATS_KEYS = ("asked", "failed", "ms", "agreed", "differed", "used")
CONTEXT_CHARS = 240  # one value in a logged decision's context
# Features whose line doesn't hold LittleBot's reply (it comes after): its next words are logged too (ai_replied).
REPLY_AFTER = ("routing", "concern", "browser", "videos", "names", "addressed")
REPLY_LINES = 3  # at most this many of its lines after one decision ("hold on" lines don't end the wait)
REPLY_CHARS = 400


def _key(config: Any, name: str) -> str:
    secret = getattr(config, name, None)
    try:
        return secret.get_secret_value() if secret is not None else ""
    except AttributeError:
        return str(secret or "")


class Decider:
    def __init__(self, ctx: Any, *, now: Any = time.monotonic, transport: Any = None) -> None:
        self.ctx = ctx
        self._now = now
        self._transport = transport
        self.client: Any = None  # tests set a fake here (it then answers whatever the provider)
        self._made: tuple[str, str, str] | None = None  # (provider, model, key) the made client is for
        self._made_client: Any = None
        self._hour: collections.deque[float] = collections.deque()
        self._day: tuple[dt.date, int] = (dt.date.today(), 0)
        self._fails = 0
        self._paused_until = 0.0
        self.last_error = ""
        self.stats: dict[str, dict[str, Any]] = {f: dict.fromkeys(STATS_KEYS, 0) for f in FEATURES}
        self.sensors: Any = None  # a voice call sets this: () -> {"last_heard", "camera_sees", "voice_is"} for the log
        self._reply_for: dict[str, int] = {}  # features waiting for LittleBot's next words -> lines still to log

    # ------------------------------------------------------------ settings
    @property
    def cfg(self) -> Any:
        return self.ctx.config.decide

    def provider(self) -> str:
        p = str(self._setting("decide.provider", self.cfg.provider) or "off")
        return p if p in PROVIDERS else "off"

    def chosen_mode(self, feature: str) -> str:
        """The mode the parent chose for ``feature`` (or the config's), before the gates."""
        modes = self._setting("decide.modes", {}) or {}
        m = modes.get(feature) if isinstance(modes, dict) else None
        if m is None:
            m = self.cfg.modes.get(feature, self.cfg.default_mode)
        if isinstance(m, bool):
            m = "on" if m else "off"
        return m if m in MODES else "off"

    def why_off(self) -> str:
        """Why nothing is asked right now ("" when requests may go)."""
        provider = self.provider()
        if provider == "off":
            return "the provider is off"
        if self.client is None and not self._api_key(provider):
            return {"jev": "TYPESAFE_API_KEY isn't set", "claude": "ANTHROPIC_API_KEY isn't set"}.get(
                provider, "OPENAI_API_KEY isn't set")
        if self.client is None and provider == "claude" and not self._claude_model():
            return "no Claude model is set (decide.claude_model or claude.model in config/local.yaml)"
        try:
            if self.ctx.config.is_offline or not self.ctx.privacy.cloud_enabled:
                return "cloud communication is off (Privacy)"
        except AttributeError:
            return "cloud communication is off (Privacy)"
        if self._now() < self._paused_until:
            return f"paused after {self.cfg.failures_to_pause} failures in a row ({self.last_error})"
        if self._over_limit():
            return "the hour's or the day's requests are used up"
        return ""

    def mode(self, feature: str) -> str:
        m = self.chosen_mode(feature)
        return "off" if m == "off" or self.why_off() else m

    def min_confidence(self) -> float:
        return float(self.cfg.min_confidence)

    def yes_at(self) -> float:
        return float(self.cfg.yes_at)

    def log_context(self) -> bool:
        v = self._setting("decide.log_context", self.cfg.log_context)
        return v if isinstance(v, bool) else str(v).lower() not in ("false", "off", "0", "no")

    # ------------------------------------------------------------ asking
    async def ask(self, feature: str, state: Any, questions: dict[str, Question], *,
                  timeout: float | None = None) -> dict[str, Answer] | None:
        if self.mode(feature) == "off" or not questions:
            return None
        client = self._client()
        if client is None:
            return None
        self._count()
        st = self.stats.setdefault(feature, dict.fromkeys(STATS_KEYS, 0))
        st["asked"] += 1
        t0 = self._now()
        try:
            answers = await client.ask(state, questions, timeout=float(timeout or self.cfg.timeout_seconds))
        except DecideError as e:
            self._failed(feature, e.kind)
            return None
        except Exception as e:  # noqa: BLE001 - a broken answer is no answer: LittleBot's own way
            self._failed(feature, type(e).__name__)
            return None
        ms = int((self._now() - t0) * 1000)
        st["ms"] += ms
        self._fails = 0
        for a in answers.values():
            a.state = state
        log.debug("decide: %s answered (%d ms, %d of %d questions)", feature, ms, len(answers), len(questions))
        return answers

    def compare(self, feature: str, *, mine: str, theirs: str, used: bool = False, answer: Answer | None = None,
                extra: dict[str, Any] | None = None) -> None:
        """One decision: LittleBot's own way said ``mine``, the decider ``theirs``. ``answer``: the decider's (what
        it was shown goes in the log line); ``extra``: more for the log (e.g. what "video_2" is)."""
        st = self.stats.setdefault(feature, dict.fromkeys(STATS_KEYS, 0))
        same = mine == theirs
        st["agreed" if same else "differed"] += 1
        if used:
            st["used"] += 1
        log.info("decide: %s (%s): LittleBot's way %s, %s %s, %s%s%s", feature, self.mode(feature) or "off", mine,
                 self.provider(), theirs, "same" if same else "different", "; the decider's answer was used"
                 if used and not same else "", self._context(answer, extra))
        if feature in REPLY_AFTER and self.sensors is not None and self.log_context():
            self._reply_for[feature] = REPLY_LINES

    def watch_call(self, sensors: Any) -> None:
        """A voice call starts (``sensors``: who face / voice recognition found) or ends (None)."""
        self.sensors = sensors
        self._reply_for.clear()

    def ai_replied(self, text: str, *, filler: bool = False) -> None:
        """A voice call's final caption of LittleBot's words: logged after a decision whose line doesn't hold its
        reply (REPLY_AFTER), so a parent sees what it did with it. ``filler``: only "hold on" (keeps waiting)."""
        if not self._reply_for:
            return
        if not self.log_context():
            self._reply_for.clear()
            return
        text = " ".join(str(text or "").split())
        if not text:
            return
        after = ", ".join(self._reply_for)
        log.info("decide: LittleBot then said (after %s): %s%s", after, text[:REPLY_CHARS],
                 "..." if len(text) > REPLY_CHARS else "")
        for f in list(self._reply_for):
            left = 0 if not filler else self._reply_for[f] - 1
            if left > 0:
                self._reply_for[f] = left
            else:
                del self._reply_for[f]

    def used(self, feature: str, theirs: str, answer: Answer | None = None) -> None:
        """The decider's answer was acted on without LittleBot's own way (e.g. no card, without a text-model
        request)."""
        st = self.stats.setdefault(feature, dict.fromkeys(STATS_KEYS, 0))
        st["used"] += 1
        log.info("decide: %s (on): %s %s, used%s", feature, self.provider(), theirs, self._context(answer, None))

    def _context(self, answer: Answer | None, extra: dict[str, Any] | None) -> str:
        """" | they_asked: ... | LittleBot_said: ... | camera: ... | voice: ...", or "" (decide.log_context off)."""
        if not self.log_context():
            return ""
        items: dict[str, Any] = {}
        state = answer.state if answer is not None else None
        if isinstance(state, dict):
            items.update(state)
        elif state:
            items["shown"] = state
        items.update(extra or {})
        if answer is not None and answer.kind == "yesno":
            items["p_yes"] = f"{answer.p:.2f}"
        elif answer is not None:
            items["confidence"] = f"{answer.confidence:.2f}"
        if self.sensors is not None:
            try:
                shown = " ".join(" ".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)
                                 for v in items.values())
                items.update({k: v for k, v in (self.sensors() or {}).items() if v and str(v) not in shown})
            except Exception:  # noqa: BLE001 - the log line must never break a decision
                log.debug("decide: reading the sensors failed", exc_info=True)
        parts = []
        for k, v in items.items():
            if isinstance(v, (list, tuple)):  # the conversation: its newest lines matter most
                text = " ".join(" / ".join(map(str, v)).split())
                limit = 2 * CONTEXT_CHARS
                text = f"...{text[-limit:]}" if len(text) > limit else text
            else:
                text = " ".join(str(v).split())
                text = f"{text[:CONTEXT_CHARS]}..." if len(text) > CONTEXT_CHARS else text
            if text:
                parts.append(f"{k}: {text}")
        return "".join(f" | {p}" for p in parts)

    def status(self) -> dict[str, Any]:
        provider = self.provider()
        features = {}
        for f in FEATURES:
            st = dict(self.stats.get(f) or {})
            ok = max(1, st.get("asked", 0) - st.get("failed", 0))
            judged = st.get("agreed", 0) + st.get("differed", 0)
            features[f] = {"chosen": self.chosen_mode(f), "mode": self.mode(f), **st,
                           "avg_ms": int(st.get("ms", 0) / ok) if st.get("asked") else None,
                           "agreement": round(st.get("agreed", 0) / judged, 2) if judged else None}
        return {"provider": provider, "why_off": self.why_off(), "last_error": self.last_error,
                "log_context": self.log_context(),
                "has_typesafe_key": bool(_key(self.ctx.config, "typesafe_api_key")),
                "has_openai_key": bool(_key(self.ctx.config, "openai_api_key")),
                "has_anthropic_key": bool(_key(self.ctx.config, "anthropic_api_key")),
                "requests_today": self._day[1] if self._day[0] == dt.date.today() else 0,
                "features": features}

    # ------------------------------------------------------------ inside
    def _setting(self, key: str, default: Any) -> Any:
        try:
            v = self.ctx.settings.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if v in (None, "") else v

    def _api_key(self, provider: str) -> str:
        return _key(self.ctx.config, {"jev": "typesafe_api_key", "claude": "anthropic_api_key"}.get(
            provider, "openai_api_key"))

    def _claude_model(self) -> str:
        claude = getattr(self.ctx.config, "claude", None)
        return str(getattr(self.cfg, "claude_model", None) or getattr(claude, "model", None) or "")

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        provider = self.provider()
        model = {"jev": self.cfg.jev_model, "claude": self._claude_model()}.get(provider, self.cfg.openai_model)
        key = self._api_key(provider)
        if self._made != (provider, model, key):
            if provider == "jev":
                from littlebot.decide.jev import JevClient  # noqa: PLC0415

                self._made_client = JevClient(key, model=model, base_url=self.cfg.jev_base_url,
                                              transport=self._transport)
            elif provider == "openai":
                from littlebot.decide.openai import (
                    OpenAIDecisionsClient,  # noqa: PLC0415
                )

                self._made_client = OpenAIDecisionsClient(key, model=model, base_url=self.cfg.openai_base_url,
                                                          transport=self._transport)
            elif provider == "claude":
                from littlebot.brain.text_model import make_claude  # noqa: PLC0415
                from littlebot.decide.claude import ClaudeDecisionsClient  # noqa: PLC0415

                self._made_client = ClaudeDecisionsClient(make_claude(self.ctx, purpose="decide", model=model))
            else:
                self._made_client = None
            self._made = (provider, model, key)
        return self._made_client

    def _over_limit(self) -> bool:
        now = self._now()
        while self._hour and now - self._hour[0] > 3600:
            self._hour.popleft()
        day, n = self._day
        if day != dt.date.today():
            self._day = (dt.date.today(), 0)
            n = 0
        return len(self._hour) >= self.cfg.max_requests_per_hour or n >= self.cfg.max_requests_per_day

    def _count(self) -> None:
        self._hour.append(self._now())
        day, n = self._day
        self._day = (day, n + 1) if day == dt.date.today() else (dt.date.today(), 1)

    def _failed(self, feature: str, kind: str) -> None:
        self.stats[feature]["failed"] += 1
        self.last_error = kind
        self._fails += 1
        if kind in ("http_401", "http_403", "no_key"):
            self._fails = max(self._fails, self.cfg.failures_to_pause)  # a wrong key won't get better by itself
        if self._fails >= self.cfg.failures_to_pause:
            self._paused_until = self._now() + self.cfg.pause_seconds
            self._fails = 0
            log.warning("decide: %s failed (%s); no decisions for %.0f s (LittleBot's own way meanwhile)",
                        feature, kind, self.cfg.pause_seconds)
        else:
            log.warning("decide: %s failed (%s); LittleBot's own way this time", feature, kind)


def get_decider(ctx: Any) -> Decider:
    """The one Decider (``ctx.extras["decider"]``), made on first use."""
    extras = getattr(ctx, "extras", None)
    if extras is None:
        return Decider(ctx)
    decider = extras.get("decider")
    if decider is None:
        decider = extras["decider"] = Decider(ctx)
    return decider


__all__ = ["Decider", "get_decider"]
