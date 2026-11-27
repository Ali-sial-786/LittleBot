"""Phone mirroring by voice: "mirror my iPhone", "show my Android phone on your screen", "stop mirroring".

The tools only check and answer; the start itself waits until the answer is said (littlebot/skills/takeover.py,
``takeover=False``: mirroring takes the screen, never LittleBot's speaker, so the voice chat carries on and
"stop mirroring" still works). The words the voice says come back in the result: for an iPhone the three steps on
the phone, for an Android phone that it must be paired once on the dashboard. The service is ``ctx.mirror``
(littlebot/mirror/service.py).
"""

from __future__ import annotations

from typing import Any

from littlebot.skills import takeover
from littlebot.skills.base import Skill, ToolSpec, err, ok, tool

LABEL = "mirror "

IPHONE_STEPS = ("On the iPhone: open Control Center (swipe down from the top-right corner), tap Screen Mirroring, "
                "and pick {name}.")
SAY = {
    "iphone": "say it's ready, then the steps: " + IPHONE_STEPS + " It waits about {wait} seconds for the phone.",
    "android": ("say their Android phone's screen is coming up on your screen now (Wireless debugging must be on, on "
                "the same Wi-Fi). If it doesn't come, it must be paired once on the dashboard: Assistant page, Phone "
                "mirroring."),
}
REFUSED = {
    "not_installed": "say mirroring that phone isn't set up on LittleBot yet: a grown-up installs it with "
                     "scripts/setup_mirroring.sh (the dashboard shows the command).",
    "not_paired": "say the Android phone has to be paired once on the dashboard first: Assistant page, Phone "
                  "mirroring, Pair (on the phone: Developer options, Wireless debugging, Pair device with pairing code).",
    "disabled": "say phone mirroring is switched off on the dashboard.",
    "unavailable": "say LittleBot can't show a phone's screen.",
    "asleep": "say LittleBot is asleep.",
    "bad_kind": "ask whether it's an iPhone or an Android phone.",
}


class Mirror(Skill):
    name = "mirror"

    def _svc(self) -> Any:
        return getattr(self.ctx, "mirror", None)

    def available(self) -> bool:
        svc = self._svc()
        try:
            return svc is not None and bool(svc.offered())
        except Exception:  # noqa: BLE001
            return False

    def tools(self) -> list[ToolSpec]:
        return [
            tool("mirror_phone", "Show a phone's screen on LittleBot's screen (screen mirroring / casting). Only when "
                 "someone asks to mirror, cast or show their phone's screen. Say what the result says.",
                 {"phone": {"type": "string", "enum": ["iphone", "android"],
                            "description": "iphone (also an iPad) or android"}}, ("phone",)),
            tool("stop_mirroring", "Stop showing the phone's screen; LittleBot's face comes back."),
        ]

    def prompt_note(self) -> str:
        return ("Phone mirroring: only when someone asks to mirror, cast or show their phone's screen on yours, call "
                "mirror_phone (ask iPhone or Android if you can't tell) and say what its result says; stop_mirroring "
                "when they want it off. Never offer it otherwise.")

    def context_note(self) -> str:
        svc = self._svc()
        try:
            st = svc.status() if svc is not None else {}
        except Exception:  # noqa: BLE001
            return ""
        if not st.get("active"):
            return ""
        phone = "an iPhone" if st.get("kind") == "iphone" else "an Android phone"
        if st.get("state") == "waiting":
            return f"Mirroring: waiting for {phone} to start Screen Mirroring."
        return f"Mirroring: {phone}'s screen is on your screen."

    async def run(self, name: str, args: dict[str, Any]) -> str:
        svc = self._svc()
        if svc is None:
            return err("LittleBot can't show a phone's screen; say so")
        if name == "stop_mirroring":
            dropped = takeover.drop(self.ctx, LABEL)
            stopped = await svc.stop("asked in the voice chat")
            if stopped or dropped:
                return ok("stopped; your face is back on the screen. Say so in a few words.")
            return ok("nothing was being mirrored; say so.")
        if name != "mirror_phone":
            return err(f"mirror has no tool called {str(name)[:40]}")
        kind = svc.kind_of((args or {}).get("phone"))
        why = svc.check(kind or "")
        if why is None and kind == "android" and not svc.phones():
            why = "not_paired"
        if why is not None:
            return err(REFUSED.get(why, "say it couldn't start."))
        st = svc.status()
        if st.get("active") and st.get("kind") == kind:
            return ok("it is on already; " + SAY[kind].format(name=st.get("airplay_name") or "LittleBot",
                                                             wait=int(st.get("wait_seconds") or 90)))

        async def start() -> bool:
            res = await svc.start(kind, source="voice")
            if not res.get("ok"):
                self.announce(_spoken_failure(res), kind="mirror")
            return bool(res.get("ok"))

        takeover.offer(self.ctx, f"{LABEL}{kind}", start, takeover=False)
        return ok(SAY[kind].format(name=st.get("airplay_name") or "LittleBot", wait=int(st.get("wait_seconds") or 90)))


def _spoken_failure(res: dict[str, Any]) -> str:
    code = res.get("error")
    if code == "no_phone":
        return "I couldn't find the Android phone. Switch on Wireless debugging on it, on the same Wi-Fi, and ask again."
    if code == "busy":
        return "My screen is busy right now, so I can't show the phone."
    return "The phone mirroring didn't start."


__all__ = ["IPHONE_STEPS", "Mirror"]
