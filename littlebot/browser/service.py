"""BrowserView: Google (SafeSearch on) on LittleBot's screen, when a grown-up explicitly asks for it.

Public API (``ctx.browser``; used by the screen director and the voice's tools, and by the dashboard)::

    await browser.open(query=None, url=None, *, source="voice") -> {"ok", "showing", "error", "detail", "already"}
        Google's results for ``query`` (SafeSearch forced on), or ``url`` (https only), in Chromium's app mode:
        full screen, no address bar, tabs or menus. Already open: the same window goes there instead.
    await browser.control(action, args, *, source="voice") -> {"ok", ...}
        "scroll" {direction: down|up|top|bottom, amount: page|half|little|<pages>}, "open_result" {n},
        "back", "forward", "reload", "zoom" {level: <factor>|in|out|reset}, "read", "close"
    await browser.read() -> {"ok", "title", "host", "text", "truncated"}  the page's main text (``browser.read_chars``)
    browser.close(reason) -> bool             (any thread; ``await browser.aclose(reason)`` waits for Chromium to exit)
    browser.active, browser.describe(), browser.status(), browser.offered(), browser.blocked_reason(source)

Error codes: "unavailable" (no Chromium, no screen, the config switch), "disabled" (the parent's switch),
"offline", "cloud_off" (Privacy: cloud communication), "child" (voice recognition says the child spoke),
"asleep", "blocked" (the query breaks LittleBot's safety rules), "bad_url", "busy" (something more important has
the screen), "failed", "not_open", "no_result", "bad_action". ``ERRORS`` has the words for each.

How the window is made
----------------------
The videos' kiosk launcher (littlebot/media/display.py ``BrowserKioskBackend``: the same Chromium binary,
switches, Wayland hint and process-group shutdown), with a throwaway ``--user-data-dir`` (a new temporary
folder per session, deleted afterwards) and DevTools on ``--remote-debugging-address=127.0.0.1``
``--remote-debugging-port=0``: Chromium picks a free port and writes it into that folder
(``DevToolsActivePort``); LittleBot connects to ``ws://127.0.0.1:<port>`` (``littlebot.browser.cdp``, which refuses
any other host). Sound is muted (``--mute-audio``: a video in the browser never plays sound; videos go through the
YouTube player), autoplay needs a tap, permission prompts are denied.

Through DevTools, before the first page loads: downloads denied, every permission (notifications, camera,
microphone, location ...) denied, new windows closed at once (``Target.targetCreated``), file pickers and
dialogs dismissed, every page load checked (``Fetch.requestPaused`` for documents, and for Google's own
searches): https only, no local addresses, SafeSearch put back on a search engine page that dropped it
(littlebot/browser/policy.py); a page LittleBot won't open is replaced by a short pink page saying so. Every
page gets LittleBot's strip (littlebot/browser/page_scripts.py: the face's mini eyes and a finger-sized ×,
in an isolated world the page's own scripts can't reach) and is zoomed (``browser.zoom``) with a phone layout.

Who and when
------------
Grown-ups only: from the voice or a tool, a turn voice recognition (bus topic ``voice.speaker``) says was the
child's in the last ``CHILD_RECENT_S`` is refused (the screen director checks the words too). Needs the parent's
switch (``browser.enabled``), cloud communication on and not offline mode; the query passes LittleBot's safety
rules (``find_blocked``).

It takes the screen like a video, but the conversation keeps its face and speaker: one lease on
``Resource.SCREEN``, taken at ``Priority.MEDIA`` (so a card or a game on the screen goes, like for a new video)
and then held at ``PRIORITY`` (45), so a game (50) or a video (60) starting later takes it and the browser
closes. A playing video is stopped first (and its kiosk closed, one Chromium at a time); a video paused for a
chat stays paused (its kiosk is closed already) and "keep watching" closes the browser. The robot's state is
not changed: the browser is used during voice chats, whose states (LISTENING / SPEAKING) can't follow
PLAYING_MEDIA, and its page is silent, so the wake word keeps its normal rules ("Hey LittleBot" starts a chat
with the browser still up).

It closes on its ×, "close it" / "close the browser" (director, tools), STOP ALL, sleep, an error state, a
video, game or activity starting, the dashboard's Close, the parent's switch or cloud communication going off,
Chromium exiting, ``browser.idle_seconds`` (30) after the voice chat ended with nobody touching the page
(real touches are reported by the strip), and during the chat ``browser.moved_on_seconds`` (20) after they talked
about something else (``moved_on``, from the screen director) without using it again.

Logs: one INFO line per open, page and close: the source, the host, never the query or what a page says.
Bus: ``browser.opened`` {source, host, search}, ``browser.navigated`` {host}, ``browser.closed`` {reason};
``screen.keep_awake`` {kind: "browser"} at most every 30 s while someone touches the page.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import contextlib
import json
import logging
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from littlebot.browser import page_scripts as scripts
from littlebot.browser.cdp import CDPConnection, CDPError
from littlebot.browser.policy import (
    bing_url,
    check,
    clean_query,
    google_url,
    host_of,
    is_google_captcha,
    query_blocked,
)
from littlebot.core.arbiter import Lease, Priority, Resource
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState

log = logging.getLogger("littlebot.browser")

OWNER = "browser"
PRIORITY = int(Priority.CONVERSATION) + 5  # above a card (40), below a game (50) and a video (60)
OPENED = "browser.opened"  # {source, host, search}
NAVIGATED = "browser.navigated"  # {host}
CLOSED = "browser.closed"  # {reason}
KEEP_AWAKE = "screen.keep_awake"  # {kind}: littlebot/screens' topic; LittleBot doesn't fall asleep meanwhile
GAME_STARTED = "game.started"  # littlebot.games.service.GAME_STARTED
VOICE_SPEAKER = "voice.speaker"  # {is_child, ...}: who spoke (voice recognition, ctx.voices)
VOICE_OWNERS = frozenset({"conversation", "live_voice"})
VOICE_RESOURCES = (Resource.FACE, Resource.AUDIO_OUT, Resource.MICROPHONE)
_CLOSE_STATES = {RobotState.SLEEPING.value, RobotState.ERROR.value, RobotState.PLAYING_MEDIA.value,
                 RobotState.PLAYING_ACTIVITY.value}

DEDUPE_S = 8.0  # the director and a tool asking for the same thing this close together: done once
CHILD_RECENT_S = 15.0  # voice recognition saying the child spoke this recently refuses the voice's requests
WATCH_S = 0.5
KEEP_AWAKE_EVERY_S = 30.0
MOVED_ON_GRACE_S = 10.0  # a turn this soon after the browser was used is about it (the voice's tool may come first)
READ_CACHE_S = 10.0  # read() again this soon: the same text (the director and a tool both asked)
TEARDOWN_TIMEOUT_S = 6.0
ZOOM_STEP = 0.15
MIN_ZOOM, MAX_ZOOM = 0.75, 2.5
MAX_RESULT = 20
SCROLL_SHARES = {"page": 0.85, "screen": 0.85, "half": 0.5, "little": 0.3, "bit": 0.3, "lot": 1.7}
TMP_PREFIX = "littlebot-browser-"
GOOGLE_RESTS_S = 3600.0  # after Google asks for a captcha, searches go to Bing (SafeSearch strict) this long
FETCH_PATTERNS = [
    {"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"},
    {"urlPattern": "*google.*/search*", "resourceType": "XHR", "requestStage": "Request"},
    {"urlPattern": "*google.*/search*", "resourceType": "Fetch", "requestStage": "Request"},
]
PERMISSIONS = ("notifications", "geolocation", "videoCapture", "audioCapture", "midi", "midiSysex",
               "clipboardReadWrite", "sensors", "backgroundSync", "durableStorage", "idleDetection",
               "displayCapture", "windowManagement", "localFonts", "storageAccess", "nfc", "protectedMediaIdentifier")
EXTRA_SWITCHES = (
    "--remote-debugging-address=127.0.0.1",  # DevTools only for this computer...
    "--remote-debugging-port=0",  # ... on a free port Chromium picks (written to DevToolsActivePort)
    "--mute-audio",  # videos play through the YouTube player, never in here
    "--autoplay-policy=document-user-activation-required",  # (the videos' kiosk lets pages autoplay)
    "--deny-permission-prompts",
    "--disable-notifications",
    "--touch-events=enabled",
    # Lighter on a 2 GB Pi (measured with a page and two cross-site frames, desktop Chromium 140 headless: 11
    # processes and 327 MB PSS -> 7 and 296 MB). One renderer for every site: it only shows Google's results
    # and the pages a grown-up opens from them, in incognito, with no passwords; site isolation's extra
    # processes buy it little. Background services it never uses are off.
    "--renderer-process-limit=1",
    "--disable-site-isolation-trials",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-breakpad",
    # (repeats the kiosk's own list: Chromium keeps only the last --disable-features)
    "--disable-features=Translate,MediaRouter,GlobalMediaControls,OptimizationHints,AutofillServerCommunication,"
    "BackForwardCache,SpareRendererForSitePerProcess",
)

ERRORS = {
    "unavailable": "The browser view isn't available on this robot.",
    "disabled": "The browser view is switched off on the dashboard (Privacy page).",
    "offline": "LittleBot is in offline mode, so it can't go on the internet.",
    "cloud_off": "Cloud communication is off (Privacy page), so LittleBot can't go on the internet.",
    "child": "Only a grown-up can open the browser.",
    "asleep": "LittleBot is asleep.",
    "blocked": "LittleBot won't search for that.",
    "bad_url": "LittleBot won't open that page.",
    "busy": "LittleBot's screen is busy with something more important right now.",
    "failed": "The browser didn't start.",
    "not_open": "The browser isn't open.",
    "no_result": "There is no such result on the page.",
    "bad_action": "The browser can't do that.",
}

# The voice's notes (the screen director sends them; littlebot/chatgpt/tools.py uses the same words).
OPEN_NOTE = ("The browser on your screen shows {what}. Don't read it out; you can say what you see if asked, and they "
             "can say \"scroll down\", \"open the first one\", \"go back\", \"what does it say?\" or \"close it\".")
READ_NOTE = "The page on your screen says: {text}\nSummarise it in a few words; don't read it out."
REFUSED_NOTE = "The browser couldn't open: {why} Say so briefly, kindly."
CLOSED_NOTE = "The browser is closed now; your face is back on the screen."
# Added to the live voice's instructions while there is a browser view (littlebot/chatgpt/live.py).
VOICE_INSTRUCTIONS = """
Your screen can also show Google, in a browser, but only when a grown-up explicitly asks: "google it", "look it up \
online", "search that in the browser", "open the browser". Then call search_in_browser with a few search words \
(what "it" is comes from the conversation); while it shows, use the browser tool when they ask to scroll, open a \
result, go back, know what the page says, or close it. LittleBot may have done it already from their words: the \
tool or a note then says so; just say okay. Never read a web page out: say the gist in a few words. Ordinary \
questions are never for the browser: answer them yourself."""

_LIVE_DIRS: set[str] = set()


@atexit.register
def _remove_live_dirs() -> None:
    for d in list(_LIVE_DIRS):
        shutil.rmtree(d, ignore_errors=True)


@dataclass(eq=False)
class _Session:
    id: int
    source: str
    started: float
    zoom: float
    tmpdir: Path | None = None
    backend: Any = None  # media.display.BrowserKioskBackend
    lease: Lease | None = None
    cdp: Any = None  # CDPConnection
    page: str | None = None  # the page's DevTools session id
    target_id: str = ""  # = the main frame's id
    context_id: str | None = None  # the browser context (incognito) the page lives in
    query: str = ""  # what was searched for last (never logged)
    url: str = ""  # the page shown now
    host: str = ""
    title: str = ""
    navigated: bool = False  # the first real page was asked for (the loading page is done)
    ended: bool = False
    reason: str = ""
    quiet_since: float | None = None  # when no voice chat was last seen (the idle timer)
    moved_on: float | None = None  # when they last talked about something else in the chat (the moved-on timer)
    used: float = 0.0  # when the browser was last opened, steered or touched
    last_awake: float = -1e9
    last_open: tuple[str, float] | None = None  # (source, when)
    last_action: tuple[str, str, float] | None = None  # (action key, source, when)
    last_read: tuple[dict[str, Any], float] | None = None
    ctl_world: int | None = None  # execution context of LittleBot's own isolated world for control scripts
    blocked: list[str] = field(default_factory=list)  # addresses replaced by the "won't open" page
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    watch: asyncio.Task[Any] | None = None


class BrowserView(Service):
    name = "browser"

    def __init__(self, ctx: Any, *, launcher: Any = None, browser_path: Any = ...,
                 connect: Callable[[str], Coroutine[Any, Any, Any]] | None = None,
                 clock: Callable[[], float] = time.monotonic, kiosk: bool | None = None,
                 tmp_root: str | Path | None = None, insecure_hosts: tuple[str, ...] = (),
                 extra_args: tuple[str, ...] = ()) -> None:
        """``launcher`` / ``connect``: how Chromium is started and DevTools reached (tests fake both).
        ``browser_path``: the Chromium binary (default: ``media.browser_command`` or the first one found, and
        none in a headless run). ``insecure_hosts``: hosts allowed over plain http (tests' local pages only)."""
        super().__init__()
        self.ctx = ctx
        self.cfg = ctx.config.browser
        self._launcher = launcher
        self._connect = connect or CDPConnection.connect
        self._clock = clock
        self._kiosk = kiosk
        self._tmp_root = str(tmp_root) if tmp_root is not None else None
        self._insecure = tuple(insecure_hosts)
        self._extra = tuple(extra_args)
        self._path = self._find_browser() if browser_path is ... else browser_path
        self._lock = threading.RLock()
        self._open_lock = asyncio.Lock()
        self._session: _Session | None = None
        self._ids = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._teardowns: set[asyncio.Future[Any]] = set()
        self._spoke: tuple[bool, float] | None = None  # (is_child, when) from voice recognition
        self._last: dict[str, Any] | None = None
        self._google_off_until = -1e18  # Google asked for a captcha: Bing until then (GOOGLE_RESTS_S)
        self.last_error: str | None = None
        self._subs: list[Subscription] = []

    # ================================================================ lifecycle
    async def start(self) -> None:  # type: ignore[override]
        self._bind_loop()
        bus = self.ctx.bus
        if not self._subs:
            self._subs = [
                bus.subscribe(Topics.STOP_ALL, lambda _e: self.close("stop all")),
                bus.subscribe(Topics.STATE_CHANGED, self._on_state),
                bus.subscribe(Topics.MEDIA_STARTED, lambda _e: self.close("a video started")),
                bus.subscribe(Topics.ACTIVITY_STARTED, lambda _e: self.close("an activity started")),
                bus.subscribe(GAME_STARTED, lambda _e: self.close("a game started")),
                bus.subscribe(Topics.SETTINGS_CHANGED, self._on_setting),
                bus.subscribe(Topics.PRIVACY_CHANGED, self._on_privacy),
                bus.subscribe(VOICE_SPEAKER, self._on_speaker),
            ]
        await asyncio.to_thread(self._sweep_old_dirs)
        why = self.unavailable_reason()
        if why is None:
            self.set_status(ServiceStatus.RUNNING)
        elif not self.cfg.enabled or self.ctx.config.is_offline:  # switched off: as the videos do
            self.set_status(ServiceStatus.DISABLED, why)
        else:  # no Chromium or no screen: degraded, like the video player without one
            self.set_status(ServiceStatus.DEGRADED, why)

    async def stop(self) -> None:  # type: ignore[override]
        await self.aclose("shutdown")
        for sub in self._subs:
            sub.cancel()
        self._subs.clear()
        self.set_status(ServiceStatus.STOPPED)

    def _find_browser(self) -> str | None:
        if self.ctx.config.display.headless or self.ctx.config.is_offline:
            return None
        from littlebot.media.display import find_browser  # noqa: PLC0415

        return find_browser(self.ctx.config.media.browser_command)

    def _sweep_old_dirs(self) -> None:
        """Profiles a crash left behind (nothing is kept between sessions)."""
        root = Path(self._tmp_root or tempfile.gettempdir())
        for d in root.glob(f"{TMP_PREFIX}*"):
            if str(d) not in _LIVE_DIRS:
                shutil.rmtree(d, ignore_errors=True)

    # ================================================================ gates
    def unavailable_reason(self) -> str | None:
        """Why there is no browser view on this robot at all (None: there is one)."""
        if not self.cfg.enabled:
            return "switched off in LittleBot's config (browser.enabled)"
        if self.ctx.config.is_offline:
            return "offline mode"
        if not self._path:
            return ("no screen (headless run)" if self.ctx.config.display.headless
                    else "Chromium is not installed (sudo apt install chromium-browser)")
        return None

    @property
    def switched_on(self) -> bool:
        return bool(self.ctx.settings.get("browser.enabled", True))

    def offered(self) -> bool:
        """The voice's tools may offer the browser (it exists and the parent's switch is on)."""
        return self.unavailable_reason() is None and self.switched_on

    def blocked_reason(self, source: str = "voice") -> str | None:
        """Why the browser can't be used now for ``source`` (an ``ERRORS`` code), else None."""
        if self.unavailable_reason() is not None:
            return "unavailable"
        if not self.switched_on:
            return "disabled"
        if self.ctx.config.is_offline:
            return "offline"
        if not self.ctx.privacy.cloud_enabled:
            return "cloud_off"
        if self.ctx.state.state in (RobotState.SLEEPING, RobotState.ERROR):
            return "asleep"
        if source in ("voice", "tool") and self._child_spoke():
            return "child"
        return None

    def _child_spoke(self) -> bool:
        spoke = self._spoke
        return spoke is not None and spoke[0] and self._clock() - spoke[1] <= CHILD_RECENT_S

    # ================================================================ opening
    @property
    def active(self) -> bool:
        s = self._session
        return s is not None and not s.ended

    async def open(self, query: str | None = None, url: str | None = None, *, source: str = "voice"
                   ) -> dict[str, Any]:
        """Google's results for ``query`` (or ``url``) on the screen; see the module docstring."""
        self._bind_loop()
        source = str(source or "voice")[:20]
        why = self.blocked_reason(source)
        if why is not None:
            return self._fail(why, log_it=why not in ("child",))
        q = clean_query(query)
        if q:
            hits = query_blocked(q, getattr(self.ctx.config.ai, "relaxed_rules", ()))
            if hits:
                log.info("browser: a search was refused by the safety rules (%s)", hits[0])
                return self._fail("blocked")
            target = self._search_url(q)
        elif url:
            verdict = check(str(url), insecure_hosts=self._insecure)
            if verdict.action == "block":
                return self._fail("bad_url", detail=verdict.why)
            target = verdict.url or str(url)
        else:
            target = google_url(None)
        async with self._open_lock:
            s = self._session
            if s is not None and not s.ended:
                return await self._open_again(s, target, q, source)
            return await self._launch(target, q, source)

    def _search_url(self, q: str) -> str:
        """Google's results for ``q``, or Bing's while Google wants a captcha (from a real call: "Google brought
        up a captcha")."""
        return bing_url(q) if self._clock() < self._google_off_until else google_url(q)

    async def _open_again(self, s: _Session, target: str, q: str, source: str) -> dict[str, Any]:
        now = self._clock()
        if s.last_open is not None and s.last_open[0] != source and now - s.last_open[1] < DEDUPE_S:
            return self._ok(s, already=True)  # the director and the voice's tool, for the same words
        if q and q == s.query and s.navigated:
            return self._ok(s, already=True)
        s.last_open = (source, now)
        self._touch(s)
        try:
            await self._navigate(s, target)
        except CDPError as e:
            log.warning("browser: going to the new page failed (%s)", str(e)[:120])
            return self._fail("failed")
        s.query = q
        log.info("browser: new %s (from %s)", "search" if q else "page", source)
        return self._ok(s)

    async def _launch(self, target: str, q: str, source: str) -> dict[str, Any]:
        from littlebot.media.display import (  # noqa: PLC0415
            BrowserKioskBackend,
            PlayerBackendError,
        )

        await self._make_room()
        self._ids += 1
        now = self._clock()
        s = _Session(id=self._ids, source=source, started=now, zoom=float(self.cfg.zoom), query=q,
                     last_open=(source, now), used=now)
        # Taken like a video (a card or a game on the screen goes), then held lower so a game or video wins later.
        lease = self.ctx.arbiter.acquire(OWNER, [Resource.SCREEN], Priority.MEDIA, on_preempt=self._preempted(s))
        if lease is None:
            return self._fail("busy")
        s.lease = self.ctx.arbiter.acquire(OWNER, [Resource.SCREEN], PRIORITY, on_preempt=self._preempted(s)) or lease
        if not self._voice_on():
            s.quiet_since = now
        with self._lock:
            self._session = s
        try:
            s.tmpdir = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix=TMP_PREFIX, dir=self._tmp_root))
            _LIVE_DIRS.add(str(s.tmpdir))
            width, height = self.ctx.config.display.width, self.ctx.config.display.height
            s.backend = BrowserKioskBackend(
                self._path, user_data_dir=s.tmpdir, width=width, height=height,
                extra_args=[*EXTRA_SWITCHES, *self.cfg.extra_args, *self._extra], launcher=self._launcher,
                kiosk=self._use_kiosk(), label="browser view")
            await s.backend.open(scripts.loading_url())
            port, path = await self._devtools_address(s)
            s.cdp = await self._connect(f"ws://127.0.0.1:{port}{path}")
            s.cdp.on_event(lambda method, params, sid, s=s: self._on_devtools(s, method, params, sid))
            await self._setup(s)
            if s.ended:
                raise CDPError("closed while starting")
            await self._navigate(s, target)
        except (CDPError, PlayerBackendError, OSError, TimeoutError, ValueError, KeyError) as e:
            if not s.ended:
                log.warning("browser: couldn't start (%s: %s)", type(e).__name__, str(e)[:160])
                self.close("failed to start")
                await self._wait_teardowns()
                await self._teardown(s)
                return self._fail("failed")
        if s.ended:  # closed while starting (STOP ALL, a video ...): whatever started is stopped again
            await self._wait_teardowns()
            await self._teardown(s)
            return self._fail("not_open", log_it=False)
        s.watch = self._spawn(s, self._watch(s))
        log.info("browser: opened (from %s; %s)", source, "a search" if q else f"a page on {host_of(target)}")
        self._publish(OPENED, {"source": source, "host": host_of(target), "search": bool(q)})
        return self._ok(s)

    def _use_kiosk(self) -> bool:
        choice = self._kiosk if self._kiosk is not None else self.cfg.kiosk
        if choice is not None:
            return bool(choice)
        from littlebot.hardware.platform import is_raspberry_pi  # noqa: PLC0415

        return bool(is_raspberry_pi())

    async def _make_room(self) -> None:
        """A video and the browser never run at once: a playing video stops (its Chromium exits first).
        A video paused for a chat has no window: it stays paused ("keep watching" then closes the browser).
        An activity (it drives the face, which the browser covers) stops, as it would for a video."""
        media = getattr(self.ctx, "media", None)
        try:
            st = media.status() if media is not None else None
        except Exception:  # noqa: BLE001
            st = None
        if isinstance(st, dict) and not st.get("held") and (
                st.get("state") not in (None, "idle") or (st.get("player") or {}).get("running")):
            log.info("browser: stopping the video first")
            try:
                await media.stop("stopped")
            except Exception:  # noqa: BLE001
                log.exception("browser: stopping the video failed")
        acts = getattr(self.ctx, "activities", None)
        try:
            running = acts is not None and bool(acts.status().get("running"))
        except Exception:  # noqa: BLE001
            running = False
        if running:
            try:
                await acts.stop("the browser opened")
            except Exception:  # noqa: BLE001
                log.exception("browser: stopping the activity failed")

    async def _devtools_address(self, s: _Session) -> tuple[int, str]:
        """The port and path Chromium wrote into DevToolsActivePort (it picked a free port)."""
        assert s.tmpdir is not None
        f = s.tmpdir / "DevToolsActivePort"
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

    async def _setup(self, s: _Session) -> None:
        """Lock the window down before the first page: see the module docstring."""
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
        s.target_id, s.context_id = str(page["targetId"]), page.get("browserContextId")
        s.page = (await cdp.send("Target.attachToTarget", {"targetId": s.target_id, "flatten": True}))["sessionId"]
        await cdp.send("Target.setDiscoverTargets", {"discover": True})  # from now on any other window is closed
        for ctx_id in dict.fromkeys([None, s.context_id]):
            scope = {"browserContextId": ctx_id} if ctx_id else {}
            await self._quietly(cdp.send("Browser.setDownloadBehavior", {"behavior": "deny", **scope}))
            for name in PERMISSIONS:  # names a Chromium doesn't know are refused: that's fine
                await self._quietly(cdp.send("Browser.setPermission",
                                             {"permission": {"name": name}, "setting": "denied", **scope}))
        await self._page(s, "Page.enable")
        await self._page(s, "Runtime.enable")
        await self._page(s, "Runtime.addBinding", {"name": scripts.BINDING, "executionContextName": scripts.WORLD})
        eye = tuple(int(c) for c in getattr(self.ctx.config.face, "eye_color", (90, 210, 255)))[:3]
        await self._page(s, "Page.addScriptToEvaluateOnNewDocument",
                         {"source": scripts.strip_script(eye), "worldName": scripts.WORLD})  # type: ignore[arg-type]
        await self._page(s, "Page.addScriptToEvaluateOnNewDocument", {"source": scripts.MAIN_WORLD_SCRIPT})
        await self._quietly(self._page(s, "Page.setInterceptFileChooserDialog", {"enabled": True}))
        await self._page(s, "Fetch.enable", {"patterns": FETCH_PATTERNS})
        await self._apply_zoom(s)
        if self.cfg.mobile_layout:
            version = await self._quietly(cdp.send("Browser.getVersion")) or {}
            await self._quietly(self._page(s, "Emulation.setUserAgentOverride", _mobile_agent(version)))
            await self._quietly(self._page(s, "Emulation.setTouchEmulationEnabled",
                                           {"enabled": True, "maxTouchPoints": 5}))

    async def _apply_zoom(self, s: _Session) -> None:
        # The window's own size (0 x 0), drawn ``zoom`` times bigger: pages lay out for a narrower screen.
        await self._page(s, "Emulation.setDeviceMetricsOverride",
                         {"width": 0, "height": 0, "deviceScaleFactor": round(s.zoom, 2), "mobile": False})

    async def _navigate(self, s: _Session, url: str) -> None:
        verdict = check(url, insecure_hosts=self._insecure)
        if verdict.action == "block":
            raise CDPError(f"won't open that page ({verdict.why})")
        s.navigated = True
        result = await self._page(s, "Page.navigate", {"url": verdict.url or url})
        if result.get("errorText"):
            log.info("browser: the page didn't load (%s)", str(result["errorText"])[:80])

    # ================================================================ DevTools events (on the loop)
    def _on_devtools(self, s: _Session, method: str, params: dict[str, Any], sid: str | None) -> None:
        if s.ended:
            return
        if method == "Fetch.requestPaused":
            self._spawn(s, self._on_request(s, params))
        elif method == "Runtime.bindingCalled" and params.get("name") == scripts.BINDING:
            self._on_binding(s, params.get("payload"))
        elif method == "Page.frameNavigated" and sid == s.page:
            frame = params.get("frame") or {}
            if not frame.get("parentId"):
                self._spawn(s, self._on_navigated(s, str(frame.get("url") or "") + str(frame.get("urlFragment") or "")))
        elif method == "Page.navigatedWithinDocument" and sid == s.page and params.get("frameId") == s.target_id:
            self._spawn(s, self._on_navigated(s, str(params.get("url") or ""), same_document=True))
        elif method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            if s.target_id and info.get("type") == "page" and info.get("targetId") not in (None, "", s.target_id):
                log.info("browser: a new window was blocked")
                self._spawn(s, self._quietly(s.cdp.send("Target.closeTarget", {"targetId": info["targetId"]})))
        elif method == "Target.targetInfoChanged":
            info = params.get("targetInfo") or {}
            if info.get("targetId") == s.target_id:
                s.title = _title(info)
        elif method == "Page.loadEventFired" and sid == s.page:
            self._spawn(s, self._refresh_title(s))  # the title isn't always announced once the page has loaded
        elif method in ("Target.targetDestroyed", "Target.detachedFromTarget"):
            if params.get("targetId") == s.target_id or (method == "Target.detachedFromTarget"
                                                          and params.get("sessionId") == s.page):
                self.close("the browser window closed")
        elif method == "Page.javascriptDialogOpening" and sid == s.page:
            accept = params.get("type") == "beforeunload"  # leaving a page is fine; alerts and prompts go away
            self._spawn(s, self._quietly(self._page(s, "Page.handleJavaScriptDialog", {"accept": accept})))
        elif method in ("Browser.downloadWillBegin", "Page.downloadWillBegin"):
            log.info("browser: a download was blocked")

    def _on_binding(self, s: _Session, payload: Any) -> None:
        try:
            kind = json.loads(str(payload or "{}")).get("type")
        except (ValueError, AttributeError):
            return
        if kind == "close":
            self.close("closed with the ×")
        elif kind == "touch":
            self._touch(s)

    async def _on_request(self, s: _Session, params: dict[str, Any]) -> None:
        req_id = params.get("requestId")
        url = str((params.get("request") or {}).get("url") or "")
        is_doc = params.get("resourceType") == "Document"
        main = is_doc and params.get("frameId") == s.target_id
        verdict = check(url, insecure_hosts=self._insecure)
        try:
            if verdict.action == "ok":
                await self._page(s, "Fetch.continueRequest", {"requestId": req_id})
            elif verdict.action == "rewrite":  # a search without SafeSearch: the same search with it
                if is_doc:
                    await self._page(s, "Fetch.fulfillRequest", {
                        "requestId": req_id, "responseCode": 302,
                        "responseHeaders": [{"name": "Location", "value": verdict.url}], "body": ""})
                else:
                    await self._page(s, "Fetch.continueRequest", {"requestId": req_id, "url": verdict.url})
            elif main:
                log.info("browser: a page was refused (%s; %s)", verdict.why, host_of(url) or "no host")
                s.blocked = [*s.blocked[-19:], url]
                body = base64.b64encode(scripts.blocked_page(_why_words(verdict.why)).encode("utf-8")).decode("ascii")
                await self._page(s, "Fetch.fulfillRequest", {
                    "requestId": req_id, "responseCode": 200, "body": body,
                    "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}]})
            else:
                await self._page(s, "Fetch.failRequest", {"requestId": req_id, "errorReason": "BlockedByClient"})
        except CDPError:
            log.debug("browser: answering a paused request failed", exc_info=True)

    async def _on_navigated(self, s: _Session, url: str, *, same_document: bool = False) -> None:
        """Every page shown: the backstop for anything the request check didn't see (file:, chrome:, data: ...)."""
        if not same_document:
            s.ctl_world = None  # a new document: a new isolated world, and nothing read yet
            s.last_read = None
        scheme = url.split(":", 1)[0].lower()
        if url == scripts.loading_url() and not s.navigated:
            return
        if scheme in ("about", "chrome-error") or url in s.blocked:
            if scheme == "chrome-error":
                log.info("browser: the page didn't load (no internet?)")
            return
        verdict = check(url, insecure_hosts=self._insecure)
        try:
            if verdict.action == "rewrite":
                await self._page(s, "Page.navigate", {"url": verdict.url})
                return
            if verdict.action == "block":
                log.info("browser: a page was refused (%s)", verdict.why)
                await self._page(s, "Page.setDocumentContent",
                                 {"frameId": s.target_id, "html": scripts.blocked_page(_why_words(verdict.why))})
                return
        except CDPError:
            log.debug("browser: the backstop failed", exc_info=True)
            return
        if is_google_captcha(url) and s.query:
            self._google_off_until = self._clock() + GOOGLE_RESTS_S
            log.info("browser: Google asked for a captcha; showing Bing's results instead (SafeSearch strict)")
            try:
                await self._page(s, "Page.navigate", {"url": bing_url(s.query)})
            except CDPError:
                log.debug("browser: going to Bing failed", exc_info=True)
            return
        if same_document and host_of(url) == s.host:
            s.url = url
            return
        s.url, s.host = url, host_of(url)
        log.info("browser: page opened (%s)", s.host or "no host")
        self._publish(NAVIGATED, {"host": s.host})

    async def _refresh_title(self, s: _Session) -> None:
        info = await self._quietly(s.cdp.send("Target.getTargetInfo", {"targetId": s.target_id}))
        if isinstance(info, dict) and isinstance(info.get("targetInfo"), dict):
            s.title = _title(info["targetInfo"])

    # ================================================================ control and read
    async def control(self, action: str, args: dict[str, Any] | None = None, *, source: str = "voice"
                      ) -> dict[str, Any]:
        """Steer the page (see the module docstring); ``{"ok": False, "error": ...}`` when it can't."""
        self._bind_loop()
        action = str(action or "").strip().lower().replace(" ", "_")
        args = dict(args or {})
        s = self._session
        if s is None or s.ended or s.page is None:
            return self._fail("not_open", log_it=False)
        if action == "close":
            self.close("asked in the voice chat" if source in ("voice", "tool") else str(source))
            return {"ok": True, "closed": True}
        why = self.blocked_reason(source)
        if why is not None:
            return self._fail(why)
        key = f"{action}:{json.dumps(args, sort_keys=True, default=str)}"
        now = self._clock()
        if action != "read" and s.last_action is not None and s.last_action[0] == key \
                and s.last_action[1] != source and now - s.last_action[2] < DEDUPE_S:
            return self._ok(s, already=True)
        s.last_action = (key, source, now)
        self._touch(s)
        handler = {"scroll": self._scroll, "open_result": self._open_result, "back": self._back,
                   "forward": self._forward, "reload": self._reload, "zoom": self._zoom}.get(action)
        if action == "read":
            return await self.read()
        if handler is None:
            return self._fail("bad_action", log_it=False)
        try:
            out = await handler(s, args)
        except CDPError as e:
            log.info("browser: %s failed (%s)", action, str(e)[:120])
            return self._fail("failed", log_it=False)
        log.debug("browser: %s (from %s)", action, source)
        return out

    async def read(self) -> dict[str, Any]:
        """The page's main text for the voice to summarise, at most ``browser.read_chars`` characters."""
        s = self._session
        if s is None or s.ended or s.page is None:
            return self._fail("not_open", log_it=False)
        if s.last_read is not None and self._clock() - s.last_read[1] < READ_CACHE_S:
            return dict(s.last_read[0])
        try:
            value = await self._eval(s, scripts.READ_SCRIPT)
        except CDPError as e:
            log.info("browser: reading the page failed (%s)", str(e)[:120])
            return self._fail("failed", log_it=False)
        value = value if isinstance(value, dict) else {}
        text, cut = trim_text(str(value.get("text") or ""), int(self.cfg.read_chars))
        out = {"ok": True, "title": " ".join(str(value.get("title") or "").split())[:120],
               "host": str(value.get("host") or s.host)[:120], "text": text, "truncated": cut}
        s.last_read = (out, self._clock())
        return dict(out)

    async def _scroll(self, s: _Session, args: dict[str, Any]) -> dict[str, Any]:
        direction = str(args.get("direction") or "down").strip().lower()
        edge = direction if direction in ("top", "bottom") else ""
        amount = args.get("amount", "page")
        try:
            pages = max(0.1, min(5.0, float(amount)))
            share = 0.85 * pages
        except (TypeError, ValueError):
            share = SCROLL_SHARES.get(str(amount or "page").strip().lower().replace("a ", ""), 0.85)
        if direction in ("up", "back", "previous"):
            share = -share
        elif not edge and direction not in ("down", "more", "next", "forward"):
            return self._fail("bad_action", log_it=False)
        pos = await self._eval(s, f"({scripts.SCROLL_SCRIPT})({json.dumps(share)}, {json.dumps(edge)})")
        pos = pos if isinstance(pos, dict) else {}
        top, end = int(pos.get("top") or 0), int(pos.get("max") or 0)
        return {**self._ok(s), "at_top": top <= 0, "at_bottom": top >= end}

    async def _open_result(self, s: _Session, args: dict[str, Any]) -> dict[str, Any]:
        n = _ordinal(args.get("n", 1))
        if n is None:
            return self._fail("no_result", log_it=False)
        results = await self._eval(s, scripts.RESULTS_SCRIPT)
        results = results if isinstance(results, list) else []
        if n == -1:
            n = len(results)
        if not 1 <= n <= len(results):
            return {**self._fail("no_result", log_it=False), "results": len(results)}
        pick = results[n - 1] if isinstance(results[n - 1], dict) else {}
        href = str(pick.get("href") or "")
        verdict = check(href, insecure_hosts=self._insecure)
        if verdict.action == "block":
            return self._fail("bad_url", detail=verdict.why, log_it=False)
        await self._navigate(s, verdict.url or href)
        return {**self._ok(s), "opened": " ".join(str(pick.get("title") or "").split())[:120], "n": n}

    async def _history(self, s: _Session, step: int) -> dict[str, Any]:
        hist = await self._page(s, "Page.getNavigationHistory")
        entries = hist.get("entries") or []
        i = int(hist.get("currentIndex") or 0) + step
        while 0 <= i < len(entries) and not str(entries[i].get("url") or "").startswith(("https:", "http:")):
            i += step  # LittleBot's own loading page is not a page to go back to
        if not 0 <= i < len(entries):
            return {**self._ok(s), "moved": False}
        await self._page(s, "Page.navigateToHistoryEntry", {"entryId": entries[i]["id"]})
        return {**self._ok(s), "moved": True}

    async def _back(self, s: _Session, args: dict[str, Any]) -> dict[str, Any]:
        return await self._history(s, -1)

    async def _forward(self, s: _Session, args: dict[str, Any]) -> dict[str, Any]:
        return await self._history(s, 1)

    async def _reload(self, s: _Session, args: dict[str, Any]) -> dict[str, Any]:
        await self._page(s, "Page.reload", {"ignoreCache": False})
        return self._ok(s)

    async def _zoom(self, s: _Session, args: dict[str, Any]) -> dict[str, Any]:
        level = args.get("level", "in")
        word = str(level).strip().lower()
        if word in ("in", "bigger", "more", "+"):
            z = s.zoom + ZOOM_STEP
        elif word in ("out", "smaller", "less", "-"):
            z = s.zoom - ZOOM_STEP
        elif word in ("reset", "normal", "default"):
            z = float(self.cfg.zoom)
        else:
            try:
                z = float(str(level).rstrip("%"))
            except ValueError:
                return self._fail("bad_action", log_it=False)
            if z > 10:  # 150 means 150 %
                z /= 100
        s.zoom = round(max(MIN_ZOOM, min(MAX_ZOOM, z)), 2)
        await self._apply_zoom(s)
        return {**self._ok(s), "zoom": s.zoom}

    async def _eval(self, s: _Session, expression: str) -> Any:
        """Run ``expression`` in LittleBot's own isolated world of the page (the page's scripts can't change what
        it sees); its JSON value."""
        for attempt in (0, 1):
            if s.ctl_world is None:
                world = await self._page(s, "Page.createIsolatedWorld",
                                         {"frameId": s.target_id, "worldName": "littlebot-control"})
                s.ctl_world = int(world["executionContextId"])
            try:
                res = await self._page(s, "Runtime.evaluate", {"expression": expression, "contextId": s.ctl_world,
                                                               "returnByValue": True, "timeout": 3000})
            except CDPError:
                if attempt:
                    raise
                s.ctl_world = None  # the page changed meanwhile: a fresh world
                continue
            if res.get("exceptionDetails"):
                raise CDPError("the page script failed")
            return (res.get("result") or {}).get("value")
        return None

    # ================================================================ closing
    def close(self, reason: str = "closed") -> bool:
        """Take the browser off the screen (Chromium exits, its profile is deleted). Any thread."""
        with self._lock:
            s = self._session
            if s is None or s.ended:
                return False
            s.ended = True
            s.reason = str(reason or "closed")[:80]
            self._session = None
        if s.lease is not None:
            s.lease.release()
        seconds = max(0.0, self._clock() - s.started)
        self._last = {"reason": s.reason, "seconds": round(seconds, 1), "at": time.time()}
        log.info("browser: closed (%s) after %.0f s", s.reason, seconds)
        self._publish(CLOSED, {"reason": s.reason})
        self._schedule_teardown(s)
        return True

    async def aclose(self, reason: str = "closed") -> bool:
        """``close``, then wait until Chromium has exited (a video's kiosk may open after this)."""
        closed = self.close(reason)
        await self._wait_teardowns()
        return closed

    async def _wait_teardowns(self) -> None:
        pending = [f for f in list(self._teardowns) if not f.done()]
        if pending:
            await asyncio.wait([asyncio.wrap_future(f) if not isinstance(f, asyncio.Future) else f
                                for f in pending], timeout=TEARDOWN_TIMEOUT_S + 2)

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
        if s.cdp is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(s.cdp.close(), 2.0)
        if s.backend is not None:
            try:
                await asyncio.wait_for(s.backend.close(), TEARDOWN_TIMEOUT_S)
            except Exception:  # noqa: BLE001
                log.exception("browser: closing Chromium failed")
                s.backend.terminate_now()
        if s.tmpdir is not None:
            await asyncio.to_thread(shutil.rmtree, s.tmpdir, True)
            _LIVE_DIRS.discard(str(s.tmpdir))
        from littlebot.core import memory  # noqa: PLC0415

        await asyncio.to_thread(memory.give_back)  # what LittleBot itself held for the page (screenshots, text)

    def _teardown_now(self, s: _Session) -> None:
        """No loop any more (interpreter shutdown): signal Chromium and delete the profile."""
        if s.backend is not None:
            s.backend.terminate_now()
        if s.tmpdir is not None:
            shutil.rmtree(s.tmpdir, ignore_errors=True)
            _LIVE_DIRS.discard(str(s.tmpdir))

    def _preempted(self, s: _Session) -> Callable[[Lease, str], None]:
        def on_preempt(lease: Lease, by: str) -> None:
            if s.lease is None or s.lease is lease:
                self.close(f"{by} took the screen")
        return on_preempt

    # ================================================================ the idle timer
    async def _watch(self, s: _Session) -> None:
        idle = float(self.cfg.idle_seconds)
        while not s.ended:
            await asyncio.sleep(WATCH_S)
            if s.ended:
                return
            if s.backend is not None and s.backend.is_running() is False:
                self.close("the browser window closed")
                return
            if s.cdp is not None and getattr(s.cdp, "closed", None) is not None and s.cdp.closed.is_set():
                self.close("lost the connection to the browser")
                return
            now = self._clock()
            if self._voice_on():
                s.quiet_since = None
            elif s.quiet_since is None:
                s.quiet_since = now
            elif now - s.quiet_since >= idle:
                self.close("nobody used it")
                return
            if s.moved_on is not None and now - s.moved_on >= float(self.cfg.moved_on_seconds):
                self.close("they moved on to something else")
                return

    def moved_on(self) -> None:
        """A turn in the chat about something else (the screen director): it closes ``browser.moved_on_seconds``
        later unless it is used again. A turn just after it was used is about it (the voice's tool may come first)."""
        s = self._session
        if s is None or s.ended or s.moved_on is not None:
            return
        now = self._clock()
        if now - s.used >= MOVED_ON_GRACE_S:
            s.moved_on = now
            log.debug("browser: they talked about something else; closing in %.0f s unless it is used",
                      float(self.cfg.moved_on_seconds))

    def _touch(self, s: _Session) -> None:
        """Someone used the page (a real touch, or a request in the chat): the idle and moved-on waits start again."""
        now = self._clock()
        s.used, s.moved_on = now, None
        if s.quiet_since is not None:
            s.quiet_since = now
        if now - s.last_awake >= KEEP_AWAKE_EVERY_S:
            s.last_awake = now
            self._publish(KEEP_AWAKE, {"kind": "browser"})

    def _voice_on(self) -> bool:
        arbiter = self.ctx.arbiter
        for r in VOICE_RESOURCES:
            h = arbiter.holder(r)
            if h is not None and h.owner in VOICE_OWNERS:
                return True
        return False

    # ================================================================ status
    def describe(self) -> str | None:
        """What the browser shows, in a few words (for the voice and the dashboard), or None."""
        s = self._session
        if s is None or s.ended:
            return None
        shown = _search_words(s.url) if s.url else s.query
        if shown is not None and (not s.url or _is_google(s.host) or _is_bing(s.host)):
            engine = "Bing" if _is_bing(s.host) else "Google"
            return f"{engine} results for “{shown}”" if shown else engine
        if s.title and s.host:
            return f"{s.title} ({s.host})"
        return s.host or "a web page"

    def status(self) -> dict[str, Any]:
        why = self.unavailable_reason()
        out: dict[str, Any] = {"available": why is None, "enabled": self.switched_on, "detail": why,
                               "active": False, "showing": None, "last": dict(self._last) if self._last else None,
                               "last_error": self.last_error, "idle_seconds": float(self.cfg.idle_seconds)}
        s = self._session
        if s is None or s.ended:
            return out
        now = self._clock()
        closes_in = None if s.quiet_since is None else round(max(0.0, self.cfg.idle_seconds - (now - s.quiet_since)), 1)
        if s.moved_on is not None:
            left = round(max(0.0, float(self.cfg.moved_on_seconds) - (now - s.moved_on)), 1)
            closes_in = left if closes_in is None else min(closes_in, left)
        out.update({"active": True, "showing": self.describe(), "host": s.host, "source": s.source,
                    "seconds": round(max(0.0, now - s.started), 1), "closes_in": closes_in, "zoom": s.zoom,
                    "voice_on": self._voice_on()})
        return out

    # ================================================================ bus callbacks (any thread)
    def _on_state(self, event: Event) -> None:
        new = event.get("new")
        if new in _CLOSE_STATES:
            self.close({"SLEEPING": "LittleBot went to sleep", "ERROR": "an error"}.get(str(new), f"state {new}"))

    def _on_setting(self, event: Event) -> None:
        if event.get("key") == "browser.enabled" and not event.get("value"):
            self.close("switched off")

    def _on_privacy(self, event: Event) -> None:
        if event.get("cloud_enabled") is False or not self.ctx.privacy.cloud_enabled:
            self.close("cloud communication switched off")

    def _on_speaker(self, event: Event) -> None:
        is_child = event.get("is_child")
        if isinstance(is_child, bool):
            self._spoke = (is_child, self._clock())

    # ================================================================ plumbing
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
            self.ctx.bus.publish(topic, data, source="browser")
        except Exception:  # noqa: BLE001
            log.debug("publish %s failed", topic, exc_info=True)

    def _ok(self, s: _Session, *, already: bool = False) -> dict[str, Any]:
        self.last_error = None
        return {"ok": True, "showing": self.describe() or "Google", "error": None, "already": already}

    def _fail(self, code: str, *, detail: str = "", log_it: bool = True) -> dict[str, Any]:
        if log_it:
            self.last_error = code
            if code not in ("blocked",):
                log.info("browser: not opened (%s)", code)
        return {"ok": False, "error": code, "detail": detail or ERRORS.get(code, ""), "showing": self.describe()}


# ---------------------------------------------------------------- helpers
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")  # incl. zero-width and bidi marks
_ORDINALS = {"first": 1, "1st": 1, "one": 1, "top": 1, "second": 2, "2nd": 2, "two": 2, "third": 3, "3rd": 3,
             "three": 3, "fourth": 4, "4th": 4, "four": 4, "fifth": 5, "5th": 5, "five": 5, "sixth": 6, "six": 6,
             "seventh": 7, "seven": 7, "eighth": 8, "eight": 8, "ninth": 9, "nine": 9, "tenth": 10, "ten": 10,
             "last": -1}


def _ordinal(value: Any) -> int | None:
    """1-based result number from 2, "2", "second" or "last" (-1); None when it isn't one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        return n if 1 <= n <= MAX_RESULT else None
    word = str(value or "").strip().lower()
    if word.isdigit():
        return _ordinal(int(word))
    return _ORDINALS.get(word)


def trim_text(text: str, limit: int) -> tuple[str, bool]:
    """Whitespace collapsed, control characters gone, at most ``limit`` characters (cut at a word): (text, cut)."""
    t = " ".join(_CONTROL.sub(" ", text or "").split())
    if len(t) <= limit:
        return t, False
    cut = t[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.8:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "…", True


def _title(info: dict[str, Any]) -> str:
    """A page's title from its target info ("" while Chromium shows the address in its place)."""
    title = " ".join(str(info.get("title") or "").split())[:120]
    url = str(info.get("url") or "")
    return "" if title and url and (title in url or url.split("://", 1)[-1].startswith(title)) else title


def _is_google(host: str) -> bool:
    return bool(re.match(r"^(?:www\.)?google\.[a-z.]+$", host or ""))


def _is_bing(host: str) -> bool:
    return bool(re.match(r"^(?:www\.)?bing\.com$", host or ""))


def _search_words(url: str) -> str | None:
    """The words of a Google (or Bing) search address ("" for its start page), None for any other page."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not _is_google(parts.hostname or "") and not _is_bing(parts.hostname or ""):
        return None
    if parts.path == "/search":
        return " ".join((parse_qs(parts.query).get("q") or [""])[0].split())[:120]
    return ""


def _why_words(why: str) -> str:
    if why.startswith("not https"):
        return "It isn't a secure (https) page."
    if why == "a local address":
        return "It is an address on your own network."
    return "It isn't a page LittleBot opens."


def _mobile_agent(version: dict[str, Any]) -> dict[str, Any]:
    """A phone's user agent for this Chromium version, so sites send the layout made for small screens."""
    product = str(version.get("product") or "Chrome/120.0.0.0")
    full = product.split("/", 1)[1] if "/" in product else "120.0.0.0"
    major = full.split(".", 1)[0] or "120"
    ua = (f"Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 "
          "Mobile Safari/537.36")
    brands = [{"brand": "Chromium", "version": major}, {"brand": "Not.A/Brand", "version": "99"}]
    return {"userAgent": ua, "platform": "Linux armv8l", "userAgentMetadata": {
        "brands": brands, "fullVersionList": [{"brand": "Chromium", "version": full}], "fullVersion": full,
        "platform": "Android", "platformVersion": "10.0.0", "architecture": "", "model": "K", "mobile": True}}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


__all__ = [
    "CLOSED",
    "CLOSED_NOTE",
    "ERRORS",
    "NAVIGATED",
    "OPENED",
    "OPEN_NOTE",
    "OWNER",
    "PRIORITY",
    "READ_NOTE",
    "REFUSED_NOTE",
    "VOICE_INSTRUCTIONS",
    "BrowserView",
    "trim_text",
]
