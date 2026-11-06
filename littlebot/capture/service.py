"""CaptureService (``ctx.capture``): photos and videos by voice, the gallery, and sharing them to a phone.

    await capture.take_photo(source="voice", who=None, countdown=None, show=True) -> {ok, item, error, detail}
    await capture.start_video(source="voice", who=None)  -> {ok, id, sound, max_minutes, error, detail}
    await capture.stop_video(reason="asked")             -> {ok, item, seconds, sound, error, detail}
    capture.recording -> dict | None                        (what is being recorded now)
    capture.gallery   -> Gallery                            (littlebot/capture/gallery.py: the files)
    await capture.show_gallery(item_id=None, kind=None)  -> {ok, error}   (the gallery card on the robot's screen)
    await capture.play_video(item_id)                    -> {ok, error}   (full screen with ffplay; the card after)
    await capture.close_player(reason)
    capture.share(item_id) -> {ok, token, url, urls, expires_at, reachable, error}   (a link for a phone)
    await capture.send_to_phone(item_id, to=None)        -> {ok, url, error, detail, where}
    await capture.show_share_code(item_id)               -> {ok, url, error}  (the link as a QR code on the screen)
    capture.share_item(token) -> Item | None;  capture.revoke_shares(item_id=None) -> int
    capture.delete(item_id) -> bool;  capture.check(kind) -> error code | None;  capture.status()

Error codes: ``ERRORS`` has the words (unavailable, disabled, camera_off, no_camera, disk_full, recording,
not_recording, no_encoder, no_player, not_found, timeout, no_frames, encoder_stopped, failed, lan_off, busy).

What is kept, and where
-----------------------
Only what someone asked for: a photo when asked ("take a picture", the dashboard's button), a video between
"record this" and "stop recording" (or ``capture.max_video_minutes``). The files stay on the robot
(``data/gallery``, littlebot/capture/gallery.py); nothing is uploaded. The camera's privacy switch stops a
recording at once (the frames simply stop), and the microphone's switch keeps the sound out of new videos.

A recording runs on its own: the voice chat that started it ends (LittleBot stops listening while people are
filmed); "Hey LittleBot, stop recording" ends it. A red REC sign shows on the face meanwhile
(littlebot/face/display.py, ``capture.recording``), and LittleBot doesn't doze off. STOP ALL, a YouTube video
starting (vision pauses for it), the camera switched off and shutdown all stop and save it.

Sharing to a phone
------------------
A phone can't be sent a file directly (AirDrop needs Apple's own Wi-Fi radio, and an iPhone is not an AirPlay
receiver), so LittleBot makes a link to the one item on itself, ``http://<the robot's address>:8765/share/<token>``
(the dashboard's server, no login: the 22-character token is the password, ``capture.share_hours`` long). The phone
opens it over the home Wi-Fi and saves it from the browser. The link reaches the phone two ways:

* ``send_to_phone``: a phone notification (littlebot/skills/push.py, ntfy) to one person's phone (``to``), or with
  ``to`` None to every phone. By voice, the camera skill works out whose (littlebot/skills/camera.py
  ``Camera.whose``: never every phone unless asked). Only the link goes through ntfy; with ``capture.push_attach``
  the ntfy app also fetches a preview straight from the robot (the photo itself, or a video's still).
* ``show_share_code``: the link as a QR code on LittleBot's screen; any phone's camera opens it.

Both need LAN access (``dashboard.lan_access``): otherwise only the robot itself can open the link ("lan_off").

Bus: ``capture.saved`` {id, kind, source}, ``capture.recording`` {active, id, started_at, sound, reason},
``capture.deleted`` {id}; ``screen.keep_awake`` {kind: "recording"} every 30 s while recording.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from littlebot.capture.gallery import Gallery, Item, valid_id
from littlebot.capture.recorder import VideoRecorder
from littlebot.core.arbiter import Lease, Priority, Resource
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState

log = logging.getLogger("littlebot.capture")

OWNER = "capture"
VIEWFINDER = "capture_viewfinder"  # vision's warm photo while "say cheese" counts down
PLAYER_OWNER = "capture_player"
PLAYER_PRIORITY = int(Priority.CONVERSATION) + 5  # as mirroring and the baby camera: a game or a video wins
SAVED = "capture.saved"  # {id, kind, source}
RECORDING = "capture.recording"  # {active, id, started_at, sound, reason}
DELETED = "capture.deleted"  # {id}
KEEP_AWAKE = "screen.keep_awake"  # {kind}
GAME_STARTED = "game.started"  # littlebot.games.service.GAME_STARTED
KEEP_AWAKE_EVERY_S = 30.0
FIRST_FRAME_CHECK_S = 7.0  # a recording with no frame by then failed (the camera)
SHARE_FILE = "shares.json"
MAX_SHARES = 200
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SHARE_PATH = "/share/"
THUMB_WIDTH = 640
TERMINATE_S = 2.0
LAN_HELP = ("LAN access is off, so a phone can't open LittleBot's links. Turn it on: add "
            "\"dashboard: lan_access: true\" to config/local.yaml (or start LittleBot with --lan) and restart.")

ERRORS = {
    "unavailable": "Photos and videos aren't available on this robot.",
    "disabled": "Photos and videos are switched off on the dashboard (Photos page).",
    "camera_off": "The camera is switched off in Privacy.",
    "no_camera": "LittleBot's camera isn't working right now (is it plugged in?).",
    "disk_full": "LittleBot's storage is nearly full: delete some photos or videos first.",
    "recording": "LittleBot is already recording a video.",
    "not_recording": "LittleBot isn't recording.",
    "no_encoder": "ffmpeg isn't installed on LittleBot (sudo apt install ffmpeg).",
    "no_player": "ffplay isn't installed on LittleBot (it comes with ffmpeg: sudo apt install ffmpeg).",
    "not_found": "There's no such photo or video.",
    "timeout": "The camera didn't send a picture in time.",
    "no_frames": "The camera didn't send any pictures, so nothing was recorded.",
    "encoder_stopped": "The video recorder stopped by itself.",
    "failed": "That didn't work.",
    "lan_off": LAN_HELP,
    "busy": "LittleBot's screen is busy with something more important right now.",
    "no_screen": "LittleBot's screen can't show it right now.",
}


@dataclass(eq=False)
class _Rec:
    id: str
    recorder: VideoRecorder
    started: float  # monotonic
    started_wall: float
    source: str
    who: str | None
    sound: bool
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    stopping: bool = False


@dataclass(eq=False)
class _Player:
    item_id: str
    handle: Any = None
    lease: Lease | None = None
    ended: bool = False
    back: bool = True  # the gallery comes back on this item afterwards
    task: Any = None
    awake: Any = None  # screen.keep_awake while it plays


def _when(ts: float, now: float | None = None) -> str:
    """"today at 2:31 pm", "yesterday at 9:05 am", "Tuesday 4 November at 2:31 pm" (for the voice and cards)."""
    t = time.localtime(ts)
    n = time.localtime(time.time() if now is None else now)
    clock = time.strftime("%I:%M %p", t).lstrip("0").lower()
    day = (time.mktime((n.tm_year, n.tm_mon, n.tm_mday, 0, 0, 0, 0, 0, -1))
           - time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))) // 86400
    if day == 0:
        return f"today at {clock}"
    if day == 1:
        return f"yesterday at {clock}"
    return f"{time.strftime('%A', t)} {t.tm_mday} {time.strftime('%B', t)} at {clock}"


class CaptureService(Service):
    name = "capture"

    def __init__(self, ctx: Any, *, popen: Callable[..., Any] = subprocess.Popen, launcher: Any = None,
                 which: Callable[[str], str | None] = shutil.which, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, gallery_dir: str | Path | None = None,
                 screen: bool | None = None) -> None:
        super().__init__()
        self.ctx = ctx
        self.cfg = ctx.config.capture
        self._popen = popen
        self._launcher = launcher
        self._which = which
        self._clock = clock
        self._wall = wall
        self._screen = (not ctx.config.display.headless) if screen is None else bool(screen)
        root = Path(gallery_dir) if gallery_dir is not None else ctx.config.data_path / str(self.cfg.folder)
        self.gallery = Gallery(root, clock=wall)
        self._lock = threading.RLock()
        self._rec_lock = asyncio.Lock()
        self._photo_lock = asyncio.Lock()
        self._rec: _Rec | None = None
        self._player: _Player | None = None
        self._shares: dict[str, dict[str, Any]] = {}
        self._shares_loaded = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: list[Subscription] = []
        self._tasks: set[Any] = set()
        self.last: dict[str, Any] | None = None
        self.last_error: str | None = None

    # ================================================================ lifecycle
    async def start(self) -> None:  # type: ignore[override]
        self._loop = asyncio.get_running_loop()
        if not self._subs:
            bus = self.ctx.bus
            self._subs = [
                bus.subscribe(Topics.PRIVACY_CHANGED, self._on_privacy),
                bus.subscribe(Topics.STOP_ALL, lambda _e: self._soon(self._stop_all())),
                bus.subscribe(Topics.MEDIA_STARTED, lambda _e: self._soon(self._media_started())),
                bus.subscribe(Topics.ACTIVITY_STARTED, lambda _e: self.close_player_soon("an activity started")),
                bus.subscribe(GAME_STARTED, lambda _e: self.close_player_soon("a game started")),
                bus.subscribe(Topics.STATE_CHANGED, self._on_state),
                bus.subscribe(Topics.SETTINGS_CHANGED, self._on_setting),
            ]
        why = self.unavailable_reason()
        if why is not None:
            self.set_status(ServiceStatus.DISABLED, why)
            return
        try:
            await asyncio.to_thread(self._prepare)
        except OSError as e:
            log.warning("capture: the gallery folder can't be used (%s)", e)
            self.set_status(ServiceStatus.DEGRADED, "the gallery folder can't be used")
            return
        self.set_status(ServiceStatus.RUNNING, "" if self._ffmpeg() else "no ffmpeg: photos only")

    def _prepare(self) -> None:
        self.gallery.ensure()
        gone = self.gallery.clean_parts()
        if gone:
            log.info("capture: removed %d unfinished recording(s) left by a power cut", gone)
        self._load_shares()

    async def stop(self) -> None:  # type: ignore[override]
        for s in self._subs:
            s.cancel()
        self._subs.clear()
        if self._rec is not None:
            await self.stop_video("shutdown")
        await self.close_player("shutdown")
        self.set_status(ServiceStatus.STOPPED)

    # ================================================================ gates
    def unavailable_reason(self) -> str | None:
        if not self.cfg.enabled:
            return "switched off in LittleBot's config (capture.enabled)"
        if getattr(self.ctx, "vision", None) is None:
            return "no camera subsystem"
        return None

    @property
    def switched_on(self) -> bool:
        return bool(self.ctx.settings.get("capture.enabled", True))

    @property
    def video_sound(self) -> bool:
        return bool(self.ctx.settings.get("capture.video_sound", self.cfg.video_sound))

    def _ffmpeg(self) -> str | None:
        return self._find(str(self.cfg.ffmpeg_command or "ffmpeg"))

    def _ffplay(self) -> str | None:
        return self._find(str(self.cfg.ffplay_command or "ffplay"))

    def _find(self, cmd: str) -> str | None:
        if Path(cmd).is_absolute():
            return cmd if Path(cmd).is_file() else None
        return self._which(cmd)

    def camera_problem(self) -> str | None:
        """Why the camera can't take a picture now (an ERRORS code), else None. Cheap."""
        if not self.ctx.privacy.camera_enabled:
            return "camera_off"
        vision = getattr(self.ctx, "vision", None)
        if vision is None:
            return "no_camera"
        try:
            st = vision.status()
        except Exception:  # noqa: BLE001
            return "no_camera"
        cam = str(st.get("camera") or "")
        if cam == "disabled":
            return "camera_off" if "privacy" in str(st.get("detail") or "") else "no_camera"
        if cam in ("unavailable", "disconnected"):
            return "no_camera"
        return None

    def check(self, kind: str = "photo") -> str | None:
        """Why a photo (``kind`` "photo") or a video ("video") can't be taken now, else None."""
        if self.unavailable_reason() is not None:
            return "unavailable"
        if not self.switched_on:
            return "disabled"
        cam = self.camera_problem()
        if cam is not None:
            return cam
        if self.gallery.free_mb() < int(self.cfg.min_free_mb):
            return "disk_full"
        if kind == "video":
            if self._rec is not None:
                return "recording"
            if self._ffmpeg() is None:
                return "no_encoder"
        return None

    def _fail(self, code: str, detail: str = "", **more: Any) -> dict[str, Any]:
        self.last_error = code
        return {"ok": False, "error": code, "detail": detail or ERRORS.get(code, ERRORS["failed"]), **more}

    # ================================================================ photos
    async def take_photo(self, *, source: str = "voice", who: str | None = None, countdown: int | None = None,
                         show: bool = True) -> dict[str, Any]:
        """A photo now (after ``countdown`` seconds: "3, 2, 1", default ``capture.countdown_seconds``), saved to the
        gallery and shown on the screen for ``capture.show_seconds``."""
        why = self.check("photo")
        if why is not None:
            return self._fail(why)
        from littlebot.vision.service import PhotoUnavailable  # noqa: PLC0415

        async with self._photo_lock:
            n = int(self.cfg.countdown_seconds if countdown is None else max(0, min(10, int(countdown))))
            card = self._photo_card(n) if show else None
            finder = asyncio.get_running_loop().create_task(self._viewfinder(card)) if card is not None and n else None
            try:
                for i in range(n, 0, -1):
                    if card is not None:
                        card.set_count(i)
                    self._sound("blip")
                    await asyncio.sleep(1.0)
                if card is not None:
                    card.set_count(0)
                jpeg = await self.ctx.vision.capture_photo(max_width=int(self.cfg.photo_width),
                                                           quality=int(self.cfg.photo_quality), timeout=3.0)
            except PhotoUnavailable as e:
                if card is not None:
                    card.failed(ERRORS.get(e.reason, ERRORS["failed"]))
                log.info("capture: no photo (%s)", e.reason)
                return self._fail(e.reason if e.reason in ERRORS else "failed")
            finally:
                if finder is not None:
                    finder.cancel()
            self._sound("click")
            taken = self._wall()
            item = await asyncio.to_thread(self._save_photo, jpeg, taken, source, who)
        log.info("capture: photo %s taken (%s, %d KB)", item.id, source, item.bytes // 1024)
        self._publish(SAVED, {"id": item.id, "kind": "photo", "source": source})
        self.last = {"what": "photo", "id": item.id, "at": taken}
        self.last_error = None
        if card is not None:
            card.taken(self.gallery.items(limit=500), item.id)
        return {"ok": True, "item": item.public(), "when": _when(item.taken_at)}

    async def _viewfinder(self, card: Any) -> None:
        """While the countdown runs: what the camera sees, ~6 times a second, behind the 3, 2, 1 (in memory only,
        through vision's warm photo; dropped the moment the countdown ends)."""
        from littlebot.screens.cards.gallery import viewfinder_frame  # noqa: PLC0415

        vision = self.ctx.vision
        try:
            vision.keep_photo_warm(VIEWFINDER, True, allowed=lambda: bool(self.ctx.privacy.camera_enabled),
                                   every=0.15, width=720, quality=70)
        except Exception:  # noqa: BLE001 - the countdown works without it
            return
        last: bytes | None = None
        try:
            while card.count is not None:
                got = vision.latest_photo(max_age=1.0)
                if got is not None and got[0] is not last:
                    last = got[0]
                    surf = await asyncio.to_thread(viewfinder_frame, last)
                    if surf is not None and card.count is not None:
                        card.set_preview(surf)
                await asyncio.sleep(0.12)
        finally:
            with contextlib.suppress(Exception):
                vision.keep_photo_warm(VIEWFINDER, False)

    def _save_photo(self, jpeg: bytes, taken: float, source: str, who: str | None) -> Item:
        from littlebot.vision.imaging import shrink_jpeg  # noqa: PLC0415

        thumb = shrink_jpeg(jpeg, THUMB_WIDTH, 80)
        return self.gallery.add_photo(jpeg, taken_at=taken, thumb=thumb if thumb is not jpeg else None,
                                      source=source, who=who)

    def _photo_card(self, countdown: int) -> Any:
        """The gallery card in its "say cheese" mode (the countdown, the flash, then the new photo)."""
        if not self._screen:
            return None
        screens = getattr(self.ctx, "screens", None)
        if screens is None:
            return None
        try:
            from littlebot.screens.cards.gallery import GalleryCard  # noqa: PLC0415

            card = GalleryCard({"title": "Say cheese!"}, screens.make_host(), gallery=self.gallery,
                               items=[], countdown=countdown, linger=float(self.cfg.show_seconds),
                               on_play=self._play_cb, on_share=self._share_cb)
            res = screens.show_card(card, source="capture")
        except Exception:  # noqa: BLE001 - the photo is what matters
            log.warning("capture: the countdown card failed", exc_info=True)
            return None
        return card if res.get("ok") else None

    # ================================================================ videos
    @property
    def recording(self) -> dict[str, Any] | None:
        r = self._rec
        if r is None:
            return None
        return {"id": r.id, "started_at": r.started_wall, "seconds": round(max(0.0, self._clock() - r.started), 1),
                "sound": r.sound and r.recorder.audio, "source": r.source,
                "max_seconds": float(self.cfg.max_video_minutes) * 60.0,
                "state": "saving" if r.stopping else r.recorder.state}

    async def start_video(self, *, source: str = "voice", who: str | None = None) -> dict[str, Any]:
        why = self.check("video")
        if why is not None:
            return self._fail(why)
        from littlebot.vision.service import PhotoUnavailable  # noqa: PLC0415

        async with self._rec_lock:
            if self._rec is not None:
                return self._fail("recording")
            item_id = self.gallery.new_id()
            await asyncio.to_thread(self.gallery.ensure)
            sound = self.video_sound and bool(self.ctx.privacy.microphone_enabled)
            rec = VideoRecorder(self.gallery.part(item_id), width=int(self.cfg.video_width),
                                fps=int(self.cfg.video_fps), crf=int(self.cfg.video_crf),
                                preset=str(self.cfg.video_preset), ffmpeg=self._ffmpeg() or "ffmpeg",
                                audio_source=str(self.cfg.audio_source) if sound else None, popen=self._popen,
                                clock=self._clock)
            try:
                self.ctx.vision.add_frame_sink(OWNER, rec.offer, float(self.cfg.video_fps))
            except PhotoUnavailable as e:
                return self._fail(e.reason if e.reason in ERRORS else "no_camera")
            r = _Rec(id=item_id, recorder=rec, started=self._clock(), started_wall=self._wall(), source=source,
                     who=who, sound=sound)
            rec.on_failed = lambda why, r=r: self._soon(self._recorder_died(r, why))
            rec.start()
            self._rec = r
            self._spawn(r, self._time_limit(r))
            self._spawn(r, self._keep_awake(r))
            self._spawn(r, self._first_frame_check(r))
        log.info("capture: recording video %s (%s, %s)", item_id, source, "with sound" if sound else "no sound")
        self._publish(RECORDING, {"active": True, "id": item_id, "started_at": r.started_wall, "sound": sound})
        self._publish(KEEP_AWAKE, {"kind": "recording"})
        self.last_error = None
        return {"ok": True, "id": item_id, "sound": sound, "max_minutes": float(self.cfg.max_video_minutes)}

    async def stop_video(self, reason: str = "asked", *, announce: str | None = None) -> dict[str, Any]:
        """End the recording and save it (finishing the file takes a moment)."""
        async with self._rec_lock:
            r = self._rec
            if r is None or r.stopping:
                return self._fail("not_recording")
            r.stopping = True
        with contextlib.suppress(Exception):
            self.ctx.vision.remove_frame_sink(OWNER)
        current = asyncio.current_task()
        for t in list(r.tasks):
            if t is not current:
                t.cancel()
        res = await asyncio.to_thread(r.recorder.stop)
        item: Item | None = None
        if res.ok:
            item = await asyncio.to_thread(self.gallery.finish_video, r.id, taken_at=r.started_wall,
                                           seconds=res.seconds, width=res.width, height=res.height, thumb=res.thumb,
                                           source=r.source, who=r.who)
        if item is None:
            await asyncio.to_thread(self.gallery.discard_part, r.id)
        async with self._rec_lock:
            if self._rec is r:
                self._rec = None
        self._publish(RECORDING, {"active": False, "id": r.id, "started_at": r.started_wall, "sound": res.audio,
                                  "reason": str(reason)[:60]})
        if item is not None:
            log.info("capture: video %s saved (%s): %.0f s, %d frames, %d MB%s", item.id, reason, res.seconds,
                     res.frames, item.bytes // (1024 * 1024), "" if res.audio else ", no sound")
            self._publish(SAVED, {"id": item.id, "kind": "video", "source": r.source})
            self.last = {"what": "video", "id": item.id, "at": self._wall(), "reason": reason}
        else:
            log.warning("capture: video %s not saved (%s; %s)", r.id, reason, res.error)
        if announce:
            self._announce(announce if item is not None else f"The recording didn't work: "
                                                              f"{ERRORS.get(res.error or '', ERRORS['failed'])}")
        if item is None:
            return self._fail(res.error or "failed", seconds=res.seconds)
        return {"ok": True, "item": item.public(), "seconds": res.seconds, "sound": res.audio,
                "audio_error": res.audio_error, "when": _when(item.taken_at)}

    def _spawn(self, r: _Rec, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        r.tasks.add(task)
        task.add_done_callback(r.tasks.discard)

    async def _time_limit(self, r: _Rec) -> None:
        minutes = float(self.cfg.max_video_minutes)
        await asyncio.sleep(minutes * 60.0)
        if self._rec is r and not r.stopping:
            await self.stop_video(f"the time limit ({minutes:g} minutes)",
                                  announce=f"I stopped recording after {minutes:g} minutes. The video is saved.")

    async def _keep_awake(self, r: _Rec) -> None:
        while self._rec is r:
            await asyncio.sleep(KEEP_AWAKE_EVERY_S)
            if self._rec is r:
                self._publish(KEEP_AWAKE, {"kind": "recording"})

    async def _first_frame_check(self, r: _Rec) -> None:
        await asyncio.sleep(FIRST_FRAME_CHECK_S)
        if self._rec is r and not r.stopping and r.recorder.state == "failed":
            await self._recorder_died(r, r.recorder.error or "failed")

    async def _recorder_died(self, r: _Rec, why: str) -> None:
        if self._rec is not r or r.stopping:
            return
        log.warning("capture: the recording stopped by itself (%s)", why)
        await self.stop_video(f"the recorder stopped ({why})",
                              announce="I stopped recording: something went wrong. What I had is saved.")

    # ================================================================ the screen
    def _play_cb(self, item_id: str) -> None:
        self._soon(self.play_video(item_id, source="screen"))

    def _share_cb(self, item_id: str) -> None:
        self._soon(self.show_share_code(item_id))

    async def show_gallery(self, item_id: str | None = None, *, kind: str | None = None, source: str = "voice",
                           slideshow: bool = False) -> dict[str, Any]:
        """The gallery card on the robot's screen, at ``item_id`` (else the newest ``kind``)."""
        if self.unavailable_reason() is not None:
            return self._fail("unavailable")
        if not self._screen:
            return self._fail("no_screen")
        screens = getattr(self.ctx, "screens", None)
        if screens is None:
            return self._fail("no_screen")
        items = await asyncio.to_thread(self.gallery.items, None, limit=500)
        if kind in ("photo", "video"):
            items = [it for it in items if it.kind == kind] or items
        if not items:
            return self._fail("not_found", "There are no photos or videos yet.")
        index = next((i for i, it in enumerate(items) if it.id == item_id), 0)
        from littlebot.screens.cards.gallery import GalleryCard  # noqa: PLC0415

        card = GalleryCard({"title": "Photos"}, screens.make_host(), gallery=self.gallery, items=items, index=index,
                           on_play=self._play_cb, on_share=self._share_cb, slideshow=slideshow)
        res = screens.show_card(card, source=str(source)[:20])
        if not res.get("ok"):
            return self._fail("busy" if res.get("error") == "busy" else "no_screen")
        return {"ok": True, "showing": card.describe(), "count": len(items), "index": index}

    async def play_video(self, item_id: str, *, source: str = "voice", back: bool = True) -> dict[str, Any]:
        """A gallery video full screen (ffplay, with its sound); the gallery card comes back when it ends."""
        item = self.gallery.get(item_id)
        if item is None or item.kind != "video":
            return self._fail("not_found")
        if not self._screen:
            return self._fail("no_screen")
        player = self._ffplay()
        if player is None:
            return self._fail("no_player")
        await self.close_player("another video")
        from littlebot.mirror.service import default_launcher, make_room  # noqa: PLC0415

        await make_room(self.ctx, "capture", "a video from the gallery")
        p = _Player(item_id=item.id, back=back)

        def preempted(lease: Lease, by: str) -> None:
            if p.lease is None or p.lease is lease:
                self.close_player_soon(f"{by} took the screen", back=False)

        # Taken like a video (the card on the screen goes), then held lower so a game or a video wins later.
        lease = self.ctx.arbiter.acquire(PLAYER_OWNER, [Resource.SCREEN], Priority.MEDIA, on_preempt=preempted)
        if lease is None:
            return self._fail("busy")
        p.lease = self.ctx.arbiter.acquire(PLAYER_OWNER, [Resource.SCREEN], PLAYER_PRIORITY,
                                           on_preempt=preempted) or lease
        argv = [player, "-hide_banner", "-loglevel", "error", "-nostats", "-fs", "-noborder", "-alwaysontop",
                "-window_title", "LittleBot", "-autoexit", str(self.gallery.file(item))]
        try:
            p.handle = await (self._launcher or default_launcher)(argv, None)
        except (OSError, ValueError) as e:
            log.warning("capture: ffplay didn't start (%s)", str(e)[:120])
            if p.lease is not None:
                p.lease.release()
            return self._fail("no_player")
        with self._lock:
            self._player = p
        loop = asyncio.get_running_loop()
        p.task = loop.create_task(self._watch_player(p))
        p.awake = loop.create_task(self._player_awake(p))
        log.info("capture: playing video %s (%s)", item.id, source)
        return {"ok": True, "id": item.id, "seconds": item.seconds}

    async def _player_awake(self, p: _Player) -> None:
        """LittleBot doesn't doze off (which would close the video) while it plays."""
        while not p.ended:
            self._publish(KEEP_AWAKE, {"kind": "video"})
            await asyncio.sleep(KEEP_AWAKE_EVERY_S)

    async def _watch_player(self, p: _Player) -> None:
        h = p.handle
        with contextlib.suppress(Exception):
            async for _line in h.lines():  # (its output, so the pipe never fills)
                if p.ended:
                    break
        with contextlib.suppress(Exception):
            await h.wait()
        if not p.ended:
            await self.close_player("it ended", back=p.back)

    def close_player_soon(self, reason: str, *, back: bool = False) -> None:
        if self._player is not None:
            self._soon(self.close_player(reason, back=back))

    async def close_player(self, reason: str = "closed", *, back: bool = False) -> bool:
        with self._lock:
            p, self._player = self._player, None
        if p is None or p.ended:
            return False
        p.ended = True
        if p.awake is not None and p.awake is not asyncio.current_task():
            p.awake.cancel()
        if p.lease is not None:
            p.lease.release()
        h = p.handle
        if h is not None and getattr(h, "returncode", None) is None:
            try:
                h.terminate()
                await asyncio.wait_for(h.wait(), TERMINATE_S)
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    h.kill()
            except Exception:  # noqa: BLE001
                log.debug("capture: stopping ffplay failed", exc_info=True)
        log.info("capture: video %s closed (%s)", p.item_id, reason)
        if back:
            await self.show_gallery(p.item_id, source="capture")
        return True

    @property
    def playing(self) -> str | None:
        p = self._player
        return p.item_id if p is not None and not p.ended else None

    # ================================================================ sharing
    def _load_shares(self) -> None:
        path = self.gallery.root / SHARE_FILE
        data: Any = {}
        with contextlib.suppress(OSError, ValueError):
            if path.is_file() and path.stat().st_size < 256 * 1024:
                data = json.loads(path.read_text(encoding="utf-8"))
        now = self._wall()
        shares = {}
        if isinstance(data, dict):
            for tok, v in data.items():
                if (TOKEN_RE.match(str(tok)) and isinstance(v, dict) and valid_id(v.get("id"))
                        and float(v.get("exp") or 0) > now):
                    shares[str(tok)] = {"id": str(v["id"]), "exp": float(v["exp"])}
        with self._lock:
            self._shares = shares
            self._shares_loaded = True

    def _save_shares(self) -> None:
        from littlebot.core.keys import write_private  # noqa: PLC0415

        with self._lock:
            data = json.dumps(self._shares, separators=(",", ":"))
        try:
            self.gallery.ensure()
            write_private(self.gallery.root, SHARE_FILE, data)
        except OSError:
            log.warning("capture: the share links couldn't be saved", exc_info=True)

    def _prune(self) -> None:
        now = self._wall()
        with self._lock:
            self._shares = {t: v for t, v in self._shares.items() if v["exp"] > now}

    def base_urls(self) -> tuple[list[str], bool]:
        """The dashboard's addresses, the ones a phone can use first; and whether a phone can reach them."""
        cfg = self.ctx.config.dashboard
        lan = bool(getattr(cfg, "lan_access", False)) or str(getattr(cfg, "host", "")) in ("0.0.0.0", "::")
        urls: list[str] = []
        robot = (getattr(self.ctx, "extras", None) or {}).get("robot")
        dash = getattr(getattr(robot, "dashboard", None), "dash", None)
        urls = [u for u in (getattr(dash, "urls", None) or []) if u]
        if not urls:
            with contextlib.suppress(Exception):
                from littlebot.dashboard.deps import dashboard_urls, resolve_bind_host  # noqa: PLC0415

                urls = dashboard_urls(resolve_bind_host(cfg), int(cfg.port))
        if not urls:
            urls = [f"http://127.0.0.1:{int(cfg.port)}/"]
        outside = [u for u in urls if (urlsplit(u).hostname or "") not in ("127.0.0.1", "localhost", "::1")]
        inside = [u for u in urls if u not in outside]
        return [u.rstrip("/") for u in outside + inside], lan and bool(outside)

    def share(self, item_id: str, *, hours: float | None = None) -> dict[str, Any]:
        """A link to one photo or video for a phone (an existing one still valid for an hour or more is reused)."""
        item = self.gallery.get(item_id)
        if item is None:
            return self._fail("not_found")
        if not self._shares_loaded:
            self._load_shares()
        self._prune()
        now = self._wall()
        hours = float(self.cfg.share_hours if hours is None else hours)
        with self._lock:
            token = next((t for t, v in self._shares.items() if v["id"] == item.id and v["exp"] - now > 3600), None)
            if token is None:
                token = secrets.token_urlsafe(16)
                self._shares[token] = {"id": item.id, "exp": now + hours * 3600.0}
                if len(self._shares) > MAX_SHARES:  # the oldest go first
                    keep = sorted(self._shares.items(), key=lambda kv: kv[1]["exp"])[-MAX_SHARES:]
                    self._shares = dict(keep)
                changed = True
            else:
                changed = False
            exp = self._shares[token]["exp"]
        if changed:
            self._save_shares()
        bases, reachable = self.base_urls()
        urls = [f"{b}{SHARE_PATH}{token}" for b in bases]
        return {"ok": True, "token": token, "url": urls[0], "urls": urls, "expires_at": exp, "reachable": reachable,
                "item": item.public(), "lan_help": None if reachable else LAN_HELP}

    def share_item(self, token: str) -> Item | None:
        """The item a share link is for, while the link is valid."""
        if not isinstance(token, str) or not TOKEN_RE.match(token):
            return None
        if not self._shares_loaded:
            self._load_shares()
        with self._lock:
            v = self._shares.get(token)
        if v is None or v["exp"] <= self._wall():
            return None
        return self.gallery.get(v["id"])

    def revoke_shares(self, item_id: str | None = None) -> int:
        with self._lock:
            before = len(self._shares)
            self._shares = {t: v for t, v in self._shares.items() if item_id is not None and v["id"] != item_id}
            n = before - len(self._shares)
        if n:
            self._save_shares()
            log.info("capture: %d share link(s) stopped working", n)
        return n

    def share_count(self) -> int:
        self._prune()
        return len(self._shares)

    async def send_to_phone(self, item_id: str, *, to: int | None = None) -> dict[str, Any]:
        """The share link to a phone as a notification (``to``: a Who's who person, else the family's phones)."""
        link = self.share(item_id)
        if not link.get("ok"):
            return link
        if not link["reachable"]:
            return self._fail("lan_off", url=link["url"])
        from littlebot.skills.push import Push  # noqa: PLC0415

        push = Push(self.ctx)
        why = push.blocked_reason(to)
        if why is not None:
            return self._fail(why, _PUSH_WORDS.get(why, "The phone notification couldn't be sent."), url=link["url"])
        item = self.gallery.get(item_id)
        assert item is not None
        what = "photo" if item.kind == "photo" else "video"
        base = link["url"]
        attach = None
        if self.cfg.push_attach:
            attach = f"{base}/file" if item.kind == "photo" else f"{base}/thumb"
        hours = float(self.cfg.share_hours)
        message = (f"Taken {_when(item.taken_at)}. Tap to open it and save it (on your home Wi-Fi; the link works "
                   f"for {hours:g} hours).")
        ok = await push.send(f"A {what} from LittleBot", message, tags=("camera",) if what == "photo" else
                             ("movie_camera",), to=to, click=base, attach=attach,
                             filename=f"littlebot-{item.id}.jpg" if attach else None)
        if not ok:
            return self._fail("push_failed", f"The phone notification didn't go ({push.last_error}).", url=base)
        where = "family" if to is None or not push.topic(to) else "own"
        log.info("capture: %s %s sent to a phone (%s)", what, item.id, where)
        return {"ok": True, "url": base, "where": where, "expires_at": link["expires_at"]}

    def phones(self) -> list[dict[str, Any]]:
        """Where "send to phone" can go: the household's phone (id None) and each grown-up's own (Who's who id)."""
        from littlebot.skills.push import Push  # noqa: PLC0415

        push = Push(self.ctx)
        names: dict[int, str] = {}
        with contextlib.suppress(Exception):
            from littlebot.vision.faces import get_faces  # noqa: PLC0415

            names = {int(p["id"]): str(p["name"]) for p in get_faces(self.ctx).store.people()}
        out: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            if push.topic():
                out.append({"id": None, "name": "The family's phone"})
            for pid in push.person_ids():
                if push.topic(pid):
                    out.append({"id": pid, "name": names.get(pid, f"Person {pid}")})
        return out

    async def show_share_code(self, item_id: str) -> dict[str, Any]:
        """The share link as a QR code on LittleBot's screen (a phone's camera opens it)."""
        link = self.share(item_id)
        if not link.get("ok"):
            return link
        if not self._screen or getattr(self.ctx, "screens", None) is None:
            return self._fail("no_screen", url=link["url"])
        from littlebot.screens.cards.gallery import ShareCard  # noqa: PLC0415

        item = self.gallery.get(item_id)
        screens = self.ctx.screens
        card = ShareCard({"title": "Scan me"}, screens.make_host(), url=link["url"], gallery=self.gallery,
                         item=item, hours=float(self.cfg.share_hours), reachable=bool(link["reachable"]))
        res = screens.show_card(card, source="capture")
        if not res.get("ok"):
            return self._fail("busy", url=link["url"])
        return {"ok": True, "url": link["url"], "reachable": link["reachable"],
                "lan_help": None if link["reachable"] else LAN_HELP}

    # ================================================================ the gallery
    def delete(self, item_id: str) -> bool:
        if self.playing == item_id:
            self.close_player_soon("deleted")
        gone = self.gallery.delete(item_id)
        if gone:
            self.revoke_shares(item_id)
            log.info("capture: %s deleted", item_id)
            self._publish(DELETED, {"id": item_id})
        return gone

    def describe_latest(self) -> str:
        item = self.gallery.latest()
        if item is None:
            return "no photos or videos yet"
        what = "a photo" if item.kind == "photo" else f"a {round(item.seconds or 0)} second video"
        return f"{what} taken {_when(item.taken_at)}"

    def status(self) -> dict[str, Any]:
        why = self.unavailable_reason()
        bases, reachable = self.base_urls()
        out: dict[str, Any] = {
            "available": why is None, "detail": why, "enabled": self.switched_on, "video_sound": self.video_sound,
            "camera": {"ok": self.camera_problem() is None,
                       "detail": ERRORS.get(self.camera_problem() or "", "") or None},
            "microphone": bool(self.ctx.privacy.microphone_enabled),
            "recording": self.recording, "playing": self.playing,
            "ffmpeg": self._ffmpeg() is not None, "ffplay": self._ffplay() is not None,
            "free_mb": self.gallery.free_mb(), "min_free_mb": int(self.cfg.min_free_mb),
            "max_video_minutes": float(self.cfg.max_video_minutes),
            "share": {"reachable": reachable, "base": bases[0] if bases else None,
                      "hours": float(self.cfg.share_hours), "links": self.share_count(),
                      "lan_help": None if reachable else LAN_HELP},
            "last": dict(self.last) if self.last else None, "last_error": self.last_error,
        }
        out.update(self.gallery.usage())
        return out

    # ================================================================ bus callbacks (any thread)
    def _on_privacy(self, event: Event) -> None:
        if not event.get("camera_enabled", True) and self._rec is not None:
            self._soon(self.stop_video("the camera was switched off in Privacy",
                                       announce="The camera was switched off, so I stopped recording. "
                                                "What I had is saved."))

    def _on_state(self, event: Event) -> None:
        if event.get("new") in (RobotState.SLEEPING.value, RobotState.ERROR.value):
            self.close_player_soon(f"robot {str(event.get('new')).lower()}")

    def _on_setting(self, event: Event) -> None:
        if event.get("key") == "capture.enabled" and not event.get("value") and self._rec is not None:
            self._soon(self.stop_video("switched off on the dashboard"))

    async def _stop_all(self) -> None:
        await self.close_player("stop all")
        if self._rec is not None:
            await self.stop_video("stop all")

    async def _media_started(self) -> None:
        await self.close_player("a video started")
        if self._rec is not None:
            await self.stop_video("a video started",
                                  announce="A video started, so I stopped recording. What I had is saved.")

    # ================================================================ helpers
    def _soon(self, coro: Any) -> None:
        """Run ``coro`` on the robot's loop, from any thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fut: Any = loop.create_task(coro)
        else:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        self._tasks.add(fut)
        fut.add_done_callback(self._tasks.discard)

    def _sound(self, name: str) -> None:
        audio = getattr(self.ctx, "audio", None)
        if audio is None:
            return
        try:
            self._soon(audio.play_sound(name, wait=False))
        except Exception:  # noqa: BLE001
            log.debug("capture: sound %s failed", name, exc_info=True)

    def _announce(self, text: str) -> None:
        from littlebot.skills.base import announce  # noqa: PLC0415

        with contextlib.suppress(Exception):
            announce(self.ctx, text, kind="capture")

    def _publish(self, topic: str, data: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self.ctx.bus.publish(topic, data, source="capture")


_PUSH_WORDS = {
    "not_set_up": "No phone is set up for notifications yet (dashboard: Assistant page, Calendars and phones).",
    "cloud_off": "Phone notifications need Cloud communication on (Privacy page).",
    "offline": "LittleBot is in offline mode, so it can't send phone notifications.",
}


__all__ = ["CaptureService", "ERRORS", "KEEP_AWAKE", "LAN_HELP", "RECORDING", "SAVED", "SHARE_PATH"]
