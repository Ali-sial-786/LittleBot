"""Android by voice: "open Android", "open the Aosu app", "close Android".

The tools only check and answer; the start itself waits until the answer is said (littlebot/skills/takeover.py,
``takeover=False``: Android takes the screen, never LittleBot's speaker, so the voice chat carries on and "close
Android" still works). Android is shut down by itself once it isn't on the screen, so the voice never has to.
The service is ``ctx.android`` (littlebot/android/service.py).
"""

from __future__ import annotations

from typing import Any

from littlebot.skills import takeover
from littlebot.skills.base import Skill, ToolSpec, err, ok, tool

LABEL = "android"

SAY = ("say Android is starting on your screen; the first time it takes a minute or two. It closes by itself when "
       "they close its window or leave it untouched for a few minutes.")
REFUSED = {
    "not_set_up": "say Android isn't set up on LittleBot yet: a grown-up runs scripts/setup_waydroid.sh on the "
                  "robot (the dashboard shows the next step).",
    "disabled": "say Android is switched off on the dashboard.",
    "unavailable": "say LittleBot can't show Android.",
    "not_ready": "say LittleBot has a problem right now, so Android can't start.",
    "low_memory": "say there isn't enough free memory for Android right now (something big is running); try "
                  "again later.",
    "unknown_app": "say that app isn't on LittleBot's list of Android apps (a grown-up adds it in the config, "
                   "android.apps); Android itself can still open.",
}


class Android(Skill):
    name = "android"

    def _svc(self) -> Any:
        return getattr(self.ctx, "android", None)

    def available(self) -> bool:
        svc = self._svc()
        try:
            return svc is not None and bool(svc.offered())
        except Exception:  # noqa: BLE001
            return False

    def _apps(self) -> list[str]:
        svc = self._svc()
        try:
            return sorted(str(k) for k in dict(svc.cfg.apps or {})) if svc is not None else []
        except Exception:  # noqa: BLE001
            return []

    def tools(self) -> list[ToolSpec]:
        apps = self._apps()
        return [
            tool("open_android", "Show Android (a real Android with Google Play, running on LittleBot) on LittleBot's "
                 "screen, or open an app in it. Only when someone asks for Android or one of its apps by name. Say "
                 "what the result says.",
                 {"app": {"type": "string", "description": "an app to open in Android, if they named one: "
                          + (", ".join(apps) if apps else "none are set up") + "; leave it out for Android itself"}}),
            tool("close_android", "Close Android; LittleBot's face comes back (its memory is freed)."),
        ]

    def prompt_note(self) -> str:
        return ("Android: only when someone asks for Android or an Android app by name (like the Aosu doorbell app), "
                "call open_android and say what its result says; close_android when they want it closed. Never "
                "offer it otherwise.")

    def context_note(self) -> str:
        svc = self._svc()
        try:
            st = svc.status() if svc is not None else {}
        except Exception:  # noqa: BLE001
            return ""
        if not st.get("active"):
            return ""
        if st.get("state") == "starting":
            return "Android: starting on your screen (its window comes in a minute or so)."
        return "Android: on your screen."

    async def run(self, name: str, args: dict[str, Any]) -> str:
        svc = self._svc()
        if svc is None:
            return err("LittleBot can't show Android; say so")
        if name == "close_android":
            dropped = takeover.drop(self.ctx, LABEL)
            closed = await svc.close("asked in the voice chat")
            if closed or dropped:
                return ok("closed; your face is back on the screen. Say so in a few words.")
            return ok("Android wasn't open; say so.")
        if name != "open_android":
            return err(f"android has no tool called {str(name)[:40]}")
        app = str((args or {}).get("app") or "").strip() or None
        if app is not None and svc.package_of(app) is None:
            return err(REFUSED["unknown_app"])
        why = svc.check()
        if why is not None:
            return err(REFUSED.get(why, "say it couldn't start."))
        if svc.status().get("active"):
            if app is not None:
                await svc.open(source="voice", app=app)
                return ok(f"Android is on already; {app} is opening in it. Say so.")
            return ok("Android is on your screen already; say so.")

        async def start() -> bool:
            res = await svc.open(source="voice", app=app)
            if not res.get("ok"):
                self.announce(_spoken_failure(res), kind="android")
            return bool(res.get("ok"))

        takeover.offer(self.ctx, f"{LABEL} {app or ''}".strip(), start, takeover=False)
        return ok(SAY if app is None else SAY.replace("Android is starting", f"Android is starting with {app}"))


def _spoken_failure(res: dict[str, Any]) -> str:
    code = res.get("error")
    if code == "busy":
        return "My screen is busy right now, so I can't show Android."
    if code == "low_memory":
        return "There isn't enough free memory for Android right now."
    return "Android didn't start."


__all__ = ["Android"]
