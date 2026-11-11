"""Photos and videos by voice: "take a picture", "record this", "stop recording", "show me my photos", "send that to
my phone", "save that" (the last 3 minutes), "turn on / off always recording".

The service is ``ctx.capture`` (littlebot/capture/service.py); this only checks, answers and hands the start over:

* a photo and a recording start once the answer was said (littlebot/skills/takeover.py, ``takeover=True``): the
  voice chat ends first, so the "3, 2, 1" and the shutter sound aren't talked over, and LittleBot doesn't listen
  while people are being filmed. "Hey LittleBot, stop recording" ends a recording.
* the gallery shows at once (a card, like any other: "next", "previous" work by voice while the chat goes on);
  playing a video waits for the answer (it needs the speaker).
* sending: a link to the one photo or video on LittleBot itself, as a phone notification and/or as a QR code on
  the screen. Nothing is uploaded. Whose phone: the one they name ("to Ali's phone", "to my wife's",
  littlebot/capture/recipients.py), else whoever is talking: their voice first, and when that isn't sure, a fresh
  look with the camera (``AssistantService.identify_speaker``). When neither can tell, LittleBot asks whose phone
  ("Ali's or Sara's?") and the answer sends it (:meth:`Camera.reply`). Every phone only when they say so
  ("everyone", "the family").
* "save that" / "save the last minute": what the always-recording buffer holds (littlebot/capture/buffer.py: the
  last 3 minutes, in RAM only) becomes a video in the gallery, at once. "Turn on / off always recording" changes
  the setting ``capture.always_recording`` (on: on the battery too, until it is low; auto: while plugged in).

:meth:`Camera.instant` answers the plain phrases without the model (littlebot/assistant/intents.py).
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import re
import time
from typing import Any

from littlebot.capture.recipients import possessive, target, words_of
from littlebot.skills import takeover
from littlebot.skills.base import BOOL, INT, STR, Skill, ToolSpec, err, ok, tool

log = logging.getLogger("littlebot.skills.camera")

PHOTO_LABEL = "photo "
VIDEO_LABEL = "recording "
PLAY_LABEL = "gallery video "
WHICH = ("screen", "latest", "latest_photo", "latest_video")
ASK_FRESH_S = 120.0  # after "whose phone should I send it to?", an answer this soon is about it
REPLY_MAX_WORDS = 8  # longer than this is never just the answer to "whose phone?"
_YES = re.compile(r"^\s*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please|go ahead|do it|yes please|sure thing|that'?s right|"
                  r"correct|right)(?:[\s,.!]+(?:please|thanks?(?: you)?|do it|send it))*[\s,.!]*$", re.I)
_NO = re.compile(r"^\s*(?:no|nope|no thanks?|no thank you|don'?t(?: send it)?|not now|nobody|no one|neither)"
                 r"[\s,.!]*$", re.I)


@dataclasses.dataclass
class Whose:
    """Where "send it" goes (:meth:`Camera.whose`)."""

    to: int | None = None  # a Who's who id (None: with ``everyone``, every phone)
    name: str = ""  # their name ("Ali")
    everyone: bool = False  # they asked for every phone
    yours: bool = False  # the phone of the one talking
    ask: list[str] | None = None  # nobody could tell: the choices to ask about ("Ali's", "the family phone")
    unknown: str = ""  # the words of a name or a role nobody on Who's who has
    no_phone: bool = False  # that person's phone isn't set up (nor the family's)
    none: bool = False  # no phone is set up at all

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
    "nothing_buffered": "there's nothing to save: always recording isn't running.",
    "no_phone": "{name}'s phone isn't set up for LittleBot's notifications yet (it's added on the dashboard: "
                "Assistant page, Calendars and phones); a QR code on the screen works instead.",
}


def _refused(code: str, name: str = "", default: str = "sorry.") -> str:
    words = REFUSED.get(code, default)
    return words.replace("{name}", name or "That person") if "{name}" in words else words


def _choice_words(choices: list[str]) -> str:
    """["Ali's", "Sara's", "the family phone"] -> "Ali's, Sara's or the family phone"."""
    return choices[0] if len(choices) == 1 else ", ".join(choices[:-1]) + " or " + choices[-1]


def ask_whose(choices: list[str], unknown: str = "") -> str:
    """The question when LittleBot can't tell whose phone ("Whose phone should I send it to: Ali's or Sara's?")."""
    lead = f"I don't know who {unknown} is. " if unknown else ""
    if len(choices) == 1:
        one = choices[0]
        return lead + f"Should I send it to {one if one.startswith('the ') else one + ' phone'}?"
    return lead + f"Whose phone should I send it to: {_choice_words(choices)}?"


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
        to = {**STR, "description": "whose phone, in their own words: a name (\"Ali\"), \"my wife\", \"my husband\", "
                                    "\"dad\", \"everyone\". Leave it out for their own phone (\"my phone\")"}
        return [
            tool("take_photo", "Take a photo with LittleBot's camera (a 3-2-1 countdown on the screen first; it's "
                 "saved in LittleBot's gallery). Only when someone asks for a photo / picture / selfie.",
                 {"countdown": {**INT, "description": "seconds to count down, 0-10 (leave out for the usual 3)"},
                  "then_send": {**BOOL, "description": "true when they also asked to send it to a phone"},
                  "to": to}),
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
                          "description": "phone (default): a notification; qr: a code on the screen to scan"},
                  "to": to}),
            tool("save_last_minutes", "Save what just happened: the last few minutes of video (up to 3, with "
                 "sound) that LittleBot's always-recording buffer holds become a video in the gallery. Only when "
                 "someone asks to save / keep / clip what just happened (\"save that\", \"save the last minute\").",
                 {"minutes": {"type": "number", "description": "how many of the last minutes (0.5 = 30 seconds); "
                                                               "leave out for everything it holds"}}),
            tool("set_always_recording", "Change LittleBot's always-recording buffer (the last 3 minutes of video "
                 "kept in memory only, saved only when asked). Only when someone asks to turn it on or off.",
                 {"mode": {"type": "string", "enum": ["auto", "on", "off"],
                           "description": "on: also on the battery (until it is low); off; auto (the usual): "
                                          "only while plugged in"}}),
        ]

    def prompt_note(self) -> str:
        return ("Camera: take_photo when asked for a photo / picture / selfie; start_recording to record or film a "
                "video, stop_recording to stop it; show_photos to see the photos and videos (or play one); "
                "send_photo_to_phone to send one to a phone (\"send that\" means the one on the screen; to: whose "
                "phone in their words, \"my wife\", \"Ali\", left out for their own). LittleBot works out who is "
                "talking itself (voice, then the camera) and says when it must ask whose phone. The "
                "photo and the recording start after you've spoken and this chat ends then: answer in a few words "
                "(\"Okay, smile!\", \"Recording!\"). Never take a photo or record unless asked. Deleting is on the "
                "dashboard's Photos page. save_last_minutes when they want to keep what just happened (\"save "
                "that\", \"clip that\"): LittleBot keeps the last 3 minutes of video in memory only (a small dot "
                "on its face shows it) and forgets them unless asked; set_always_recording turns that on or off.")

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
            buf = svc.status_buffer() if hasattr(svc, "status_buffer") else None
            if buf and buf.get("running"):
                parts.append(f"Always recording: on ({_minutes(buf.get('seconds') or 0)} can be saved now).")
            asked = self._pending()
            if asked is not None:
                then = ("take_photo with then_send true and to" if asked.get("photo_first") else
                        "send_photo_to_phone with to")
                parts.append(f"You asked whose phone to send it to ({_choice_words(asked['choices'])}): when they "
                             f"answer, call {then} = what they said.")
        except Exception:  # noqa: BLE001
            log.debug("camera: context note failed", exc_info=True)
        return " ".join(parts)

    async def run(self, name: str, args: dict[str, Any]) -> str:
        svc = self._svc()
        if svc is None:
            return err("LittleBot can't take photos or videos; say so")
        args = args or {}
        to = str(args.get("to") or "").strip()[:60] or None
        if name == "take_photo":
            return await self._offer_photo(svc, args.get("countdown"), bool(args.get("then_send")), to)
        if name == "start_recording":
            return self._offer_video(svc)
        if name == "stop_recording":
            return await self._stop(svc)
        if name == "show_photos":
            return await self._show(svc, str(args.get("which") or "latest"), bool(args.get("slideshow")),
                                    bool(args.get("play")))
        if name == "send_photo_to_phone":
            return await self._send(svc, str(args.get("which") or "latest"), str(args.get("how") or "phone"), to)
        if name == "save_last_minutes":
            return await self._save_clip(svc, args.get("minutes"))
        if name == "set_always_recording":
            res = await svc.set_always_recording(str(args.get("mode") or "").strip().lower(), source="voice")
            if not res.get("ok"):
                return err("say it must be on, off or auto")
            return ok(f"say this in a few words: {_always_words(res)}")
        return err(f"camera has no tool called {str(name)[:40]}")

    # ------------------------------------------------------------ the instant phrases (no model)
    async def instant(self, what: str, *, kind: str | None = None, to: str | None = None,
                      seconds: float | None = None) -> str:
        """For littlebot/assistant/intents.py: do ``what`` (photo, record, stop_record, show, send, save_clip,
        buffer_on, buffer_off) and return the few words to say. ``kind``: "photo" / "video" when the words said
        which; ``to``: whose phone, in their words ("my", "my wife's", "Ali's"); ``seconds``: how much of the
        always-recording buffer "save the last minute" asked for. A photo or a recording is only queued
        (littlebot/skills/takeover.py): the caller hands it to its host to start after the words."""
        svc = self._svc()
        if svc is None or not self.available():
            return "I can't take photos or videos."
        if what == "photo":
            res = await self._offer_photo(svc, None, False)
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
                                  "phone", to)
            return _sent_words(out)
        if what == "save_clip":
            _to, name = self._who()
            res = await svc.save_buffer(seconds, source="voice", who=name)
            if res.get("ok"):
                return str(res.get("words") or "Saved.")
            return str(res.get("detail") or "") if res.get("error") == "nothing_buffered" else \
                _sorry(REFUSED.get(str(res.get("error")), "I couldn't save it."))
        if what in ("buffer_on", "buffer_off"):
            res = await svc.set_always_recording("on" if what == "buffer_on" else "off", source="voice")
            return _always_words(res) if res.get("ok") else "I can't change that right now."
        return "Okay."

    # ------------------------------------------------------------ the answer to "whose phone?"
    def _pending(self) -> dict[str, Any] | None:
        asked = getattr(self, "_asked", None)
        if asked is None or time.monotonic() - float(asked["at"]) > ASK_FRESH_S:
            return None
        return asked

    def asking(self) -> bool:
        """LittleBot just asked whose phone to send a photo or video to (an answer now is :meth:`reply`'s)."""
        return self._pending() is not None

    async def reply(self, text: str) -> str | None:
        """Their answer to "whose phone should I send it to?" ("Ali's", "mine", "my wife's", "everyone", "yes" to
        a single choice, "no"): sent, and the few words to say; None when ``text`` isn't such an answer (the brain
        takes it, with the question in its context)."""
        asked = self._pending()
        svc = self._svc()
        if asked is None or svc is None or len(str(text or "").split()) > REPLY_MAX_WORDS:
            return None
        if _NO.match(text):
            self._asked = None
            return "Okay, I won't send it."
        said = text
        if _YES.match(text):
            if len(asked["choices"]) != 1:
                return None
            said = asked["says"][0]
        elif not words_of(text) or target(text, self._people(), is_child=self._child_test()).kind in (
                "unknown", "unclear"):
            return None
        log.info("camera: they answered whose phone")
        if not asked.get("photo_first"):
            return _sent_words(await self.send(svc, "pending", str(asked.get("how") or "phone"), said))
        whose = await self.whose(svc, said)
        if whose.ask is not None:
            self._remember_ask(whose, photo_first=True, countdown=asked.get("countdown"))
            return ask_whose(whose.ask, whose.unknown)
        self._asked = None
        res = await self._offer_photo(svc, asked.get("countdown"), True, whose=whose)
        return "Okay, smile!" if res.startswith("ok") else _sorry(res)

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

    async def _identify(self) -> Any:
        """Who is talking, for "send it to my phone": their voice first, else a fresh look with the camera
        (``AssistantService.identify_speaker``). A sure ``assistant.who.Who``, or None."""
        svc = getattr(self.ctx, "assistant", None)
        ident = getattr(svc, "identify_speaker", None)
        if callable(ident):
            try:
                return await ident()
            except Exception:  # noqa: BLE001
                log.debug("camera: identifying the speaker failed", exc_info=True)
                return None
        pid, _name = self._who()
        if pid is not None:
            with contextlib.suppress(Exception):
                return svc.speaker()
        from littlebot.assistant.who import by_face, child_test, look_now  # noqa: PLC0415

        people = await look_now(self.ctx)
        return by_face(people, None, child_test(self.ctx)) if people is not None else None

    def _people(self) -> list[dict[str, Any]]:
        """Who's who (id, name, role), for whose phone."""
        try:
            from littlebot.vision.faces import get_faces  # noqa: PLC0415

            return list(get_faces(self.ctx).store.people())
        except Exception:  # noqa: BLE001
            return []

    def _child_test(self) -> Any:
        from littlebot.assistant.who import child_test  # noqa: PLC0415

        return child_test(self.ctx)

    async def whose(self, svc: Any, said: str | None) -> Whose:
        """Where "send it" goes: the phone ``said`` names (littlebot/capture/recipients.py), or for "my phone" (and
        nothing said) that of whoever is talking (voice, else a look with the camera). Every phone only when they
        said so. When nobody can tell: ``ask`` (the phones to choose from), never every phone."""
        try:
            phones = list(svc.phones())
        except Exception:  # noqa: BLE001
            phones = []
        if not phones:
            return Whose(none=True)
        people, child = self._people(), self._child_test()
        t = target(said or "", people, is_child=child)
        speaker = None
        if t.kind in ("me", "needs_speaker"):
            speaker = await self._identify()
            if speaker is not None:
                t = target(said or "", people, speaker_id=int(speaker.person_id), is_child=child)
        if t.kind == "everyone":
            return Whose(everyone=True)
        if t.kind == "me" and speaker is not None:  # (recognised, but not on the list the recipients read)
            t = dataclasses.replace(t, kind="person", person={"id": int(speaker.person_id), "name": str(speaker.name),
                                                              "role": str(getattr(speaker, "role", "") or "")})
        if t.kind == "person" and t.person is not None:
            pid, name = int(t.person["id"]), str(t.person["name"])
            talking = int(speaker.person_id) if speaker is not None else self._who()[0]
            own = {int(p["id"]) for p in phones if p.get("id") is not None}
            family = any(p.get("id") is None for p in phones)
            return Whose(to=pid, name=name, yours=talking == pid, no_phone=pid not in own and not family)
        choices = [("the family phone" if p.get("id") is None else possessive(str(p["name"]))) for p in phones]
        unknown = " ".join(words_of(said or "")) if t.kind == "unknown" else ""
        log.info("camera: can't tell whose phone (%s); asking", t.kind)
        return Whose(ask=choices, unknown=unknown)

    def _remember_ask(self, whose: Whose, *, item: str | None = None, how: str = "phone", photo_first: bool = False,
                      countdown: Any = None) -> None:
        """LittleBot is asking whose phone: an answer in the next ASK_FRESH_S sends it (:meth:`reply`)."""
        choices = list(whose.ask or [])
        says = ["family" if c == "the family phone" else c.removesuffix("'s") for c in choices]
        self._asked = {"at": time.monotonic(), "item": item, "how": how, "choices": choices, "says": says,
                       "photo_first": photo_first, "countdown": countdown}

    async def _offer_photo(self, svc: Any, countdown: Any, then_send: bool, to: str | None = None, *,
                           whose: Whose | None = None) -> str:
        why = svc.check("photo")
        if why is not None:
            return err("say " + REFUSED.get(why, "the photo can't be taken right now."))
        n: int | None = None
        if countdown is not None:
            with contextlib.suppress(TypeError, ValueError):
                n = max(0, min(10, int(countdown)))
        _talking, name = self._who()
        if then_send and whose is None:
            whose = await self.whose(svc, to)
            if whose.ask is not None:  # whose phone first: the chat ends when the photo starts
                self._remember_ask(whose, photo_first=True, countdown=n)
                return ok(("they named " + whose.unknown + ", who isn't on Who's who; " if whose.unknown else
                           "LittleBot can't tell who is asking (voice and face recognition weren't sure); ") +
                          f"no photo yet. Ask whose phone it should go to ({_choice_words(whose.ask)}) in a few "
                          "words; when they answer, call take_photo again with then_send true and to = what they "
                          "said")
            self._asked = None

        async def snap() -> bool:
            res = await svc.take_photo(source="voice", who=name, countdown=n)
            if not res.get("ok"):
                self.announce("The photo didn't work: " + REFUSED.get(str(res.get("error")), "sorry."), kind="camera")
                return False
            if then_send and whose is not None:
                self.announce(await self._push(svc, res["item"]["id"], whose), kind="camera")
            return True

        takeover.offer(self.ctx, f"{PHOTO_LABEL}{int(time.time())}", snap)
        secs = svc.cfg.countdown_seconds if n is None else n
        when = f"after a {secs}-second countdown on the screen" if secs else "straight away"
        where = "" if whose is None else (" and that you'll send it to " + _where(whose))
        return ok(f"the photo is taken as soon as you've finished speaking, {when} (this chat ends then). Say a few "
                  f"words like \"Okay, smile!\"" + where)

    async def _push(self, svc: Any, item_id: str, whose: Whose) -> str:
        """The notification to ``whose`` phone once a photo is taken: what to announce."""
        if whose.none or whose.no_phone:
            code = "not_set_up" if whose.none else "no_phone"
            return "I couldn't send it: " + _refused(code, whose.name).split(";")[0] + "."
        sent = await svc.send_to_phone(item_id, to=None if whose.everyone else whose.to)
        if sent.get("ok"):
            return "I sent it to " + _where(whose, sent.get("where")) + "."
        return "I couldn't send it: " + _refused(str(sent.get("error")), whose.name, str(sent.get("detail") or
                                                                                      "sorry."))

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

    async def _save_clip(self, svc: Any, minutes: Any) -> str:
        """The always-recording buffer's last ``minutes`` (default: all of it) into the gallery, now."""
        seconds = None
        if minutes not in (None, ""):
            try:
                seconds = max(10.0, float(minutes) * 60.0)
            except (TypeError, ValueError):
                seconds = None
        _to, name = self._who()
        res = await svc.save_buffer(seconds, source="voice", who=name)
        if not res.get("ok"):
            code = str(res.get("error"))
            words = str(res.get("detail") or "") if code == "nothing_buffered" else REFUSED.get(code, "")
            return err("nothing was saved; say " + (words or "it couldn't be saved."))
        sound = "with sound" if res.get("sound") else "without sound"
        return ok(f"a {_minutes(res.get('seconds') or 0)} video ({sound}) is in the gallery now; say "
                  f"\"{res.get('words')}\" in a few words")

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

    async def _send(self, svc: Any, which: str, how: str, to: str | None = None) -> str:
        out = await self.send(svc, which, how, to)
        what = out.get("what") or "photo"
        if out.get("error") == "nothing":
            return ok("there's no photo or video to send yet; say so")
        if out.get("error"):
            return err("say " + REFUSED.get(out["error"], str(out.get("detail") or "it couldn't be sent.")))
        said: list[str] = []
        if out.get("ask") is not None:
            said.append(("they named " + out["unknown"] + ", who isn't on Who's who, so " if out.get("unknown") else
                         "LittleBot can't tell who is asking (voice and face recognition weren't sure), so ") +
                        f"nothing went to a phone yet. Ask whose phone the {what} should go to "
                        f"({_choice_words(out['ask'])}); when they answer, call send_photo_to_phone again with to = "
                        "what they said")
        elif out.get("pushed"):
            said.append(f"the {what} went to {_where(out['whose'], out.get('where'))} as a notification: tap it, "
                        "then save it (on the home Wi-Fi)")
        elif out.get("push_error"):
            said.append("no notification could go (" + _refused(out["push_error"], out.get("name", ""),
                                                                "it didn't send") + ")")
        if out.get("qr"):
            said.append(f"a QR code for the {what} is on your screen: their phone's camera opens it")
        return ok("; ".join(said) + ". Say so in a few words")

    async def send(self, svc: Any, which: str, how: str, to: str | None = None) -> dict[str, Any]:
        """Send one item: ``{what, id, pushed, where, whose, push_error, name, ask, unknown, qr, error, detail}``.
        ``to``: whose phone, in their words (:meth:`whose`); ``which`` "pending": the one LittleBot asked about.
        A notification that can't go (no phone set up, cloud off) falls back to the QR code on the screen; when
        nobody can tell whose phone, ``ask`` holds the choices and nothing is sent (a QR code still shows with
        ``how`` "both")."""
        asked = self._pending()
        item = None
        if asked is not None and asked.get("item") and (which == "pending" or to and which in ("screen", "latest")):
            item = svc.gallery.get(asked["item"])
        if item is None:
            item = self._pick(svc, "screen" if which == "pending" else which)
        if item is None:
            return {"error": "nothing"}
        out: dict[str, Any] = {"what": "photo" if item.kind == "photo" else "video", "id": item.id}
        if how in ("phone", "both"):
            whose = await self.whose(svc, to)
            out["whose"], out["name"] = whose, whose.name
            if whose.ask is not None:
                self._remember_ask(whose, item=item.id, how=how)
                out.update(ask=whose.ask, unknown=whose.unknown)
                if how == "phone":
                    return out
                how = "qr"
            elif whose.none or whose.no_phone:
                out["push_error"] = "not_set_up" if whose.none else "no_phone"
                how = "qr"  # the code on the screen still gets it to their phone
            else:
                res = await svc.send_to_phone(item.id, to=None if whose.everyone else whose.to)
                if res.get("ok"):
                    self._asked = None
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


def _where(whose: Whose, where: str | None = None) -> str:
    """ "your phone", "Sara's phone", "everyone's phones" ("family": that person's own isn't set up, so the
    family's phone got it)."""
    if whose.everyone:
        return "everyone's phones"
    if where == "family":
        return "the family phone"
    if whose.yours:
        return "your phone"
    return possessive(whose.name) + " phone" if whose.name else "the phone"


def _sent_words(out: dict[str, Any]) -> str:
    """What to say after :meth:`Camera.send` (an instant phrase, or the answer to "whose phone?")."""
    if out.get("error") == "nothing":
        return "There's nothing to send yet."
    if out.get("error"):
        return _sorry(REFUSED.get(out["error"], str(out.get("detail") or "I couldn't send it.")))
    if out.get("ask") is not None:
        return ask_whose(out["ask"], out.get("unknown") or "")
    if out.get("pushed"):
        return "Sent to " + _where(out["whose"], out.get("where")) + "."
    if out.get("push_error") == "no_phone":
        return f"{possessive(out.get('name') or 'That person')} phone isn't set up for me yet, so scan this code " \
               "with a phone's camera."
    return "I can't send a notification, so scan this code with your phone's camera."


def _always_words(res: dict[str, Any]) -> str:
    """What to say after "turn on / off always recording" (``CaptureService.set_always_recording``'s answer)."""
    asked, now = str(res.get("asked")), str(res.get("setting"))
    going = res.get("state") in ("running", "no_frames")  # (no_frames: just switched on, the camera is starting)
    if asked == "off":
        return "Always recording is off."
    if asked == "on" and now != "on":
        return "My battery is too low for always recording."
    if not going:
        why = str(res.get("detail") or "").rstrip(".")
        return f"Always recording is {'on' if asked == 'on' else 'set to automatic'}, but it isn't running now" + \
            (f": {why[0].lower()}{why[1:]}." if why else ".")
    if asked == "on":
        return "Always recording is on, until my battery is low." if res.get("on_battery") else \
            "Always recording is on."
    return "Always recording is on while I'm plugged in."


def _sorry(result: str) -> str:
    """A tool's "error: say ..." (or just the words) as a sentence to say."""
    words = result.split(":", 1)[-1].strip() if result.startswith(("error:", "ok:")) else result.strip()
    words = words.removeprefix("say ").strip()
    if not words:
        return "Sorry, I can't right now."
    words = words[0].upper() + words[1:]
    return words if words.endswith((".", "!", "?")) else words + "."


__all__ = ["Camera"]
