"""GamesService: runs one on-screen game at a time.

Public API (used by the dashboard and by voice / AI tools)::

    games.list()                -> [{id, name, description, ages, ...}]
    await games.start(game_id)  -> bool   (False: unknown id or LittleBot is busy; see status()["last_error"])
    games.stop()                -> truthy if a game was stopped (also awaitable: ``await games.stop()``)
    games.status()              -> {running, id, name, seconds_left, game: {...}, last, last_error, ...}

Display hooks (``littlebot.face.display`` calls these on the main thread)::

    games.active                -> bool
    games.render(surface, rect, dt) -> bool   (True: the game was drawn instead of the face)
    games.handle_tap(nx, ny)    -> bool   (normalised 0..1 position inside the face rect)
    games.handle_key(name)      -> bool   (pygame key name; "escape" stops the game)

Leases and sharing
------------------
A game owns the screen: it acquires ``Resource.SCREEN`` and ``FACE`` + ``AUDIO_OUT`` at
``Priority.ACTIVITY`` and moves the robot to ``PLAYING_ACTIVITY`` (so it does not fall asleep
mid-game). It ends on STOP ALL, on preemption (a video, an activity, the parent), when the robot
goes to sleep, or when its time is up (after a short finale); the face comes back afterwards.

Voice exception: when a conversation (``conversation`` / ``live_voice`` lease owners) holds the
face / speaker - e.g. a voice tool starts a game in the middle of a reply - the game takes only
the screen and *shares* the rest, so the reply is not cut off; the robot state is left to the
conversation. A live voice call that starts during a game likewise takes the face / speaker
while the game keeps the screen. When the conversation ends the game takes them back.

Threading: one ``RLock`` guards the session. ``render`` / ``handle_*`` run on the display
thread, ``start`` on the asyncio runtime, ``stop`` / preemption / bus callbacks on any thread.
Games themselves never block: sounds are submitted to the runtime fire-and-forget (throttled so
a toddler's tap storm never builds an audio backlog).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Iterable

from littlebot.core.arbiter import Lease, Priority, Resource
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState
from littlebot.games.catalog import GAMES, GameInfo

log = logging.getLogger("littlebot.games")

OWNER = "games"
GAME_STARTED = "game.started"  # {id, name, source, limit_seconds, shared}
GAME_FINISHED = "game.finished"  # {id, name, reason, completed, duration_seconds, summary, score}

VOICE_OWNERS = frozenset({"conversation", "live_voice"})
SHARABLE = (Resource.FACE, Resource.AUDIO_OUT)
MIN_MINUTES, MAX_MINUTES = 0.5, 15.0
WATCH_PERIOD = 0.25  # s, session watchdog (time limit, headless driving, lease upgrade)
HEADLESS_AFTER = 0.6  # s without a drawn frame -> the watchdog advances the game itself
FINALE_GRACE = 6.0  # s after the time limit before a game that never finishes is ended anyway
MAX_DT = 0.1
MAX_FRAME_ERRORS = 30
GAZE_FRESH_SECONDS = 2.0
_STOP_STATES = {RobotState.SLEEPING.value, RobotState.ERROR.value, RobotState.PLAYING_MEDIA.value}

# Approximate lengths of the built-in sounds (assets/sounds), for the audio backlog throttle.
SOUND_SECONDS = {
    "bell": 1.6, "blip": 0.1, "boing": 0.65, "boop": 0.26, "chime": 1.35, "clap": 0.8, "click": 0.08,
    "drum": 0.7, "giggle": 0.65, "notes": 1.2, "peekaboo": 0.95, "pop": 0.18, "sleep": 1.7, "sparkle": 1.0,
    "success": 1.15, "tada": 1.15, "uhoh": 0.78, "wake": 1.0, "whoosh": 0.75, "yay": 0.85,
}
MAX_SOUND_BACKLOG = 0.35  # s of queued game audio before new requests are dropped (taps stay snappy)
MAX_SPEECH_BACKLOG = 1.2


class StopResult:
    """What ``stop()`` returns: truthy if a game was stopped, and awaitable (``await games.stop()``
    works too - the robot's shutdown awaits every service's ``stop()``)."""

    __slots__ = ("stopped",)

    def __init__(self, stopped: bool) -> None:
        self.stopped = bool(stopped)

    def __bool__(self) -> bool:
        return self.stopped

    def __eq__(self, other: object) -> bool:
        return bool(self) == bool(other) if isinstance(other, (bool, StopResult)) else NotImplemented

    def __hash__(self) -> int:
        return hash(self.stopped)

    def __await__(self) -> Any:
        return self._value().__await__()

    async def _value(self) -> bool:
        return self.stopped

    def __repr__(self) -> str:
        return f"StopResult({self.stopped})"


@dataclass(eq=False)
class _Session:
    info: GameInfo
    source: str
    started: float
    limit: float
    game: Any = None
    screen: Lease | None = None
    face: Lease | None = None
    shared: bool = False
    our_state: bool = False
    loop: asyncio.AbstractEventLoop | None = None
    task: "asyncio.Task[Any] | None" = None
    last_render: float = -1e9
    last_tick: float = 0.0
    wrapping: bool = False
    ended: bool = False
    errors: int = 0
    taps: int = 0
    tasks: set[Any] = field(default_factory=set)


class _Host:
    """The real ``GameHost``: sounds / speech through ``ctx.audio`` on the runtime, camera gaze."""

    def __init__(self, svc: "GamesService") -> None:
        self.svc = svc
        self._busy_until = 0.0
        self._lock = threading.Lock()

    @property
    def eye_color(self) -> tuple[int, int, int]:
        try:
            c = self.svc.ctx.config.face.eye_color
            return (int(c[0]), int(c[1]), int(c[2]))
        except Exception:  # noqa: BLE001
            return (90, 210, 255)

    def _reserve(self, seconds: float, max_backlog: float) -> bool:
        now = self.svc._clock()
        with self._lock:
            start = max(now, self._busy_until)
            if start - now > max_backlog:
                return False
            self._busy_until = start + max(0.05, seconds)
            return True

    def sound(self, name: str, *, volume: float | None = None, max_wait: float | None = None) -> None:
        if self._reserve(SOUND_SECONDS.get(name, 0.6), MAX_SOUND_BACKLOG if max_wait is None else float(max_wait)):
            self.svc._submit(lambda a: a.play_sound(name, wait=False, volume=volume), f"sound {name}")

    def melody(self, notes: list[tuple[str, float]], *, tempo_bpm: float = 120) -> None:
        beats = sum(float(b) for _n, b in notes)
        if self._reserve(beats * 60.0 / max(20.0, tempo_bpm) + 0.08, MAX_SOUND_BACKLOG):
            self.svc._submit(lambda a: a.play_melody(list(notes), tempo_bpm=tempo_bpm, wait=False), "melody")

    def say(self, text: str) -> None:
        if self._reserve(0.35 + 0.075 * len(text), MAX_SPEECH_BACKLOG):
            self.svc._submit(lambda a: a.say(text), "speech")

    def child_gaze(self) -> tuple[float, float] | None:
        return self.svc.child_gaze()

    def hands_ready(self) -> bool:
        from littlebot.games import hand_sign  # noqa: PLC0415

        return hand_sign.blocked_reason(self.svc.ctx) is None

    def see_hand(self) -> Any:
        from littlebot.games import hand_sign  # noqa: PLC0415

        look = hand_sign.HandLook()
        if not self.svc._run(hand_sign.look_for_sign(self.svc.ctx, look), "hand look"):
            look.finish(None, "unavailable")
        return look

    def now(self) -> float:
        return self.svc._clock()


class GamesService(Service):
    name = "games"

    def __init__(self, ctx: Any, *, catalog: Iterable[GameInfo] = GAMES, clock: Callable[[], float] = time.monotonic,
                 seed: int | None = None) -> None:
        super().__init__()
        self.ctx = ctx
        entries = list(catalog)
        self.catalog: dict[str, GameInfo] = {g.id: g for g in entries}
        self._order = [g.id for g in entries]
        self._clock = clock
        self._seed = seed
        self._runs = 0
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._last: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._gaze: tuple[float, float] | None = None
        self._gaze_t = 0.0
        self._canvas: Any = None
        self._scaled: Any = None
        self.host = _Host(self)
        bus = ctx.bus
        self._subs: list[Subscription] = [
            bus.subscribe(Topics.STOP_ALL, self._on_stop_all),
            bus.subscribe(Topics.STATE_CHANGED, self._on_state),
            bus.subscribe(Topics.VISION_FACES, self._on_faces),
            bus.subscribe(Topics.VISION_PERSON_LEFT, self._on_person_left),
            bus.subscribe(Topics.AUDIO_UTTERANCE, self._on_utterance),
        ]

    # ================================================================ catalogue
    def list(self) -> list[dict[str, Any]]:
        """Every game: {id, name, description, ages, min_age_months, audience, minutes, emoji, how_to}."""
        return [self.catalog[i].summary() for i in self._order]

    def get(self, game_id: str) -> dict[str, Any] | None:
        info = self.catalog.get(str(game_id))
        return info.summary() if info is not None else None

    # ================================================================ control
    async def start(self, game_id: str | None = None, *, source: str = "voice",  # type: ignore[override]
                    minutes: float | None = None) -> bool:
        """Put a game on the screen (replacing any running one). Without an id this is the
        Service lifecycle start. Returns False for an unknown id or when LittleBot is busy with
        something more important (a video, the parent); ``status()["last_error"]`` says why."""
        if game_id is None:
            self.set_status(ServiceStatus.RUNNING)
            return True
        info = self.catalog.get(str(game_id).strip().lower())
        if info is None:
            return self._fail(f"There is no game called {str(game_id)[:40]!r}.")
        try:
            cls = info.load()
        except Exception:  # noqa: BLE001
            log.exception("could not load game %s", info.id)
            return self._fail(f"{info.name} could not be loaded.")
        await self._stop_activity()
        limit = 60.0 * float(minutes if minutes is not None else info.minutes)
        limit = max(MIN_MINUTES * 60, min(MAX_MINUTES * 60, limit))
        s = _Session(info=info, source=str(source)[:40], started=self._clock(), limit=limit)
        if not self._acquire(s):
            return False
        with self._lock:
            old = self._session
        if old is not None:
            # Our new leases already replaced the old game's (same owner); keep the robot state.
            self._end(old, "replaced", keep_state=True, quiet_face=True)
        state = self.ctx.state
        if not s.shared:
            s.our_state = bool(state.transition(RobotState.PLAYING_ACTIVITY, f"game {info.id}", game=info.id))
        self._runs += 1
        seed = None if self._seed is None else self._seed + self._runs
        try:
            s.game = cls(self.host, seed=seed)
            s.game.begin()
        except Exception:  # noqa: BLE001
            log.exception("game %s failed to start", info.id)
            self._release(s)
            if s.our_state:
                state.release(RobotState.PLAYING_ACTIVITY, f"game {info.id} failed")
            return self._fail(f"{info.name} could not start.")
        s.last_tick = self._clock()
        with self._lock:
            self._session = s
        try:
            s.loop = asyncio.get_running_loop()
            s.task = s.loop.create_task(self._watch(s), name=f"game:{info.id}")
        except RuntimeError:  # pragma: no cover - start() is always awaited on a loop
            s.loop = None
        self.last_error = None
        log.info("game %s started (%s, %.0fs%s)", info.id, s.source, limit,
                 ", sharing face/speaker" if s.shared else "")
        self._publish(GAME_STARTED, {"id": info.id, "name": info.name, "source": s.source,
                                     "limit_seconds": round(limit), "shared": s.shared})
        return True

    def stop(self, reason: str = "stopped") -> StopResult:  # type: ignore[override]
        """Stop the running game (no-op if none). Safe from any thread; also the lifecycle stop."""
        with self._lock:
            s = self._session
        if s is None:
            return StopResult(False)
        return StopResult(self._end(s, reason))

    def close(self) -> None:
        """Drop bus subscriptions and stop any game (tests / teardown)."""
        for sub in self._subs:
            sub.cancel()
        self._subs.clear()
        self.stop("closed")

    @property
    def active(self) -> bool:
        s = self._session
        return s is not None and not s.ended

    def status(self) -> dict[str, Any]:
        with self._lock:
            s = self._session
            out: dict[str, Any] = {"available": True, "running": s is not None, "games": len(self.catalog),
                                   "last": dict(self._last) if self._last else None, "last_error": self.last_error}
            if s is None:
                return out
            now = self._clock()
            elapsed = max(0.0, now - s.started)
            try:
                game = s.game.status() if s.game is not None else {}
            except Exception:  # noqa: BLE001
                game = {}
            out.update({
                "id": s.info.id, "name": s.info.name, "emoji": s.info.emoji, "source": s.source,
                "elapsed_seconds": round(elapsed, 1), "limit_seconds": round(s.limit),
                "seconds_left": max(0, int(round(s.limit - elapsed))), "shared": s.shared,
                "drawn": now - s.last_render < 2.0, "game": game,
            })
            return out

    def child_gaze(self) -> tuple[float, float] | None:
        g = self._gaze
        if g is None or self._clock() - self._gaze_t > GAZE_FRESH_SECONDS:
            return None
        return g

    # ================================================================ display hooks
    def render(self, surface: Any, rect: Any = None, dt: float = 0.0) -> bool:
        """Advance and draw the running game into ``rect`` of ``surface``. False: nothing drawn."""
        s = self._session
        if s is None:
            return False
        end: str | None = None
        drawn = False
        with self._lock:
            if s.ended or self._session is not s:
                return False
            now = self._clock()
            s.last_render = s.last_tick = now
            game = s.game
            try:
                self._check_time(s, now)
                game.update(max(0.0, min(float(dt), MAX_DT)))
                self._paint(game, surface, rect)
                s.errors = 0
                drawn = True
            except Exception:  # noqa: BLE001 - a broken game must never take the face down
                s.errors += 1
                if s.errors <= 3:
                    log.exception("game %s frame failed", s.info.id)
                if s.errors >= MAX_FRAME_ERRORS:
                    end = "error"
            if end is None and game.finished:
                end = "time up" if s.wrapping else "completed"
        if end is not None:
            self._end(s, end, completed=end != "error")
        return drawn

    def handle_tap(self, nx: float, ny: float) -> bool:
        """A tap at normalised (nx, ny) inside the game area. True while a game is running
        (taps never fall through to the face underneath)."""
        with self._lock:
            s = self._session
            if s is None or s.ended:
                return False
            game = s.game
            s.taps += 1
            try:
                game.tap(min(1.0, max(0.0, float(nx))) * game.w, min(1.0, max(0.0, float(ny))) * game.h)
            except Exception:  # noqa: BLE001
                log.exception("game %s tap failed", s.info.id)
            return True

    def handle_key(self, name: str) -> bool:
        """A key press while a game runs. "escape" stops the game; others go to the game."""
        name = str(name or "").lower()
        with self._lock:
            s = self._session
            if s is None or s.ended:
                return False
            if name != "escape":
                try:
                    return bool(s.game.key(name))
                except Exception:  # noqa: BLE001
                    log.exception("game %s key failed", s.info.id)
                    return False
        self.stop("keyboard")
        return True

    def tick(self) -> None:
        """Run the session checks now (time limit, headless driving). The watchdog calls this."""
        s = self._session
        if s is not None:
            self._check(s)

    # ================================================================ internals
    def _fail(self, message: str) -> bool:
        self.last_error = message
        log.info("game not started: %s", message)
        return False

    async def _stop_activity(self) -> None:
        acts = self.ctx.activities
        if acts is None:
            return
        try:
            if acts.status().get("running"):
                await acts.stop("game started")
        except Exception:  # noqa: BLE001
            log.exception("could not stop the running activity")

    def _acquire(self, s: _Session) -> bool:
        arbiter = self.ctx.arbiter
        shared = [r for r in SHARABLE if (h := arbiter.holder(r)) is not None and h.owner in VOICE_OWNERS]
        screen = arbiter.acquire(OWNER, [Resource.SCREEN], Priority.ACTIVITY,
                                 on_preempt=lambda _l, by, s=s: self._on_screen_preempt(s, by))
        if screen is None:
            holder = arbiter.holder(Resource.SCREEN)
            what = "a video" if holder is not None and holder.owner == "media" else "something more important"
            return self._fail(f"LittleBot's screen is busy with {what} right now.")
        s.screen = screen
        rest = [r for r in SHARABLE if r not in shared]
        if rest:
            face = arbiter.acquire(OWNER, rest, Priority.ACTIVITY,
                                   on_preempt=lambda _l, by, s=s: self._on_face_preempt(s, by))
            if face is None:
                screen.release()
                with self._lock:
                    old = self._session
                if old is not None:  # our screen lease replaced the old game's: end it properly
                    self._end(old, "replaced")
                return self._fail("LittleBot is busy with something more important right now.")
            s.face = face
        s.shared = bool(shared)
        return True

    def _release(self, s: _Session) -> None:
        for lease in (s.screen, s.face):
            if lease is not None:
                lease.release()

    def _end(self, s: _Session, reason: str, *, completed: bool = False, preempted: bool = False,
             keep_state: bool = False, quiet_face: bool = False) -> bool:
        """Single, idempotent cleanup path for every way a game ends."""
        with self._lock:
            if s.ended:
                return False
            s.ended = True
            if self._session is s:
                self._session = None
            game = s.game
            try:
                summary = game.status() if game is not None else {}
            except Exception:  # noqa: BLE001
                summary = {}
            if game is not None:
                try:
                    game.close()
                except Exception:  # noqa: BLE001
                    log.debug("game close failed", exc_info=True)
        self._cancel_task(s)
        self._release(s)
        if not quiet_face:
            self._restore_face(completed=completed, preempted=preempted)
        if s.our_state and not keep_state:
            self.ctx.state.release(RobotState.PLAYING_ACTIVITY, f"game {s.info.id} {reason}")
        duration = round(max(0.0, self._clock() - s.started), 1)
        self._last = {"id": s.info.id, "name": s.info.name, "reason": reason, "completed": completed,
                      "duration_seconds": duration, "summary": summary.get("summary", ""),
                      "score": summary.get("score", 0), "ended_at": time.time()}
        log.info("game %s ended (%s) after %.0fs", s.info.id, reason, duration)
        self._publish(GAME_FINISHED, {k: v for k, v in self._last.items() if k != "ended_at"})
        return True

    def _restore_face(self, *, completed: bool, preempted: bool) -> None:
        face = self.ctx.face
        if face is None:
            return
        calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = [("open_eyes", (), {})]
        if not preempted:
            calls.append(("clear_gaze", (), {}))
            if completed:
                calls += [("set_expression", ("happy",), {"hold": 2.5}), ("play_animation", ("sparkle",), {})]
            else:
                calls.append(("set_expression", ("neutral",), {}))
        for name, args, kwargs in calls:
            try:
                getattr(face, name)(*args, **kwargs)
            except Exception:  # noqa: BLE001
                log.debug("face %s failed", name, exc_info=True)

    def _cancel_task(self, s: _Session) -> None:
        task, loop = s.task, s.loop
        if task is None or task.done() or loop is None:
            return
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:  # loop closed
            pass

    async def _watch(self, s: _Session) -> None:
        while not s.ended:
            await asyncio.sleep(WATCH_PERIOD)
            try:
                self._check(s)
            except Exception:  # noqa: BLE001
                log.exception("game watchdog failed")

    def _check_time(self, s: _Session, now: float) -> None:
        if not s.wrapping and now - s.started >= s.limit:
            s.wrapping = True
            s.game.wrap_up()

    def _check(self, s: _Session) -> None:
        end: str | None = None
        with self._lock:
            if s.ended or self._session is not s:
                return
            now = self._clock()
            game = s.game
            try:
                self._check_time(s, now)
                if now - s.last_render > HEADLESS_AFTER:  # no window: keep the game's clock running
                    game.update(max(0.0, min(now - s.last_tick, 0.25)))
                s.last_tick = now
            except Exception:  # noqa: BLE001
                log.exception("game %s update failed", s.info.id)
            if game.finished:
                end = "time up" if s.wrapping else "completed"
            elif s.wrapping and now - s.started >= s.limit + FINALE_GRACE:
                end = "time up"
        if end is not None:
            self._end(s, end, completed=True)
            return
        if s.shared:
            self._try_unshare(s)

    def _try_unshare(self, s: _Session) -> None:
        """The conversation that held the face / speaker is done: take them for the game."""
        arbiter = self.ctx.arbiter
        for r in SHARABLE:
            holder = arbiter.holder(r)
            if holder is not None and holder.owner != OWNER:
                return
        lease = arbiter.acquire(OWNER, list(SHARABLE), Priority.ACTIVITY,
                                on_preempt=lambda _l, by, s=s: self._on_face_preempt(s, by))
        if lease is None:
            return
        with self._lock:
            if s.ended:
                lease.release()
                return
            s.face, s.shared = lease, False
        if self.ctx.state.state in (RobotState.IDLE, RobotState.PLAYING_ACTIVITY):
            s.our_state = bool(self.ctx.state.transition(RobotState.PLAYING_ACTIVITY, f"game {s.info.id}",
                                                         game=s.info.id)) or s.our_state
        log.info("game %s has the face and speaker again", s.info.id)

    def _paint(self, game: Any, surface: Any, rect: Any) -> None:
        import pygame

        rect = pygame.Rect(rect) if rect is not None else surface.get_rect()
        if rect.size == game.size and surface.get_rect().contains(rect):
            game.draw(surface.subsurface(rect))
            return
        if self._canvas is None or self._canvas.get_size() != game.size:
            self._canvas = pygame.Surface(game.size, 0, 32)
        game.draw(self._canvas)
        if self._scaled is None or self._scaled.get_size() != rect.size:
            self._scaled = pygame.Surface(rect.size, 0, 32)
        pygame.transform.smoothscale(self._canvas, rect.size, self._scaled)
        surface.blit(self._scaled, rect)

    def _submit(self, make: Callable[[Any], Coroutine[Any, Any, Any]], what: str) -> None:
        audio = self.ctx.audio
        if audio is None:
            return
        try:
            coro = make(audio)
        except Exception:  # noqa: BLE001
            log.debug("%s failed", what, exc_info=True)
            return
        self._run(coro, what)

    def _run(self, coro: Coroutine[Any, Any, Any], what: str) -> bool:
        """Run ``coro`` on the robot's loop (from the display thread); False if there is none."""
        runtime = self.ctx.runtime
        try:
            if runtime is not None and runtime.running:
                runtime.submit(coro).add_done_callback(lambda f: _log_failure(f, what))
                return True
            loop = asyncio.get_running_loop()  # tests / tools running on a loop in this thread
            task = loop.create_task(coro)
            s = self._session
            if s is not None:
                s.tasks.add(task)
                task.add_done_callback(s.tasks.discard)
            return True
        except RuntimeError:
            coro.close()  # no loop to play it on (headless, no runtime)
            return False

    def _publish(self, topic: str, data: dict[str, Any]) -> None:
        try:
            self.ctx.bus.publish(topic, data, source="games")
        except Exception:  # noqa: BLE001
            log.exception("publishing %s failed", topic)

    # ---------------------------------------------------------------- bus / arbiter callbacks
    def _on_screen_preempt(self, s: _Session, by: str) -> None:
        self._end(s, f"preempted by {by}", preempted=True)

    def _on_face_preempt(self, s: _Session, by: str) -> None:
        if by in VOICE_OWNERS and not s.ended:
            with self._lock:
                s.face, s.shared = None, True
            log.info("game %s shares the face and speaker with %s", s.info.id, by)
            return
        self._end(s, f"preempted by {by}", preempted=True)

    def _on_stop_all(self, _event: Event) -> None:
        self.stop("stop all")

    def _on_state(self, event: Event) -> None:
        new = event.get("new")
        if new in _STOP_STATES and self._session is not None:
            self.stop(f"robot {str(new).lower()}")

    def _on_faces(self, event: Event) -> None:
        faces = event.get("faces") or []
        if not faces:
            return
        try:
            f = next((f for f in faces if f.get("primary")), None) or max(
                faces, key=lambda f: float(f.get("w", 0)) * float(f.get("h", 0)))
            self._gaze = (float(f["gaze_x"]), float(f["gaze_y"]))
            self._gaze_t = self._clock()
        except (KeyError, TypeError, ValueError):
            pass

    def _on_person_left(self, _event: Event) -> None:
        self._gaze = None

    def _on_utterance(self, event: Event) -> None:
        text = str(event.get("text") or "")
        if not text:
            return
        with self._lock:
            s = self._session
            if s is None or s.ended:
                return
            try:
                s.game.hear(text)
            except Exception:  # noqa: BLE001
                log.debug("game hear failed", exc_info=True)


def _log_failure(fut: Any, what: str) -> None:
    try:
        fut.result()
    except Exception as e:  # noqa: BLE001
        log.debug("game %s failed: %s", what, e)
