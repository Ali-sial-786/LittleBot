"""CallsService: video and voice calls through WhatsApp Web on LittleBot's screen, and Chromium shut down afterwards.

Public API (``ctx.calls``; used by the instant phrases and the voice's tools, littlebot/skills/calls.py, the
dashboard and the settings panel)::

    await calls.open(source="voice", contact=None, kind="video", answer=False)
        -> {"ok", "state", "error", "detail", "already", "contact"}
        WhatsApp Web on the screen. ``contact`` (a said name on the parent's list): once WhatsApp is ready, that
        chat is found with WhatsApp's search box and the video (``kind="voice"``: voice) call button is pressed.
        ``answer``: a call that is ringing there is accepted as soon as its Accept button is found.
    await calls.hang_up(reason) -> {"ok", "was_call", "closed"}   the End button if found, then everything shuts down
    await calls.close(reason) -> bool        Chromium shut down, its memory and the camera back (True if it was open)
    calls.close_soon(reason) -> bool         the same from any thread or a bus callback (doesn't wait)
    await calls.forget() -> bool             sign out on the robot: the saved profile folder is deleted
    calls.contacts() / calls.set_contacts(raw), calls.status(), calls.check(source), calls.offered(), calls.active,
    calls.in_call

Error codes: "unavailable" (no Chromium, no screen, offline mode, the config switch), "disabled" (the parent's
switch), "cloud_off", "microphone_off", "camera_off" (a video call with the camera off in Privacy), "child" (voice
recognition says the child spoke), "not_ready" (an error state), "low_memory", "busy", "failed",
"unknown_contact", "no_contacts", "in_call". ``ERRORS`` has the words.

Only while it is on the screen (the owner's rule)
-------------------------------------------------
Chromium with WhatsApp takes several hundred MB of the robot's 4 GB, so there is NO WhatsApp in the background and
the robot never rings by itself: the phone rings, and "answer on WhatsApp" opens it. It is shut down (its whole
process group is killed, then anything of it still running is looked for and killed too) when:

* "hang up" / "end the call" / "close WhatsApp", the cover's Close, the dashboard's Close;
* a call that was on is over (its buttons and pictures gone for ``CALL_END_GRACE_S``);
* ``calls.idle_seconds`` (120) pass with WhatsApp open, no call, and nobody touching it;
* its window is closed, or another window is in front of it for ``calls.off_screen_seconds``;
* something else takes the screen (a video, a game, an activity, Android, phone mirroring, the Nanit camera, the
  browser), STOP ALL, sleep, an error state, the screen going dark, the parent's switch, Privacy (cloud, the
  microphone, or the camera during a video call) going off;
* free memory falls under ``calls.min_free_mb_running`` while it runs: LittleBot comes first;
* LittleBot stopping.

How the window is made
----------------------
The videos' kiosk launcher (littlebot/media/display.py ``BrowserKioskBackend``: the same Chromium binary, app
window, Wayland hint and process-group shutdown) with a SAVED, non-incognito profile (``data/whatsapp-profile``),
so the QR link survives, and DevTools on ``--remote-debugging-address=127.0.0.1`` ``--remote-debugging-port=0``
(``littlebot.browser.cdp`` refuses any other host). Through DevTools, before WhatsApp loads:

* the camera and the microphone are granted to ``https://web.whatsapp.com`` ONLY (``Browser.grantPermissions`` with
  that origin, which also refuses it everything else; never ``--use-fake-ui-for-media-stream``). Without the
  camera's Privacy switch on, only the microphone. Notifications are granted only while the chats aren't hidden
  (their pop-ups show message previews);
* downloads, file pickers and dialogs are refused; every page load is checked (littlebot/calls/policy.py): the
  window shows web.whatsapp.com and nothing else, a link to anywhere else is refused, another window is closed
  unless it is WhatsApp's own (a call's);
* LittleBot's cover goes over the page (littlebot/calls/page_scripts.py; the setting ``calls.hide_chats``).

The camera
----------
A USB camera serves one program at a time, so while WhatsApp is open LittleBot's vision lends it
(``ctx.vision.lend_camera("calls")``: the device is closed, face tracking, photos and always recording pause; the
Privacy switch is not touched) and takes it back when Chromium is gone (``return_camera``).

What is never done
------------------
Chats and messages are never read out, handed to a model, sent or logged. The page scripts report only whether and
where a thing is. The logs name a contact by the name on the parent's list, never by what WhatsApp shows.

Logs: one INFO line per open, per state (linking, ready, a call), per step that failed, and per shutdown (the
reason, how long, the memory free before and after).
Bus: ``calls.opened`` {source}, ``calls.state`` {state, call}, ``calls.closed`` {reason}; ``calls.used`` {} every
30 s while it is open and on every touch (it keeps the face's screen on and LittleBot awake, as Android's does).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from littlebot.android.service import free_mb, where_android_is
from littlebot.android.windows import Window, list_windows
from littlebot.browser.cdp import CDPConnection, CDPError
from littlebot.calls import contacts as C
from littlebot.calls import page_scripts as scripts
from littlebot.calls import policy
from littlebot.core.arbiter import Lease, Priority, Resource
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState
from littlebot.mirror.service import PRIORITY as MIRROR_PRIORITY
from littlebot.mirror.service import make_room

log = logging.getLogger("littlebot.calls")

OWNER = "calls"
PRIORITY = MIRROR_PRIORITY  # above a card (40), below a game (50) and a video (60): as Android and the browser
OPENED = "calls.opened"  # {source}
STATE = "calls.state"  # {state, call}
CLOSED = "calls.closed"  # {reason}
USED = "calls.used"  # {}: WhatsApp is open / someone touched it (LittleBot doesn't doze off meanwhile)
GAME_STARTED = "game.started"  # littlebot.games.service.GAME_STARTED
VOICE_SPEAKER = "voice.speaker"  # {is_child, ...}: who spoke (voice recognition, ctx.voices)
CONTACTS = "calls.contacts"  # the setting: {said name: WhatsApp name}
HIDE_CHATS = "calls.hide_chats"  # the setting: LittleBot's cover over WhatsApp's page
ENABLED = "calls.enabled"

POLL_S = 1.0  # how often the page is asked what it shows
STEP_POLL_S = 0.4  # ... while a step of placing a call waits for something to appear
WINDOW_EVERY = 3  # every this many polls the desktop is asked which window is in front
KEEP_AWAKE_EVERY_S = 30.0
CHILD_RECENT_S = 15.0  # voice recognition saying the child spoke this recently refuses the voice's requests
CALL_END_GRACE_S = 3.0  # a call's buttons and pictures gone this long: the call is over
TEARDOWN_TIMEOUT_S = 6.0
KILL_WAIT_S = 2.0  # after the kill, anything of Chromium still there is waited for this long
SETTLE_S = 1.0  # the memory comes back a moment after Chromium is gone
SEARCH_WAIT_S, CHAT_WAIT_S, HEADER_WAIT_S, BUTTON_WAIT_S, CALL_WAIT_S = 4.0, 8.0, 6.0, 3.0, 12.0
_STALE_PROFILE_FILES = ("DevToolsActivePort", "SingletonLock", "SingletonSocket", "SingletonCookie")
_CLOSE_STATES = {RobotState.SLEEPING.value, RobotState.ERROR.value, RobotState.PLAYING_MEDIA.value,
                 RobotState.PLAYING_ACTIVITY.value}
FETCH_PATTERNS = [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]
EXTRA_SWITCHES = (
    "--remote-debugging-address=127.0.0.1",  # DevTools only for this computer...
    "--remote-debugging-port=0",  # ... on a free port Chromium picks (written to DevToolsActivePort)
    "--deny-permission-prompts",  # what DevTools didn't grant is refused without asking
    "--touch-events=enabled",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-breakpad",
    # (repeats the kiosk's own list: Chromium keeps only the last --disable-features)
    "--disable-features=Translate,MediaRouter,GlobalMediaControls,OptimizationHints,AutofillServerCommunication",
)

ERRORS = {
    "unavailable": "WhatsApp calls aren't available on this robot.",
    "disabled": "WhatsApp calls are switched off on the dashboard (Assistant page).",
    "cloud_off": "Cloud communication is off (Privacy page), so LittleBot can't go on the internet.",
    "microphone_off": "The microphone is switched off in Privacy.",
    "camera_off": "The camera is switched off in Privacy, so it can only be a voice call.",
    "child": "Only a grown-up can make a call.",
    "not_ready": "LittleBot has an error right now.",
    "low_memory": "There isn't enough free memory for WhatsApp right now.",
    "busy": "LittleBot's screen is busy with something more important right now.",
    "failed": "WhatsApp didn't open.",
    "unknown_contact": "That name isn't on LittleBot's list of people to call.",
    "no_contacts": "Nobody is on LittleBot's list of people to call yet (dashboard, Assistant page, WhatsApp).",
    "in_call": "A call is on already.",
}
# A step that didn't work: what the cover and the voice say ({name}: the said name on the parent's list).
PROBLEMS = {
    "load": "WhatsApp didn't load.",
    "search": "I couldn't find WhatsApp's search box.",
    "contact": "I couldn't find {name} in WhatsApp.",
    "chat": "I couldn't open {name}'s chat.",
    "call_button": "I couldn't find the call button.",
    "no_call": "I pressed the call button, but the call didn't start.",
    "failed": "Something went wrong in WhatsApp.",
}
# ... and, only when the chats aren't hidden, what to tap instead.
TAP_HINTS = {
    "search": "Tap the search box at the top of the list and find {name} yourself.",
    "contact": "Find {name} in the list and tap the name, then the camera at the top.",
    "chat": "Tap {name} in the list, then the camera at the top.",
    "call_button": "Tap the camera at the top of the chat.",
    "no_call": "Tap the call button at the top of the chat.",
}
COVERED_HINT = "It stays covered. Say close WhatsApp, or try again."
LINK_SAY = ("WhatsApp isn't linked to me yet. On your phone, open WhatsApp, go to Linked devices, tap Link a device, "
            "and scan the code on my screen.")


@dataclass(eq=False)
class _Session:
    id: int
    source: str
    started: float
    state: str = "starting"  # starting | loading | linking | ready
    call: str = "none"  # none | incoming | in_call
    contact: tuple[str, str] | None = None  # (the said name, the name in WhatsApp) to call once it is ready
    kind: str = "video"  # video | voice
    answer: bool = False  # accept the call that is ringing, once its button is found
    backend: Any = None  # media.display.BrowserKioskBackend
    lease: Lease | None = None
    cdp: Any = None  # CDPConnection
    page: str | None = None  # the page's DevTools session id
    target_id: str = ""  # = the main frame's id
    world: int | None = None  # execution context of LittleBot's isolated world
    windows: dict[str, str] = field(default_factory=dict)  # WhatsApp's own other windows: target id -> session id
    call_page: str | None = None  # the DevTools session the call's buttons are in (the page, or such a window)
    ended: bool = False
    reason: str = ""
    free_before: int | None = None  # MB free just before it was shut down
    placing: bool = False  # a call is being placed (the steps are running)
    problem: str = ""  # what went wrong (shown on the cover)
    had_call: bool = False
    no_call_since: float | None = None
    idle_since: float = 0.0
    last_awake: float = -1e9
    seen_front: bool = False
    off_since: float | None = None
    told_link: bool = False
    voice_ended: bool = False
    camera: bool = False  # the camera was borrowed from vision
    video_ok: bool = False  # the camera was granted to WhatsApp
    hide: bool = True  # the cover was on when it opened (notifications follow it)
    cover: str = ""  # the cover state last handed to the page (JSON)
    last: dict[str, Any] = field(default_factory=dict)  # the page's last answer (state_script)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)


class CallsService(Service):
    name = "calls"

    def __init__(self, ctx: Any, *, launcher: Any = None, browser_path: Any = ...,
                 connect: Callable[[str], Coroutine[Any, Any, Any]] | None = None,
                 clock: Callable[[], float] = time.monotonic, kiosk: bool | None = None,
                 profile_dir: str | Path | None = None, memory: Callable[[], int | None] = free_mb,
                 windows: Callable[[], list[Window] | None] | None = None, poll: float = POLL_S,
                 step_poll: float = STEP_POLL_S, settle: float = SETTLE_S, home: str = policy.HOME,
                 origin: str = policy.ORIGIN, insecure_hosts: tuple[str, ...] = (),
                 extra_args: tuple[str, ...] = (), proc_root: str = "/proc") -> None:
        """Everything that touches the robot is a parameter (tests fake them): ``launcher`` / ``connect`` start
        Chromium and reach DevTools, ``memory`` reads the free memory, ``windows`` asks the desktop.
        ``browser_path``: the Chromium binary (default: ``media.browser_command`` or the first one found, and none
        in a headless run or offline mode). ``home`` / ``origin`` / ``insecure_hosts``: a test's own local page."""
        super().__init__()
        self.ctx = ctx
        self.cfg = ctx.config.calls
        self._launcher = launcher
        self._connect = connect or CDPConnection.connect
        self._clock = clock
        self._kiosk = kiosk
        self._memory = memory
        self._windows = windows or list_windows
        self._poll, self._step_poll, self._settle = float(poll), float(step_poll), float(settle)
        self._home, self._origin, self._insecure = str(home), str(origin), tuple(insecure_hosts)
        self._extra = tuple(extra_args)
        self._proc_root = proc_root
        self._path = self._find_browser() if browser_path is ... else browser_path
        extras = getattr(ctx, "extras", None) or {}
        if profile_dir is None:
            profile_dir = extras.get("whatsapp_profile_dir") or ctx.config.data_path / str(self.cfg.profile_dir)
        self._profile_dir = Path(profile_dir)
        self._lock = threading.RLock()
        self._open_lock = asyncio.Lock()
        self._session: _Session | None = None
        self._ids = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._teardowns: set[Any] = set()
        self._spoke: tuple[bool, float] | None = None  # (is_child, when) from voice recognition
        self._blind_told = False
        self._last: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._subs: list[Subscription] = []

    # ================================================================ lifecycle
    async def start(self) -> None:  # type: ignore[override]
        self._bind_loop()
        bus = self.ctx.bus
        if not self._subs:
            self._subs = [
                bus.subscribe(Topics.STOP_ALL, lambda _e: self.close_soon("stop all")),
                bus.subscribe(Topics.STATE_CHANGED, self._on_state),
                bus.subscribe(Topics.MEDIA_STARTED, lambda _e: self.close_soon("a video started")),
                bus.subscribe(Topics.ACTIVITY_STARTED, lambda _e: self.close_soon("an activity started")),
                bus.subscribe(GAME_STARTED, lambda _e: self.close_soon("a game started")),
                bus.subscribe(Topics.FACE_SCREEN, self._on_screen),
                bus.subscribe(Topics.SETTINGS_CHANGED, self._on_setting),
                bus.subscribe(Topics.PRIVACY_CHANGED, self._on_privacy),
                bus.subscribe(VOICE_SPEAKER, self._on_speaker),
            ]
        why = self.unavailable_reason()
        if why is None:
            self.set_status(ServiceStatus.RUNNING)
            await asyncio.to_thread(self._kill_leftovers, "at start")  # a Chromium a crash left behind
        elif not self.cfg.enabled or self.ctx.config.is_offline:
            self.set_status(ServiceStatus.DISABLED, why)
        else:
            self.set_status(ServiceStatus.DEGRADED, why)

    async def stop(self) -> None:  # type: ignore[override]
        await self.close("LittleBot is stopping")
        for sub in self._subs:
            sub.cancel()
        self._subs.clear()
        self.set_status(ServiceStatus.STOPPED)

    def _find_browser(self) -> str | None:
        if self.ctx.config.display.headless or self.ctx.config.is_offline:
            return None
        from littlebot.media.display import find_browser  # noqa: PLC0415

        return find_browser(self.ctx.config.media.browser_command)

    # ================================================================ gates
    def unavailable_reason(self) -> str | None:
        """Why there are no WhatsApp calls on this robot at all (None: there are)."""
        if not self.cfg.enabled:
            return "switched off in LittleBot's config (calls.enabled)"
        if self.ctx.config.is_offline:
            return "offline mode"
        if not self._path:
            return ("no screen (headless run)" if self.ctx.config.display.headless
                    else "Chromium is not installed (sudo apt install chromium-browser)")
        return None

    @property
    def switched_on(self) -> bool:
        return bool(self.ctx.settings.get(ENABLED, True))

    @property
    def hide_chats(self) -> bool:
        """The parent's switch: LittleBot's cover hides WhatsApp's page (on unless a grown-up turned it off)."""
        return bool(self.ctx.settings.get(HIDE_CHATS, True))

    def offered(self) -> bool:
        """The voice may offer calls (they exist and the parent's switch is on)."""
        return self.unavailable_reason() is None and self.switched_on

    def check(self, source: str = "voice", *, kind: str | None = None) -> str | None:
        """Why WhatsApp can't open now for ``source`` (an ``ERRORS`` code), else None. Cheap: nothing is run.
        ``kind="video"``: also whether a video call may use the camera."""
        if self.unavailable_reason() is not None:
            return "unavailable"
        if not self.switched_on:
            return "disabled"
        privacy = self.ctx.privacy
        if not privacy.cloud_enabled:
            return "cloud_off"
        if not privacy.microphone_enabled:
            return "microphone_off"
        if kind == "video" and not privacy.camera_enabled:
            return "camera_off"
        if self.ctx.state.state == RobotState.ERROR:
            return "not_ready"
        if source in ("voice", "tool") and self._child_spoke():
            return "child"
        if not self.active:
            free = self._memory()
            if free is not None and free < int(self.cfg.min_free_mb):
                return "low_memory"
        return None

    def _child_spoke(self) -> bool:
        spoke = self._spoke
        return spoke is not None and spoke[0] and self._clock() - spoke[1] <= CHILD_RECENT_S

    @property
    def active(self) -> bool:
        s = self._session
        return s is not None and not s.ended

    @property
    def in_call(self) -> bool:
        """A call is ringing or on in WhatsApp right now (as far as its page shows)."""
        s = self._session
        return s is not None and not s.ended and s.call != "none"

    # ================================================================ who can be called
    def contacts(self) -> dict[str, str]:
        """``{said name: name in WhatsApp}``: the parent's list (the dashboard), else the config's."""
        raw = self.ctx.settings.get(CONTACTS, None)
        if not isinstance(raw, dict):
            raw = dict(self.cfg.contacts or {})
        return {str(k): str(v) for k, v in raw.items() if str(k).strip()}

    def set_contacts(self, raw: Any, *, source: str = "dashboard") -> dict[str, str]:
        """Keep the dashboard's list (``contacts.clean`` raises ``ValueError`` with words for the parent)."""
        cleaned = C.clean(raw)
        self.ctx.settings.set(CONTACTS, cleaned, source=source)
        log.info("calls: the list of people to call was changed from %s (%d on it)", source, len(cleaned))
        return cleaned

    def find_contact(self, said: Any) -> tuple[str, str] | None:
        return C.find(self.contacts(), said)

    # ================================================================ opening
    async def open(self, *, source: str = "voice", contact: str | None = None, kind: str = "video",
                   answer: bool = False) -> dict[str, Any]:
        """WhatsApp on the screen (and a call placed or answered once it is ready); see the module docstring."""
        self._bind_loop()
        source = str(source or "voice")[:20]
        kind = "voice" if str(kind or "").lower() in ("voice", "audio") else "video"
        who: tuple[str, str] | None = None
        if contact:
            people = self.contacts()
            if not people:
                return self._fail("no_contacts", log_it=False)
            who = C.find(people, contact)
            if who is None:
                return self._fail("unknown_contact", log_it=False,
                                  detail=f"{ERRORS['unknown_contact']} You can call {C.say_who(people)}.")
        why = self.check(source, kind=kind if who is not None else None)
        if why is not None:
            return self._fail(why, log_it=why != "child")
        async with self._open_lock:
            s = self._session
            if s is not None and not s.ended:
                return self._ask_again(s, who, kind, answer, source)
            return await self._launch(source, who, kind, answer)

    def _ask_again(self, s: _Session, who: tuple[str, str] | None, kind: str, answer: bool, source: str
                   ) -> dict[str, Any]:
        """WhatsApp is open already: the new request (a call to place, a call to answer) goes to it."""
        now = self._clock()
        s.idle_since = now
        if who is not None:
            if s.call != "none" or s.placing:
                return self._fail("in_call", log_it=False)
            s.contact, s.kind, s.problem, s.answer = who, kind, "", False
            log.info("calls: asked to %s call %s (from %s; WhatsApp is open already)", kind, who[0], source)
            if s.state == "ready":
                self._begin_call(s)
        elif answer:
            s.answer, s.problem = True, ""
            if s.call == "incoming":
                self._spawn(s, self._accept(s))
        return self._ok(s, already=True)

    async def _launch(self, source: str, who: tuple[str, str] | None, kind: str, answer: bool) -> dict[str, Any]:
        from littlebot.media.display import (  # noqa: PLC0415
            BrowserKioskBackend,
            PlayerBackendError,
        )

        if self.ctx.state.state == RobotState.SLEEPING:
            self.ctx.state.transition(RobotState.IDLE, "WhatsApp was opened")
        await make_room(self.ctx, "calls", "WhatsApp opened")
        await self._others_make_way()
        self._ids += 1
        now = self._clock()
        s = _Session(id=self._ids, source=source, started=now, contact=who, kind=kind, answer=bool(answer),
                     idle_since=now, hide=self.hide_chats)
        # Taken like a video (a card or a game on the screen goes), then held lower so a game or video wins later.
        lease = self.ctx.arbiter.acquire(OWNER, [Resource.SCREEN], Priority.MEDIA, on_preempt=self._preempted(s))
        if lease is None:
            return self._fail("busy")
        s.lease = self.ctx.arbiter.acquire(OWNER, [Resource.SCREEN], PRIORITY, on_preempt=self._preempted(s)) or lease
        with self._lock:
            self._session = s
        free = self._memory()
        log.info("calls: opening WhatsApp (from %s%s)%s", source,
                 f", to {kind} call {who[0]}" if who else ", to answer a call" if answer else "",
                 f"; {free} MB of memory free" if free is not None else "")
        try:
            await asyncio.to_thread(self._kill_leftovers, "before opening")
            profile = await asyncio.to_thread(self._profile)
            await self._borrow_camera(s)
            width, height = self.ctx.config.display.width, self.ctx.config.display.height
            switches = [*EXTRA_SWITCHES, *(["--disable-notifications"] if s.hide else []), *self.cfg.extra_args,
                        *self._extra]
            s.backend = BrowserKioskBackend(
                self._path, user_data_dir=profile, width=width, height=height, extra_args=switches,
                launcher=self._launcher, kiosk=self._use_kiosk(), label="WhatsApp window", incognito=False)
            await s.backend.open(scripts.loading_url(self._theme()))
            port, path = await self._devtools_address(s, profile)
            s.cdp = await self._connect(f"ws://127.0.0.1:{port}{path}")
            s.cdp.on_event(lambda method, params, sid, s=s: self._on_devtools(s, method, params, sid))
            await self._setup(s)
            if s.ended:
                raise CDPError("closed while starting")
            s.state = "loading"
            result = await self._page(s, "Page.navigate", {"url": self._home})
            if result.get("errorText"):
                log.info("calls: WhatsApp didn't load (%s)", str(result["errorText"])[:80])
        except (CDPError, PlayerBackendError, OSError, TimeoutError, ValueError, KeyError) as e:
            if not s.ended:
                log.warning("calls: WhatsApp couldn't open (%s: %s)", type(e).__name__, str(e)[:160])
                self.close_soon("failed to start")
            await self._wait_teardowns()
            return self._fail("failed")
        if s.ended:  # closed while starting (STOP ALL, a video ...)
            await self._wait_teardowns()
            return self._fail("failed", log_it=False)
        self._publish(OPENED, {"source": source})
        self._awake(s, now)
        self._spawn(s, self._watch(s))
        return self._ok(s)

    async def _others_make_way(self) -> None:
        """One thing on the screen at a time: the browser view, Android, phone mirroring and the baby camera close
        (each would lose the screen's lease anyway; this waits until they are gone, the browser's Chromium first)."""
        for name, method, args in (("browser", "aclose", ("WhatsApp opened",)), ("android", "close", ("WhatsApp opened",)),
                                   ("mirror", "astop", ("WhatsApp opened",)), ("nanit", "close", ("WhatsApp opened",))):
            svc = getattr(self.ctx, name, None)
            try:
                on = svc is not None and bool(getattr(svc, "active", False))
            except Exception:  # noqa: BLE001
                on = False
            fn = getattr(svc, method, None) if on else None
            if fn is None:
                continue
            try:
                res = fn(*args)
                if asyncio.iscoroutine(res):
                    await asyncio.wait_for(res, 15.0)
            except Exception:  # noqa: BLE001 - the lease takes the screen from it anyway
                log.debug("calls: closing %s first failed", name, exc_info=True)

    def _profile(self) -> Path:
        d = self._profile_dir
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in _STALE_PROFILE_FILES:  # left by an earlier run: a new port is written, and no lock holds it
            (d / name).unlink(missing_ok=True)
        return d

    def linked_before(self) -> bool:
        """A saved WhatsApp profile is on the robot (it may hold a link to the owner's WhatsApp)."""
        return self._profile_dir.is_dir()

    async def forget(self) -> bool:
        """Sign out on the robot: WhatsApp is closed and the saved profile folder is deleted. True if there was
        one. (The phone still lists the robot under Linked devices until it is removed there.)"""
        await self.close("signing out of WhatsApp on the robot")
        existed = self.linked_before()
        await asyncio.to_thread(shutil.rmtree, self._profile_dir, True)
        if existed:
            log.info("calls: the saved WhatsApp profile (the sign-in) was deleted")
        return existed

    def _use_kiosk(self) -> bool:
        choice = self._kiosk if self._kiosk is not None else self.cfg.kiosk
        if choice is not None:
            return bool(choice)
        from littlebot.hardware.platform import is_raspberry_pi  # noqa: PLC0415

        return bool(is_raspberry_pi())

    def _theme(self) -> str:
        return "dark" if self.ctx.settings.get("display.theme", "light") == "dark" else "light"

    async def _devtools_address(self, s: _Session, profile: Path) -> tuple[int, str]:
        """The port and path Chromium wrote into DevToolsActivePort (it picked a free port)."""
        f = profile / "DevToolsActivePort"
        deadline = self._clock() + float(self.cfg.start_timeout_seconds)
        while True:
            if s.ended:
                raise CDPError("closed while starting")
            text = await asyncio.to_thread(_read_text, f)
            lines = text.splitlines() if text else []
            if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].startswith("/devtools/"):
                return int(lines[0]), lines[1].strip()
            if s.backend.is_running() is False:
                raise CDPError("Chromium exited while starting")
            if self._clock() >= deadline:
                raise TimeoutError("Chromium's DevTools didn't answer")
            await asyncio.sleep(0.1)

    def permissions(self, s: _Session) -> list[str]:
        """What web.whatsapp.com is granted (and nothing else, to nobody else)."""
        out = ["audioCapture"]
        if s.video_ok:
            out.append("videoCapture")
        if not s.hide:
            out.append("notifications")  # their pop-ups show message previews: never while the chats are hidden
        return out

    async def _setup(self, s: _Session) -> None:
        """Before WhatsApp loads: the permissions, the locks and the cover (see the module docstring)."""
        cdp = s.cdp
        page = None
        for _ in range(50):
            infos = (await cdp.send("Target.getTargets")).get("targetInfos") or []
            page = next((t for t in infos if t.get("type") == "page"), None)
            if page is not None:
                break
            await asyncio.sleep(0.1)
        if page is None:
            raise CDPError("no page in the browser")
        s.target_id = str(page["targetId"])
        s.page = (await cdp.send("Target.attachToTarget", {"targetId": s.target_id, "flatten": True}))["sessionId"]
        await cdp.send("Target.setDiscoverTargets", {"discover": True})  # other windows are checked from now on
        await self._quietly(cdp.send("Browser.setDownloadBehavior", {"behavior": "deny"}))
        s.video_ok = bool(self.ctx.privacy.camera_enabled)
        # this origin gets these and is refused everything else; no other origin gets anything
        await cdp.send("Browser.grantPermissions", {"origin": self._origin, "permissions": self.permissions(s)})
        await self._page(s, "Page.enable")
        await self._page(s, "Runtime.enable")
        await self._page(s, "Runtime.addBinding", {"name": scripts.BINDING, "executionContextName": scripts.WORLD})
        first = self._cover_state(s)
        s.cover = json.dumps(first, sort_keys=True)
        await self._page(s, "Page.addScriptToEvaluateOnNewDocument",
                         {"source": scripts.cover_script(first), "worldName": scripts.WORLD})
        await self._quietly(self._page(s, "Page.setInterceptFileChooserDialog", {"enabled": True}))
        await self._page(s, "Fetch.enable", {"patterns": FETCH_PATTERNS})
        zoom = float(self.cfg.zoom)
        if abs(zoom - 1.0) > 0.01:
            await self._quietly(self._page(s, "Emulation.setDeviceMetricsOverride",
                                           {"width": 0, "height": 0, "deviceScaleFactor": round(zoom, 2),
                                            "mobile": False}))

    # ================================================================ the camera (lent by vision)
    async def _borrow_camera(self, s: _Session) -> None:
        if not self.ctx.privacy.camera_enabled:
            return  # the camera stays off: voice calls only
        vision = getattr(self.ctx, "vision", None)
        lend = getattr(vision, "lend_camera", None)
        if lend is None:
            return
        try:
            released = await lend(OWNER)
        except Exception:  # noqa: BLE001 - a call without LittleBot's camera closed is still worth trying
            log.warning("calls: vision couldn't lend the camera", exc_info=True)
            return
        s.camera = True
        if not released:
            log.warning("calls: vision didn't close the camera in time; WhatsApp may not get it")

    def _return_camera(self, s: _Session) -> None:
        if not s.camera:
            return
        s.camera = False
        give = getattr(getattr(self.ctx, "vision", None), "return_camera", None)
        if give is None:
            return
        try:
            give(OWNER)
        except Exception:  # noqa: BLE001
            log.warning("calls: giving the camera back to vision failed", exc_info=True)

    # ================================================================ DevTools events (on the loop)
    def _on_devtools(self, s: _Session, method: str, params: dict[str, Any], sid: str | None) -> None:
        if s.ended:
            return
        if method == "Fetch.requestPaused":
            self._spawn(s, self._on_request(s, params))
        elif method == "Runtime.bindingCalled" and params.get("name") == scripts.BINDING:
            try:
                kind = json.loads(str(params.get("payload") or "{}")).get("type")
            except (ValueError, AttributeError):
                return
            self._asked(s, str(kind or ""))
        elif method == "Page.frameNavigated" and sid == s.page:
            frame = params.get("frame") or {}
            if not frame.get("parentId"):
                s.world = None  # a new document: a new isolated world
                self._spawn(s, self._on_navigated(s, str(frame.get("url") or "")))
        elif method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            tid = info.get("targetId")
            if s.target_id and info.get("type") == "page" and tid not in (None, "", s.target_id):
                self._spawn(s, self._on_window(s, str(tid), str(info.get("url") or ""), new=True))
        elif method == "Target.targetInfoChanged":
            info = params.get("targetInfo") or {}
            tid = str(info.get("targetId") or "")
            if tid in s.windows:
                self._spawn(s, self._on_window(s, tid, str(info.get("url") or ""), new=False))
        elif method in ("Target.targetDestroyed", "Target.detachedFromTarget"):
            tid = params.get("targetId")
            if tid == s.target_id or (method == "Target.detachedFromTarget" and params.get("sessionId") == s.page):
                self.close_soon("its window was closed")
            elif tid in s.windows:
                s.windows.pop(str(tid), None)
        elif method == "Page.javascriptDialogOpening" and sid == s.page:
            accept = params.get("type") == "beforeunload"
            self._spawn(s, self._quietly(self._page(s, "Page.handleJavaScriptDialog", {"accept": accept})))
        elif method in ("Browser.downloadWillBegin", "Page.downloadWillBegin"):
            log.info("calls: a download was blocked")

    async def _on_request(self, s: _Session, params: dict[str, Any]) -> None:
        """Every page (document) Chromium asks for: WhatsApp Web in the window, WhatsApp's own hosts in a frame."""
        req_id = params.get("requestId")
        url = str((params.get("request") or {}).get("url") or "")
        main = params.get("frameId") == s.target_id
        why = (policy.check_page(url, insecure_hosts=self._insecure) if main
               else policy.check_frame(url, insecure_hosts=self._insecure))
        try:
            if why is None:
                await self._page(s, "Fetch.continueRequest", {"requestId": req_id})
            else:
                log.info("calls: a %s was refused (%s)", "page" if main else "frame", why)
                await self._page(s, "Fetch.failRequest", {"requestId": req_id, "errorReason": "BlockedByClient"})
        except CDPError:
            log.debug("calls: answering a paused request failed", exc_info=True)

    async def _on_navigated(self, s: _Session, url: str) -> None:
        """Every page the window shows: the backstop for anything the request check didn't see."""
        scheme = url.split(":", 1)[0].lower()
        if scheme in ("about", "data"):
            return
        if scheme == "chrome-error":
            log.info("calls: WhatsApp didn't load (no internet?)")
            return
        why = policy.check_page(url, insecure_hosts=self._insecure)
        if why is None:
            return
        log.info("calls: the window left WhatsApp (%s); going back", why)
        with contextlib.suppress(CDPError):
            await self._page(s, "Page.navigate", {"url": self._home})

    async def _on_window(self, s: _Session, tid: str, url: str, *, new: bool) -> None:
        """Another window: WhatsApp's own (a call's) may stay and is watched for the call's buttons; any other
        is closed at once."""
        why = policy.check_window(url, insecure_hosts=self._insecure)
        if why is not None:
            s.windows.pop(tid, None)
            log.info("calls: another window was closed (%s)", why)
            await self._quietly(s.cdp.send("Target.closeTarget", {"targetId": tid}))
            return
        if new and tid not in s.windows:
            got = await self._quietly(s.cdp.send("Target.attachToTarget", {"targetId": tid, "flatten": True}))
            if isinstance(got, dict) and got.get("sessionId"):
                s.windows[tid] = str(got["sessionId"])
                log.info("calls: WhatsApp opened a window of its own")

    # ================================================================ watching it
    async def _watch(self, s: _Session) -> None:
        """Every ``POLL_S``: is Chromium there, is there memory, is the window in front, and what does the page
        show (linked? a call?); then the cover's words, and the reasons to shut down."""
        n = 0
        while not s.ended:
            await asyncio.sleep(self._poll)
            if s.ended:
                return
            n += 1
            if s.backend is not None and s.backend.is_running() is False:
                self.close_soon("its window was closed")
                return
            if s.cdp is not None and getattr(s.cdp, "closed", None) is not None and s.cdp.closed.is_set():
                self.close_soon("lost the connection to Chromium")
                return
            free = self._memory()
            if free is not None and free < int(self.cfg.min_free_mb_running):
                self.close_soon(f"memory ran low ({free} MB free)")
                return
            now = self._clock()
            if n % WINDOW_EVERY == 0 and await self._off_screen(s, now):
                self.close_soon("another window is in front of it")
                return
            try:
                await self._look(s, now)
            except CDPError:
                log.debug("calls: asking the page failed", exc_info=True)
            if s.ended:
                return
            if self._idle(s, now):
                self.close_soon("nobody used it")
                return
            if now - s.last_awake >= KEEP_AWAKE_EVERY_S:
                self._awake(s, now)

    async def _off_screen(self, s: _Session, now: float) -> bool:
        windows = await asyncio.to_thread(self._windows)
        where = where_android_is(windows, str(self.cfg.window_match))
        if where is None:
            if not self._blind_told:
                self._blind_told = True
                log.info("calls: the desktop can't say which window is in front; WhatsApp is shut down when it is "
                         "idle, the screen goes dark or something else takes it")
            return False
        if where == "front":
            s.seen_front, s.off_since = True, None
            return False
        if not s.seen_front or where == "none":  # not found by its name: Chromium's exit is what says it closed
            return False
        if s.off_since is None:
            s.off_since = now
        return now - s.off_since >= float(self.cfg.off_screen_seconds)

    def _idle(self, s: _Session, now: float) -> bool:
        if s.call != "none" or s.placing:
            s.idle_since = now
            return False
        return now - s.idle_since >= float(self.cfg.idle_seconds)

    async def _state(self, s: _Session) -> dict[str, Any]:
        """What the page shows (``page_scripts.state_script``); a call in one of WhatsApp's own windows counts."""
        st = await self._eval(s, scripts.state_script())
        st = st if isinstance(st, dict) else {}
        st.setdefault("call", "none")
        st.setdefault("points", {})
        s.call_page = s.page
        if st["call"] == "none":
            for sid in list(s.windows.values()):
                try:
                    other = await self._eval_in(s, sid, scripts.state_script())
                except CDPError:
                    continue
                if isinstance(other, dict) and other.get("call") not in (None, "none"):
                    st["call"] = other["call"]
                    st["points"] = {**st["points"], **{k: v for k, v in (other.get("points") or {}).items()
                                                       if k in ("end_call", "accept_call", "decline_call")}}
                    s.call_page = sid
                    break
        return st

    async def _look(self, s: _Session, now: float) -> None:
        st = await self._state(s)
        if s.ended:
            return
        s.last = st
        ask = st.get("ask")
        if ask:
            self._asked(s, str(ask))
            if s.ended:
                return
        self._follow_phase(s, st, now)
        self._follow_call(s, st, now)
        await self._refresh_cover(s, st)

    def _follow_phase(self, s: _Session, st: dict[str, Any], now: float) -> None:
        was = s.state
        if st.get("linked"):
            s.state = "ready"
        elif st.get("qr"):
            s.state = "linking"
        if s.state == was:
            if s.state == "loading" and not s.problem and now - s.started >= float(self.cfg.load_timeout_seconds):
                self._problem(s, "load")
            return
        s.idle_since = now
        self._publish(STATE, {"state": s.state, "call": s.call})
        if s.state == "linking":
            log.info("calls: WhatsApp isn't linked; its QR code is on the screen to scan")
            if not s.told_link:
                s.told_link = True
                self._say(LINK_SAY)
        elif s.state == "ready":
            log.info("calls: WhatsApp is linked and ready (%.0f s after it was opened)", now - s.started)
            if s.problem == PROBLEMS["load"]:
                s.problem = ""
            if s.contact is not None and s.call == "none":
                self._begin_call(s)

    def _follow_call(self, s: _Session, st: dict[str, Any], now: float) -> None:
        call = str(st.get("call") or "none")
        was = s.call
        if call != "none":
            s.no_call_since = None
            s.idle_since = now
        if call != was:
            s.call = call
            self._publish(STATE, {"state": s.state, "call": call})
            if call == "incoming":
                s.had_call = True
                log.info("calls: a call is ringing in WhatsApp")
                if s.answer:
                    self._spawn(s, self._accept(s))
            elif call == "in_call":
                s.had_call = True
                s.problem = ""
                log.info("calls: a call is on")
                if not s.voice_ended:
                    s.voice_ended = True
                    self._spawn(s, self._end_voice_chat())
        if call == "none" and s.had_call and not s.placing:
            if s.no_call_since is None:
                s.no_call_since = now
            elif now - s.no_call_since >= CALL_END_GRACE_S:
                self.close_soon("the call ended")

    async def _end_voice_chat(self) -> None:
        """A call is on: LittleBot's own voice chat ends, so its voice doesn't talk into the call. (The wake word
        still starts a new one: "Hey LittleBot, hang up".)"""
        from littlebot.skills import takeover  # noqa: PLC0415

        try:
            if await takeover.end_voice_chat(self.ctx, "a WhatsApp call started"):
                log.info("calls: the voice chat ended for the call")
        except Exception:  # noqa: BLE001
            log.debug("calls: ending the voice chat failed", exc_info=True)

    # ================================================================ the cover
    def _cover_state(self, s: _Session, st: dict[str, Any] | None = None) -> dict[str, Any]:
        name = s.contact[0] if s.contact is not None else ""
        return scripts.cover_state(hide=self.hide_chats, theme=self._theme(), phase=s.state, call=s.call,
                                   name=name, answer=s.answer, problem=s.problem)

    async def _refresh_cover(self, s: _Session, st: dict[str, Any] | None = None) -> None:
        """The cover's words follow what is going on; a page that lost its cover gets it back."""
        state = self._cover_state(s, st)
        text = json.dumps(state, sort_keys=True)
        missing = st is not None and st.get("cover") is False
        if text == s.cover and not missing:
            return
        there = await self._quietly(self._eval(s, scripts.cover_update_script(state)))
        if there is False or (there is None and missing):
            await self._quietly(self._eval(s, scripts.cover_script(state)))
            if state.get("on"):
                log.info("calls: the cover was put back over WhatsApp's page")
        s.cover = text

    def _asked(self, s: _Session, what: str) -> None:
        """A button on the cover (or a touch on the page)."""
        now = self._clock()
        if what == "touch":
            s.idle_since = now
            self._awake(s, now)
        elif what == "close":
            self.close_soon("closed on the screen")
        elif what in ("hangup", "decline"):
            self._spawn(s, self.hang_up("hung up on the screen"))
        elif what == "answer":
            s.answer = True
            self._spawn(s, self._accept(s))

    # ================================================================ placing and answering a call
    def _begin_call(self, s: _Session) -> None:
        if s.placing or s.contact is None:
            return
        s.placing = True
        self._spawn(s, self._place_call(s))

    async def _place_call(self, s: _Session) -> None:
        assert s.contact is not None
        said, name = s.contact
        try:
            await self._refresh_cover(s)
            step = await self._call_steps(s, name)
        except CDPError as e:
            log.info("calls: placing the call failed (%s)", str(e)[:120])
            step = "failed"
        finally:
            s.placing = False
        if s.ended:
            return
        s.idle_since = self._clock()
        if step is None:
            log.info("calls: the %s call to %s was started", s.kind, said)
            return
        self._problem(s, step)

    async def _call_steps(self, s: _Session, name: str) -> str | None:
        """Search for the chat, open it, press the call button. None: the call started; else the step that
        didn't work (a ``PROBLEMS`` code): everything stops there, WhatsApp stays on the screen."""
        st = await self._state(s)
        if st.get("call") != "none":
            return None  # a call is on already
        box = await self._point(s, "search_box", 0.0)
        if box is None:
            button = (st.get("points") or {}).get("search_button")
            if button:
                await self._click(s, button)
            box = await self._point(s, "search_box", SEARCH_WAIT_S)
        if box is None:
            return "search"
        await self._click(s, box)
        await self._page(s, "Input.insertText", {"text": name})
        row = await self._until(s, lambda: self._eval(s, scripts.find_chat_script(name)),
                                lambda v: isinstance(v, dict) and v.get("found"), CHAT_WAIT_S)
        if row is None:
            return "contact"
        await self._click(s, row)
        # the chat that opened must be the one asked for (never a call to somebody else)
        head = await self._until(s, lambda: self._eval(s, scripts.header_script(name)),
                                 lambda v: isinstance(v, dict) and v.get("match"), HEADER_WAIT_S)
        if head is None:
            return "chat"
        key = "voice_call" if s.kind == "voice" else "video_call"
        button = await self._point(s, key, BUTTON_WAIT_S)
        if button is None:
            menu = await self._point(s, "call_menu", 0.0)
            if menu is not None:
                await self._click(s, menu)
                button = await self._point(s, key, BUTTON_WAIT_S)
        if button is None:
            return "call_button"
        await self._click(s, button)
        started = await self._until(s, lambda: self._state(s), lambda v: v.get("call") != "none", CALL_WAIT_S)
        return None if started is not None else "no_call"

    async def _accept(self, s: _Session) -> bool:
        """Press WhatsApp's Accept button, if it is found."""
        try:
            st = await self._state(s)
            point = (st.get("points") or {}).get("accept_call")
            if st.get("call") != "incoming" or not point:
                return False
            await self._click(s, point, session=s.call_page)
        except CDPError:
            return False
        s.answer = False
        log.info("calls: the ringing call was answered")
        return True

    async def hang_up(self, reason: str = "asked to hang up") -> dict[str, Any]:
        """End the call with WhatsApp's own button when it is found (so the other side sees it end), then shut
        everything down. Fine to call with no call on: WhatsApp just closes."""
        s = self._session
        if s is None or s.ended:
            return {"ok": True, "was_call": False, "closed": False}
        was_call = s.call != "none"
        try:
            st = await self._state(s)
            points = st.get("points") or {}
            point = points.get("end_call") or (points.get("decline_call") if st.get("call") == "incoming" else None)
            if st.get("call") != "none" and point:
                was_call = True
                await self._click(s, point, session=s.call_page)
                await asyncio.sleep(min(0.5, self._poll))  # WhatsApp tells the other side
        except CDPError:
            log.debug("calls: pressing the end button failed", exc_info=True)
        closed = await self.close(reason)
        return {"ok": True, "was_call": was_call, "closed": closed}

    def _problem(self, s: _Session, step: str) -> None:
        """A step didn't work: it stops there. The cover says so (the chats stay hidden) and so does the voice,
        which says what to tap only when the chats aren't hidden."""
        name = s.contact[0] if s.contact is not None else "them"
        words = PROBLEMS.get(step, PROBLEMS["failed"]).format(name=name)
        s.problem = words
        if step != "load":  # (a slow start: the call is still placed if WhatsApp comes up after all)
            s.contact = None
        log.info("calls: stopped at a step that didn't work (%s); WhatsApp stays on the screen", step)
        hint = COVERED_HINT if self.hide_chats else TAP_HINTS.get(step, "").format(name=name)
        self._say(f"{words} {hint}".strip())
        self._spawn(s, self._refresh_cover(s))

    def describe_problem(self) -> str:
        s = self._session
        return s.problem if s is not None and not s.ended else ""

    # ================================================================ pressing and asking the page
    async def _click(self, s: _Session, point: dict[str, Any], *, session: str | None = None) -> None:
        """A real press at ``point`` ({x, y}). The cover lets it through for that instant and stays opaque."""
        sid = session or s.page
        x, y = float(point.get("x") or 0), float(point.get("y") or 0)
        covered = sid == s.page
        if covered:
            await self._quietly(self._eval(s, scripts.pass_script(True)))
        try:
            for kind in ("mousePressed", "mouseReleased"):
                await s.cdp.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                              "clickCount": 1}, session=sid)
        finally:
            if covered:
                await self._quietly(self._eval(s, scripts.pass_script(False)))

    async def _point(self, s: _Session, key: str, wait: float) -> dict[str, Any] | None:
        """Where the thing ``key`` (a ``SELECTORS`` key) is, waiting up to ``wait`` seconds for it; else None."""
        async def ask() -> Any:
            return ((await self._state(s)).get("points") or {}).get(key)

        return await self._until(s, ask, bool, wait)

    async def _until(self, s: _Session, ask: Callable[[], Coroutine[Any, Any, Any]], good: Callable[[Any], Any],
                     wait: float) -> Any:
        """``ask()`` again and again until ``good(value)``, at most ``wait`` seconds; the value, else None."""
        deadline = self._clock() + wait
        while not s.ended:
            value = await ask()
            if good(value):
                return value
            if self._clock() >= deadline:
                return None
            await asyncio.sleep(self._step_poll)
        return None

    async def _eval(self, s: _Session, expression: str) -> Any:
        """Run ``expression`` in LittleBot's own isolated world of the page; its JSON value."""
        for attempt in (0, 1):
            if s.world is None:
                world = await self._page(s, "Page.createIsolatedWorld",
                                         {"frameId": s.target_id, "worldName": scripts.WORLD})
                s.world = int(world["executionContextId"])
            try:
                res = await self._page(s, "Runtime.evaluate", {"expression": expression, "contextId": s.world,
                                                               "returnByValue": True, "timeout": 3000})
            except CDPError:
                if attempt:
                    raise
                s.world = None  # the page changed meanwhile: a fresh world
                continue
            if res.get("exceptionDetails"):
                raise CDPError("the page script failed")
            return (res.get("result") or {}).get("value")
        return None

    async def _eval_in(self, s: _Session, session: str, expression: str) -> Any:
        res = await s.cdp.send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                    "timeout": 3000}, session=session)
        if res.get("exceptionDetails"):
            raise CDPError("the page script failed")
        return (res.get("result") or {}).get("value")

    # ================================================================ stopping
    def close_soon(self, reason: str = "closed") -> bool:
        """Shut WhatsApp's Chromium down, the face back. Any thread. True if it was open."""
        with self._lock:
            s = self._session
            if s is None or s.ended:
                return False
            s.ended = True
            s.reason = str(reason or "closed")[:160]
            self._session = None
        if s.lease is not None:
            s.lease.release()
        s.free_before = self._memory()
        log.info("calls: shutting WhatsApp down (%s)", s.reason)
        self._publish(CLOSED, {"reason": s.reason})
        self._schedule_teardown(s)
        return True

    async def close(self, reason: str = "closed") -> bool:
        """``close_soon``, then wait until Chromium is gone and the camera is back."""
        closed = self.close_soon(reason)
        await self._wait_teardowns()
        return closed

    async def _wait_teardowns(self) -> None:
        pending = [f for f in list(self._teardowns) if not f.done()]
        if pending:
            await asyncio.wait([asyncio.wrap_future(f) if not isinstance(f, asyncio.Future) else f
                                for f in pending], timeout=TEARDOWN_TIMEOUT_S + KILL_WAIT_S + self._settle + 4)

    def _schedule_teardown(self, s: _Session) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self._teardown_now(s)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fut: Any = loop.create_task(self._teardown(s))
        else:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._teardown(s), loop)
            except RuntimeError:
                self._teardown_now(s)
                return
        self._teardowns.add(fut)
        fut.add_done_callback(self._teardowns.discard)

    async def _teardown(self, s: _Session) -> None:
        current = asyncio.current_task()
        for t in list(s.tasks):
            if t is not current:
                t.cancel()
        try:
            if s.cdp is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(s.cdp.close(), 2.0)
            if s.backend is not None:
                try:
                    await asyncio.wait_for(s.backend.close(), TEARDOWN_TIMEOUT_S)  # SIGTERM, then SIGKILL: the group
                except Exception:  # noqa: BLE001
                    log.exception("calls: closing Chromium failed")
                    s.backend.terminate_now()
            left = await asyncio.to_thread(self._kill_leftovers, "after closing")  # nothing of it may stay
        finally:
            self._return_camera(s)  # only now: Chromium has let go of it
        await asyncio.sleep(self._settle)
        from littlebot.core import memory  # noqa: PLC0415

        await asyncio.to_thread(memory.give_back)
        free = self._memory()
        seconds = max(0.0, self._clock() - s.started)
        back = None if free is None or s.free_before is None else free - s.free_before
        self._last = {"reason": s.reason, "seconds": round(seconds, 1), "at": time.time(), "free_mb": free,
                      "free_before_mb": s.free_before, "freed_mb": back, "source": s.source,
                      "had_call": s.had_call, "leftovers": left}
        log.info("calls: WhatsApp is off (%s) after %.0f s%s%s", s.reason, seconds,
                 f"; {s.free_before} MB of memory free before, {free} MB after ({back:+d} MB)"
                 if free is not None and back is not None else "",
                 f"; {left} leftover Chromium process(es) had to be killed" if left else "")

    def _teardown_now(self, s: _Session) -> None:
        """No loop any more (interpreter shutdown): signal Chromium, give the camera back."""
        if s.backend is not None:
            s.backend.terminate_now()
        self._return_camera(s)

    def _leftovers(self) -> list[int]:
        """Processes of this user still running on WhatsApp's profile (``--user-data-dir=<the profile>``)."""
        want = f"--user-data-dir={self._profile_dir}"
        me = os.getpid()
        uid = os.getuid() if hasattr(os, "getuid") else None
        try:
            names = os.listdir(self._proc_root)
        except OSError:
            return []
        found = []
        for name in names:
            if not name.isdigit() or int(name) == me:
                continue
            base = os.path.join(self._proc_root, name)
            try:
                if uid is not None and os.stat(base).st_uid != uid:
                    continue
                with open(os.path.join(base, "cmdline"), "rb") as fh:
                    args = fh.read().split(b"\0")
            except OSError:
                continue
            if want.encode("utf-8", "replace") in args:
                found.append(int(name))
        return found

    def _kill_leftovers(self, when: str) -> int:
        """Anything of WhatsApp's Chromium still running is killed and waited for. How many there were."""
        pids = self._leftovers()
        if not pids:
            return 0
        log.warning("calls: %d Chromium process(es) of WhatsApp were still running %s; killing them", len(pids), when)
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        deadline = time.monotonic() + KILL_WAIT_S
        while time.monotonic() < deadline and self._leftovers():
            time.sleep(0.05)
        still = self._leftovers()
        if still:
            log.error("calls: %d Chromium process(es) of WhatsApp did not exit", len(still))
        return len(pids)

    def _preempted(self, s: _Session) -> Callable[[Lease, str], None]:
        def on_preempt(lease: Lease, by: str) -> None:
            if s.lease is None or s.lease is lease:
                self.close_soon(f"{by} took the screen")
        return on_preempt

    # ================================================================ status
    def status(self) -> dict[str, Any]:
        why = self.unavailable_reason()
        people = self.contacts()
        out: dict[str, Any] = {
            "available": why is None, "enabled": self.switched_on, "detail": why, "hide_chats": self.hide_chats,
            "active": False, "state": None, "call": "none", "linked": None, "calling": None, "problem": "",
            "source": None, "seconds": None, "closes_in": None, "profile": self.linked_before(),
            "contacts": [{"name": k, "whatsapp": v} for k, v in people.items()],
            "memory_free_mb": self._memory(), "min_free_mb": int(self.cfg.min_free_mb),
            "idle_seconds": float(self.cfg.idle_seconds), "last": dict(self._last) if self._last else None,
            "last_error": self.last_error}
        s = self._session
        if s is not None and not s.ended:
            now = self._clock()
            idle = None if s.call != "none" or s.placing else round(
                max(0.0, float(self.cfg.idle_seconds) - (now - s.idle_since)), 1)
            out.update({"active": True, "state": s.state, "call": s.call, "source": s.source,
                        "linked": True if s.state == "ready" else False if s.state == "linking" else None,
                        "calling": s.contact[0] if s.contact is not None else None, "problem": s.problem,
                        "seconds": round(max(0.0, now - s.started), 1), "closes_in": idle,
                        "camera_lent": s.camera})
        return out

    # ================================================================ bus callbacks (any thread)
    def _on_state(self, event: Event) -> None:
        new = event.get("new")
        if new in _CLOSE_STATES:
            self.close_soon({"SLEEPING": "LittleBot went to sleep", "ERROR": "an error"}.get(str(new),
                                                                                             f"state {new}"))

    def _on_screen(self, event: Event) -> None:
        if event.get("on") is False:
            self.close_soon(f"the screen went dark ({event.get('reason') or 'nothing happening'})")

    def _on_setting(self, event: Event) -> None:
        if event.get("key") == ENABLED and not event.get("value"):
            self.close_soon("switched off")

    def _on_privacy(self, event: Event) -> None:
        privacy = self.ctx.privacy
        if not privacy.cloud_enabled:
            self.close_soon("cloud communication switched off")
        elif not privacy.microphone_enabled:
            self.close_soon("the microphone was switched off in Privacy")
        elif not privacy.camera_enabled:
            s = self._session
            if s is not None and not s.ended and s.video_ok:
                self.close_soon("the camera was switched off in Privacy")

    def _on_speaker(self, event: Event) -> None:
        is_child = event.get("is_child")
        if isinstance(is_child, bool):
            self._spoke = (is_child, self._clock())

    # ================================================================ plumbing
    def _say(self, text: str) -> None:
        """Through whichever voice is on (never into a call)."""
        s = self._session
        if s is not None and s.call != "none":
            return
        try:
            self.ctx.bus.publish("assistant.announce", {"text": str(text), "kind": "calls", "urgent": False},
                                 source="calls")
        except Exception:  # noqa: BLE001
            log.debug("calls: announcing failed", exc_info=True)

    def _awake(self, s: _Session, now: float) -> None:
        s.last_awake = now
        self._publish(USED, {})

    async def _page(self, s: _Session, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if s.cdp is None or s.page is None:
            raise CDPError("no page")
        return await s.cdp.send(method, params, session=s.page)

    @staticmethod
    async def _quietly(coro: Any) -> Any:
        try:
            return await coro
        except CDPError:
            return None

    def _spawn(self, s: _Session, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        s.tasks.add(task)
        task.add_done_callback(s.tasks.discard)
        return task

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None or self._loop.is_closed() or not self._loop.is_running():
            self._loop = loop

    def _publish(self, topic: str, data: dict[str, Any]) -> None:
        try:
            self.ctx.bus.publish(topic, data, source="calls")
        except Exception:  # noqa: BLE001
            log.debug("publish %s failed", topic, exc_info=True)

    def _ok(self, s: _Session, *, already: bool = False) -> dict[str, Any]:
        self.last_error = None
        return {"ok": True, "state": s.state, "error": None, "detail": "", "already": already,
                "contact": s.contact[0] if s.contact is not None else None}

    def _fail(self, code: str, *, detail: str = "", log_it: bool = True) -> dict[str, Any]:
        words = detail or ERRORS.get(code, "")
        if code == "low_memory":
            words = f"{words} It needs {int(self.cfg.min_free_mb)} MB; {self._memory()} MB are free."
        if log_it:
            self.last_error = code
            log.info("calls: not opened (%s)", code)
        return {"ok": False, "state": None, "error": code, "detail": words, "already": False, "contact": None}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


__all__ = ["CLOSED", "CONTACTS", "ERRORS", "HIDE_CHATS", "LINK_SAY", "OPENED", "OWNER", "PRIORITY", "PROBLEMS",
           "STATE", "USED", "CallsService"]
