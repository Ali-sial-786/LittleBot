"""The camera skill (littlebot/skills/camera.py): photos and recordings start after the answer (a take-over that ends
the chat), the gallery shows at once, sending falls back to the QR code, and the instant phrases' words."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from littlebot.skills import takeover
from littlebot.skills.base import ANNOUNCE
from littlebot.skills.camera import PHOTO_LABEL, VIDEO_LABEL, Camera
from littlebot.skills.hub import SkillHub
from littlebot.testing import make_test_context


def _item(item_id: str, kind: str = "photo", seconds: float | None = None) -> Any:
    return SimpleNamespace(id=item_id, kind=kind, seconds=seconds)


class FakeGallery:
    def __init__(self) -> None:
        self.all: list[Any] = []

    def latest(self, kind: str | None = None) -> Any:
        found = [it for it in self.all if kind is None or it.kind == kind]
        return found[0] if found else None


class FakeCapture:
    def __init__(self) -> None:
        self.cfg = SimpleNamespace(countdown_seconds=3, max_video_minutes=10.0)
        self.video_sound = True
        self.why: str | None = None
        self.gallery = FakeGallery()
        self.rec: dict[str, Any] | None = None
        self.calls: list[tuple[str, Any]] = []
        self.push: dict[str, Any] = {"ok": True, "where": "own", "url": "http://x/share/t"}
        self.code: dict[str, Any] = {"ok": True, "reachable": True, "url": "http://x/share/t"}
        self.shown: dict[str, Any] = {"ok": True, "showing": "photo 1 of 2", "count": 2}

    def unavailable_reason(self) -> str | None:
        return None

    def check(self, kind: str) -> str | None:
        return self.why

    @property
    def recording(self) -> dict[str, Any] | None:
        return self.rec

    def describe_latest(self) -> str:
        return "a photo taken today at 2:31 pm" if self.gallery.all else "no photos or videos yet"

    async def take_photo(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("photo", kw))
        item = _item("20261106-143100")
        self.gallery.all.insert(0, item)
        return {"ok": True, "item": {"id": item.id, "kind": "photo"}}

    async def start_video(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("start", kw))
        self.rec = {"id": "20261106-143200", "seconds": 0.0, "sound": True}
        return {"ok": True}

    async def stop_video(self, reason: str = "asked", **kw: Any) -> dict[str, Any]:
        self.calls.append(("stop", reason))
        self.rec = None
        return {"ok": True, "seconds": 12.3, "sound": True, "item": {"id": "20261106-143200"}}

    async def show_gallery(self, item_id: str | None = None, **kw: Any) -> dict[str, Any]:
        self.calls.append(("show", kw))
        return self.shown

    async def play_video(self, item_id: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("play", item_id))
        return {"ok": True}

    async def send_to_phone(self, item_id: str, *, to: int | None = None) -> dict[str, Any]:
        self.calls.append(("send", (item_id, to)))
        return self.push

    async def show_share_code(self, item_id: str) -> dict[str, Any]:
        self.calls.append(("code", item_id))
        return self.code


class FakeAssistant:
    def __init__(self, who: Any = None) -> None:
        self.who = who

    def speaker(self) -> Any:
        return self.who


def _skill(who: Any = None) -> tuple[Camera, FakeCapture, Any]:
    ctx = make_test_context()
    ctx.capture = FakeCapture()
    ctx.assistant = FakeAssistant(who)
    return Camera(ctx), ctx.capture, ctx


def _said(ctx: Any) -> list[str]:
    return [e.data["text"] for e in ctx.bus.recent(60, ANNOUNCE)]


async def test_a_photo_is_taken_after_the_answer_and_ends_the_chat() -> None:
    skill, svc, ctx = _skill(SimpleNamespace(person_id=3, name="Ali", sure=True))
    out = await skill.run("take_photo", {})
    assert out.startswith("ok:") and "3-second countdown" in out and "smile" in out.lower()
    assert svc.calls == []  # not while it is still talking
    items = takeover.claim(ctx)
    assert len(items) == 1 and items[0].label.startswith(PHOTO_LABEL) and items[0].takeover is True
    assert await items[0].run() is True
    assert svc.calls == [("photo", {"source": "voice", "who": "Ali", "countdown": None})]
    ctx.db.close()


async def test_a_photo_and_then_to_their_phone() -> None:
    skill, svc, ctx = _skill(SimpleNamespace(person_id=3, name="Ali", sure=True))
    out = await skill.run("take_photo", {"countdown": 5, "then_send": True})
    assert "5-second" in out and "phone" in out
    (item,) = takeover.claim(ctx)
    await item.run()
    assert ("send", ("20261106-143100", 3)) in svc.calls
    assert _said(ctx) == ["I sent it to your phone."]
    ctx.db.close()


async def test_someone_unsure_gets_the_familys_phone() -> None:
    skill, svc, ctx = _skill(SimpleNamespace(person_id=3, name="Ali", sure=False))
    svc.gallery.all = [_item("20261106-100000")]
    await skill.run("send_photo_to_phone", {})
    assert ("send", ("20261106-100000", None)) in svc.calls
    ctx.db.close()


async def test_refused_with_the_reason() -> None:
    skill, svc, ctx = _skill()
    svc.why = "camera_off"
    out = await skill.run("take_photo", {})
    assert out.startswith("error:") and "Privacy" in out and takeover.claim(ctx) == []
    out = await skill.run("start_recording", {})
    assert out.startswith("error:") and takeover.claim(ctx) == []
    ctx.db.close()


async def test_recording_starts_after_the_answer_and_stops_when_asked() -> None:
    skill, svc, ctx = _skill()
    out = await skill.run("start_recording", {})
    assert out.startswith("ok:") and "stop recording" in out and "10 minutes" in out and "with sound" in out
    (item,) = takeover.claim(ctx)
    assert item.label.startswith(VIDEO_LABEL) and item.takeover is True
    await item.run()
    assert svc.calls[-1][0] == "start" and "recording a video now" in skill.context_note()
    out = await skill.run("stop_recording", {})
    assert out.startswith("ok:") and "12 seconds" in out and svc.calls[-1] == ("stop", "asked by voice")
    out = await skill.run("stop_recording", {})
    assert "nothing was being recorded" in out
    ctx.db.close()


async def test_stop_before_the_recording_started_drops_it() -> None:
    skill, svc, ctx = _skill()
    await skill.run("start_recording", {})
    out = await skill.run("stop_recording", {})
    assert "hadn't started" in out and takeover.pending(ctx) == []
    ctx.db.close()


async def test_the_gallery_shows_at_once_and_a_video_plays_after_the_answer() -> None:
    skill, svc, ctx = _skill()
    out = await skill.run("show_photos", {"which": "videos", "slideshow": True})
    assert out.startswith("ok:") and svc.calls[-1] == ("show", {"kind": "video", "source": "voice", "slideshow": True})
    assert takeover.claim(ctx) == []
    svc.gallery.all = [_item("20261106-120000", "video", 8.0)]
    out = await skill.run("show_photos", {"play": True})
    assert "8 seconds" in out
    (item,) = takeover.claim(ctx)
    assert item.takeover is True
    await item.run()
    assert svc.calls[-1] == ("play", "20261106-120000")
    ctx.db.close()


async def test_no_photos_yet() -> None:
    skill, svc, ctx = _skill()
    svc.shown = {"ok": False, "error": "not_found"}
    assert "no photos or videos yet" in await skill.run("show_photos", {})
    assert "nothing to send" in (await skill.instant("send")).lower()
    assert "no videos yet" in await skill.run("show_photos", {"play": True})
    ctx.db.close()


async def test_sending_falls_back_to_the_code_on_the_screen() -> None:
    skill, svc, ctx = _skill()
    svc.gallery.all = [_item("20261106-100000")]
    svc.push = {"ok": False, "error": "not_set_up"}
    out = await skill.run("send_photo_to_phone", {"which": "latest"})
    assert out.startswith("ok:") and "QR code" in out and "no notification" in out
    assert svc.calls[-1] == ("code", "20261106-100000")
    assert "scan" in await skill.instant("send")
    ctx.db.close()


async def test_sending_without_lan_access_says_so() -> None:
    skill, svc, ctx = _skill()
    svc.gallery.all = [_item("20261106-100000")]
    svc.push = {"ok": False, "error": "lan_off"}
    out = await skill.run("send_photo_to_phone", {})
    assert out.startswith("error:") and "LAN access" in out
    ctx.db.close()


async def test_the_one_on_the_screen_is_the_one_sent() -> None:
    skill, svc, ctx = _skill()
    shown = _item("20261105-090000")
    svc.gallery.all = [_item("20261106-100000"), shown]
    ctx.screens = SimpleNamespace(current=SimpleNamespace(kind="gallery", item=shown, describe=lambda: "a photo"))
    await skill.run("send_photo_to_phone", {"which": "screen"})
    assert ("send", ("20261105-090000", None)) in svc.calls
    assert "On your screen: a photo." in skill.context_note()
    ctx.db.close()


async def test_instant_words() -> None:
    skill, svc, ctx = _skill()
    assert await skill.instant("photo") == "Okay, smile!"
    assert [i.label.split(" ")[0] for i in takeover.claim(ctx)] == ["photo"]
    assert (await skill.instant("record")).startswith("Recording!")
    takeover.claim(ctx)
    assert await skill.instant("stop_record") == "I'm not recording."
    svc.rec = {"id": "x", "seconds": 3.0}
    assert await skill.instant("stop_record") == "Saved. That's 12 seconds of video."
    assert await skill.instant("show") == "Here they are."
    svc.why = "disk_full"
    assert (await skill.instant("photo")).startswith("LittleBot's storage is nearly full")
    ctx.db.close()


async def test_the_hub_offers_it_only_with_the_service() -> None:
    skill, svc, ctx = _skill()
    hub = SkillHub(ctx, [skill])
    names = {s["name"] for s in hub.specs()}
    assert {"take_photo", "start_recording", "stop_recording", "show_photos", "send_photo_to_phone"} <= names
    assert "Never take a photo or record unless asked" in hub.prompt_notes()
    ctx.capture = None
    assert hub.specs() == [] or "take_photo" not in {s["name"] for s in hub.specs()}
    ctx.db.close()
    ctx2 = make_test_context()
    assert SkillHub(ctx2).get("camera") is not None
    ctx2.db.close()
