"""The decider (``get_decider(ctx)``): asks the chosen decision model, within LittleBot's rules, and keeps score.

``mode(feature)``: what a feature does now: "off" (nothing is asked), "shadow" (asked alongside LittleBot's own
way, only logged) or "on" (its answer is used when sure enough). It is "off" whatever the setting while the
provider is off or has no key, the Privacy page's cloud switch is off, LittleBot is in offline mode, the hour's
or the day's requests are used up, or the provider failed ``failures_to_pause`` times in a row (paused for
``pause_seconds``).

``ask(feature, state, questions)`` never raises: None when it can't (the caller then does what it always did).
``compare(feature, mine=, theirs=, used=)`` logs one line for each decision and counts how often the two agree
(the dashboard's Decisions panel shows it), never the words that were decided about.

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
            return "TYPESAFE_API_KEY isn't set" if provider == "jev" else "OPENAI_API_KEY isn't set"
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
        log.debug("decide: %s answered (%d ms, %d of %d questions)", feature, ms, len(answers), len(questions))
        return answers

    def compare(self, feature: str, *, mine: str, theirs: str, used: bool = False) -> None:
        """One decision: LittleBot's own way said ``mine``, the decider ``theirs`` (labels, never words)."""
        st = self.stats.setdefault(feature, dict.fromkeys(STATS_KEYS, 0))
        same = mine == theirs
        st["agreed" if same else "differed"] += 1
        if used:
            st["used"] += 1
        log.info("decide: %s (%s): LittleBot's way %s, %s %s, %s%s", feature, self.mode(feature) or "off", mine,
                 self.provider(), theirs, "same" if same else "different", "; the decider's answer was used"
                 if used and not same else "")

    def used(self, feature: str, theirs: str) -> None:
        """The decider's answer was acted on without LittleBot's own way (e.g. no card, without a text-model
        request)."""
        st = self.stats.setdefault(feature, dict.fromkeys(STATS_KEYS, 0))
        st["used"] += 1
        log.info("decide: %s (on): %s %s, used", feature, self.provider(), theirs)

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
                "has_typesafe_key": bool(_key(self.ctx.config, "typesafe_api_key")),
                "has_openai_key": bool(_key(self.ctx.config, "openai_api_key")),
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
        return _key(self.ctx.config, "typesafe_api_key" if provider == "jev" else "openai_api_key")

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        provider = self.provider()
        model = self.cfg.jev_model if provider == "jev" else self.cfg.openai_model
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
