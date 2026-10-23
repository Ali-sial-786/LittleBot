"""ConversationEngine: one safe think -> speak turn at a time (implements BrainAPI).

Per turn::

    lease(face, audio_out, microphone @ CONVERSATION)   -> refused while an activity/media runs ("busy")
    [LISTENING: audio.listen()]                          -> converse_once() only
    THINKING:  choose provider (parent opt-in, privacy, offline, key, limits, budget; an optional minimum age)
               concerning input? -> parent alert (category only); the cloud answers, told it may be an
               emergency ("my baby swallowed a coin, what do I do?" gets real help), else - no cloud, a
               failure or a blocked answer - the gentle "Let's get a grown-up right now" line
               cloud: scrub PII, tiny in-memory context, timeout, typed errors -> friendly fallback
               hidden [face: NAME] tags removed first (never spoken, remembered or stored)
               validate_reply()  (markdown/emoji strip, links said as site names, length cap, the few
               block rules that protect the child); a reply that urges a grown-up about a hazard also
               alerts the parent and is never swapped for a game
    SPEAKING:  audio.say(reply) with a fitting expression (or the one the model asked for), then the
               requested face action (wink, spin_eyes, peekaboo, ...) if any
    -> release state back to IDLE

Tools (chatgpt provider, ``chatgpt.tools_enabled``): the model may also act on the robot when asked
("turn the volume up", "play baby shark", "start peekaboo", "how hot are you?"; littlebot/chatgpt/tools.py).
Whatever needs the screen or speaker (a video, an activity, sleep) is queued and starts only after
the reply has been spoken and this turn's lease is released (games share the speaker: they start at once).

Privacy: nothing is persisted by default - only the last few (scrubbed) turns are kept in
memory and cleared on stop / sleep / STOP ALL / after ``ai.context_ttl_seconds``. Usage
counters (no text) are stored per day for the budget. Camera data is never used here.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from littlebot.brain.face_actions import FaceAction, extract_tags, perform
from littlebot.brain.prompts import build_system_prompt, clean_profile_items
from littlebot.brain.providers import (
    AnthropicProvider, AuthenticationError, BaseProvider, Message, MockProvider, OpenAIProvider, ProviderError,
    ProviderReply, ProviderTimeoutError,
)
from littlebot.brain.usage import UsageLimits, UsageTracker
from littlebot.core.arbiter import Lease, Priority, Resource
from littlebot.core.context import RobotContext
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.interfaces import EXPRESSIONS
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState
from littlebot.safety.content import (
    classify_child_input, normalise, safe_fallback, sanitize_user_text_report, truncate_words, validate_reply,
)

log = logging.getLogger("littlebot.brain")

OWNER = "conversation"
CLOUD_PROVIDERS = ("anthropic", "openai", "chatgpt")

ERROR_FALLBACKS: tuple[str, ...] = (
    "Hmm, my thinking cap is sleepy. Let's play instead!",
    "Oops! My robot brain needs a rest. Can you clap?",
    "Beep boop, I got muddled. Let's sing a song instead!",
)
DIDNT_HEAR = ("I didn't hear that. Can you say it again?", "Hmm? Say it again?")
TOOLS_EXTRA_S = 30.0  # extra time for an answer that uses tools (a video search, a few more requests)


@dataclass(frozen=True)
class Selection:
    provider: BaseProvider
    name: str
    reason: str
    cloud_allowed: bool
    requested: str


class _Stopped(Exception):
    """Internal: the turn was stopped (STOP ALL / preempted) between awaits."""


def choose_expression(text: str) -> str:
    """Pick a face expression that fits a (validated) reply."""
    t = normalise(text)
    if re.search(r"\b(?:sleep|sleepy|yawn|night|nap|rest)\b", t):
        return "sleepy"
    if re.search(r"\b(?:hooray|yay|woohoo|well done|great job|you did it)\b", t):
        return "celebrating"
    if re.search(r"\b(?:sorry|aww|hug|hugs|cuddle|it'?s okay)\b", t):
        return "affectionate"
    if re.search(r"\b(?:wow|whoa|ooh)\b", t):
        return "excited"
    if text.rstrip().endswith("?"):
        return "curious"
    return "happy"


class ConversationEngine(Service):
    """Implements ``core.interfaces.BrainAPI`` + ``core.service.Service``."""

    name = "brain"

    def __init__(
        self,
        ctx: RobotContext,
        *,
        providers: dict[str, BaseProvider] | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        usage: UsageTracker | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()  # lifecycle state lives in self.service_status (status() is the BrainAPI dict)
        self.ctx = ctx
        self.cfg = ctx.config.ai
        self._providers: dict[str, BaseProvider] = providers or self._build_providers(http_transport)
        self._providers.setdefault("mock", self._make_mock())
        # ChatGPT sign-in uses the parent's plan: no per-token cost (the plan's own limits apply).
        prices = {**{m: (0.0, 0.0) for m in ctx.config.chatgpt.models}, **self.cfg.price_per_mtok}
        self.usage = usage or UsageTracker(ctx.db, prices=prices, now=now)
        self._lock = threading.RLock()
        self._context: collections.deque[tuple[str, str]] = collections.deque(maxlen=max(0, self.cfg.context_turns))
        self._context_at = 0.0
        self._busy = False
        self._phase = "idle"  # idle | listening | thinking | speaking
        self._turn_task: asyncio.Task[Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_requested = False
        self._our_state: RobotState | None = None  # the state this engine last entered for a turn
        self._subs: list[Subscription] = []
        self._auth_errors: set[str] = set()
        self._last_error: str | None = None
        self._last_provider: str | None = None
        self._last_heard: tuple[str, float] = ("", 0.0)
        self._turns = 0
        self._errors = 0
        self._turn_tools: Any = None  # this turn's RobotToolbox (chatgpt tools), if any
        self._ensure_transcript_table()

    # ------------------------------------------------------------------ build
    def _make_mock(self) -> MockProvider:
        def favourites() -> list[str]:
            profile = self.ctx.settings.get("profile") or {}
            return clean_profile_items(profile.get("favourite_animals"))

        return MockProvider(age_months=self.ctx.settings.child_age_months, robot_name=self.ctx.config.robot_name,
                            favourite_animals=favourites)

    def _build_providers(self, transport: httpx.AsyncBaseTransport | None) -> dict[str, BaseProvider]:
        cfg, root = self.cfg, self.ctx.config
        return {
            "mock": self._make_mock(),
            "anthropic": AnthropicProvider(
                api_key=root.anthropic_api_key, model=cfg.anthropic_model, base_url=cfg.anthropic_base_url,
                effort=cfg.anthropic_effort, thinking=cfg.anthropic_thinking,
                thinking_headroom=cfg.thinking_headroom_tokens, transport=transport,
            ),
            "openai": OpenAIProvider(api_key=root.openai_api_key, model=cfg.openai_model,
                                     base_url=cfg.openai_base_url, transport=transport),
            "chatgpt": self._make_chatgpt(transport),
        }

    def _make_chatgpt(self, transport: httpx.AsyncBaseTransport | None) -> BaseProvider:
        from littlebot.chatgpt import get_auth  # noqa: PLC0415
        from littlebot.chatgpt.responses import ChatGPTProvider  # noqa: PLC0415

        auth = get_auth(self.ctx)
        auth.on_change(lambda: self._auth_errors.discard("chatgpt"))  # signed in (again): try it again
        cfg = self.ctx.config.chatgpt
        return ChatGPTProvider(auth, models=list(cfg.models), effort=cfg.reasoning_effort, transport=transport)

    def _ensure_transcript_table(self) -> None:
        if self.cfg.store_transcripts:
            self.ctx.db.execute(
                "CREATE TABLE IF NOT EXISTS brain_transcript (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                "role TEXT NOT NULL, provider TEXT, text TEXT NOT NULL)"
            )

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        bus = self.ctx.bus
        self._subs = [
            bus.subscribe(Topics.STOP_ALL, self._on_stop_all),
            bus.subscribe(Topics.STATE_CHANGED, self._on_state_changed),
            bus.subscribe(Topics.SETTINGS_CHANGED, self._on_settings_changed),
            bus.subscribe_async(Topics.AUDIO_UTTERANCE, self._on_utterance_event, self._loop),
        ]
        sel = self.select_provider()
        self.set_status(ServiceStatus.RUNNING, sel.reason)
        self._publish_status(sel.name, "ready", sel.reason)

    async def stop(self) -> None:
        for s in self._subs:
            s.cancel()
        self._subs.clear()
        self._cancel_turn("service stop")
        task = self._turn_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), 2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        self.clear_context("service stop")
        for p in self._providers.values():
            try:
                await p.aclose()
            except Exception:  # noqa: BLE001
                log.debug("provider close failed", exc_info=True)
        self.set_status(ServiceStatus.STOPPED)

    # ------------------------------------------------------- provider choice
    def _setting(self, key: str, default: Any) -> Any:
        value = self.ctx.settings.get(key, default)
        return default if value is None else value

    def _limits(self) -> UsageLimits:
        return UsageLimits(
            max_requests_per_hour=int(self._setting("ai.max_requests_per_hour", self.cfg.max_requests_per_hour)),
            max_requests_per_day=int(self._setting("ai.max_requests_per_day", self.cfg.max_requests_per_day)),
            daily_budget_usd=float(self._setting("ai.daily_budget_usd", self.cfg.daily_budget_usd)),
        )

    def _max_words(self) -> int:
        try:
            return max(3, int(self._setting("ai.max_words", self.cfg.max_words)))
        except (TypeError, ValueError):
            return self.cfg.max_words

    def select_provider(self, *, check_usage: bool = True, projected_cost: float = 0.0) -> Selection:
        """Decide which provider answers the next turn, and why. Enforced in code."""
        ctx, cfg = self.ctx, self.cfg
        requested = str(self._setting("ai.provider", cfg.provider) or "mock").strip().lower()
        mock = self._providers["mock"]

        def use_mock(reason: str) -> Selection:
            return Selection(mock, "mock", f"mock: {reason}", False, requested)

        if ctx.config.is_offline:
            return use_mock("offline mode")
        policy = ctx.age_policy()
        if not policy.cloud_ai_allowed():  # only with a minimum age set (ai.min_age_months_for_cloud)
            return use_mock("child age not set" if policy.age_months is None
                            else f"child under {policy.min_age_months_for_cloud_ai} months")
        if requested == "mock":
            return use_mock("mock provider selected")
        provider = self._providers.get(requested)
        if provider is None or not provider.is_cloud:
            return use_mock(f"unknown provider {requested!r}")
        if not bool(self._setting("ai.enabled", False)):
            return use_mock("cloud AI disabled by parent")
        if not ctx.privacy.cloud_enabled:
            return use_mock("cloud access disabled in privacy settings")
        if not provider.configured():
            return use_mock("not signed in with ChatGPT" if requested == "chatgpt" else "no API key")
        if requested in self._auth_errors:
            return use_mock("ChatGPT sign-in rejected (sign in again)" if requested == "chatgpt"
                            else "API key rejected (check the key)")
        if check_usage:
            block = self.usage.check(self._limits(), projected_cost)
            if block:
                return use_mock(block)
        return Selection(provider, requested, f"{requested}: cloud AI active", True, requested)

    # ------------------------------------------------------------ BrainAPI
    async def handle_utterance(self, text: str, *, source: str = "simulated", speak: bool = True) -> dict[str, Any]:
        text = str(text or "").strip()
        if not text:
            return self._result(error="empty")
        result = await self._exclusive(lambda: self._utterance_turn(text, source, speak), speak=speak)
        return await self._after_turn(result)

    async def converse_once(self) -> dict[str, Any]:
        if self.ctx.audio is None:
            return {**self._result(error="no_audio", detail="No speaker or microphone available."), "heard": None}
        if not self.ctx.privacy.microphone_enabled:
            return {**self._result(error="microphone_disabled", detail="The microphone is turned off in Privacy."),
                    "heard": None}
        result = await self._after_turn(await self._exclusive(self._converse_turn, speak=True))
        result.setdefault("heard", None)
        return result

    async def _after_turn(self, result: dict[str, Any]) -> dict[str, Any]:
        """Start what the AI was asked to start (a video, an activity, sleep) now the reply is spoken."""
        tools = result.pop("_tools", None)
        if tools is None or not getattr(tools, "pending", None) or self._stop_requested:
            return result
        done = await tools.run_pending()
        if done:
            result["actions"] = done
        return result

    def providers(self) -> list[dict[str, Any]]:
        out = []
        for name, p in self._providers.items():
            info = p.describe()
            info["available"] = (not p.is_cloud) or self._cloud_gates_ok(name)
            info["status"] = self._provider_status(name)
            out.append(info)
        return out

    def _cloud_gates_ok(self, name: str) -> bool:
        """Would ``name`` be usable if selected (ignoring which provider is currently chosen)?"""
        p = self._providers.get(name)
        ctx = self.ctx
        if p is None or not p.is_cloud or ctx.config.is_offline or not ctx.privacy.cloud_enabled:
            return False
        return (p.configured() and name not in self._auth_errors and ctx.age_policy().cloud_ai_allowed()
                and bool(self._setting("ai.enabled", False)))

    def _provider_status(self, name: str) -> str:
        p = self._providers.get(name)
        if p is None:
            return "unknown"
        if not p.is_cloud:
            return "ok"
        if not p.configured():
            return "not_configured"
        if name in self._auth_errors:
            return "auth_error"
        return "ok"

    async def test_provider(self, name: str) -> dict[str, Any]:
        name = str(name or "").strip().lower()
        p = self._providers.get(name)
        if p is None:
            return {"ok": False, "latency_ms": None, "error": f"unknown provider {name!r}", "provider": name}
        t0 = time.monotonic()
        if not p.is_cloud:
            reply = await p.reply([{"role": "user", "content": "hello"}], system="", max_tokens=20, timeout=2.0)
            return {"ok": True, "latency_ms": int((time.monotonic() - t0) * 1000), "error": None,
                    "provider": name, "model": reply.model, "reply": reply.text}
        if self.ctx.config.is_offline or not self.ctx.privacy.cloud_enabled:
            return {"ok": False, "latency_ms": None, "error": "cloud_disabled", "provider": name, "model": p.model}
        if not p.configured():
            return {"ok": False, "latency_ms": None, "error": "not_signed_in" if name == "chatgpt" else "no_api_key",
                    "provider": name, "model": p.model}
        block = self.usage.check(self._limits())
        if block:
            return {"ok": False, "latency_ms": None, "error": block, "provider": name, "model": p.model}
        self.usage.record_request(name)
        try:
            reply = await asyncio.wait_for(
                p.reply([{"role": "user", "content": "Say hello in two words."}],
                        system="You are a friendly toy robot. Reply in at most three words.",
                        max_tokens=16, timeout=self.cfg.timeout_seconds),
                timeout=self.cfg.timeout_seconds + 1.0,
            )
        except AuthenticationError as e:
            self.usage.record_failure()
            self._auth_errors.add(name)
            self._last_error = e.kind
            self._publish_status(name, "auth_error", "API key rejected")
            return {"ok": False, "latency_ms": int((time.monotonic() - t0) * 1000), "error": e.kind,
                    "provider": name, "model": p.model}
        except (ProviderError, TimeoutError) as e:
            self.usage.record_failure()
            kind = e.kind if isinstance(e, ProviderError) else "timeout"
            return {"ok": False, "latency_ms": int((time.monotonic() - t0) * 1000), "error": kind,
                    "provider": name, "model": p.model}
        self.usage.record_usage(name, reply.model or p.model, reply.input_tokens, reply.output_tokens)
        self._auth_errors.discard(name)
        self._publish_status(name, "ok", "provider test passed")
        return {"ok": True, "latency_ms": int((time.monotonic() - t0) * 1000), "error": None, "provider": name,
                "model": reply.model or p.model}

    def status(self) -> dict[str, Any]:
        sel = self.select_provider()
        limits = self._limits()
        return {
            "provider": sel.requested,
            "active_provider": sel.name,
            "cloud_allowed": sel.cloud_allowed,
            "reason": sel.reason,
            "usage": self.usage.snapshot(limits),
            "last_error": self._last_error,
            "busy": self._busy,
            "phase": self._phase,
            "models": {name: p.model for name, p in self._providers.items()},
            "provider_status": {name: self._provider_status(name) for name in self._providers},
            "last_provider": self._last_provider,
            "context_turns": len(self._context),
            "store_transcripts": self._transcripts_enabled(),
            "service": self.service_status.value,
        }

    # --------------------------------------------------------- privacy ops
    def clear_context(self, reason: str = "") -> None:
        with self._lock:
            had = len(self._context)
            self._context.clear()
            self._context_at = 0.0
        if had:
            log.debug("conversation context cleared (%s)", reason or "requested")

    def clear_history(self) -> None:
        """Dashboard 'clear history': forget in-memory context and any opted-in transcripts."""
        self.clear_context("clear history")
        exists = self.ctx.db.query_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='brain_transcript'")
        if exists:
            self.ctx.db.execute("DELETE FROM brain_transcript")

    def _transcripts_enabled(self) -> bool:
        return bool(self.cfg.store_transcripts and self.ctx.privacy.record_history)

    def _store_transcript(self, role: str, provider: str, text: str) -> None:
        if not self._transcripts_enabled():
            return
        db = self.ctx.db
        db.execute("INSERT INTO brain_transcript(ts, role, provider, text) VALUES (?, ?, ?, ?)",
                   (time.time(), role, provider, text[:500]))
        db.execute("DELETE FROM brain_transcript WHERE id NOT IN "
                   "(SELECT id FROM brain_transcript ORDER BY id DESC LIMIT 200)")

    def _context_messages(self) -> list[Message]:
        with self._lock:
            if self._context and time.monotonic() - self._context_at > self.cfg.context_ttl_seconds:
                self._context.clear()
            msgs: list[Message] = []
            for user, assistant in self._context:
                msgs += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
            return msgs

    def _remember(self, user: str, assistant: str) -> None:
        if self._context.maxlen == 0:
            return
        with self._lock:
            self._context.append((user, assistant))
            self._context_at = time.monotonic()

    # ------------------------------------------------------ turn machinery
    async def _exclusive(self, body: Callable[[], Awaitable[dict[str, Any]]], *, speak: bool) -> dict[str, Any]:
        """Run one turn with the conversation lease, cancellable by STOP ALL / preemption."""
        with self._lock:
            if self._busy:
                return self._result(error="busy", detail="LittleBot is already talking.")
            resources = [Resource.FACE] + ([Resource.AUDIO_OUT, Resource.MICROPHONE] if speak else [])
            lease = self.ctx.arbiter.acquire(OWNER, resources, Priority.CONVERSATION, on_preempt=self._on_preempt)
            if lease is None:
                return self._result(error="busy", detail="LittleBot is busy with an activity or video right now.")
            self._busy = True
            self._stop_requested = False
            self._our_state = None
        self._loop = asyncio.get_running_loop()
        task = self._loop.create_task(body())
        self._turn_task = task
        try:
            return await task
        except (asyncio.CancelledError, _Stopped):
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return self._result(error="stopped", provider=self._last_provider)
        finally:
            self._turn_task = None
            self._finish(lease)

    def _finish(self, lease: Lease) -> None:
        lease.release()
        with self._lock:
            self._busy = False
            self._phase = "idle"
            ours, self._our_state = self._our_state, None
        # Only undo a state we entered ourselves: if an activity/media preempted us and moved
        # the robot to PLAYING_*, release() is a no-op and their state is left alone.
        if ours is not None:
            self.ctx.state.release(ours, "conversation finished")

    def _enter(self, state: RobotState, reason: str) -> bool:
        if self._stop_requested:
            return False
        if not self.ctx.state.transition(state, reason):
            return False
        self._our_state = state
        return True

    def _check_stop(self) -> None:
        if self._stop_requested:
            raise _Stopped()

    def _cancel_turn(self, reason: str) -> None:
        """Thread-safe: stop the in-flight turn (cancels the provider request, stops speech)."""
        with self._lock:
            if not self._busy:
                return
            self._stop_requested = True
            task, loop, phase = self._turn_task, self._loop, self._phase
        log.info("conversation turn stopped (%s)", reason)
        if phase == "speaking" and self.ctx.audio is not None:
            try:
                self.ctx.audio.stop_output()
            except Exception:  # noqa: BLE001
                log.debug("stop_output failed", exc_info=True)
        if task is not None and not task.done() and loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    def _on_preempt(self, lease: Lease, by: str) -> None:
        self._cancel_turn(f"preempted by {by}")

    async def _converse_turn(self) -> dict[str, Any]:
        ctx = self.ctx
        assert ctx.audio is not None
        if ctx.state.state == RobotState.SLEEPING:
            ctx.state.transition(RobotState.IDLE, "woken for conversation")
        if not self._enter(RobotState.LISTENING, "listening (push to talk)"):
            return self._result(error="busy", detail="LittleBot can't listen right now.")
        self._phase = "listening"
        self._face("listening")
        heard = await ctx.audio.listen(timeout=self.cfg.listen_timeout_seconds)
        self._check_stop()
        heard = (heard or "").strip()
        if not heard:
            reply = None
            if self.cfg.say_didnt_hear:
                limit = ctx.age_policy().max_reply_words(self._max_words())
                reply = truncate_words(DIDNT_HEAR[0] if limit >= 9 else DIDNT_HEAR[1], limit)[0]
                await self._speak(reply, "curious")
            return {**self._result(reply=reply, error="nothing_heard"), "heard": None}
        result = await self._utterance_turn(heard, "stt", True)
        result["heard"] = heard
        return result

    async def _utterance_turn(self, text: str, source: str, speak: bool) -> dict[str, Any]:
        ctx, cfg = self.ctx, self.cfg
        t0 = time.monotonic()
        # Concern check on everything the child said, before the length cap can cut it off.
        concern = classify_child_input(text)
        text = text[: max(20, cfg.max_input_chars)]
        self._last_heard = (normalise(text), time.monotonic())
        if ctx.state.state == RobotState.SLEEPING:
            ctx.state.transition(RobotState.IDLE, "woken for conversation")
        if not self._enter(RobotState.THINKING, f"thinking ({source})"):
            return self._result(error="busy", detail="LittleBot can't talk right now.")
        self._turn_tools = None
        self._phase = "thinking"
        self._face("thinking")
        self._turns += 1
        policy = ctx.age_policy()
        max_words = self._max_words()
        limit = policy.max_reply_words(max_words)
        error: str | None = None
        filtered = False
        reasons: list[str] = []
        profile = self.ctx.settings.get("profile") or {}
        nickname = str(profile.get("nickname") or "").strip()
        share_name = bool(self._setting("ai.share_child_name", cfg.share_child_name))
        sent, scrubbed = sanitize_user_text_report(text, names=[] if share_name else [nickname],
                                                   max_chars=cfg.max_input_chars)
        if scrubbed:
            log.info("removed personal info before thinking: %s", ",".join(scrubbed))
        attention: str | None = None
        face_action: FaceAction | None = None
        if concern is not None:
            # Worrying words: the parent is alerted (category only) and earlier turns are forgotten. The
            # cloud still answers, told it may be an emergency, since a grown-up may be asking for help
            # ("my baby swallowed a coin, what do I do?"); without it LittleBot says the gentle line.
            attention = concern.category
            self.clear_context("concerning input")
            log.warning("child input needs a grown-up (category=%s)", concern.category)
            self._publish_status("safety", "attention", concern.category)
        sel = self.select_provider()
        if concern is not None and not sel.cloud_allowed:
            provider_name, reason = "safety", f"safety: {concern.category}"
            reply, expression = concern.reply_for(limit), "affectionate"
        else:
            provider_name, reason = sel.name, sel.reason
            try:
                raw, sel = await self._ask(sel, text, sent, policy, limit,
                                           emergency=concern.category if concern is not None else None)
                provider_name, reason = sel.name, sel.reason
            except ProviderError as e:
                error = e.kind
                self._last_error = e.kind
                if concern is not None:  # never "let's play instead" after worrying words
                    reply, expression = concern.reply_for(limit), "affectionate"
                else:
                    reply = truncate_words(ERROR_FALLBACKS[self._errors % len(ERROR_FALLBACKS)], limit)[0]
                    expression = "confused"
                self._errors += 1
                log.warning("provider %s failed: %s", provider_name, e.kind)
                if isinstance(e, AuthenticationError):
                    self._auth_errors.add(provider_name)
                    self._publish_status(provider_name, "auth_error", "API key rejected")
                else:
                    self._publish_status(provider_name, "error", e.kind)
            else:
                # Hidden face tags go first: only the clean words are checked, spoken, remembered or stored.
                text_only, face_actions = extract_tags(raw.text)
                result = validate_reply(text_only, policy, max_words, allow=self.cfg.relaxed_rules)
                reply, reasons = result.text, list(result.reasons)
                filtered = not result.ok
                if raw.stop_reason == "refusal" and result.concern is None:
                    if result.ok:
                        reply = safe_fallback(text_only, limit)
                    filtered = True
                    reasons.append("provider_refusal")
                if result.concern is not None and concern is None:  # (worrying words already alerted)
                    # The reply talked about a hazard while urgently sending the child to a grown-up:
                    # treat it like concerning input (gentle face, parent alert, forget the context).
                    attention = result.concern
                    expression = "affectionate"
                    self.clear_context("reply flagged a possible danger")
                    log.warning("reply from %s urges a grown-up (category=%s)", provider_name, result.concern)
                    self._publish_status(provider_name, "attention", result.concern)
                elif filtered:
                    expression = "playful"
                    log.info("reply from %s replaced by a safe fallback (%s)", provider_name,
                             ",".join(r for r in reasons if ":" not in r))
                    if face_actions and "empty" in result.reasons and raw.stop_reason != "refusal":
                        face_action = face_actions[-1]  # the reply was only a face tag: still show the face
                else:
                    expression = raw.expression if raw.expression in EXPRESSIONS else choose_expression(reply)
                    face_action = face_actions[-1] if face_actions else None
                    self._remember(sent, reply)
                if face_action is not None and face_action.name in EXPRESSIONS:
                    expression = face_action.name  # the model asked for this face: it wins over the guess
                if concern is not None:
                    # Gentle face, no face game; a blocked answer becomes the grown-up line (never a game).
                    expression, face_action = "affectionate", None
                    if filtered and result.concern is None:
                        reply = concern.reply_for(limit)
                if provider_name in CLOUD_PROVIDERS:
                    self._last_error = None
        self._last_provider = provider_name
        self._check_stop()
        ctx.bus.publish(Topics.BRAIN_REPLY, {"text": reply, "provider": provider_name, "filtered": filtered},
                        source="brain")
        self._store_transcript("child", provider_name, sent)
        self._store_transcript("robot", provider_name, reply)
        spoken = None
        if speak:
            spoken = await self._speak(reply, expression)
        else:
            self._face(expression, hold=2.0)
        if face_action is not None and not self._stop_requested:
            # After the words, so the requested face is the last one shown (peekaboo takes ~1.5 s).
            await perform(ctx, face_action)
        latency = int((time.monotonic() - t0) * 1000)
        tools, self._turn_tools = self._turn_tools, None
        log.info("turn done provider=%s words=%d filtered=%s error=%s latency_ms=%d tools=%s",
                 provider_name, len(reply.split()), filtered, error, latency,
                 ",".join(tools.calls) if tools is not None and tools.calls else "-")
        return {
            **self._result(reply=reply, provider=provider_name, filtered=filtered, error=error),
            "reason": reason, "expression": expression, "latency_ms": latency, "spoken": spoken,
            "checks": [r for r in reasons if r != "empty"][:8],
            **({"attention": attention} if attention is not None else {}),
            **({"face_action": face_action.name} if face_action is not None else {}),
            **({"tools_used": list(tools.calls)} if tools is not None and tools.calls else {}),
            # What the tools queued runs after the turn (handle_utterance / converse_once), only for
            # a real answer: not after a provider error or a reply that needs a grown-up.
            **({"_tools": tools} if tools is not None and error is None and attention is None else {}),
        }

    async def _ask(self, sel: Selection, text: str, sent: str, policy: Any, limit: int, *,
                   emergency: str | None = None) -> tuple[ProviderReply, Selection]:
        """Call the chosen provider (cloud only ever sees ``sent``, the scrubbed text).

        ``emergency``: the concern category of worrying words, so the model knows it may be one."""
        cfg = self.cfg
        # Room for a reply of ``limit`` words (about 1.3 tokens a word, plus a face tag).
        max_tokens = max(cfg.max_output_tokens, 2 * limit)
        profile = self.ctx.settings.get("profile") or {}
        share_name = bool(self._setting("ai.share_child_name", cfg.share_child_name))
        share_interests = bool(self._setting("ai.share_interests", cfg.share_interests))
        nickname = str(profile.get("nickname") or "").strip()
        if sel.provider.is_cloud:
            toolbox = self._toolbox_for(sel)
            system = build_system_prompt(
                robot_name=self.ctx.config.robot_name, age_months=policy.age_months, max_words=limit,
                child_name=nickname if share_name else None,
                interests=profile.get("interests") if share_interests else None,
                favourite_animals=profile.get("favourite_animals") if share_interests else None,
                face_tags=True, tools=toolbox is not None, emergency=emergency,
            ) + (toolbox.instructions() if toolbox is not None else "")  # weather, time, web search
            messages = self._context_messages() + [{"role": "user", "content": sent}]
            projected = self.usage.estimate_cost(
                sel.provider.model,
                (len(system) + sum(len(m["content"]) for m in messages)) // 3 + 10,
                max_tokens + (cfg.thinking_headroom_tokens if sel.name == "anthropic" else 0),
            )
            again = self.select_provider(projected_cost=projected)
            if not again.cloud_allowed:  # e.g. this request would exceed the budget
                return await self._ask(again, text, sent, policy, limit, emergency=emergency)
            self.usage.record_request(sel.name)
            extra: dict[str, Any] = {}
            budget = cfg.timeout_seconds + 0.5
            if toolbox is not None:
                extra = {"tools": toolbox.specs(), "run_tool": toolbox.run,
                         "max_tool_rounds": self.ctx.config.chatgpt.max_tool_rounds}
                budget += TOOLS_EXTRA_S
                self._turn_tools = toolbox
            try:
                reply = await asyncio.wait_for(
                    sel.provider.reply(messages, system=system, max_tokens=max_tokens,
                                       timeout=cfg.timeout_seconds, **extra),
                    timeout=budget,
                )
            except ProviderError:
                self.usage.record_failure()
                raise
            except TimeoutError:
                self.usage.record_failure()
                raise ProviderTimeoutError(f"{sel.name}: no answer within {cfg.timeout_seconds:.0f}s") from None
            except (asyncio.CancelledError, _Stopped):
                raise
            except Exception as e:  # noqa: BLE001 - never crash a turn on an unexpected provider bug
                self.usage.record_failure()
                log.error("provider %s raised %s", sel.name, type(e).__name__)
                raise ProviderError(f"{sel.name}: internal error", kind="internal_error") from None
            self.usage.record_usage(sel.name, reply.model or sel.provider.model, reply.input_tokens,
                                    reply.output_tokens)
            return reply, sel
        # Scripted mock runs locally: it may see the raw words (nothing leaves the robot).
        reply = await sel.provider.reply([{"role": "user", "content": text}], system="",
                                         max_tokens=cfg.max_output_tokens, timeout=cfg.timeout_seconds)
        return reply, sel

    def _toolbox_for(self, sel: Selection) -> Any:
        """LittleBot's tools for this turn: the chatgpt provider only (it supports function calling)."""
        if sel.name != "chatgpt" or not getattr(sel.provider, "supports_tools", False):
            return None
        if not self.ctx.config.chatgpt.tools_enabled:
            return None
        from littlebot.chatgpt.tools import RobotToolbox  # noqa: PLC0415

        return RobotToolbox(self.ctx, mode="chat", defer=True)

    async def _speak(self, text: str, expression: str) -> bool | None:
        ctx = self.ctx
        self._check_stop()
        audio = ctx.audio
        if audio is None:
            self._face(expression, hold=2.0)
            return None
        if not self._enter(RobotState.SPEAKING, "speaking"):
            return False
        self._phase = "speaking"
        self._face(expression)
        try:
            ok = await asyncio.wait_for(audio.say(text), timeout=10.0 + 0.8 * len(text.split()))
        except asyncio.TimeoutError:
            log.warning("speech did not finish in time; stopping output")
            audio.stop_output()
            ok = False
        self._face(expression, hold=1.5)
        return bool(ok)

    # --------------------------------------------------------------- events
    def _on_stop_all(self, event: Event) -> None:
        self.clear_context("stop all")
        self._cancel_turn(f"stop all ({event.get('source', '?')})")

    def _on_state_changed(self, event: Event) -> None:
        if event.get("new") == RobotState.SLEEPING.value:
            self.clear_context("sleep")

    def _on_settings_changed(self, event: Event) -> None:
        key = str(event.get("key", ""))
        if key.startswith("ai.") and self._auth_errors:
            self._auth_errors.clear()  # parent changed AI settings: retry the key
        if key == "privacy.cloud_enabled" and not event.get("value"):
            self.clear_context("cloud disabled")
        if key == "profile":
            self.clear_context("profile changed")

    async def _on_utterance_event(self, event: Event) -> None:
        """Reply to speech heard while idle (not mid-activity, not our own push-to-talk)."""
        if not self.cfg.reply_to_utterance_events:
            return
        text = str(event.get("text") or "").strip()
        if not text or self._busy or self.ctx.state.state != RobotState.IDLE:
            return
        source = str(event.get("source") or "stt")
        if source == "stt" and not self.ctx.privacy.microphone_enabled:
            return
        last, at = self._last_heard
        if normalise(text) == last and time.monotonic() - at < 3.0:
            return  # the same words we just answered (e.g. echoed by listen())
        await self.handle_utterance(text, source=source)

    # -------------------------------------------------------------- helpers
    def _face(self, expression: str, hold: float | None = None) -> None:
        face = self.ctx.face
        if face is None or expression not in EXPRESSIONS:
            return
        try:
            face.set_expression(expression, hold=hold)
        except Exception:  # noqa: BLE001
            log.debug("face.set_expression failed", exc_info=True)

    def _publish_status(self, provider: str, status: str, detail: str) -> None:
        self.ctx.bus.publish(Topics.BRAIN_STATUS, {"provider": provider, "status": status, "detail": detail},
                             source="brain")

    @staticmethod
    def _result(*, reply: str | None = None, provider: str | None = None, filtered: bool = False,
                error: str | None = None, detail: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"reply": reply, "provider": provider, "filtered": filtered, "error": error}
        if detail:
            out["detail"] = detail
        return out
