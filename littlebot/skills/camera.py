"""Photos and videos by voice: "take a picture", "record this", "stop recording", "show me my photos", "send that to
my phone".

The service is ``ctx.capture`` (littlebot/capture/service.py); this only checks, answers and hands the start over:

* a photo and a recording start once the answer was said (littlebot/skills/takeover.py, ``takeover=True``): the
  voice chat ends first, so the "3, 2, 1" and the shutter sound aren't talked over, and LittleBot doesn't listen
  while people are being filmed. "Hey LittleBot, stop recording" ends a recording.
* the gallery shows at once (a card, like any other: "next", "previous" work by voice while the chat goes on);
  playing a video waits for the answer (it needs the speaker).
* sending: a link to the one photo or video on LittleBot itself, as a phone notification to whoever asked (voice
  recognition, else the family's phones) and/or as a QR code on the screen. Nothing is uploaded.

:meth:`Camera.instant` answers the plain phrases without the model (littlebot/assistant/intents.py).
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from littlebot.skills import takeover
from littlebot.skills.base import BOOL, INT, Skill, ToolSpec, err, ok, tool

log = logging.getLogger("littlebot.skills.camera")

PHOTO_LABEL = "photo "
VIDEO_LABEL = "recording "
PLAY_LABEL = "gallery video "
WHICH = ("screen", "latest", "latest_photo", "latest_video")

# What the voice says when something can't be done (the service's error codes).
REFUSED = {
    "unavailable": "LittleBot can't take photos or videos.",
    "disabled": "photos and videos are switched off on the dashboard (Photos page).",
    "camera_off": "the camera is switched off in Privacy.",
    "no_camera": "LittleBot's camera isn't working right now.",
    "disk_full": "LittleBot's storage is nearly full: some photos or videos must be deleted first, on the dashboard.",
    "recording": "LittleBot is recording a video already.",
    "not_recording": "LittleBot isn't recording.",
    "no_encoder": "LittleBot can't record videos yet: ffmpeg isn't installed.",
    "no_player": "LittleBot can't play videos yet: ffplay isn't installed.",
    "not_found": "there are no photos or videos yet.",
    "no_screen": "LittleBot's screen can't show it right now.",
    "busy": "LittleBot's screen is busy with something more important right now.",
    "lan_off": "phones can't open LittleBot's links yet: a grown-up turns on LAN access first (the dashboard says how).",
    "not_set_up": "no phone is set up for LittleBot's notifications yet (dashboard: Assistant page, Calendars and "
                  "phones); a QR code on the screen works instead.",
    "cloud_off": "phone notifications need Cloud communication on (Privacy page); a QR code on the screen works "
                 "instead.",
    "offline": "LittleBot is offline, so it can't send a phone notification; a QR code on the screen works instead.",
}


def _minutes(seconds: float) -> str:
    s = int(round(max(0.0, float(seconds or 0.0))))
    if s < 60:
        return f"{s} second{'s' if s != 1 else ''}"
    m, s = divmod(s, 60)
    return f"{m} minute{'s' if m != 1 else ''}" + (f" {s} second{'s' if s != 1 else ''}" if s else "")


class Camera(Skill):
    name = "camera"

    def _svc(self) -> Any:
        return getattr(self.ctx, "capture", None)

    def available(self) -> bool:
        svc = self._svc()
        try:
            return svc is not None and svc.unavailable_reason() is None
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------ the model's side
    def tools(self) -> list[ToolSpec]:
        which = {"type": "string", "enum": list(WHICH),
                 "description": "screen: the one on LittleBot's screen now; latest (default): the newest of either"}
        return [
            tool("take_photo", "Take a photo with LittleBot's camera (a 3-2-1 countdown on the screen first; it's "
                 "saved in LittleBot's gallery). Only when someone asks for a photo / picture / selfie.",
                 {"countdown": {**INT, "description": "seconds to count down, 0-10 (leave out for the usual 3)"},
                  "then_send": {**BOOL, "description": "true when they also asked to send it to their phone"}}),
            tool("start_recording", "Start recording a video (picture and sound) with LittleBot's camera, saved in "
                 "the gallery. Only when someone asks to record / film / make a video."),
            tool("stop_recording", "Stop the video recording and save it."),
            tool("show_photos", "Show LittleBot's photo and video gallery on the screen (newest first), or play a "
                 "video from it.",
                 {"which": {"type": "string", "enum": ["latest", "photos", "videos", "all"],
                            "description": "photos / videos: only those; latest / all: everything, newest first"},
                  "slideshow": {**BOOL, "description": "go through them by themselves, every few seconds"},
                  "play": {**BOOL, "description": "play the newest video (or the one on the screen) full screen"}}),
            tool("send_photo_to_phone", "Send a photo or video from LittleBot's gallery to a phone: a notification "
                 "with a link that opens and saves it (over the home Wi-Fi), and/or a QR code on the screen.",
                 {"which": which,
                  "how": {"type": "string", "enum": ["phone", "qr", "both"],
                          "description": "phone (default): a notification; qr: a code on the screen to scan"}}),
        ]

    def prompt_note(self) -> str:
        return ("Camera: take_photo when asked for a photo / picture / selfie; start_recording to record or film a "
                "video, stop_recording to stop it; show_photos to see the photos and videos (or play one); "
                "send_photo_to_phone to send one to their phone (\"send that\" means the one on the screen). The "
                "photo and the recording start after you've spoken and this chat ends then: answer in a few words "
                "(\"Okay, smile!\", \"Recording!\"). Never take a photo or record unless asked. Deleting is on the "
                "dashboard's Photos page.")

    def context_note(self) -> str:
        svc = self._svc()
        if svc is None:
            return ""
        parts: list[str] = []
        try:
            rec = svc.recording
            if rec:
                sound = "with sound" if rec.get("sound") else "no sound"
                parts.append(f"Camera: recording a video now ({_minutes(rec.get('seconds') or 0)} so far, {sound}).")
            card = self._gallery_card()
            if card is not None and card.item is not None:
                parts.append(f"On your screen: {card.describe()}.")
            elif not rec:
                latest = svc.describe_latest()
                if latest and not latest.startswith("no "):
                    parts.append(f"Gallery: newest is {latest}.")
        except Exception:  # noqa: BLE001
            log.debug("camera: context note failed", exc_info=True)
        return " ".join(parts)

    async def run(self, name: str, args: dict[str, Any]) -> str:
        svc = self._svc()
        if svc is None:
            return err("LittleBot can't take photos or videos; say so")
        args = args or {}
        if name == "take_photo":
            return self._offer_photo(svc, args.get("countdown"), bool(args.get("then_send")))
        if name == "start_recording":
            return self._offer_video(svc)
        if name == "stop_recording":
            return await self._stop(svc)
        if name == "show_photos":
            return await self._show(svc, str(args.get("which") or "latest"), bool(args.get("slideshow")),
                                    bool(args.get("play")))
        if name == "send_photo_to_phone":
            return await self._send(svc, str(args.get("which") or "latest"), str(args.get("how") or "phone"))
        return err(f"camera has no tool called {str(name)[:40]}")

    # ------------------------------------------------------------ the instant phrases (no model)
    async def instant(self, what: str, *, kind: str | None = None) -> str:
        """For littlebot/assistant/intents.py: do ``what`` (photo, record, stop_record, show, send) and return the
        few words to say. ``kind``: "photo" / "video" when the words said which. A photo or a recording is only
        queued (littlebot/skills/takeover.py): the caller hands it to its host to start after the words."""
        svc = self._svc()
        if svc is None or not self.available():
            return "I can't take photos or videos."
        if what == "photo":
            res = self._offer_photo(svc, None, False)
            return "Okay, smile!" if res.startswith("ok") else _sorry(res)
        if what == "record":
            res = self._offer_video(svc)
            return "Recording! Say Hey LittleBot, stop recording, when you're done." if res.startswith("ok") \
                else _sorry(res)
        if what == "stop_record":
            dropped = takeover.drop(self.ctx, VIDEO_LABEL)
            if not svc.recording:
                return "Okay, I won't record." if dropped else "I'm not recording."
            res = await svc.stop_video("asked by voice")
            if not res.get("ok"):
                return "Sorry, the recording didn't work."
            return f"Saved. That's {_minutes(res.get('seconds') or 0)} of video" + \
                ("." if res.get("sound") else ", with no sound.")
        if what == "show":
            res = await svc.show_gallery(kind=kind, source="voice")
            if res.get("ok"):
                return "Here they are."
            code = str(res.get("error"))
            return "There are no photos or videos yet." if code == "not_found" else \
                _sorry(REFUSED.get(code, "I can't show them right now."))
        if what == "send":
            out = await self.send(svc, {"photo": "latest_photo", "video": "latest_video"}.get(kind or "", "screen"),
                                  "phone")
            if out.get("error") == "nothing":
                return "There's nothing to send yet."
            if out.get("error"):
                return _sorry(REFUSED.get(out["error"], str(out.get("detail") or "I couldn't send it.")))
            if out.get("pushed"):
                return "Sent to your phone." if out.get("where") == "own" else "Sent to the family's phones."
            return "I can't send a notification, so scan this code with your phone's camera."
        return "Okay."

    # ------------------------------------------------------------ the work
    def _who(self) -> tuple[int | None, str | None]:
        """(Who's who id, name) of whoever is talking, when voice or face recognition is sure."""
        svc = getattr(self.ctx, "assistant", None)
        try:
            who = svc.speaker() if svc is not None and hasattr(svc, "speaker") else None
        except Exception:  # noqa: BLE001
            who = None
        if who is None or not getattr(who, "sure", False):
            return None, None
        pid = getattr(who, "person_id", None)
        return (int(pid) if pid is not None else None), (str(getattr(who, "name", "") or "") or None)

    def _offer_photo(self, svc: Any, countdown: Any, then_send: bool) -> str:
        why = svc.check("photo")
        if why is not None:
            return err("say " + REFUSED.get(why, "the photo can't be taken right now."))
        n: int | None = None
        if countdown is not None:
            with contextlib.suppress(TypeError, ValueError):
                n = max(0, min(10, int(countdown)))
        to, name = self._who()

        async def snap() -> bool:
            res = await svc.take_photo(source="voice", who=name, countdown=n)
            if not res.get("ok"):
                self.announce("The photo didn't work: " + REFUSED.get(str(res.get("error")), "sorry."), kind="camera")
                return False
            if then_send:
                sent = await svc.send_to_phone(res["item"]["id"], to=to)
                if sent.get("ok"):
                    self.announce("I sent it to your phone." if sent.get("where") == "own" else
                                  "I sent it to the family's phones.", kind="camera")
                else:
                    self.announce("I couldn't send it: " + REFUSED.get(str(sent.get("error")),
                                                                       str(sent.get("detail") or "sorry.")),
                                  kind="camera")
            return True

        takeover.offer(self.ctx, f"{PHOTO_LABEL}{int(time.time())}", snap)
        secs = svc.cfg.countdown_seconds if n is None else n
        when = f"after a {secs}-second countdown on the screen" if secs else "straight away"
        return ok(f"the photo is taken as soon as you've finished speaking, {when} (this chat ends then). Say a few "
                  f"words like \"Okay, smile!\"" + (" and that you'll send it to their phone" if then_send else ""))

    def _offer_video(self, svc: Any) -> str:
        why = svc.check("video")
        if why is not None:
            return err("say " + REFUSED.get(why, "it can't record right now."))
        _to, name = self._who()

        async def record() -> bool:
            res = await svc.start_video(source="voice", who=name)
            if not res.get("ok"):
                self.announce("I couldn't start recording: " + REFUSED.get(str(res.get("error")), "sorry."),
                              kind="camera")
                return False
            return True

        takeover.offer(self.ctx, f"{VIDEO_LABEL}{int(time.time())}", record)
        limit = float(svc.cfg.max_video_minutes)
        sound = "with sound" if svc.video_sound and bool(self.ctx.privacy.microphone_enabled) else "without sound"
        return ok(f"recording ({sound}) starts as soon as you've finished speaking and this chat ends then; it "
                  f"stops by itself after {limit:g} minutes. Say \"Recording!\" and that \"Hey LittleBot, stop "
                  f"recording\" stops it, in a few words")

    async def _stop(self, svc: Any) -> str:
        dropped = takeover.drop(self.ctx, VIDEO_LABEL)
        if not svc.recording:
            return ok("it hadn't started yet; nothing is recorded. Say so") if dropped else \
                ok("nothing was being recorded; say so")
        res = await svc.stop_video("asked by voice")
        if not res.get("ok"):
            return err("say the recording didn't work: " + str(res.get("detail") or "something went wrong"))
        sound = "" if res.get("sound") else " (no sound)"
        return ok(f"saved: {_minutes(res.get('seconds') or 0)} of video{sound}. Say so in a few words; they can ask "
                  f"to watch it or to send it to their phone")

    async def _show(self, svc: Any, which: str, slideshow: bool, play: bool) -> str:
        if play:
            card = self._gallery_card()
            item = card.item if card is not None and card.item is not None and card.item.kind == "video" else \
                svc.gallery.latest("video")
            if item is None:
                return ok("there are no videos yet; say so")

            async def start() -> bool:
                res = await svc.play_video(item.id, source="voice")
                if not res.get("ok"):
                    self.announce("I can't play it: " + REFUSED.get(str(res.get("error")), "sorry."), kind="camera")
                return bool(res.get("ok"))

            takeover.offer(self.ctx, f"{PLAY_LABEL}{item.id}", start)
            return ok(f"the video ({_minutes(item.seconds or 0)}) plays full screen as soon as you've finished "
                      f"speaking (this chat ends then); say so in a few words")
        kind = {"photos": "photo", "videos": "video"}.get(which)
        res = await svc.show_gallery(kind=kind, source="voice", slideshow=slideshow)
        if not res.get("ok"):
            code = str(res.get("error"))
            return ok("there are no photos or videos yet; say so") if code == "not_found" else \
                err("say " + REFUSED.get(code, "the gallery can't show right now."))
        count = int(res.get("count") or 0)
        only = f" (there are no {which} yet, so all of them)" if kind and not svc.gallery.latest(kind) else ""
        return ok(f"showing {res.get('showing')}, the newest of {count}{only}. Swipe or say next / previous; "
                  f"\"play\" plays a video. Say so in a few words")

    async def _send(self, svc: Any, which: str, how: str) -> str:
        out = await self.send(svc, which, how)
        what = out.get("what") or "photo"
        if out.get("error") == "nothing":
            return ok("there's no photo or video to send yet; say so")
        if out.get("error"):
            return err("say " + REFUSED.get(out["error"], str(out.get("detail") or "it couldn't be sent.")))
        said: list[str] = []
        if out.get("pushed"):
            said.append(f"the {what} went to {'your own phone' if out.get('where') == 'own' else 'the family phones'} "
                        "as a notification: tap it, then save it (on the home Wi-Fi)")
        elif out.get("push_error"):
            said.append("no notification could go (" + REFUSED.get(out["push_error"], "it didn't send") + ")")
        if out.get("qr"):
            said.append(f"a QR code for the {what} is on your screen: their phone's camera opens it")
        return ok("; ".join(said) + ". Say so in a few words")

    async def send(self, svc: Any, which: str, how: str) -> dict[str, Any]:
        """Send one item: ``{what, pushed, where, push_error, qr, error, detail}``. A notification that can't go
        (no phone set up, cloud off) falls back to the QR code on the screen."""
        item = self._pick(svc, which)
        if item is None:
            return {"error": "nothing"}
        out: dict[str, Any] = {"what": "photo" if item.kind == "photo" else "video", "id": item.id}
        to, _name = self._who()
        if how in ("phone", "both"):
            res = await svc.send_to_phone(item.id, to=to)
            if res.get("ok"):
                out.update(pushed=True, where=res.get("where"))
            else:
                code = str(res.get("error"))
                if code not in ("not_set_up", "cloud_off", "offline", "push_failed"):
                    return {**out, "error": code, "detail": res.get("detail")}
                out["push_error"] = code
                how = "qr"  # the code on the screen still gets it to their phone
        if how in ("qr", "both"):
            res = await svc.show_share_code(item.id)
            if not res.get("ok"):
                return {**out, "error": str(res.get("error")), "detail": res.get("detail")}
            if not res.get("reachable"):
                return {**out, "error": "lan_off"}
            out["qr"] = True
        return out

    def _pick(self, svc: Any, which: str) -> Any:
        """The item ``which`` means: the one on the screen (screen, latest, or a kind it is), else the newest."""
        kind = {"latest_photo": "photo", "latest_video": "video"}.get(which)
        card = self._gallery_card()
        shown = card.item if card is not None else None
        if shown is not None and (which in ("screen", "latest") or shown.kind == kind):
            return shown
        return svc.gallery.latest(kind) or (svc.gallery.latest() if kind else None)

    def _gallery_card(self) -> Any:
        screens = getattr(self.ctx, "screens", None)
        try:
            card = screens.current if screens is not None else None
        except Exception:  # noqa: BLE001
            return None
        return card if card is not None and getattr(card, "kind", "") == "gallery" else None


def _sorry(result: str) -> str:
    """A tool's "error: say ..." (or just the words) as a sentence to say."""
    words = result.split(":", 1)[-1].strip() if result.startswith(("error:", "ok:")) else result.strip()
    words = words.removeprefix("say ").strip()
    if not words:
        return "Sorry, I can't right now."
    words = words[0].upper() + words[1:]
    return words if words.endswith((".", "!", "?")) else words + "."


__all__ = ["Camera"]
