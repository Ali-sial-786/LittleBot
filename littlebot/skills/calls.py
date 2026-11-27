"""WhatsApp calls by voice: "video call Dad", "call Mum on WhatsApp", "open WhatsApp", "answer on WhatsApp",
"hang up", "close WhatsApp".

The tools (and the instant phrases, littlebot/assistant/intents.py, through ``instant``) only check and answer; the
opening itself waits until the answer is said (littlebot/skills/takeover.py, ``takeover=False``: WhatsApp takes the
screen, not LittleBot's speaker). Once a call is on, the service ends the voice chat itself so LittleBot's voice
doesn't talk into it; "Hey LittleBot, hang up" still works. What goes wrong later (WhatsApp isn't linked, a button
wasn't found) is said by the service. The service is ``ctx.calls`` (littlebot/calls/service.py).

Only a grown-up: a turn voice recognition says was the child's is refused, as for the browser. Only people on the
parent's list (dashboard, Assistant page) can be called. Nothing of WhatsApp's chats ever reaches the voice.
"""

from __future__ import annotations

from typing import Any

from littlebot.skills import takeover
from littlebot.skills.base import BOOL, STR, Skill, ToolSpec, err, ok, tool

LABEL = "whatsapp"

REFUSED = {
    "unavailable": "LittleBot can't make WhatsApp calls.",
    "disabled": "WhatsApp calls are switched off on the dashboard.",
    "cloud_off": "Cloud communication is switched off in Privacy, so WhatsApp can't open.",
    "microphone_off": "The microphone is switched off in Privacy, so WhatsApp can't open.",
    "camera_off": "The camera is switched off in Privacy. Ask for a voice call instead.",
    "child": "Only a grown-up can make a call.",
    "not_ready": "LittleBot has a problem right now, so WhatsApp can't open.",
    "low_memory": "There isn't enough free memory for WhatsApp right now. Try again in a moment.",
    "busy": "My screen is busy right now, so I can't open WhatsApp.",
    "failed": "WhatsApp didn't open.",
    "no_contacts": "Nobody is on my list of people to call yet. A grown-up can add them on the dashboard.",
    "in_call": "A call is on already.",
}


class Calls(Skill):
    name = "calls"

    def _svc(self) -> Any:
        return getattr(self.ctx, "calls", None)

    def available(self) -> bool:
        svc = self._svc()
        try:
            return svc is not None and bool(svc.offered())
        except Exception:  # noqa: BLE001
            return False

    def _names(self) -> list[str]:
        svc = self._svc()
        try:
            return list(svc.contacts()) if svc is not None else []
        except Exception:  # noqa: BLE001
            return []

    def knows(self, said: Any) -> bool:
        """``said`` is someone on the parent's list ("call Dad" is then a WhatsApp call, not a question)."""
        svc = self._svc()
        try:
            return svc is not None and svc.find_contact(said) is not None
        except Exception:  # noqa: BLE001
            return False

    @property
    def active(self) -> bool:
        """WhatsApp is on the screen ("hang up", "answer" are about it)."""
        svc = self._svc()
        return svc is not None and getattr(svc, "active", False) is True

    def tools(self) -> list[ToolSpec]:
        names = self._names()
        who = ", ".join(names) if names else "nobody yet (a grown-up adds people on the dashboard)"
        return [
            tool("whatsapp_call", "Call someone on WhatsApp from LittleBot's screen (a video call unless they asked "
                 "for a voice call). Only when a grown-up asks to call one of these people: " + who + ". Say what "
                 "the result says.",
                 {"name": {**STR, "description": "who to call, as on the list"},
                  "kind": {"type": "string", "enum": ["video", "voice"], "description": "video (the default) or voice"}},
                 ("name",)),
            tool("open_whatsapp", "Open WhatsApp on LittleBot's screen, to answer a call that is ringing on the "
                 "phone or to link it. Only when a grown-up asks for WhatsApp. Say what the result says.",
                 {"answer": {**BOOL, "description": "true when they want to answer a call that is ringing"}}),
            tool("close_whatsapp", "Hang up the WhatsApp call and close WhatsApp; LittleBot's face comes back. When "
                 "they say hang up, end the call or close WhatsApp."),
        ]

    def prompt_note(self) -> str:
        return ("WhatsApp calls: only when a grown-up asks to call someone on the list, call whatsapp_call; "
                "\"open WhatsApp\" or \"answer on WhatsApp\": open_whatsapp; \"hang up\", \"end the call\", \"close "
                "WhatsApp\": close_whatsapp. Say what the result says, briefly. You never see, read or send "
                "WhatsApp messages, and never offer to.")

    def context_note(self) -> str:
        svc = self._svc()
        try:
            st = svc.status() if svc is not None else {}
        except Exception:  # noqa: BLE001
            return ""
        if not st.get("active"):
            return ""
        if st.get("call") == "in_call":
            return "WhatsApp: a call is on, on your screen (close_whatsapp hangs up)."
        if st.get("call") == "incoming":
            return "WhatsApp: a call is ringing on your screen (open_whatsapp with answer answers it)."
        if st.get("state") == "linking":
            return "WhatsApp: its QR code is on your screen, waiting to be scanned with the phone."
        return "WhatsApp: open on your screen."

    def hearing_words(self) -> list[str]:
        return ["WhatsApp", *self._names()]

    async def run(self, name: str, args: dict[str, Any]) -> str:
        args = args or {}
        if name == "close_whatsapp":
            return ok(await self.instant("hang_up") or "the call is ended and WhatsApp is closed; say so in a few "
                                                       "words.")
        if name == "open_whatsapp":
            words = await self.instant("answer" if args.get("answer") else "open")
        elif name == "whatsapp_call":
            words = await self.instant("call", name=str(args.get("name") or ""), kind=str(args.get("kind") or "video"))
        else:
            return err(f"calls has no tool called {str(name)[:40]}")
        return ok(f"say: {words}") if self._last_ok else err(f"say: {words}")

    _last_ok = True

    async def instant(self, what: str, *, name: str = "", kind: str = "video") -> str:
        """An instant phrase or a tool: ``what`` is call | open | answer | hang_up. Returns the words to say ("" for
        a hang-up that worked: nothing is said into the end of a call)."""
        self._last_ok = True
        svc = self._svc()
        if svc is None or not self.available():
            return self._no(REFUSED["unavailable"])
        if what == "hang_up":
            dropped = takeover.drop(self.ctx, LABEL)
            res = await svc.hang_up("asked in the voice chat")
            if res.get("was_call"):
                return ""
            return "WhatsApp is closed." if res.get("closed") or dropped else "WhatsApp isn't open."
        said: str | None = None
        if what == "call":
            people = svc.contacts()
            if not people:
                return self._no(REFUSED["no_contacts"])
            found = svc.find_contact(name)
            if found is None:
                from littlebot.calls.contacts import say_who  # noqa: PLC0415

                return self._no(f"I can only call {say_who(people)}.")
            said = found[0]
        voice = str(kind).lower() in ("voice", "audio", "phone")
        why = svc.check("voice", kind=None if said is None or voice else "video")
        if why is not None:
            return self._no(REFUSED.get(why, REFUSED["failed"]))
        answer = what == "answer"

        async def start() -> bool:
            res = await svc.open(source="voice", contact=said, kind="voice" if voice else "video", answer=answer)
            if not res.get("ok"):
                self.announce(REFUSED.get(str(res.get("error")), REFUSED["failed"]), kind="calls")
            return bool(res.get("ok"))

        if svc.active:  # open already: the request goes straight to it
            res = await svc.open(source="voice", contact=said, kind="voice" if voice else "video", answer=answer)
            if not res.get("ok"):
                return self._no(REFUSED.get(str(res.get("error")), REFUSED["failed"]))
        else:
            takeover.offer(self.ctx, f"{LABEL} {what} {said or ''}".strip(), start, takeover=False)
        if said is not None:
            return f"Calling {said} on WhatsApp."
        return "Opening WhatsApp to answer." if answer else "Opening WhatsApp."

    def _no(self, words: str) -> str:
        self._last_ok = False
        return words


__all__ = ["Calls"]
