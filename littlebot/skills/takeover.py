"""Take-overs asked for by a skill: something that needs the screen and the speaker (music) starts after the answer.

A voice chat is using the speaker (and often the screen) while a skill's tool runs, so starting music right then
would cut the answer off, or fail ("LittleBot is busy"). A skill hands the start to :func:`offer` instead:

* a host with its own after-the-turn queue claims it right after the tool call returned (``claim(ctx)``, before
  it awaits anything) and runs it with its own take-overs: ``RobotToolbox.pending`` (GPT-Live then ends the call
  once the voice has said its line, like for a video), or the assistant's session after it has spoken;
* nobody claims it: it starts by itself (:func:`start_when_free`) once LittleBot has said its line (the robot's
  state is no longer SPEAKING or THINKING and has been quiet a moment), at most ``max_wait`` seconds after it
  was asked for. A voice chat that still holds the speaker then (GPT-Live keeps it for the whole call) is ended
  first, as the GPT-Live hand-over would (the answer said it starts "once you have finished speaking").

Only the newest take-over is kept (like ``RobotToolbox``: "play jazz ... no, play rock" plays rock). Everything
runs on the robot's asyncio loop; nothing here raises into the caller.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import Any, Awaitable, Callable

from littlebot.core.arbiter import Priority, Resource
from littlebot.core.state import RobotState

log = logging.getLogger("littlebot.skills.takeover")

KEY = "skills.takeovers"  # ctx.extras: the _Queue
MAX_WAIT_S = 15.0  # the start waits at most this long for the answer to be said
QUIET_S = 0.8  # LittleBot has been quiet this long: its line is said (live voice's GOODBYE_QUIET_S)
POLL_S = 0.1
SPEAKER_BACK_S = 4.0  # after ending a voice chat: wait at most this long for the speaker to play again
HANDOVER_REASON = "handed over to what was asked for"  # (live voice's own hand-over reason)
# Lease owner -> the ctx field of the voice that holds the speaker for a whole chat.
VOICES = {"live_voice": "live", "assistant": "assistant"}
BUSY = (RobotState.SPEAKING, RobotState.THINKING)


@dataclasses.dataclass
class Takeover:
    """Shaped like ``chatgpt.tools.Pending``: ``run()`` returns False when it didn't happen."""

    label: str  # "music <video id>"
    run: Callable[[], Awaitable[Any]]
    takeover: bool = True
    fallback: asyncio.Task[Any] | None = dataclasses.field(default=None, repr=False)


class _Queue:
    def __init__(self) -> None:
        self.items: list[Takeover] = []


def _queue(ctx: Any) -> _Queue:
    extras = getattr(ctx, "extras", None)
    if not isinstance(extras, dict):
        return _Queue()
    q = extras.get(KEY)
    if not isinstance(q, _Queue):
        q = extras[KEY] = _Queue()
    return q


def offer(ctx: Any, label: str, run: Callable[[], Awaitable[Any]], *, max_wait: float | None = None,
          on_fail: Callable[[str], None] | None = None) -> Takeover:
    """Queue ``run`` to start after the answer: a host may :func:`claim` it, else it starts by itself.

    ``on_fail(why)`` hears about a start that didn't happen by itself (a host that claimed it reports its own)."""
    q = _queue(ctx)
    for old in q.items:  # the newest wins
        _cancel(old)
    item = Takeover(str(label)[:80], run)
    q.items = [item]
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return item  # no loop (a test calling it bare): only a host can run it
    item.fallback = loop.create_task(_fallback(ctx, item, max_wait, on_fail))
    return item


def claim(ctx: Any) -> list[Takeover]:
    """What skills asked to start after this turn (oldest first), now the caller's to run: they won't start by
    themselves. Call it right after the tool call returned, before awaiting anything."""
    q = _queue(ctx)
    items, q.items = q.items, []
    for item in items:
        if item.fallback is not None and not item.fallback.done():
            item.fallback.cancel()
        item.fallback = None
    return items


def pending(ctx: Any) -> list[str]:
    """The labels waiting to start (claimed ones are not listed)."""
    return [t.label for t in _queue(ctx).items]


def drop(ctx: Any, prefix: str) -> bool:
    """Forget a queued take-over whose label starts with ``prefix`` ("stop the music" before it started)."""
    q = _queue(ctx)
    keep = [t for t in q.items if not t.label.startswith(prefix)]
    gone = [t for t in q.items if t.label.startswith(prefix)]
    for t in gone:
        _cancel(t)
    q.items = keep
    return bool(gone)


def _cancel(item: Takeover) -> None:
    if item.fallback is not None and not item.fallback.done() and item.fallback is not _current():
        item.fallback.cancel()


def _current() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


async def _fallback(ctx: Any, item: Takeover, max_wait: float | None,
                    on_fail: Callable[[str], None] | None) -> None:
    await asyncio.sleep(0)  # a host claims it right after the tool call (then this task is cancelled)
    q = _queue(ctx)
    if item not in q.items:
        return
    try:
        await wait_until_said(ctx, max_wait=max_wait)
        if item not in q.items:  # claimed or replaced meanwhile
            return
        q.items = [t for t in q.items if t is not item]
        await end_voice_chat(ctx)
        ok = await item.run()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - PermissionError (videos off, busy), ValueError ...
        why = str(e).strip("'\"") or type(e).__name__
        log.info("take-over %s didn't start: %s", item.label.split(" ", 1)[0], why[:120])
        if on_fail is not None:
            _safe(on_fail, why)
        return
    if ok is False:
        log.info("take-over %s didn't start", item.label.split(" ", 1)[0])
        if on_fail is not None:
            _safe(on_fail, "it couldn't start right now")
        return
    log.info("take-over started: %s", item.label.split(" ", 1)[0])


def _safe(fn: Callable[[str], None], why: str) -> None:
    try:
        fn(why)
    except Exception:  # noqa: BLE001
        log.debug("take-over on_fail failed", exc_info=True)


def _state(ctx: Any) -> RobotState | None:
    try:
        return ctx.state.state
    except Exception:  # noqa: BLE001
        return None


def voice_chat(ctx: Any) -> str | None:
    """The lease owner of a voice chat holding the speaker above media's priority ("live_voice"), else None."""
    arbiter = getattr(ctx, "arbiter", None)
    if arbiter is None:
        return None
    try:
        lease = arbiter.holder(Resource.AUDIO_OUT)
    except Exception:  # noqa: BLE001
        return None
    if lease is None or int(getattr(lease, "priority", 0)) <= int(Priority.MEDIA):
        return None
    return str(getattr(lease, "owner", "") or "") or None


async def wait_until_said(ctx: Any, *, max_wait: float | None = None, quiet: float | None = None,
                          poll: float | None = None, clock: Callable[[], float] = time.monotonic) -> bool:
    """Wait until LittleBot has said its answer: not SPEAKING / THINKING for ``quiet`` seconds (and, while a voice
    chat holds the speaker, only after it spoke). False when ``max_wait`` ran out first."""
    max_wait = MAX_WAIT_S if max_wait is None else float(max_wait)
    quiet = QUIET_S if quiet is None else float(quiet)
    poll = POLL_S if poll is None else float(poll)
    t0 = clock()
    spoke = False
    quiet_since: float | None = None
    while True:
        now = clock()
        state = _state(ctx)
        if state in BUSY:
            spoke = spoke or state == RobotState.SPEAKING
            quiet_since = None
        elif quiet_since is None:
            quiet_since = now
        if quiet_since is not None and now - quiet_since >= quiet and (spoke or voice_chat(ctx) is None):
            return True
        if now - t0 >= max_wait:
            return False
        await asyncio.sleep(poll)


async def end_voice_chat(ctx: Any, reason: str = HANDOVER_REASON) -> bool:
    """End the voice chat that holds the speaker (if any) and wait until the speaker plays again."""
    owner = voice_chat(ctx)
    if owner is None:
        return False
    voice = getattr(ctx, VOICES.get(owner, owner), None)
    stop = getattr(voice, "stop_session", None)
    if stop is None:
        return False
    log.info("take-over: ending the voice chat (%s) for what was asked for", owner)
    try:
        await asyncio.wait_for(stop(reason), 10.0)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.warning("take-over: the voice chat didn't end", exc_info=True)
        return False
    back = getattr(getattr(ctx, "audio", None), "speaker_back", None)
    if back is not None:
        try:
            await asyncio.wait_for(back(), SPEAKER_BACK_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - start anyway
            log.debug("take-over: speaker check failed", exc_info=True)
    return True


__all__ = ["MAX_WAIT_S", "Takeover", "claim", "drop", "end_voice_chat", "offer", "pending", "voice_chat",
           "wait_until_said"]
