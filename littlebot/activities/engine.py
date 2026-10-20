"""Activity engine: runs YAML-scripted, caregiver-friendly activities.

Implements ``core.interfaces.ActivityAPI`` on top of ``core.service.Service``.

Lifecycle of one run
--------------------
``start(id)`` stops any running activity, acquires an arbiter lease on the face
and speaker (``Priority.ACTIVITY``), moves the robot to ``PLAYING_ACTIVITY`` and
spawns one asyncio task that executes the steps in order. Every exit path -
completion, ``end`` step, ``stop()``, parent STOP ALL, lease preemption, an
unexpected error - goes through ``_finish`` which opens the eyes, calms the
face, releases the lease, releases the state (without clobbering a newer one)
and publishes ``activity.finished``.

Waiting
-------
All waits (``wait``, step ``pause``, prompt / wait_for timeouts) are scaled by
``ctx.clock`` (tests use ``time_scale=0``) and are *frozen while paused*.
Pausing takes effect at the next step boundary (a "safe point") and stops any
speech immediately; an interrupted sentence is repeated after resuming.

Robustness
----------
Face / audio / motion may be missing (``None``) or raise: every call is
guarded, logged once, and the activity carries on. An activity must never
crash the app.

Name clash note: ``Service.start()/stop()`` and ``ActivityAPI.start(id)/stop(reason)``
share names. ``start()`` with no activity id is the service lifecycle start;
``stop()`` always stops the current activity (which is exactly what shutdown needs).
Lifecycle state is ``Service.service_status``; ``status()`` is the ActivityAPI dict.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from littlebot.activities import model as m
from littlebot.activities.model import Activity, ActivityLibrary
from littlebot.core.arbiter import Lease, Priority, Resource
from littlebot.core.events import Event, Subscription, Topics, topic_matches
from littlebot.core.paths import ACTIVITY_SCRIPTS_DIR
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState

log = logging.getLogger("littlebot.activities")

OWNER = "activities"
SKIP = "__skip__"  # prompt answer meaning "carry on without a branch"
HISTORY_TABLE = "activity_history"

_FALLBACKS = {
    "child_name": "friend",
    "child_initial": "B",
    "favourite_animal": "puppy",
    "favourite_song": "Twinkle, Twinkle",
}
# Robot states that end a running activity when someone else switches to them.
_STATES_THAT_STOP = {RobotState.SLEEPING.value, RobotState.ERROR.value, RobotState.PLAYING_MEDIA.value}


class _EndActivity(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------

@dataclass(eq=False)
class _Prompt:
    id: str
    text: str
    options: list[str]
    timeout: float | None
    kind: str  # "prompt" | "wait_for"
    future: asyncio.Future[Any]
    loop: asyncio.AbstractEventLoop
    thread_id: int
    answered: bool = False

    def resolve(self, response: str) -> str | None:
        """Map a caregiver reply to an option label, SKIP, or None (not understood)."""
        n = m.normalise_answer(response)
        if not n:
            return None
        for opt in self.options:
            if m.normalise_answer(opt) == n:
                return opt
        if n == m.SKIP_WORD:
            return SKIP
        if n.isdigit() and 1 <= int(n) <= len(self.options):
            return self.options[int(n) - 1]
        if n in m.CONFIRM_WORDS:
            return self.options[0]
        return None

    def deliver(self, value: Any) -> None:
        def _set() -> None:
            if not self.future.done():
                self.future.set_result(value)

        if threading.get_ident() == self.thread_id:
            _set()
        else:
            try:
                self.loop.call_soon_threadsafe(_set)
            except RuntimeError:  # loop closed
                pass

    def as_dict(self) -> dict[str, Any]:
        return {"prompt_id": self.id, "text": self.text, "options": list(self.options),
                "timeout_seconds": self.timeout, "kind": self.kind}


@dataclass(eq=False)
class _Run:
    activity: Activity
    source: str
    rng: random.Random
    loop: asyncio.AbstractEventLoop
    thread_id: int
    values: dict[str, Any]
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    started_at: float = field(default_factory=time.time)
    started_mono: float = field(default_factory=time.monotonic)
    task: asyncio.Task[Any] | None = None
    lease: Lease | None = None
    paused: bool = False
    pause_epoch: int = 0
    paused_evt: asyncio.Event = field(default_factory=asyncio.Event)
    resumed: asyncio.Event = field(default_factory=asyncio.Event)
    done_evt: asyncio.Event = field(default_factory=asyncio.Event)
    step_index: int = 0
    step_kind: str = ""
    step_text: str = ""
    prompt: _Prompt | None = None
    prompt_seq: int = 0
    picked: dict[str, list[str]] = field(default_factory=dict)
    choices_used: dict[int, set[int]] = field(default_factory=dict)
    gesture_task: asyncio.Task[Any] | None = None
    stop_reason: str | None = None
    preempted: bool = False
    finished: bool = False
    warned: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.resumed.set()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ActivityEngine(Service):
    """Runs one activity at a time. Thread-safe entry points: respond(), status(), list(), get()."""

    name = "activities"

    def __init__(self, ctx: Any, library: ActivityLibrary | None = None, *, seed: int | None = None) -> None:
        super().__init__()  # lifecycle state lives in ``service_status``
        self.ctx = ctx
        if library is None:
            library = ActivityLibrary(default_script_dirs(ctx))
            library.load()
        self.library = library
        cfg_seed = getattr(getattr(ctx.config, "activities", None), "random_seed", None)
        self._seed = seed if seed is not None else cfg_seed
        self._run_counter = 0
        self._run: _Run | None = None
        self._last: dict[str, Any] | None = None
        self._lock = threading.RLock()
        self._start_lock = asyncio.Lock()
        self._history_ready = False
        bus = ctx.bus
        self._subs: list[Subscription] = [
            bus.subscribe(Topics.STOP_ALL, self._on_stop_all),
            bus.subscribe(Topics.CAREGIVER_RESPONSE, self._on_caregiver_response),
            bus.subscribe(Topics.STATE_CHANGED, self._on_state_changed),
        ]

    # ------------------------------------------------------------ Service
    def health(self) -> dict[str, Any]:
        return {**super().health(), "activities": len(self.library), "script_errors": len(self.library.errors)}

    def close(self) -> None:
        """Drop bus subscriptions (tests / teardown)."""
        for s in self._subs:
            s.cancel()
        self._subs.clear()

    # ------------------------------------------------------------ catalogue
    def list(self, age_months: int | None = None, category: str | None = None) -> list[dict[str, Any]]:
        """Activity summaries.

        * ``age_months`` given -> only activities whose range includes that age
          (``min <= age < max``), most suitable first.
        * ``age_months`` None -> every activity; when the child's age is known
          (profile) suitable ones come first, otherwise youngest first.
        * Disabled activities (profile ``activity_preferences``) are still listed
          here with ``enabled: False`` so parents can re-enable / start them;
          use :meth:`recommended` for the filtered "suggested" list.
        """
        acts = self.library.all()
        if category is not None:
            acts = [a for a in acts if a.category == category]
        prefs = self._preferences()
        profile_age = self._child_age()
        if age_months is not None:
            acts = [a for a in acts if a.suitable_for(age_months)]
            ref_age: int | None = age_months
        else:
            ref_age = profile_age
        acts.sort(key=lambda a: _sort_key(a, ref_age, prefs))
        return [self._summary(a, prefs, profile_age if age_months is None else age_months) for a in acts]

    def recommended(self, age_months: int | None = None, *, category: str | None = None,
                    limit: int | None = None) -> list[dict[str, Any]]:
        """Suggested activities: enabled ones suitable for the child's age (profile age if not given), favourites first."""
        age = age_months if age_months is not None else self._child_age()
        items = self.list(age_months=age, category=category)
        items = [i for i in items if i["enabled"]]
        items.sort(key=lambda i: not i["favourite"])  # stable: keeps suitability order
        return items[:limit] if limit else items

    def get(self, activity_id: str) -> dict[str, Any] | None:
        act = self.library.get(activity_id)
        if act is None:
            return None
        out = self._summary(act, self._preferences(), self._child_age())
        out["step_count"] = sum(1 for _ in act.all_steps())
        out["source"] = Path(act.source).name if act.source else ""
        out["outline"] = [{"kind": s.kind, "text": s.summary()} for s in act.steps if s.summary()][:40]
        return out

    def categories(self) -> list[str]:
        return sorted({a.category for a in self.library.all()})

    def reload(self) -> list[str]:
        """Re-read the YAML scripts (e.g. after a parent added one). Returns problems found."""
        errors = self.library.load()
        if errors:
            self.set_status(ServiceStatus.DEGRADED, f"{len(errors)} script problem(s)")
        elif self.service_status == ServiceStatus.DEGRADED:
            self.set_status(ServiceStatus.RUNNING)
        return errors

    # ------------------------------------------------------------ control
    async def start(self, activity_id: str | None = None, *, source: str = "dashboard") -> dict[str, Any]:  # type: ignore[override]
        """Start an activity (ActivityAPI). Without an id this is the Service lifecycle start."""
        if activity_id is None:
            errors = self.library.errors
            if errors:
                self.set_status(ServiceStatus.DEGRADED, f"{len(errors)} script problem(s)")
            else:
                self.set_status(ServiceStatus.RUNNING)
            return self.health()

        activity = self.library.get(activity_id)
        if activity is None:
            raise KeyError(activity_id)
        async with self._start_lock:
            await self._stop_current("replaced")
            loop = asyncio.get_running_loop()
            self._run_counter += 1
            seed = None if self._seed is None else self._seed + self._run_counter - 1
            run = _Run(activity=activity, source=source, rng=random.Random(seed), loop=loop,
                       thread_id=threading.get_ident(), values=self._initial_values(activity))
            lease = self.ctx.arbiter.acquire(
                OWNER, [Resource.FACE, Resource.AUDIO_OUT], Priority.ACTIVITY,
                on_preempt=lambda _lease, by, run=run: self._on_preempt(run, by),
            )
            if lease is None:
                raise PermissionError("LittleBot is busy with something more important right now; try again in a moment")
            run.lease = lease
            if not self.ctx.state.transition(RobotState.PLAYING_ACTIVITY, f"activity {activity.id}",
                                             activity=activity.id):
                lease.release()
                raise PermissionError(f"cannot start an activity while the robot is {self.ctx.state.state.value}")
            with self._lock:
                self._run = run
            run.task = loop.create_task(self._run_activity(run), name=f"activity:{activity.id}")
            # Safety net: a task cancelled before its first step never enters _run_activity's finally.
            run.task.add_done_callback(lambda _t, run=run: self._finish(run, False, run.stop_reason or "cancelled"))
            log.info("activity %s started (%s)", activity.id, source)
            self._publish(Topics.ACTIVITY_STARTED, {"id": activity.id, "name": activity.name,
                                                    "category": activity.category, "source": source})
            return self.status()

    async def stop(self, reason: str = "stopped") -> None:
        """Stop the running activity (no-op if none). Also used at shutdown."""
        await self._stop_current(reason)

    async def pause(self) -> bool:
        run = self._run
        if run is None or run.finished or not run.activity.pausable:
            return False
        if run.paused:
            return True
        run.paused = True
        run.pause_epoch += 1
        run.resumed.clear()
        run.paused_evt.set()
        self._audio_stop()
        if run.gesture_task is not None and not run.gesture_task.done():
            self._motion_cancel()
        self._face("set_expression", "neutral")
        log.info("activity %s paused", run.activity.id)
        self._publish(Topics.ACTIVITY_PAUSED, {"id": run.activity.id})
        return True

    async def resume(self) -> bool:
        run = self._run
        if run is None or run.finished or not run.paused:
            return False
        run.paused = False
        run.paused_evt.clear()
        run.resumed.set()
        log.info("activity %s resumed", run.activity.id)
        self._publish(Topics.ACTIVITY_RESUMED, {"id": run.activity.id})
        return True

    def respond(self, response: str, prompt_id: str | None = None) -> bool:
        """Caregiver answer to the current prompt. Thread-safe. False if nothing matches."""
        with self._lock:
            run = self._run
            prompt = run.prompt if run is not None else None
            if prompt is None or prompt.answered:
                return False
            if prompt_id and prompt_id != prompt.id:
                return False
            value = prompt.resolve(str(response or ""))
            if value is None:
                return False
            prompt.answered = True
        prompt.deliver(value)
        return True

    def status(self) -> dict[str, Any]:
        run = self._run
        if run is None:
            return {"running": False, "paused": False, "id": None, "name": None, "step_index": 0,
                    "total_steps": 0, "step_text": "", "prompt": None, "last": self._last}
        with self._lock:
            prompt = run.prompt.as_dict() if run.prompt is not None and not run.prompt.answered else None
        act = run.activity
        return {
            "running": True,
            "paused": run.paused,
            "id": act.id,
            "name": act.name,
            "category": act.category,
            "pausable": act.pausable,
            "source": run.source,
            "step_index": run.step_index,
            "total_steps": len(act.steps),
            "step_kind": run.step_kind,
            "step_text": run.step_text,
            "prompt": prompt,
            "elapsed_seconds": round(time.monotonic() - run.started_mono, 1),
            "last": self._last,
        }

    async def wait_until_finished(self, timeout: float | None = None) -> bool:
        """Await the end of the current run (True if nothing is running by then)."""
        run = self._run
        if run is None:
            return True
        try:
            await asyncio.wait_for(run.done_evt.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    # ------------------------------------------------------------ history (opt-in)
    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recorded runs (only exists if the parent enabled privacy.record_history). Never scores."""
        if not self._history_table_exists():
            return []
        return self.ctx.db.query(
            f"SELECT activity_id, started, finished, completed, reason, source FROM {HISTORY_TABLE} "
            "ORDER BY id DESC LIMIT ?", (int(limit),))

    def clear_history(self) -> int:
        if not self._history_table_exists():
            return 0
        row = self.ctx.db.query_one(f"SELECT COUNT(*) AS n FROM {HISTORY_TABLE}")
        self.ctx.db.execute(f"DELETE FROM {HISTORY_TABLE}")
        return int(row["n"]) if row else 0

    def _history_table_exists(self) -> bool:
        return self.ctx.db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                                     (HISTORY_TABLE,)) is not None

    def _record_history(self, run: _Run, completed: bool, reason: str) -> None:
        try:
            if not self.ctx.privacy.record_history:
                return
            if not self._history_ready:
                self.ctx.db.execute(
                    f"CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "activity_id TEXT NOT NULL, started REAL NOT NULL, finished REAL NOT NULL, "
                    "completed INTEGER NOT NULL, reason TEXT, source TEXT)")
                self._history_ready = True
            self.ctx.db.execute(
                f"INSERT INTO {HISTORY_TABLE}(activity_id, started, finished, completed, reason, source) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (run.activity.id, run.started_at, time.time(), int(completed), reason[:80], run.source[:40]))
        except Exception:  # noqa: BLE001
            log.warning("could not record activity history", exc_info=True)

    # ------------------------------------------------------------ bus callbacks (any thread)
    def _on_stop_all(self, _event: Event) -> None:
        run = self._run
        if run is None or run.finished:
            return
        if run.stop_reason is None:
            run.stop_reason = "stop all"
        self._audio_stop()
        self._cancel_run(run)

    def _on_caregiver_response(self, event: Event) -> None:
        response = event.get("response")
        if response is not None:
            self.respond(str(response), event.get("prompt_id"))

    def _on_state_changed(self, event: Event) -> None:
        run = self._run
        if run is None or run.finished or event.get("new") not in _STATES_THAT_STOP:
            return
        if run.stop_reason is None:
            run.stop_reason = f"robot state {str(event.get('new')).lower()}"
        self._cancel_run(run)

    def _on_preempt(self, run: _Run, by: str) -> None:
        if run.finished:
            return
        run.preempted = True
        if run.stop_reason is None:
            run.stop_reason = f"preempted by {by}"
        # Silence our speech *now*, before the new lease holder starts its own output.
        self._audio_stop()
        self._face("open_eyes")
        self._cancel_run(run)

    def _cancel_run(self, run: _Run) -> None:
        task = run.task
        if task is None or task.done():
            return
        try:
            if threading.get_ident() == run.thread_id:
                task.cancel()
            else:
                run.loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:  # loop closed
            pass

    async def _stop_current(self, reason: str) -> None:
        run = self._run
        if run is None or run.finished:
            return
        if run.stop_reason is None:
            run.stop_reason = reason
        self._cancel_run(run)
        task = run.task
        if task is None or task is asyncio.current_task():
            return
        try:
            loop = asyncio.get_running_loop()
            if run.loop is loop:
                await asyncio.wait({task}, timeout=3.0)
            else:  # stopped from another loop/thread: poll briefly
                deadline = time.monotonic() + 3.0
                while not run.finished and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
        finally:
            if not run.finished:
                log.warning("activity %s did not stop in time; forcing cleanup", run.activity.id)
                self._finish(run, False, run.stop_reason or reason)

    # ------------------------------------------------------------ the run task
    async def _run_activity(self, run: _Run) -> None:
        completed, reason = False, "error"
        try:
            self._face("open_eyes")
            await self._run_steps(run, run.activity.steps, top=True)
            completed, reason = True, "completed"
        except _EndActivity as e:
            completed, reason = True, e.reason
        except asyncio.CancelledError:
            completed, reason = False, run.stop_reason or "cancelled"
        except Exception:  # noqa: BLE001 - an activity must never crash the app
            log.exception("activity %s failed", run.activity.id)
            completed, reason = False, "error"
        finally:
            self._finish(run, completed, reason)

    def _finish(self, run: _Run, completed: bool, reason: str) -> None:
        """Single, idempotent cleanup path for every way a run can end."""
        if run.finished:
            return
        run.finished = True
        act = run.activity
        try:
            self._clear_prompt(run, None, cancelled=True)
            if not completed and run.gesture_task is not None and not run.gesture_task.done():
                run.gesture_task.cancel()  # on normal completion a last gesture may finish on its own
                self._motion_cancel()
            if not completed and not run.preempted:
                self._audio_stop()
            self._face("open_eyes")
            if not run.preempted:
                if completed:
                    self._face("set_expression", act.finish_expression, hold=2.0)
                else:
                    self._face("set_expression", "neutral")
            if run.lease is not None:
                run.lease.release()
            self.ctx.state.release(RobotState.PLAYING_ACTIVITY, f"activity {act.id} {reason}")
            self._record_history(run, completed, reason)
        except Exception:  # noqa: BLE001
            log.exception("activity cleanup problem")
        finally:
            duration = round(time.monotonic() - run.started_mono, 1)
            self._last = {"id": act.id, "name": act.name, "completed": completed, "reason": reason,
                          "duration_seconds": duration}
            with self._lock:
                if self._run is run:
                    self._run = None
            log.info("activity %s finished (%s)", act.id, reason)
            self._publish(Topics.ACTIVITY_FINISHED, {"id": act.id, "name": act.name, "completed": completed,
                                                     "reason": reason, "duration_seconds": duration})
            run.done_evt.set()

    async def _run_steps(self, run: _Run, steps: list[m.Step], *, top: bool = False) -> None:
        for i, step in enumerate(steps):
            await self._checkpoint(run)
            if top:
                run.step_index = i
            handler = self._handlers.get(step.kind)
            if handler is None:  # pragma: no cover - model guarantees known kinds
                log.warning("no handler for step %s", step.kind)
                continue
            await handler(self, run, step)
            if step.pause:
                await self._sleep(run, step.pause)

    async def _checkpoint(self, run: _Run) -> None:
        """Safe point: block here while paused."""
        while run.paused:
            await run.resumed.wait()

    # ------------------------------------------------------------ waiting helpers
    async def _await_any(self, run: _Run, futs: list[asyncio.Future[Any]], timeout: float | None) -> asyncio.Future[Any] | None:
        """Wait until one of ``futs`` completes; None on timeout.

        ``timeout`` is in script seconds (scaled by the clock) and does not run
        down while the activity is paused.
        """
        loop = asyncio.get_running_loop()
        remaining = None if timeout is None else self.ctx.clock.scaled(timeout)
        while True:
            for f in futs:
                if f.done():
                    return f
            if run.paused:
                waiter = loop.create_task(run.resumed.wait())
                try:
                    await asyncio.wait([*futs, waiter], return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiter.cancel()
                continue
            if remaining is not None and remaining <= 0:
                await asyncio.sleep(0)  # let thread-safe callbacks land
                for f in futs:
                    if f.done():
                        return f
                return None
            waiter = loop.create_task(run.paused_evt.wait())
            t0 = loop.time()
            try:
                await asyncio.wait([*futs, waiter], timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
            if remaining is not None:
                remaining -= loop.time() - t0

    async def _sleep(self, run: _Run, seconds: float) -> None:
        if seconds > 0:
            await self._await_any(run, [], seconds)
        else:
            await asyncio.sleep(0)

    # ------------------------------------------------------------ text
    def _initial_values(self, activity: Activity) -> dict[str, Any]:
        profile = self._profile()
        nickname = _clean_text(profile.get("nickname"))
        animals = [_clean_text(a) for a in profile.get("favourite_animals") or [] if _clean_text(a)]
        songs = [_clean_text(s) for s in profile.get("favourite_songs") or [] if _clean_text(s)]
        # Empty strings fall back at render time ("friend", "puppy" ... or an inline {name|fallback}).
        values: dict[str, Any] = {
            "child_name": nickname,
            "child_initial": nickname[0].upper() if nickname[:1].isalpha() else "",
            "favourite_animal": animals[0] if animals else "",
            "favourite_song": songs[0] if songs else "",
            "robot_name": _clean_text(getattr(self.ctx.config, "robot_name", "")) or "LittleBot",
            "round": 1,
        }
        for k, v in activity.variables.items():
            values[k] = v
        return values

    def _render(self, run: _Run, text: str) -> str:
        return m.render_template(text, lambda name: _lookup(run.values, name), _FALLBACKS)

    def _pick_text(self, run: _Run, text: str | list[str]) -> str:
        return self._render(run, run.rng.choice(text) if isinstance(text, list) else text)

    def _step_event(self, run: _Run, kind: str, text: str = "") -> None:
        run.step_kind, run.step_text = kind, text
        self._publish(Topics.ACTIVITY_STEP, {"id": run.activity.id, "step_index": run.step_index,
                                             "kind": kind, "text": text})

    # ------------------------------------------------------------ device wrappers (never raise)
    def _publish(self, topic: str, data: dict[str, Any]) -> None:
        try:
            self.ctx.bus.publish(topic, data, source="activities")
        except Exception:  # noqa: BLE001
            log.exception("publish %s failed", topic)

    def _face(self, method: str, *args: Any, **kwargs: Any) -> None:
        face = self.ctx.face
        if face is None:
            return
        try:
            getattr(face, method)(*args, **kwargs)
        except Exception:  # noqa: BLE001
            log.warning("face.%s failed", method, exc_info=log.isEnabledFor(logging.DEBUG))

    def _audio_stop(self) -> None:
        audio = self.ctx.audio
        if audio is None:
            return
        try:
            audio.stop_output()
        except Exception:  # noqa: BLE001
            log.warning("audio.stop_output failed", exc_info=log.isEnabledFor(logging.DEBUG))

    def _motion_cancel(self) -> None:
        motion = self.ctx.motion
        if motion is None:
            return
        try:
            motion.cancel()
        except Exception:  # noqa: BLE001
            log.warning("motion.cancel failed", exc_info=log.isEnabledFor(logging.DEBUG))

    async def _guard(self, run: _Run, label: str, call: Callable[[], Awaitable[Any]]) -> Any:
        """Await a device call; on failure log once per run and return None."""
        try:
            return await call()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            if label not in run.warned:
                run.warned.add(label)
                log.warning("%s failed during activity %s; continuing", label, run.activity.id, exc_info=True)
            return None

    async def _speak(self, run: _Run, text: str, rate: float | None = None) -> None:
        if not text:
            return
        audio = self.ctx.audio
        for _attempt in range(2):
            epoch = run.pause_epoch
            if audio is None:
                await self._sleep(run, min(4.0, 0.3 * len(text.split()) + 0.5))
                return
            ok = await self._guard(run, "audio.say", lambda: audio.say(text, rate=rate))
            if ok is None:  # failed: keep the pacing so the face still makes sense
                await self._sleep(run, min(3.0, 0.25 * len(text.split())))
                return
            if ok is not False or run.pause_epoch == epoch:
                return
            await self._checkpoint(run)  # paused mid-sentence: say it again after resuming

    async def _play_melody(self, run: _Run, notes: list[tuple[str, float]], tempo: float, wait: bool = True) -> None:
        audio = self.ctx.audio
        for _attempt in range(2):
            epoch = run.pause_epoch
            ok = None
            if audio is not None:
                ok = await self._guard(run, "audio.play_melody",
                                       lambda: audio.play_melody(list(notes), tempo_bpm=tempo, wait=wait))
            if ok is None:
                if wait:
                    await self._sleep(run, m.melody_seconds(notes, tempo))
                return
            if ok is not False or run.pause_epoch == epoch:
                return
            await self._checkpoint(run)

    # ------------------------------------------------------------ step handlers
    async def _do_say(self, run: _Run, step: m.SayStep) -> None:
        text = self._pick_text(run, step.text)
        if step.expression:
            self._face("set_expression", step.expression)
        if step.animation:
            self._face("play_animation", step.animation)
        self._step_event(run, "say", text)
        await self._speak(run, text, step.rate)

    async def _do_expression(self, run: _Run, step: m.ExpressionStep) -> None:
        self._face("set_expression", step.name, hold=step.hold)

    async def _do_animation(self, run: _Run, step: m.AnimationStep) -> None:
        self._face("play_animation", step.name)

    async def _do_sound(self, run: _Run, step: m.SoundStep) -> None:
        audio = self.ctx.audio
        ok = None
        if audio is not None:
            ok = await self._guard(run, "audio.play_sound",
                                   lambda: audio.play_sound(step.name, wait=step.wait, volume=step.volume))
        if ok is None and step.wait:
            await self._sleep(run, 0.4)

    async def _do_melody(self, run: _Run, step: m.MelodyStep) -> None:
        self._step_event(run, "melody", step.title or "")
        await self._play_melody(run, step.notes, step.tempo, step.wait)

    async def _do_gesture(self, run: _Run, step: m.GestureStep) -> None:
        motion = self.ctx.motion
        if motion is None:
            return
        try:
            if (motion.status() or {}).get("estop"):
                return
            if step.name not in (motion.gestures() or []):
                log.debug("gesture %s not available", step.name)
                return
        except Exception:  # noqa: BLE001
            if "motion" not in run.warned:
                run.warned.add("motion")
                log.warning("motion unavailable during activity %s", run.activity.id, exc_info=True)
            return
        if step.wait:
            await self._guard(run, "motion.run_gesture", lambda: motion.run_gesture(step.name))
            return
        if run.gesture_task is not None and not run.gesture_task.done():
            return  # still moving: skip rather than queue
        run.gesture_task = asyncio.get_running_loop().create_task(
            self._guard(run, "motion.run_gesture", lambda: motion.run_gesture(step.name)))

    async def _do_look(self, run: _Run, step: m.LookStep) -> None:
        self._face("look_at", step.x, step.y, hold=step.hold)

    async def _do_clear_gaze(self, run: _Run, step: m.Step) -> None:
        self._face("clear_gaze")

    async def _do_close_eyes(self, run: _Run, step: m.Step) -> None:
        self._face("close_eyes")

    async def _do_open_eyes(self, run: _Run, step: m.Step) -> None:
        self._face("open_eyes")

    async def _do_blink(self, run: _Run, step: m.BlinkStep) -> None:
        self._face("blink", double=step.double)

    async def _do_wait(self, run: _Run, step: m.WaitStep) -> None:
        await self._sleep(run, step.seconds)

    async def _do_note(self, run: _Run, step: m.NoteStep) -> None:
        self._step_event(run, "note", self._render(run, step.text))

    async def _do_prompt(self, run: _Run, step: m.PromptStep) -> None:
        text = self._render(run, step.text)
        options = [self._render(run, o) or o for o in step.options]
        timeout = step.timeout if step.timeout is not None else self._caregiver_timeout()
        prompt = self._open_prompt(run, text, options, timeout, "prompt")
        try:
            self._step_event(run, "prompt", text)
            self._publish(Topics.ACTIVITY_PROMPT, {"id": run.activity.id, **prompt.as_dict()})
            await self._await_any(run, [prompt.future], timeout)
        except BaseException:
            self._clear_prompt(run, prompt, None, cancelled=True)
            raise
        answer = _future_value(prompt.future)
        self._clear_prompt(run, prompt, answer, timed_out=answer is None)
        if answer is None:
            await self._run_steps(run, step.on_timeout)
        elif answer != SKIP:
            idx = options.index(answer) if answer in options else 0
            await self._run_steps(run, step.branch_for(step.options[idx]))

    async def _do_wait_for(self, run: _Run, step: m.WaitForStep) -> None:
        loop = asyncio.get_running_loop()
        event_fut: asyncio.Future[Any] = loop.create_future()
        thread_id = threading.get_ident()
        match = {k: _norm_value(v) for k, v in step.match.items()}
        contains = {k: [self._render(run, w).casefold() for w in words] for k, words in step.contains.items()}

        def on_event(event: Event) -> None:
            if not _event_matches(event, match, contains):
                return

            def _set() -> None:
                if not event_fut.done():
                    event_fut.set_result(event)

            if threading.get_ident() == thread_id:
                _set()
            else:
                try:
                    loop.call_soon_threadsafe(_set)
                except RuntimeError:
                    pass

        patterns = [p for p in step.event if self._source_available(p)]
        subs = [self.ctx.bus.subscribe(p, on_event) for p in patterns]
        prompt: _Prompt | None = None
        try:
            futs: list[asyncio.Future[Any]] = [event_fut]
            text = self._render(run, step.ask_caregiver) if step.ask_caregiver else ""
            if step.ask_caregiver:
                option = self._render(run, step.caregiver_option) or step.caregiver_option
                prompt = self._open_prompt(run, text, [option], step.timeout, "wait_for")
                futs.append(prompt.future)
            self._step_event(run, "wait_for", text)
            if prompt is not None:
                self._publish(Topics.ACTIVITY_PROMPT, {"id": run.activity.id, **prompt.as_dict()})
            if not subs and prompt is None:
                done = None  # nothing could ever satisfy it (camera / mic off): don't make the child wait
            else:
                done = await self._await_any(run, futs, step.timeout)
        except BaseException:
            if prompt is not None:
                self._clear_prompt(run, prompt, None, cancelled=True)
            raise
        finally:
            for sub in subs:
                sub.cancel()
        if prompt is not None:
            answer = _future_value(prompt.future)
            self._clear_prompt(run, prompt, answer, timed_out=done is None)
        if done is event_fut or (done is not None and _future_value(done) != SKIP):
            await self._run_steps(run, step.on_event)
        elif done is None:
            await self._run_steps(run, step.on_timeout)

    async def _do_listen(self, run: _Run, step: m.ListenStep) -> None:
        self._step_event(run, "listen", "")
        audio = self.ctx.audio
        heard: str | None = None
        if audio is not None and self._mic_allowed():
            seconds = self.ctx.clock.scaled(step.timeout)
            result = await self._guard(run, "audio.listen",
                                       lambda: asyncio.wait_for(audio.listen(timeout=seconds), seconds + 5.0))
            heard = result.strip() if isinstance(result, str) and result.strip() else None
        # Heard text is never logged or stored beyond this run's memory.
        if step.store_as:
            run.values[step.store_as] = heard or ""
        if heard is None:
            await self._run_steps(run, step.on_timeout)
            return
        expected = [self._render(run, w).casefold() for w in step.expect]
        low = heard.casefold()
        if expected and step.on_match and any(_contains_word(low, w) for w in expected if w):
            await self._run_steps(run, step.on_match)
        else:
            await self._run_steps(run, step.on_heard)

    async def _do_repeat(self, run: _Run, step: m.RepeatStep) -> None:
        previous = run.values.get("round", 1)
        try:
            for i in range(step.times):
                run.values["round"] = i + 1
                await self._run_steps(run, step.steps)
        finally:
            run.values["round"] = previous

    async def _do_choice(self, run: _Run, step: m.ChoiceStep) -> None:
        indices = list(range(len(step.options)))
        used = run.choices_used.setdefault(id(step), set()) if step.avoid_repeats else set()
        fresh = [i for i in indices if i not in used] or indices
        if len(fresh) == len(indices):
            used.clear()
        if step.weights:
            idx = run.rng.choices(fresh, weights=[step.weights[i] for i in fresh], k=1)[0] \
                if sum(step.weights[i] for i in fresh) > 0 else run.rng.choice(fresh)
        else:
            idx = run.rng.choice(fresh)
        used.add(idx)
        await self._run_steps(run, step.options[idx])

    async def _do_for_each(self, run: _Run, step: m.ForEachStep) -> None:
        items = (self.library.data_list(step.in_, run.activity) if isinstance(step.in_, str)
                 else list(step.in_))
        if step.shuffle:
            run.rng.shuffle(items)
        if step.limit:
            items = items[: step.limit]
        previous = (run.values.get(step.var), run.values.get("round", 1))
        try:
            for i, item in enumerate(items):
                run.values[step.var] = item
                run.values["round"] = i + 1
                await self._run_steps(run, step.steps)
        finally:
            run.values[step.var], run.values["round"] = previous

    async def _do_set(self, run: _Run, step: m.SetStep) -> None:
        for k, v in step.values.items():
            run.values[k] = self._render(run, v) if isinstance(v, str) else v

    async def _do_pick(self, run: _Run, step: m.PickStep) -> None:
        items = (self.library.data_list(step.from_, run.activity) if isinstance(step.from_, str)
                 else list(step.from_))
        if not items:
            run.values[step.var] = ""
            return
        favourites: list[str] = []
        if step.prefer_profile:
            favourites = [_singular(_clean_text(f).casefold()) for f in self._profile().get(step.prefer_profile) or []]
            favourites = [f for f in favourites if f]
        used = run.picked.setdefault(step.var, [])

        def key(item: Any) -> str:
            return _singular(m.to_text(item).casefold())

        def is_fav(item: Any) -> bool:
            names = {key(item)}
            if isinstance(item, dict):
                names |= {_singular(str(a).casefold()) for a in item.get("aliases") or []}
            return bool(names & set(favourites))

        chosen: list[Any] = []
        for _ in range(min(step.count, len(items))):
            fresh = [i for i in items if not step.avoid_repeats or key(i) not in used]
            fresh = [i for i in fresh if all(key(i) != key(c) for c in chosen)]
            if not fresh:
                used.clear()
                fresh = [i for i in items if all(key(i) != key(c) for c in chosen)] or list(items)
            favs = [i for i in fresh if is_fav(i)]
            item = run.rng.choice(favs or fresh)
            chosen.append(item)
            used.append(key(item))
        run.values[step.var] = chosen[0] if step.count == 1 else chosen

    async def _do_song(self, run: _Run, step: m.SongStep) -> None:
        title = self._render(run, step.title)
        self._step_event(run, "song", f"♪ {title}")
        for line in step.lines:
            await self._checkpoint(run)
            words = self._render(run, line.words) if line.words else ""
            if words:
                self._step_event(run, "song", words)
            speak = bool(words) and step.style != "play_only"
            play = bool(line.notes) and step.style != "speak_only"
            if step.style == "play_then_speak":
                if play:
                    await self._play_melody(run, line.notes, step.tempo)
                if speak:
                    await self._speak(run, words)
            else:
                if speak:
                    await self._speak(run, words)
                if play:
                    await self._play_melody(run, line.notes, step.tempo)
                elif not speak and words:  # play_only line without notes: give time to sing it
                    await self._sleep(run, 0.4 * len(words.split()))

    async def _do_end(self, run: _Run, step: m.EndStep) -> None:
        raise _EndActivity(step.reason)

    _handlers: dict[str, Callable[..., Awaitable[None]]] = {
        "say": _do_say, "expression": _do_expression, "animation": _do_animation, "sound": _do_sound,
        "melody": _do_melody, "gesture": _do_gesture, "look": _do_look, "clear_gaze": _do_clear_gaze,
        "close_eyes": _do_close_eyes, "open_eyes": _do_open_eyes, "blink": _do_blink, "wait": _do_wait,
        "note": _do_note, "prompt": _do_prompt, "wait_for": _do_wait_for, "listen": _do_listen,
        "repeat": _do_repeat, "choice": _do_choice, "set": _do_set, "pick": _do_pick, "song": _do_song,
        "for_each": _do_for_each,
        "end": _do_end,
    }

    # ------------------------------------------------------------ prompts
    def _open_prompt(self, run: _Run, text: str, options: list[str], timeout: float | None, kind: str) -> _Prompt:
        run.prompt_seq += 1
        prompt = _Prompt(id=f"{run.run_id}-{run.prompt_seq}", text=text, options=options, timeout=timeout,
                         kind=kind, future=asyncio.get_running_loop().create_future(), loop=run.loop,
                         thread_id=run.thread_id)
        with self._lock:
            run.prompt = prompt
        return prompt

    def _clear_prompt(self, run: _Run, prompt: _Prompt | None, response: Any = None, *,
                      timed_out: bool = False, cancelled: bool = False) -> None:
        with self._lock:
            current = run.prompt
            if current is None or (prompt is not None and current is not prompt):
                return
            run.prompt = None
            current.answered = True
        if not current.future.done():
            current.future.cancel()
        payload: dict[str, Any] = {"id": run.activity.id, "prompt_id": current.id,
                                   "response": None if response in (None, SKIP) else response}
        if response == SKIP:
            payload["skipped"] = True
        if timed_out:
            payload["timed_out"] = True
        if cancelled:
            payload["cancelled"] = True
        self._publish(Topics.ACTIVITY_PROMPT_CLEARED, payload)

    # ------------------------------------------------------------ context helpers
    def _profile(self) -> dict[str, Any]:
        try:
            p = self.ctx.settings.get("profile") or {}
            return p if isinstance(p, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _preferences(self) -> dict[str, dict[str, Any]]:
        prefs = self._profile().get("activity_preferences") or {}
        return prefs if isinstance(prefs, dict) else {}

    def _child_age(self) -> int | None:
        try:
            return self.ctx.settings.child_age_months()
        except Exception:  # noqa: BLE001
            return None

    def _caregiver_timeout(self) -> float:
        try:
            return float(self.ctx.config.activities.caregiver_timeout_seconds)
        except Exception:  # noqa: BLE001
            return 45.0

    def _mic_allowed(self) -> bool:
        try:
            return bool(self.ctx.privacy.microphone_enabled)
        except Exception:  # noqa: BLE001
            return False

    def _source_available(self, pattern: str) -> bool:
        """Can anything publish this event right now (privacy switches)?"""
        try:
            if pattern.startswith("vision."):
                if not self.ctx.privacy.camera_enabled:
                    return False
                if topic_matches(pattern, Topics.VISION_GESTURE) and pattern != "vision.*":
                    return bool(self.ctx.settings.get("vision.gestures_enabled", False))
            if pattern.startswith("audio.utterance"):
                return bool(self.ctx.privacy.microphone_enabled)
        except Exception:  # noqa: BLE001
            return True
        return True

    def _summary(self, a: Activity, prefs: dict[str, dict[str, Any]], age: int | None) -> dict[str, Any]:
        pref = prefs.get(a.id) or {}
        return {
            "id": a.id,
            "name": a.name,
            "description": a.description,
            "min_age_months": a.min_age_months,
            "max_age_months": a.max_age_months,
            "age_groups": a.age_groups(),
            "age_range": a.age_range_label(),
            "category": a.category,
            "participation": a.participation,
            "parent_instructions": a.parent_instructions,
            "safety_notes": list(a.safety_notes),
            "duration_seconds": a.duration_seconds,
            "pausable": a.pausable,
            "tags": list(a.tags),
            "materials": list(a.materials),
            "enabled": bool(pref.get("enabled", True)),
            "favourite": bool(pref.get("favourite", False)),
            "suitable": None if age is None else a.suitable_for(age),
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def default_script_dirs(ctx: Any) -> list[Path]:
    """Built-in scripts plus optional parent folders (config activities.custom_dirs, relative to data_dir)."""
    dirs = [ACTIVITY_SCRIPTS_DIR]
    cfg = getattr(ctx.config, "activities", None)
    for d in getattr(cfg, "custom_dirs", None) or []:
        p = Path(d).expanduser()
        if not p.is_absolute():
            p = ctx.config.data_path / p
        dirs.append(p)
    return dirs


def _sort_key(a: Activity, age: int | None, prefs: dict[str, dict[str, Any]]) -> tuple[Any, ...]:
    fav = bool((prefs.get(a.id) or {}).get("favourite"))
    if age is None:
        return (not fav, a.min_age_months, a.max_age_months, a.name.lower())
    span = max(1, a.max_age_months - a.min_age_months)
    if a.suitable_for(age):
        # Prefer activities aimed at this age: near the middle of a *narrow* range.
        centre = (a.min_age_months + a.max_age_months) / 2
        distance = 0.5 * abs(age - centre) / span + min(1.0, span / 72)
    else:
        gap = a.min_age_months - age if age < a.min_age_months else age - a.max_age_months + 1
        distance = 2 + gap / 12
    return (not fav, round(distance, 3), a.name.lower())


def _clean_text(value: Any, max_len: int = 32) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(ch for ch in value if ch.isprintable() and ch not in "{}")
    return re.sub(r"\s+", " ", text).strip()[:max_len].strip()


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _lookup(values: dict[str, Any], name: str) -> Any:
    """Dotted lookup into run variables (fallbacks are applied by the renderer)."""
    root, *rest = name.split(".")
    value: Any = values.get(root, "")
    for part in rest:
        if isinstance(value, dict):
            value = value.get(part, "")
        elif isinstance(value, (list, tuple)) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return ""
    return value


def _future_value(fut: asyncio.Future[Any]) -> Any:
    if fut.done() and not fut.cancelled():
        return fut.result()
    return None


def _norm_value(v: Any) -> Any:
    return v.casefold() if isinstance(v, str) else v


def _contains_word(text: str, word: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None


def _event_matches(event: Event, match: dict[str, Any], contains: dict[str, list[str]]) -> bool:
    for k, expected in match.items():
        if _norm_value(event.get(k)) != expected:
            return False
    for k, words in contains.items():
        value = event.get(k)
        if not isinstance(value, str):
            return False
        low = value.casefold()
        if not any(_contains_word(low, w) for w in words if w):
            return False
    return True
