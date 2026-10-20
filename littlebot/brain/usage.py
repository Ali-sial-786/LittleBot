"""Cloud AI spending controls: request rate limits and an *estimated* daily budget.

Only cloud requests count (the scripted mock is free and offline). Per day we persist
small aggregate counters in the shared SQLite kv store - never any text:

    brain.usage.YYYY-MM-DD -> {requests, failed, input_tokens, output_tokens, cost_usd, by_provider}
    brain.usage.recent     -> [unix timestamps of cloud requests in the last hour]

PRICES ARE ESTIMATES. ``PRICES_PER_MTOK`` holds list prices (USD per million tokens)
copied from the providers' public price lists when this was written (Anthropic: Oct 2026
via the claude-api reference; OpenAI entries are unverified). Prices change - check the
provider's pricing page and override with ``ai.price_per_mtok`` in config if needed. The
budget is a safety brake, not an invoice: set a hard spending limit in the provider console
as well. Unknown models are priced pessimistically so the budget trips early.
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from littlebot.core.storage import Database

# USD per 1M tokens: (input, output). Estimates - see module docstring.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    # Anthropic first-party API list prices (Haiku 5.5 rate applies to prompts <= 100K tokens).
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    # OpenAI (UNVERIFIED estimates - check https://openai.com/api/pricing).
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o": (2.50, 10.00),
}
UNKNOWN_MODEL_PRICE: tuple[float, float] = (5.00, 25.00)

KEY_PREFIX = "brain.usage."
RECENT_KEY = "brain.usage.recent"


@dataclass(frozen=True)
class UsageLimits:
    max_requests_per_hour: int
    max_requests_per_day: int
    daily_budget_usd: float


class UsageTracker:
    """Thread-safe counters; ``status()``-cheap (in-memory copy of today's record)."""

    def __init__(
        self,
        db: Database,
        *,
        prices: Mapping[str, tuple[float, float]] | None = None,
        now: Callable[[], float] = time.time,
        keep_days: int = 31,
    ) -> None:
        self._db = db
        self._now = now
        self._prices = dict(PRICES_PER_MTOK)
        if prices:
            self._prices.update({k: (float(v[0]), float(v[1])) for k, v in prices.items()})
        self._lock = threading.RLock()
        self._day = ""
        self._rec: dict[str, Any] = {}
        self._recent: list[float] = []
        self._keep_days = keep_days
        with self._lock:
            raw = db.kv_get(RECENT_KEY, [])
            self._recent = [float(t) for t in raw if isinstance(t, (int, float))] if isinstance(raw, list) else []
            self._ensure_day()
            self._prune_old_days()

    # -- pricing ----------------------------------------------------------
    def price_for(self, model: str) -> tuple[float, float]:
        if model in self._prices:
            return self._prices[model]
        # Dated / suffixed IDs ("claude-haiku-5-5-2026...", "gpt-4o-mini-2024-07-18") -> longest prefix.
        best = max((k for k in self._prices if model.startswith(k)), key=len, default=None)
        return self._prices[best] if best else UNKNOWN_MODEL_PRICE

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        pin, pout = self.price_for(model)
        return (max(0, input_tokens) * pin + max(0, output_tokens) * pout) / 1_000_000

    # -- day bookkeeping --------------------------------------------------
    def _today(self) -> str:
        return _dt.datetime.fromtimestamp(self._now()).date().isoformat()

    def _ensure_day(self) -> None:
        day = self._today()
        if day == self._day:
            return
        self._day = day
        stored = self._db.kv_get(KEY_PREFIX + day)
        rec = stored if isinstance(stored, dict) else {}
        self._rec = {
            "date": day,
            "requests": int(rec.get("requests", 0) or 0),
            "failed": int(rec.get("failed", 0) or 0),
            "input_tokens": int(rec.get("input_tokens", 0) or 0),
            "output_tokens": int(rec.get("output_tokens", 0) or 0),
            "cost_usd": float(rec.get("cost_usd", 0.0) or 0.0),
            "by_provider": dict(rec.get("by_provider", {}) or {}),
        }

    def _prune_old_days(self) -> None:
        cutoff = (_dt.datetime.fromtimestamp(self._now()).date() - _dt.timedelta(days=self._keep_days)).isoformat()
        for key in list(self._db.kv_items(KEY_PREFIX)):
            day = key[len(KEY_PREFIX):]
            if len(day) == 10 and day[4] == "-" and day < cutoff:
                self._db.kv_delete(key)

    def _trim_recent(self) -> None:
        horizon = self._now() - 3600
        self._recent = [t for t in self._recent if t > horizon][-2000:]

    def _save(self) -> None:
        self._db.kv_set(KEY_PREFIX + self._day, self._rec)
        self._db.kv_set(RECENT_KEY, self._recent)

    # -- API ----------------------------------------------------------------
    def check(self, limits: UsageLimits, projected_cost: float = 0.0) -> str | None:
        """Return a human-readable block reason, or None if another cloud request is allowed."""
        with self._lock:
            self._ensure_day()
            self._trim_recent()
            if limits.max_requests_per_hour <= 0 or len(self._recent) >= limits.max_requests_per_hour:
                return "hourly request limit reached"
            if limits.max_requests_per_day <= 0 or self._rec["requests"] >= limits.max_requests_per_day:
                return "daily request limit reached"
            cost = self._rec["cost_usd"]
            if limits.daily_budget_usd <= 0 or cost >= limits.daily_budget_usd \
                    or cost + max(0.0, projected_cost) > limits.daily_budget_usd:
                return "daily budget reached"
        return None

    def record_request(self, provider: str) -> None:
        """Count a cloud request *before* it is sent (failed requests still count)."""
        with self._lock:
            self._ensure_day()
            self._trim_recent()
            self._recent.append(self._now())
            self._rec["requests"] += 1
            bp = self._rec["by_provider"].setdefault(provider, {"requests": 0, "cost_usd": 0.0})
            bp["requests"] = int(bp.get("requests", 0)) + 1
            self._save()

    def record_failure(self) -> None:
        with self._lock:
            self._ensure_day()
            self._rec["failed"] += 1
            self._save()

    def record_usage(self, provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
        """Add token usage for a completed request; returns its estimated cost in USD."""
        cost = self.estimate_cost(model, input_tokens, output_tokens)
        with self._lock:
            self._ensure_day()
            self._rec["input_tokens"] += max(0, int(input_tokens))
            self._rec["output_tokens"] += max(0, int(output_tokens))
            self._rec["cost_usd"] = round(self._rec["cost_usd"] + cost, 8)
            bp = self._rec["by_provider"].setdefault(provider, {"requests": 0, "cost_usd": 0.0})
            bp["cost_usd"] = round(float(bp.get("cost_usd", 0.0)) + cost, 8)
            self._save()
        return cost

    def snapshot(self, limits: UsageLimits | None = None) -> dict[str, Any]:
        with self._lock:
            self._ensure_day()
            self._trim_recent()
            out: dict[str, Any] = {
                "date": self._day,
                "requests_hour": len(self._recent),
                "requests_today": self._rec["requests"],
                "failed_today": self._rec["failed"],
                "input_tokens_today": self._rec["input_tokens"],
                "output_tokens_today": self._rec["output_tokens"],
                "cost_today_usd": round(self._rec["cost_usd"], 6),
                "cost_is_estimate": True,
            }
        if limits is not None:
            out.update({
                "budget_usd": limits.daily_budget_usd,
                "max_requests_per_hour": limits.max_requests_per_hour,
                "max_requests_per_day": limits.max_requests_per_day,
            })
        return out

    def history(self, days: int = 7) -> list[dict[str, Any]]:
        """Recent per-day aggregates (newest first) for the dashboard."""
        rows = [v for k, v in self._db.kv_items(KEY_PREFIX).items()
                if k != RECENT_KEY and isinstance(v, dict) and "date" in v]
        rows.sort(key=lambda r: r["date"], reverse=True)
        return rows[:days]
