"""CaptureService (littlebot/capture/service.py) with a fake camera, a fake ffmpeg and a fake screen: photos, videos
from vision's frames, what stops a recording, the gallery on the screen, playing a video, share links and sending
one to a phone."""

from __future__ import annotations

import asyncio
import io
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from littlebot.capture import recorder as rec_mod
from littlebot.capture.service import DELETED, RECORDING, SAVED, SHARE_PATH, CaptureService
from littlebot.core.arbiter import Resource
from littlebot.core.events import Topics
from littlebot.screens.base import RecordingHost
from littlebot.skills.base import ANNOUNCE
from littlebot.testing import make_test_context
from littlebot.vision.service import PhotoUnavailable

JPEG = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xc0\x00\x11\x08\x00\x03\x00\x04\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01" + b"\x00" * 32 + b"\xff\xd9")


class FakeVision:
    def __init__(self) -> None:
        self.sinks: dict[str, Any] = {}
        self.photos = 0
        self.camera = "ok"
        self.warm: list[tuple[str, bool]] = []

    def status(self) -> dict[str, Any]:
        return {"camera": self.camera, "detail": ""}

    async def capture_photo(self, *, max_width: int, quality: int, timeout: float) -> bytes:
        if self.camera != "ok":
            raise PhotoUnavailable("no_camera", "unplugged")
        self.photos += 1
        return JPEG

    def add_frame_sink(self, owner: str, fn: Any, fps: float = 30.0) -> None:
        self.sinks[owner] = fn

    def remove_frame_sink(self, owner: str) -> bool:
        return self.sinks.pop(owner, None) is not None

    def keep_photo_warm(self, owner: str, on: bool, **_kw: Any) -> None:
        self.warm.append((owner, on))

    def latest_photo(self, max_age: float = 1.0) -> Any:
        return None


class FakeFFmpeg:
    def __init__(self, argv: list[str]) -> None:
        self.argv, self.out, self.sound = argv, Path(argv[-1]), "pulse" in argv
        self.returncode: int | None = None
        self.written = 0
        self.stderr = io.BytesIO(b"")
        self._done = threading.Event()
        proc = self

        class _In:
            closed = False

            def write(self, data: bytes) -> int:
                if self.closed or proc.returncode is not None:
                    raise BrokenPipeError
                proc.written += len(data)
                return len(data)

            def close(self) -> None:
                self.closed = True
                if not proc.sound:
                    proc._exit(0)

        self.stdin = _In()

    def _exit(self, code: int) -> None:
        if self.returncode is None:
            if code in (0, 255) and self.written:
                self.out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
            self.returncode = code
            self._done.set()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return int(self.returncode or 0)

    def send_signal(self, sig: int) -> None:
        if sig == signal.SIGINT:
            self._exit(255)

    def terminate(self) -> None:
        self._exit(-15)

    def kill(self) -> None:
        self._exit(-9)


class FakeScreens:
    def __init__(self) -> None:
        self.cards: list[Any] = []
        self.ok = True

    def make_host(self) -> RecordingHost:
        return RecordingHost()

    def show_card(self, card: Any, *, source: str = "") -> dict[str, Any]:
        if not self.ok:
            return {"ok": False, "error": "busy"}
        self.cards.append(card)
        return {"ok": True}

    @property
    def current(self) -> Any:
        return self.cards[-1] if self.cards else None


class FakePlayer:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.returncode: int | None = None
        self._done = asyncio.Event()

    async def lines(self) -> Any:
        await self._done.wait()
        if False:  # pragma: no cover - an async generator with nothing to say
            yield ""

    async def wait(self) -> int:
        await self._done.wait()
        return int(self.returncode or 0)

    def end(self, code: int = 0) -> None:
        self.returncode = code
        self._done.set()

    def terminate(self) -> None:
        self.end(-15)

    def kill(self) -> None:
        self.end(-9)


class Env:
    def __init__(self, tmp: Path, *, screen: bool = False, **cfg: Any) -> None:
        self.ctx = make_test_context(capture=cfg) if cfg else make_test_context()
        self.ctx.vision = FakeVision()
        self.screens = FakeScreens()
        self.ctx.screens = self.screens
        self.procs: list[FakeFFmpeg] = []
        self.players: list[FakePlayer] = []
        self.wall = [1_762_400_000.0]
        self.svc = CaptureService(self.ctx, popen=self._popen, launcher=self._launch,
                                  which=lambda cmd: f"/usr/bin/{cmd}", wall=lambda: self.wall[0],
                                  gallery_dir=tmp / "gallery", screen=screen)
        self.events: list[tuple[str, dict[str, Any]]] = []
        for topic in (SAVED, RECORDING, DELETED, ANNOUNCE):
            self.ctx.bus.subscribe(topic, lambda e, t=topic: self.events.append((t, dict(e.data))))

    def _popen(self, argv: list[str], **_kw: Any) -> FakeFFmpeg:
        p = FakeFFmpeg(list(argv))
        self.procs.append(p)
        return p

    async def _launch(self, argv: Any, env: Any = None) -> FakePlayer:
        p = FakePlayer(list(argv))
        self.players.append(p)
        return p

    def topics(self, topic: str) -> list[dict[str, Any]]:
        return [d for t, d in self.events if t == topic]

    def feed(self, stop: threading.Event) -> threading.Thread:
        def run() -> None:
            while not stop.is_set():
                fn = self.ctx.vision.sinks.get("capture")
                if fn is not None:
                    fn(np.zeros((72, 128, 3), dtype=np.uint8), time.monotonic())
                time.sleep(1 / 30)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rec_mod, "AUDIO_PROBE_S", 0.15)
    monkeypatch.setattr(rec_mod, "SOUND_TAIL_S", 0.02)
    made: list[Env] = []

    async def _make(**kw: Any) -> Env:
        e = Env(tmp_path, **kw)
        await e.svc.start()
        made.append(e)
        return e

    yield _make
    for e in made:
        e.ctx.db.close()


async def _until(cond: Any, timeout: float = 3.0) -> None:
    t0 = time.monotonic()
    while not cond():
        assert time.monotonic() - t0 < timeout, "timed out"
        await asyncio.sleep(0.01)


# ------------------------------------------------------------------ photos
async def test_a_photo_is_saved_and_announced_on_the_bus(env) -> None:
    e = await env()
    res = await e.svc.take_photo(source="voice", who="Ali", countdown=0, show=False)
    assert res["ok"] and res["item"]["kind"] == "photo" and res["item"]["who"] == "Ali"
    item = e.svc.gallery.get(res["item"]["id"])
    assert item is not None and e.svc.gallery.file(item).read_bytes() == JPEG
    assert e.topics(SAVED) == [{"id": item.id, "kind": "photo", "source": "voice"}]
    await asyncio.sleep(0.01)
    assert "click" in e.ctx.audio.sounds and e.svc.last["id"] == item.id


async def test_the_countdown_beeps_and_shows_on_the_screen(env) -> None:
    e = await env(screen=True)
    res = await e.svc.take_photo(countdown=1)
    assert res["ok"]
    await asyncio.sleep(0.01)
    assert e.ctx.audio.sounds.count("blip") == 1 and "click" in e.ctx.audio.sounds
    card = e.screens.cards[-1]
    assert card.kind == "gallery" and card.item is not None and card.item.id == res["item"]["id"]
    assert ("capture_viewfinder", True) in e.ctx.vision.warm and e.ctx.vision.warm[-1] == ("capture_viewfinder", False)


@pytest.mark.parametrize("why", ["camera_off", "disabled", "disk_full", "no_camera"])
async def test_a_photo_is_refused_with_the_reason(env, why: str) -> None:
    e = await env()
    if why == "camera_off":
        e.ctx.privacy.set(camera_enabled=False)
    elif why == "disabled":
        e.ctx.settings.set("capture.enabled", False, source="test")
    elif why == "disk_full":
        e.svc.gallery.free_mb = lambda: 10  # type: ignore[method-assign]
    else:
        e.ctx.vision.camera = "disconnected"
    res = await e.svc.take_photo(countdown=0, show=False)
    assert res == {"ok": False, "error": why, "detail": res["detail"]} and res["detail"]
    assert e.svc.gallery.items() == [] and e.ctx.vision.photos == 0


# ------------------------------------------------------------------ videos
async def test_a_video_from_visions_frames_with_sound(env) -> None:
    e = await env()
    res = await e.svc.start_video(source="voice", who="Ali")
    assert res["ok"] and res["sound"] is True
    assert "capture" in e.ctx.vision.sinks
    assert e.topics(RECORDING)[-1]["active"] is True and e.svc.recording["id"] == res["id"]
    stop = threading.Event()
    feeder = e.feed(stop)
    await _until(lambda: e.svc.recording and e.svc.recording["state"] == "recording")
    await asyncio.sleep(0.3)
    done = await e.svc.stop_video("asked")
    stop.set()
    feeder.join()
    assert done["ok"] and done["sound"] is True and done["seconds"] > 0
    item = e.svc.gallery.get(done["item"]["id"])
    assert item is not None and item.kind == "video" and item.who == "Ali" and (item.width, item.height) == (128, 72)
    assert not e.svc.gallery.part(item.id).exists()
    assert "capture" not in e.ctx.vision.sinks and e.svc.recording is None
    assert e.topics(RECORDING)[-1]["active"] is False
    assert e.topics(SAVED)[-1] == {"id": item.id, "kind": "video", "source": "voice"}
    assert e.procs[0].sound


async def test_no_sound_when_the_microphone_is_off(env) -> None:
    e = await env()
    e.ctx.privacy.set(microphone_enabled=False)
    res = await e.svc.start_video()
    assert res["ok"] and res["sound"] is False
    await e.svc.stop_video("asked")


async def test_one_recording_at_a_time_and_stop_when_none(env) -> None:
    e = await env()
    assert (await e.svc.stop_video())["error"] == "not_recording"
    assert (await e.svc.start_video())["ok"]
    assert (await e.svc.start_video())["error"] == "recording"
    assert e.svc.check("video") == "recording"
    await e.svc.stop_video()


async def test_no_ffmpeg_means_no_videos(env) -> None:
    e = await env()
    e.svc._which = lambda cmd: None  # noqa: SLF001
    assert (await e.svc.start_video())["error"] == "no_encoder"
    assert (await e.svc.take_photo(countdown=0, show=False))["ok"]  # photos still work


@pytest.mark.parametrize("what", ["privacy", "stop_all", "video"])
async def test_what_stops_a_recording_and_saves_it(env, what: str) -> None:
    e = await env()
    assert (await e.svc.start_video())["ok"]
    stop = threading.Event()
    feeder = e.feed(stop)
    await _until(lambda: e.svc.recording and e.svc.recording["state"] == "recording")
    await asyncio.sleep(0.2)
    if what == "privacy":
        e.ctx.privacy.set(camera_enabled=False)
        e.ctx.bus.publish(Topics.PRIVACY_CHANGED, e.ctx.privacy.snapshot(), source="test")
    elif what == "stop_all":
        e.ctx.bus.publish(Topics.STOP_ALL, {}, source="test")
    else:
        e.ctx.bus.publish(Topics.MEDIA_STARTED, {}, source="test")
    await _until(lambda: e.svc.recording is None)
    stop.set()
    feeder.join()
    assert len(e.svc.gallery.items("video")) == 1
    said = [d["text"] for d in e.topics(ANNOUNCE)]
    assert (what == "stop_all") == (said == [])  # STOP ALL is quiet; the others say what happened
    if said:
        assert "saved" in said[-1]


async def test_a_recording_that_never_got_a_frame_is_not_kept(env, monkeypatch: pytest.MonkeyPatch) -> None:
    e = await env()
    assert (await e.svc.start_video())["ok"]
    rid = e.svc.recording["id"]
    res = await e.svc.stop_video()  # no frame came at all
    assert not res["ok"] and e.svc.gallery.items() == [] and not e.svc.gallery.part(rid).exists()


async def test_the_time_limit_stops_it(env) -> None:
    e = await env()
    # (0.3 s; the config itself never goes this low: model_copy skips its checks)
    e.svc.cfg = e.svc.cfg.model_copy(update={"max_video_minutes": 0.005})
    assert (await e.svc.start_video())["ok"]
    stop = threading.Event()
    feeder = e.feed(stop)
    await _until(lambda: e.svc.recording is None, timeout=5.0)
    stop.set()
    feeder.join()
    assert any("stopped recording after" in d["text"] for d in e.topics(ANNOUNCE))


# ------------------------------------------------------------------ the screen
async def test_the_gallery_on_the_screen(env) -> None:
    e = await env(screen=True)
    first = (await e.svc.take_photo(countdown=0, show=False))["item"]["id"]
    e.wall[0] += 5
    second = (await e.svc.take_photo(countdown=0, show=False))["item"]["id"]
    res = await e.svc.show_gallery()
    assert res["ok"] and res["count"] == 2
    card = e.screens.cards[-1]
    assert card.item.id == second
    res = await e.svc.show_gallery(first)
    assert e.screens.cards[-1].item.id == first and res["index"] == 1
    e.screens.ok = False
    assert (await e.svc.show_gallery())["error"] == "busy"


async def test_no_gallery_without_a_screen_or_photos(env) -> None:
    e = await env(screen=False)
    assert (await e.svc.show_gallery())["error"] == "no_screen"
    e2 = await env(screen=True)
    assert (await e2.svc.show_gallery())["error"] == "not_found"


async def _a_video(e: Env) -> str:
    assert (await e.svc.start_video())["ok"]
    stop = threading.Event()
    feeder = e.feed(stop)
    await _until(lambda: e.svc.recording and e.svc.recording["state"] == "recording")
    await asyncio.sleep(0.1)
    res = await e.svc.stop_video()
    stop.set()
    feeder.join()
    return res["item"]["id"]


async def test_a_video_plays_full_screen_then_the_gallery_comes_back(env) -> None:
    e = await env(screen=True)
    vid = await _a_video(e)
    res = await e.svc.play_video(vid)
    assert res["ok"] and e.svc.playing == vid
    argv = e.players[-1].argv
    assert argv[0] == "/usr/bin/ffplay" and "-fs" in argv and argv[-1].endswith(f"{vid}.mp4")
    holder = e.ctx.arbiter.holder(Resource.SCREEN)
    assert holder is not None and holder.owner == "capture_player"
    e.players[-1].end(0)  # the video ended
    await _until(lambda: e.svc.playing is None)
    await _until(lambda: e.screens.cards and e.screens.cards[-1].item.id == vid)
    assert e.ctx.arbiter.holder(Resource.SCREEN) is None or e.ctx.arbiter.holder(Resource.SCREEN).owner != \
        "capture_player"


async def test_going_to_sleep_closes_the_video(env) -> None:
    e = await env(screen=True)
    vid = await _a_video(e)
    assert (await e.svc.play_video(vid))["ok"]
    e.ctx.bus.publish(Topics.STATE_CHANGED, {"old": "IDLE", "new": "SLEEPING"}, source="test")
    await _until(lambda: e.svc.playing is None)
    assert e.players[-1].returncode == -15


async def test_a_photo_is_not_played(env) -> None:
    e = await env(screen=True)
    pid = (await e.svc.take_photo(countdown=0, show=False))["item"]["id"]
    assert (await e.svc.play_video(pid))["error"] == "not_found"


# ------------------------------------------------------------------ sharing
async def test_a_share_link_for_one_item(env, tmp_path: Path) -> None:
    e = await env()
    item_id = (await e.svc.take_photo(countdown=0, show=False))["item"]["id"]
    e.svc.base_urls = lambda: (["http://192.168.2.112:8765"], True)  # type: ignore[method-assign]
    link = e.svc.share(item_id)
    assert link["ok"] and link["reachable"] and link["url"] == f"http://192.168.2.112:8765{SHARE_PATH}{link['token']}"
    assert len(link["token"]) >= 20
    assert e.svc.share_item(link["token"]).id == item_id
    assert e.svc.share(item_id)["token"] == link["token"]  # the same link again
    assert e.svc.share_item("nope") is None and e.svc.share_item("../../etc/passwd") is None
    # kept across a restart
    again = CaptureService(e.ctx, which=lambda c: None, wall=lambda: e.wall[0], gallery_dir=tmp_path / "gallery",
                           screen=False)
    again._load_shares()  # noqa: SLF001
    assert again.share_item(link["token"]).id == item_id
    # and stop working after share_hours
    e.wall[0] += 25 * 3600
    assert e.svc.share_item(link["token"]) is None


async def test_deleting_stops_its_links(env) -> None:
    e = await env()
    item_id = (await e.svc.take_photo(countdown=0, show=False))["item"]["id"]
    token = e.svc.share(item_id)["token"]
    assert e.svc.delete(item_id) is True
    assert e.svc.share_item(token) is None and e.svc.gallery.get(item_id) is None
    assert e.topics(DELETED) == [{"id": item_id}]
    assert e.svc.delete(item_id) is False


async def test_stop_all_links(env) -> None:
    e = await env()
    a = (await e.svc.take_photo(countdown=0, show=False))["item"]["id"]
    tok = e.svc.share(a)["token"]
    assert e.svc.revoke_shares() == 1 and e.svc.share_item(tok) is None and e.svc.share_count() == 0


async def test_sending_needs_lan_access(env) -> None:
    e = await env()
    item_id = (await e.svc.take_photo(countdown=0, show=False))["item"]["id"]
    e.svc.base_urls = lambda: (["http://127.0.0.1:8765"], False)  # type: ignore[method-assign]
    res = await e.svc.send_to_phone(item_id)
    assert res["error"] == "lan_off" and "lan_access" in res["detail"]


async def test_sending_says_when_no_phone_is_set_up(env) -> None:
    e = await env()
    item_id = (await e.svc.take_photo(countdown=0, show=False))["item"]["id"]
    e.svc.base_urls = lambda: (["http://192.168.2.112:8765"], True)  # type: ignore[method-assign]
    res = await e.svc.send_to_phone(item_id)
    assert res["error"] == "not_set_up" and "Calendars and phones" in res["detail"]


async def test_send_to_phone_sends_only_the_link(env, monkeypatch: pytest.MonkeyPatch) -> None:
    from littlebot.skills import push as push_mod

    sent: list[dict[str, Any]] = []

    async def fake_send(self: Any, title: str, message: str, **kw: Any) -> bool:
        sent.append({"title": title, "message": message, **kw})
        return True

    monkeypatch.setattr(push_mod.Push, "blocked_reason", lambda self, to=None: None)
    monkeypatch.setattr(push_mod.Push, "send", fake_send)
    monkeypatch.setattr(push_mod.Push, "topic", lambda self, pid=None: "littlebot-x" if pid in (None, 3) else "")
    e = await env()
    item_id = (await e.svc.take_photo(countdown=0, show=False))["item"]["id"]
    e.svc.base_urls = lambda: (["http://192.168.2.112:8765"], True)  # type: ignore[method-assign]
    res = await e.svc.send_to_phone(item_id, to=3)
    assert res["ok"] and res["where"] == "own"
    msg = sent[-1]
    assert msg["click"] == res["url"] and msg["attach"] == res["url"] + "/file" and msg["to"] == 3
    assert msg["title"] == "A photo from LittleBot" and "24 hours" in msg["message"]
    assert JPEG not in repr(sent).encode()  # never the photo itself


async def test_show_share_code_puts_a_qr_card_up(env) -> None:
    e = await env(screen=True)
    item_id = (await e.svc.take_photo(countdown=0, show=False))["item"]["id"]
    e.svc.base_urls = lambda: (["http://192.168.2.112:8765"], True)  # type: ignore[method-assign]
    res = await e.svc.show_share_code(item_id)
    assert res["ok"] and res["reachable"]
    card = e.screens.cards[-1]
    assert card.kind == "share" and card.url == res["url"]


async def test_status_for_the_dashboard(env) -> None:
    e = await env()
    await e.svc.take_photo(countdown=0, show=False)
    st = e.svc.status()
    assert st["available"] and st["enabled"] and st["ffmpeg"] and st["photos"] == 1 and st["videos"] == 0
    assert st["recording"] is None and "share" in st and st["share"]["hours"] == 24.0


async def test_shutdown_saves_a_recording(env) -> None:
    e = await env()
    assert (await e.svc.start_video())["ok"]
    stop = threading.Event()
    feeder = e.feed(stop)
    await _until(lambda: e.svc.recording and e.svc.recording["state"] == "recording")
    await asyncio.sleep(0.1)
    await e.svc.stop()
    stop.set()
    feeder.join()
    assert len(e.svc.gallery.items("video")) == 1
