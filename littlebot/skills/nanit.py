"""The Nanit baby camera by voice: "show the baby camera", "let me see the nursery", "close the camera".

The tools only check and answer; the view itself opens once the answer is said (littlebot/skills/takeover.py,
``takeover=False``: the camera takes the screen, never LittleBot's speaker, so the voice chat carries on and "close
the camera" or "stop" still works). The words the voice says come back in the result. A picture that never comes
(the bridge isn't running, the camera isn't sending) is said afterwards. The service is ``ctx.nanit``
(littlebot/nanit/service.py). Hidden while the bridge isn't set up or the parent's switch is off.
"""

from __future__ import annotations

from typing import Any

from littlebot.skills import takeover
from littlebot.skills.base import STR, Skill, ToolSpec, err, ok, tool

LABEL = "nanit "
STOPPED = "nanit.stopped"  # littlebot.nanit.service.STOPPED
NO_STREAM = "no_stream"  # littlebot.nanit.service.NO_STREAM

REFUSED = {
    "not_set_up": "say the baby camera isn't set up on LittleBot yet: it needs setting up on the dashboard "
                  "(Assistant page, Baby camera), where a grown-up finds the one command to run.",
    "disabled": "say the baby camera is switched off on the dashboard.",
    "unavailable": "say LittleBot can't show the baby camera.",
    "no_player": "say the video player isn't installed on LittleBot; the dashboard says how.",
    "not_running": "say the camera connection on LittleBot isn't running right now; a grown-up can check it on the "
                   "dashboard (Assistant page, Baby camera).",
    "no_cameras": "say LittleBot hasn't found the Nanit camera yet; try again in a minute.",
    "asleep": "say LittleBot is asleep.",
    "busy": "say the screen is busy with something more important right now.",
}


class NanitCamera(Skill):
    name = "nanit"

    def _svc(self) -> Any:
        return getattr(self.ctx, "nanit", None)

    def available(self) -> bool:
        svc = self._svc()
        try:
            return svc is not None and bool(svc.offered())
        except Exception:  # noqa: BLE001
            return False

    def tools(self) -> list[ToolSpec]:
        return [
            tool("show_baby_camera", "Show the Nanit baby camera's live picture full screen on LittleBot's screen. "
                 "Only when someone asks to see the baby, the nursery, the Nanit or the baby camera. Say what the "
                 "result says.",
                 {"camera": {**STR, "description": "the camera's name, only if they named one (several cameras)"}}),
            tool("close_baby_camera", "Close the baby camera's picture; LittleBot's face comes back. When they say "
                 "stop, close the camera, or that's enough while the camera shows."),
        ]

    def prompt_note(self) -> str:
        return ("Baby camera: when someone asks to see the baby, the nursery, the Nanit or the baby camera, call "
                "show_baby_camera and say what its result says; close_baby_camera when they want it off (\"stop\", "
                "\"close the camera\"). Never offer it otherwise.")

    def context_note(self) -> str:
        svc = self._svc()
        try:
            st = svc.status() if svc is not None else {}
        except Exception:  # noqa: BLE001
            return ""
        if not st.get("active"):
            return ""
        if st.get("state") == "starting":
            return f"Baby camera: {st.get('camera')} is coming up on your screen."
        return f"Baby camera: {st.get('camera')} is on your screen (close_baby_camera closes it)."

    async def run(self, name: str, args: dict[str, Any]) -> str:
        svc = self._svc()
        if svc is None:
            return err("LittleBot can't show the baby camera; say so")
        if name == "close_baby_camera":
            dropped = takeover.drop(self.ctx, LABEL)
            closed = await svc.close("asked in the voice chat")
            if closed or dropped:
                return ok("closed; your face is back on the screen. Say so in a few words.")
            return ok("the baby camera wasn't showing; say so.")
        if name != "show_baby_camera":
            return err(f"nanit has no tool called {str(name)[:40]}")
        why = svc.check()
        if why is not None:
            return err(REFUSED.get(why, "say the baby camera couldn't open."))
        wanted = str((args or {}).get("camera") or "").strip()[:40]
        cams = svc.cameras()
        if not cams:
            return err(REFUSED["no_cameras"])
        cam = svc.pick(wanted or None)
        if cam is None:
            names = ", ".join(c["name"] for c in cams)
            return err(f"there's no camera called {wanted!r}; the cameras are: {names}. Ask which one.")
        st = svc.status()
        if st.get("active") and st.get("camera") == cam["name"]:
            return ok(f"the {cam['name']} camera is on the screen already; say so.")

        async def start() -> bool:
            res = await svc.show(cam["name"], source="voice")
            if not res.get("ok"):
                self.announce(_spoken_failure(res), kind="nanit")
                return False
            self._say_if_no_picture()
            return True

        takeover.offer(self.ctx, f"{LABEL}{cam['uid']}", start, takeover=False)
        other = [c["name"] for c in cams if c["uid"] != cam["uid"]]
        more = f" (there's also: {', '.join(other)})" if other else ""
        return ok(f"say the {cam['name']} camera is coming up on your screen now{more}. It can take a few seconds; "
                  "to close it they say close the camera.")

    def _say_if_no_picture(self) -> None:
        """Once: if the view closes because no picture came, say why."""
        bus = self.ctx.bus
        holder: dict[str, Any] = {}

        def on_stopped(event: Any) -> None:
            sub = holder.pop("sub", None)
            if sub is not None:
                sub.cancel()
            data = getattr(event, "data", None) or {}
            if data.get("code") == NO_STREAM:
                if "bridge" in str(data.get("reason") or ""):
                    self.announce("The baby camera didn't come up: the camera connection on LittleBot isn't "
                                  "running. A grown-up can check it on the dashboard.", kind="nanit")
                else:
                    self.announce("The baby camera didn't come up: the camera isn't sending its picture right now. "
                                  "Check that it's online in the Nanit app, then ask again.", kind="nanit")

        holder["sub"] = bus.subscribe(STOPPED, on_stopped)


def _spoken_failure(res: dict[str, Any]) -> str:
    code = res.get("error")
    if code == "busy":
        return "My screen is busy right now, so I can't show the baby camera."
    if code == "not_running":
        return "The camera connection on LittleBot isn't running, so I can't show the baby camera."
    if code == "no_cameras":
        return "I haven't found the Nanit camera yet. Try again in a minute."
    return "The baby camera didn't open."


__all__ = ["NanitCamera"]
