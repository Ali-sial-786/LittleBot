"""LittleBot's toolbox: what ChatGPT may do on the robot when someone asks (function calling).

"Turn the volume up", "play baby shark", "start the peekaboo activity", "how hot are you?",
"what can you do?": the text model (``responses.ChatGPTProvider`` with ``tools=``/``run_tool=``)
calls one of these tools, ``RobotToolbox.run`` does it on the robot and returns a short text or
JSON result, and the model then answers in words. Ported from WatchGPT's ``Toolbox``: model output
is untrusted, so every argument is checked here and a tool never raises (a crash becomes an
``error: ...`` result). Results never contain tokens or personal details.

Used by typed / push-to-talk chat (``brain.conversation``, ``mode="chat"``) and by live-voice
hand-offs (``chatgpt.live``, ``mode="live"``). Everything runs on the asyncio loop; blocking reads
(system monitor, Wi-Fi) go to a worker thread.

Live information: ``get_weather`` (Open-Meteo, littlebot/info/weather.py) and ``get_time``
(littlebot/info/clock.py), plus OpenAI's own web search (``WEB_SEARCH``, a hosted tool the endpoint
runs itself; ``chatgpt.web_search``) for news, scores, opening hours and other facts that change.
``instructions()`` is the note callers add to the system prompt so the model uses them rather than
guessing.

Take-overs: starting a video or an activity, carrying on with a video paused for a chat ("Hey LittleBot"
while it played: ``MediaController.hold``), and going to sleep, need the screen and the speaker
that the conversation itself is using (starting them mid-turn would cut the answer off).
With ``defer=True`` they are checked now (unknown activity, videos switched off, ...) but only
queued (``pending``); the host runs ``run_pending()`` once the reply has been spoken (chat) or once
the voice has said its line and the call has ended (live voice). Only the last take-over asked
for in a turn is kept. Games start at once: ``ctx.games`` shares the face and speaker with a
conversation and only takes the screen (see littlebot/games/service.py).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("littlebot.chatgpt.tools")

MAX_LISTED = 40
VOLUME_STEP = 10  # percent
SOURCE = "chatgpt"  # recorded as the activity / media source


@dataclass
class Pending:
    """An action that runs after the turn (``run`` returns False when it didn't happen)."""

    label: str
    run: Callable[[], Awaitable[Any]]
    takeover: bool = True  # needs the screen / speaker (only the last one asked for runs)


@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    modes: tuple[str, ...] = ("chat", "live")


def _obj(props: dict[str, Any] | None = None, required: tuple[str, ...] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": props or {}, "additionalProperties": False}
    if required:
        out["required"] = list(required)
    return out


_STR = {"type": "string"}
_PERCENT = {"type": "integer", "minimum": 0, "maximum": 100}

TOOLS: tuple[_Tool, ...] = (
    _Tool("get_robot_status", "LittleBot's own status right now: what it is doing, CPU temperature, CPU load, "
          "memory, how long it has been running, Wi-Fi, which devices work (camera, microphone, speaker, screen, "
          "motors), volume, the ChatGPT plan, live voice on or off, any video / activity / game running, and the "
          "privacy switches. Use it for questions like 'how hot are you?' or 'are you connected?'.", _obj()),
    _Tool("describe_yourself", "What LittleBot is and everything it can do (faces, camera, activities, videos, "
          "games, volume, sleep...). Use it for 'what can you do?' or 'tell me about yourself'.", _obj()),
    _Tool("list_activities", "The activities LittleBot can run (songs, stories, games, movement...), suitable "
          "ones for the child's age first.",
          _obj({"category": {**_STR, "description": "optional, e.g. music, story"}})),
    _Tool("start_activity", "Start one of LittleBot's activities, by its id or name (for example 'peekaboo' or "
          "'Old MacDonald'). It starts once you have finished speaking.",
          _obj({"activity": {**_STR, "description": "activity id or name"}}, ("activity",))),
    _Tool("stop_activity", "Stop the activity that is running.", _obj()),
    _Tool("get_volume", "LittleBot's speaker and video volume, in percent.", _obj()),
    _Tool("set_volume", "Set the volume in percent (it is capped at the safe maximum).",
          _obj({"percent": {**_PERCENT, "description": "0 to 100"},
                "what": {"type": "string", "enum": ["all", "speaker", "video"], "description": "default all"}},
               ("percent",))),
    _Tool("volume_up", "Make LittleBot louder (default one step of 10 percent).",
          _obj({"step": {**_PERCENT, "description": "percent, default 10"}})),
    _Tool("volume_down", "Make LittleBot quieter (default one step of 10 percent).",
          _obj({"step": {**_PERCENT, "description": "percent, default 10"}})),
    _Tool("set_face", "Show a face or face animation on LittleBot's screen, e.g. happy, surprised, sleepy, wink, "
          "spin_eyes, peekaboo, hearts, confetti.", _obj({"face": _STR}, ("face",))),
    _Tool("go_to_sleep", "Put LittleBot to sleep (sleepy face, quiet) once you have said good night.", _obj()),
    _Tool("wake_up", "Wake LittleBot up from sleep.", _obj()),
    _Tool("stop_everything", "Stop any video, activity or game that is running (the conversation carries on).",
          _obj()),
    _Tool("search_youtube", "Search YouTube for videos. Returns video ids, titles, channels and lengths.",
          _obj({"query": {**_STR, "description": "what to look for, e.g. 'baby shark song'"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, ("query",))),
    _Tool("play_youtube", "Play a YouTube video on LittleBot's screen: give video_id (from search_youtube or "
          "list_approved_videos, e.g. 'approved:3') or just a query and the best match plays. It starts once you "
          "have finished speaking.",
          _obj({"video_id": {**_STR, "description": "11-character YouTube id, or approved:<n>"},
                "query": {**_STR, "description": "what to search for when there is no video_id"}})),
    _Tool("list_approved_videos", "The videos a grown-up added to LittleBot's approved list.", _obj()),
    _Tool("stop_video", "Stop the video that is playing or paused (also one paused while LittleBot talks).", _obj()),
    _Tool("pause_video", "Pause the video that is playing.", _obj()),
    _Tool("resume_video", "Carry on playing a paused video where it stopped. A video paused while LittleBot talks "
          "('keep watching', 'go back to the video') carries on once you have finished speaking.", _obj()),
    _Tool("list_games", "The games LittleBot can play.", _obj()),
    _Tool("start_game", "Start one of LittleBot's on-screen games by id or name (you can keep talking while it "
          "runs).", _obj({"game": {**_STR, "description": "game id or name"}}, ("game",))),
    _Tool("stop_game", "Stop the game that is running.", _obj()),
    _Tool("look_through_camera", "Take one photo with LittleBot's camera (only if a grown-up allows it) and say "
          "what is in it, to answer a question like 'what am I holding?'.",
          _obj({"question": _STR}, ("question",)), modes=("chat",)),
    _Tool("get_weather", "The real weather from a weather service: now (temperature, feels-like, conditions, wind) "
          "and the forecast (high, low, conditions, chance of rain or snow, wind, sunrise and sunset) for today, "
          "tomorrow or the next days. Call it for EVERY question about the weather, the temperature outside, rain, "
          "snow, sun, wind, or what to wear outside, instead of guessing or saying you don't know. Without a place it "
          "is the weather at home; for somewhere else give the place, e.g. 'Paris' or 'Springfield, Illinois'.",
          _obj({"place": {**_STR, "description": "a city or town; leave out for home"},
                "days": {"type": "integer", "minimum": 1, "maximum": 7,
                         "description": "days of forecast: 1 today, 2 (default) today and tomorrow, up to 7"}})),
    _Tool("get_time", "The current time, date and day of the week where LittleBot is (or in another place). Call it "
          "for 'what time is it?', 'what day is it?', 'what's the date?' or how long until something, instead of "
          "guessing.", _obj({"place": {**_STR, "description": "a city, for the time somewhere else; leave out for "
                                                            "here"}})),
)
TOOL_NAMES = frozenset(t.name for t in TOOLS)

# OpenAI's own web search, run on OpenAI's side (a hosted tool, not a function: RobotToolbox never runs
# it). ``responses.ChatGPTProvider`` sends it as {"type": "web_search"}; "name" is for logs and fakes.
WEB_SEARCH = {"type": "web_search", "name": "web_search"}

# Appended to the system prompt whenever the tools are offered (typed chat and live-voice hand-offs).
INFO_NOTE = """
Live information: never guess, and never say you can't know or can't check. For the weather (today, tomorrow, \
anywhere) call get_weather; for the time, the day or the date call get_time.{search} Then answer in one or two \
short, plain sentences with the key facts (for the weather: what it is like, the temperature and whether to expect \
rain), rounded numbers, no links, no website names and no list."""
SEARCH_NOTE = (" For anything else that changes or that you are not sure of (news, sports scores, opening hours, "
               "events, prices, who someone is now, recent facts), use web search first.")


def _ok(text: str) -> str:
    return f"ok: {text}"


def _err(text: str) -> str:
    return f"error: {text}"


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower().replace("'", "")).strip()


class RobotToolbox:
    """The tools for one turn (chat) or one hand-off (live voice)."""

    def __init__(self, ctx: Any, *, mode: str = "chat", defer: bool = True) -> None:
        self.ctx = ctx
        self.mode = mode if mode in ("chat", "live") else "chat"
        self.defer = bool(defer)
        self.calls: list[str] = []  # tool names in the order they ran (never their arguments)
        self.done: list[str] = []  # short confirmations of what was done or queued
        self.pending: list[Pending] = []
        self._found: list[dict[str, Any]] = []  # the last search results (titles for play_youtube)

    # ------------------------------------------------------------ the model's side
    def specs(self) -> list[dict[str, Any]]:
        """Tool specs for ``ChatGPTProvider.reply(tools=...)``: LittleBot's functions, then OpenAI's web
        search (``WEB_SEARCH``) when ``chatgpt.web_search`` is on."""
        out: list[dict[str, Any]] = [{"name": t.name, "description": t.description, "parameters": t.parameters}
                                     for t in TOOLS if self.mode in t.modes]
        if self.web_search:
            out.append(dict(WEB_SEARCH))
        return out

    @property
    def web_search(self) -> bool:
        return bool(getattr(getattr(self.ctx.config, "chatgpt", None), "web_search", False))

    def instructions(self) -> str:
        """What to add to the system prompt with these tools: use them for live information."""
        return INFO_NOTE.format(search=SEARCH_NOTE if self.web_search else "")

    async def run(self, name: str, arguments: str | dict[str, Any] | None = None) -> str:
        """Run one tool call; returns what to tell the model. Never raises (except cancellation)."""
        tool = next((t for t in TOOLS if t.name == name and self.mode in t.modes), None)
        if tool is None:
            return _err(f"there is no tool called {str(name)[:40]}")
        if isinstance(arguments, dict):
            args = arguments
        else:
            try:
                args = json.loads(arguments or "{}")
            except ValueError:
                return _err("the arguments were not valid JSON")
            if not isinstance(args, dict):
                args = {}
        self.calls.append(tool.name)
        try:
            result = await getattr(self, f"_{tool.name}")(args)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken tool must never break the conversation
            log.warning("tool %s crashed", tool.name, exc_info=True)
            result = _err("the action failed")
        log.info("tool %s (%s) -> %s", tool.name, self.mode, result.split(":", 1)[0] if result else "")
        return result

    # ------------------------------------------------------------ after the turn
    async def run_pending(self) -> list[str]:
        """Run what was queued for after the turn; returns the labels of what happened."""
        pending, self.pending = self.pending, []
        done: list[str] = []
        for p in pending:
            try:
                ok = await p.run()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("after the turn, %s didn't start (%s): %s", p.label, type(e).__name__, str(e)[:200])
                continue
            if ok is not False:
                done.append(p.label)
                log.info("after the turn: %s", p.label)
        return done

    def _queue(self, label: str, run: Callable[[], Awaitable[Any]], *, takeover: bool = True) -> None:
        if takeover:
            self.pending = [p for p in self.pending if not p.takeover]
        self.pending.append(Pending(label, run, takeover))

    def _when(self) -> str:
        if self.mode == "live":
            return ("as soon as you have finished this sentence (this voice chat ends then, because it needs the "
                    "screen and speaker). Tell them briefly and say a short goodbye")
        return "as soon as you have finished speaking. Tell them briefly"

    async def _takeover(self, label: str, run: Callable[[], Awaitable[Any]], what: str) -> str:
        """Run ``run`` now, or (defer) queue it for after the turn."""
        if self.defer:
            self._queue(label, run)
            self.done.append(f"{what} starts in a moment")
            return _ok(f"{what} will start {self._when()}.")
        try:
            ok = await run()
        except (PermissionError, KeyError, ValueError) as e:
            return _err(str(e).strip("'\"") or "it couldn't start")
        if ok is False:
            return _err(f"{what} couldn't start right now")
        self.done.append(f"{what} started")
        return _ok(f"{what} started.")

    # ------------------------------------------------------------ status / about
    async def _get_robot_status(self, args: dict[str, Any]) -> str:
        ctx = self.ctx
        out: dict[str, Any] = {"name": ctx.config.robot_name}
        try:
            from littlebot import __version__  # noqa: PLC0415

            out["version"] = __version__
        except Exception:  # noqa: BLE001
            pass
        out["doing"] = _safe(lambda: ctx.state.state.value.lower().replace("_", " "))
        out["local_time"] = _dt.datetime.now().strftime("%A %H:%M")
        out["offline_mode"] = bool(ctx.config.is_offline)
        mon = await asyncio.to_thread(_safe, ctx.monitor.status) or {}
        robot = ctx.extras.get("robot")
        started = getattr(robot, "started_at", None)
        running_s = (time.time() - started) if isinstance(started, (int, float)) else mon.get("uptime_seconds")
        system = {
            "cpu_temperature_c": mon.get("temperature_c"),
            "cpu_load_percent": mon.get("system_cpu_percent", mon.get("cpu_percent")),
            "littlebot_cpu_percent": mon.get("cpu_percent"),
            "memory_used_percent": mon.get("system_memory_percent"),
            "memory_total_mb": mon.get("system_memory_total_mb"),
            "littlebot_memory_mb": mon.get("memory_mb"),
            "littlebot_running_minutes": round(running_s / 60, 1) if isinstance(running_s, (int, float)) else None,
            "computer_uptime_hours": await asyncio.to_thread(_machine_uptime_hours),
            "raspberry_pi": mon.get("is_raspberry_pi"),
            "power_or_heat_warnings": mon.get("throttled_flags") or [],
        }
        out["system"] = {k: v for k, v in system.items() if v is not None}
        out["wifi"] = await asyncio.to_thread(wifi_status)
        out["devices"] = self._devices()
        out["volume_percent"] = self._volumes()
        auth = _safe(self._auth_status) or {}
        out["chatgpt"] = {"signed_in": bool(auth.get("signed_in")), "plan": auth.get("plan")}
        live = ctx.live
        live_on = bool(_safe(lambda: live.status().get("active"))) if live is not None else False
        out["live_voice"] = "on" if live_on else "off"
        media = ctx.media
        if media is not None:
            m = _safe(media.status) or {}
            item = m.get("item") or {}
            out["video"] = {"state": m.get("state"), "title": item.get("title")} if item else {"state": m.get("state")}
            if m.get("held"):
                out["video"]["paused_while_talking"] = True  # resume_video carries on with it
        acts = ctx.activities
        if acts is not None:
            a = _safe(acts.status) or {}
            out["activity"] = a.get("name") if a.get("running") else None
        games = self._games()
        if games is not None:
            out["game"] = _safe(games.status)
        out["privacy"] = {k: v for k, v in (_safe(ctx.privacy.snapshot) or {}).items()
                          if k in ("camera_enabled", "microphone_enabled", "cloud_enabled", "ai_camera_enabled")}
        return _json(out)

    def _devices(self) -> dict[str, Any]:
        ctx = self.ctx
        dev: dict[str, Any] = {}
        if ctx.vision is not None:
            dev["camera"] = _safe(lambda: ctx.vision.status().get("camera")) or "unknown"
        else:
            dev["camera"] = "not connected"
        audio = ctx.audio
        st = _safe(audio.status) if audio is not None and hasattr(audio, "status") else None
        for part in ("microphone", "speaker"):
            info = (st or {}).get(part)
            if isinstance(info, dict):
                dev[part] = info.get("status")
            else:
                dev[part] = "not connected" if audio is None else "unknown"
        if ctx.live is not None and _safe(lambda: ctx.live.status().get("active")):
            dev["microphone"] = dev["speaker"] = "in use by the live voice chat"
        face = ctx.face
        dev["screen_face"] = "ok" if face is not None else "not running"
        motion = ctx.motion
        if motion is not None:
            mst = _safe(motion.status) or {}
            dev["motors"] = "on" if mst.get("physical_enabled") else "simulated only (no motors moving)"
        return dev

    def _auth_status(self) -> dict[str, Any]:
        from littlebot.chatgpt import get_auth  # noqa: PLC0415

        return get_auth(self.ctx).status()

    async def _describe_yourself(self, args: dict[str, Any]) -> str:
        ctx = self.ctx
        try:
            from littlebot.brain.face_actions import menu_text  # noqa: PLC0415

            faces = menu_text()
        except Exception:  # noqa: BLE001
            faces = "happy, sad, surprised, sleepy, wink, peekaboo"
        acts = ctx.activities
        n_acts = len(_safe(acts.list) or []) if acts is not None else 0
        media = ctx.media
        approved = len(_safe(media.list_items) or []) if media is not None else 0
        search = bool(_safe(media.search_allowed)) if media is not None and hasattr(media, "search_allowed") else False
        games = self._games()
        game_names = [g.get("name") for g in (_safe(games.list) or []) if isinstance(g, dict)] if games else []
        ai_camera = bool(getattr(ctx.privacy, "ai_camera_enabled", False))
        lines = [
            f"I am {ctx.config.robot_name}, a small toy robot with an animated face on a screen, running on a "
            "Raspberry Pi. A grown-up looks after my settings on my dashboard.",
            "I can talk: typed chat, push to talk, and live voice chats with ChatGPT.",
            f"Faces I can make: {faces}.",
            ("I have a camera; I can look and say what I see when asked (the grown-up has allowed it)." if ai_camera
             else "I have a camera, but I only look for the AI when a grown-up switches that on in Privacy."),
            f"I know {n_acts} activities: songs, stories, games, movement and more." if n_acts else
            "Activities are not available right now.",
            ("I can find and play videos when asked" + (f", and {approved} videos are on my approved list." if approved
                                                        else ".")) if search else
            (f"I can play the {approved} videos on my approved list." if approved else "No videos are set up yet."),
            (f"Games: {', '.join(n for n in game_names if n)[:300]}." if game_names else
             "Games are not available yet." if games is None else "No games are installed yet."),
            "I can change my volume, go to sleep and wake up, stop everything, and tell you how I am "
            "(temperature, memory, Wi-Fi, what's connected).",
            "I can tell you the weather here or anywhere, today or in the next days, and the time and date"
            + (", and find out things that change, like news, sports scores or opening hours." if self.web_search
               else "."),
            "I am a robot, not a person.",
        ]
        return " ".join(lines)

    # ------------------------------------------------------------ activities
    async def _list_activities(self, args: dict[str, Any]) -> str:
        acts = self.ctx.activities
        if acts is None:
            return _err("activities are not available on this robot")
        category = str(args.get("category") or "").strip().lower() or None
        items = acts.list(category=category) if category else acts.list()
        out = []
        for a in items:
            if not a.get("enabled", True):
                continue
            out.append({"id": a.get("id"), "name": a.get("name"), "category": a.get("category"),
                        "ages": a.get("age_range"), "minutes": round((a.get("duration_seconds") or 0) / 60, 1),
                        **({"suitable": a["suitable"]} if a.get("suitable") is not None else {})})
            if len(out) >= MAX_LISTED:
                break
        return _json(out) if out else _ok("no activities found")

    def _find_activity(self, wanted: str) -> dict[str, Any] | None:
        acts = self.ctx.activities
        items = [a for a in acts.list() if a.get("enabled", True)]
        return _best_match(wanted, items, extra=("tags",))

    async def _start_activity(self, args: dict[str, Any]) -> str:
        acts = self.ctx.activities
        if acts is None:
            return _err("activities are not available on this robot")
        wanted = str(args.get("activity") or "").strip()
        if not wanted:
            return _err("say which activity")
        act = self._find_activity(wanted)
        if act is None:
            return _err(f"there is no activity called {wanted[:60]}; list_activities shows them")
        aid, name = act["id"], act.get("name") or act["id"]

        async def start() -> bool:
            await acts.start(aid, source=SOURCE)
            return True

        return await self._takeover(f"activity {aid}", start, f"the activity '{name}'")

    async def _stop_activity(self, args: dict[str, Any]) -> str:
        acts = self.ctx.activities
        if acts is None:
            return _err("activities are not available on this robot")
        dropped = self._drop_pending("activity ")
        st = _safe(acts.status) or {}
        if not st.get("running"):
            return _ok("the activity won't start") if dropped else _ok("no activity is running")
        await acts.stop("stopped (asked the AI)")
        self.done.append("activity stopped")
        return _ok(f"stopped '{st.get('name') or 'the activity'}'")

    # ------------------------------------------------------------ volume
    def _max_speaker(self) -> float:
        return float(self.ctx.config.audio.speaker.max_volume)

    def _speaker_volume(self) -> float:
        audio = self.ctx.audio
        st = _safe(audio.status) if audio is not None and hasattr(audio, "status") else None
        v = (st or {}).get("volume")
        if not isinstance(v, (int, float)):
            v = self.ctx.settings.get("audio.volume", self.ctx.config.audio.speaker.volume)
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(self.ctx.config.audio.speaker.volume)

    def _video_volume(self) -> float | None:
        media = self.ctx.media
        if media is None:
            return None
        v = (_safe(media.status) or {}).get("volume")
        return float(v) if isinstance(v, (int, float)) else None

    def _volumes(self) -> dict[str, Any]:
        out: dict[str, Any] = {"speaker": round(self._speaker_volume() * 100),
                               "speaker_max": round(self._max_speaker() * 100)}
        video = self._video_volume()
        if video is not None:
            out["video"] = round(video * 100)
            out["video_max"] = round(float(self.ctx.config.media.max_volume) * 100)
        return out

    def _set_speaker(self, value: float) -> float:
        value = round(max(0.0, min(self._max_speaker(), value)), 3)
        audio = self.ctx.audio
        if audio is not None and hasattr(audio, "set_volume"):
            return float(audio.set_volume(value))
        return float(self.ctx.settings.set("audio.volume", value, source=SOURCE))  # the live voice reads it

    def _set_video(self, value: float) -> float | None:
        media = self.ctx.media
        if media is None or not hasattr(media, "set_volume"):
            return None
        return float(media.set_volume(round(max(0.0, min(float(self.ctx.config.media.max_volume), value)), 3)))

    def _volume_text(self, speaker: float | None, video: float | None) -> str:
        parts = []
        if speaker is not None:
            capped = " (the most allowed)" if speaker >= self._max_speaker() - 1e-6 else ""
            parts.append(f"speaker volume is now {round(speaker * 100)}%{capped}")
        if video is not None:
            parts.append(f"video volume is now {round(video * 100)}%")
        return "; ".join(parts) or "nothing changed"

    async def _get_volume(self, args: dict[str, Any]) -> str:
        return _json(self._volumes())

    async def _set_volume(self, args: dict[str, Any]) -> str:
        try:
            percent = float(args.get("percent"))
        except (TypeError, ValueError):
            return _err("give the volume as a percent from 0 to 100")
        if percent != percent:  # NaN
            return _err("give the volume as a percent from 0 to 100")
        level = max(0.0, min(100.0, percent)) / 100
        what = str(args.get("what") or "all").lower()
        speaker = self._set_speaker(level) if what in ("all", "speaker") else None
        video = self._set_video(level) if what in ("all", "video") else None
        self.done.append("volume changed")
        return _ok(self._volume_text(speaker, video))

    async def _change_volume(self, args: dict[str, Any], sign: int) -> str:
        try:
            step = float(args.get("step") or VOLUME_STEP)
        except (TypeError, ValueError):
            step = VOLUME_STEP
        step = max(1.0, min(100.0, step)) / 100 * sign
        speaker = self._set_speaker(self._speaker_volume() + step)
        video_now = self._video_volume()
        video = self._set_video(video_now + step) if video_now is not None else None
        self.done.append("volume changed")
        return _ok(self._volume_text(speaker, video))

    async def _volume_up(self, args: dict[str, Any]) -> str:
        return await self._change_volume(args, +1)

    async def _volume_down(self, args: dict[str, Any]) -> str:
        return await self._change_volume(args, -1)

    # ------------------------------------------------------------ face / sleep / stop
    async def _set_face(self, args: dict[str, Any]) -> str:
        from littlebot.brain.face_actions import extract_tags, menu_text, perform  # noqa: PLC0415

        name = str(args.get("face") or "").strip()[:40]
        _text, actions = extract_tags(f"[face: {name}]")
        if not actions:
            return _err(f"unknown face; faces: {menu_text()}")
        action = actions[-1]
        if self.ctx.face is None:
            return _err("the screen face isn't running")
        shown = action.name.replace("_", " ")
        if self.mode == "chat" and self.defer:
            # The reply is about to be spoken with its own expression: show the face after it.
            self._queue(f"face {action.name}", lambda: perform(self.ctx, action), takeover=False)
            self.done.append(f"showing {shown}")
            return _ok(f"LittleBot will show {shown} on its face as soon as you finish speaking")
        await perform(self.ctx, action)
        self.done.append(f"showing {shown}")
        return _ok(f"LittleBot is showing {shown} on its face now")

    async def _go_to_sleep(self, args: dict[str, Any]) -> str:
        from littlebot.core.state import RobotState  # noqa: PLC0415

        ctx = self.ctx

        async def sleep() -> bool:
            robot = ctx.extras.get("robot")
            if robot is not None and callable(getattr(robot, "sleep", None)):
                return bool(robot.sleep(SOURCE))
            return bool(ctx.state.transition(RobotState.SLEEPING, f"sleep ({SOURCE})"))

        if ctx.state.state == RobotState.SLEEPING:
            return _ok("LittleBot is already asleep")
        return await self._takeover("sleep", sleep, "sleeping")

    async def _wake_up(self, args: dict[str, Any]) -> str:
        from littlebot.core.state import RobotState  # noqa: PLC0415

        ctx = self.ctx
        self._drop_pending("sleep")
        if ctx.state.state not in (RobotState.SLEEPING, RobotState.ERROR):
            return _ok("LittleBot is awake")
        robot = ctx.extras.get("robot")
        ok = robot.wake(SOURCE) if robot is not None and callable(getattr(robot, "wake", None)) \
            else ctx.state.transition(RobotState.IDLE, f"wake ({SOURCE})")
        return _ok("LittleBot woke up") if ok else _err("LittleBot couldn't wake up right now")

    async def _stop_everything(self, args: dict[str, Any]) -> str:
        """Videos, activities and games (not the conversation: STOP ALL would end it)."""
        ctx = self.ctx
        stopped: list[str] = []
        if self.pending:
            self.pending = []
            stopped.append("what was about to start")
        media = ctx.media
        if media is not None and (_safe(media.status) or {}).get("state") not in (None, "idle"):
            await media.stop("stopped")
            stopped.append("the video")
        acts = ctx.activities
        if acts is not None and (_safe(acts.status) or {}).get("running"):
            await acts.stop("stopped (asked the AI)")
            stopped.append("the activity")
        games = self._games()
        if games is not None and _running(_safe(games.status)):
            await _maybe_await(games.stop())
            stopped.append("the game")
        if ctx.motion is not None:
            _safe(ctx.motion.cancel)
        if not stopped:
            return _ok("nothing was running")
        self.done.append("stopped " + ", ".join(stopped))
        return _ok("stopped " + ", ".join(stopped))

    def _drop_pending(self, prefix: str) -> bool:
        before = len(self.pending)
        self.pending = [p for p in self.pending if not p.label.startswith(prefix)]
        return len(self.pending) != before

    # ------------------------------------------------------------ videos
    def _media(self) -> Any:
        return self.ctx.media

    async def _search_youtube(self, args: dict[str, Any]) -> str:
        media = self._media()
        if media is None:
            return _err("videos aren't available on this robot")
        query = str(args.get("query") or "").strip()
        if not query:
            return _err("say what to search for")
        try:
            limit = max(1, min(10, int(args.get("limit") or 5)))
        except (TypeError, ValueError):
            limit = 5
        results, problem = await self._search(query, limit)
        if problem:
            return _err(problem)
        if not results:
            return _ok("no videos found; try other words")
        return _json([_brief(r) for r in results])

    async def _search(self, query: str, limit: int) -> tuple[list[dict[str, Any]], str | None]:
        """Search results (remembered for play_youtube), or an error for the model."""
        media = self._media()
        if not hasattr(media, "search") or not getattr(media, "search_allowed", lambda: False)():
            approved = self._approved_matches(query)
            if approved:
                self._found = approved
                return approved, None
            return [], ("only the videos on the approved list can play here (searching is switched off); "
                        "list_approved_videos shows them")
        from littlebot.media.search import SearchError  # noqa: PLC0415

        try:
            results = await media.search(query, limit=limit)
        except PermissionError as e:
            return [], str(e)
        except SearchError as e:
            approved = self._approved_matches(query)
            if approved:
                self._found = approved
                return approved, None
            return [], {"unavailable": "video search isn't installed on this robot (yt-dlp)",
                        "timeout": "the video search took too long",
                        "bad_query": "say what to search for"}.get(e.kind, "the video search didn't work")
        self._found = list(results)
        return results, None

    def _approved_matches(self, query: str) -> list[dict[str, Any]]:
        words = [w for w in _norm(query).split() if len(w) >= 3]
        out = []
        for item in self._approved():
            title = _norm(item.get("title"))
            if words and any(w in title for w in words):
                out.append(_approved_result(item))
        return out

    def _approved(self) -> list[dict[str, Any]]:
        media = self._media()
        items = _safe(media.list_items) if media is not None else None
        return [i for i in items or [] if i.get("enabled", True)]

    async def _list_approved_videos(self, args: dict[str, Any]) -> str:
        if self._media() is None:
            return _err("videos aren't available on this robot")
        items = [_approved_result(i) for i in self._approved()][:MAX_LISTED]
        return _json([_brief(i) for i in items]) if items else _ok("no videos are on the approved list")

    async def _play_youtube(self, args: dict[str, Any]) -> str:
        media = self._media()
        if media is None:
            return _err("videos aren't available on this robot")
        vid = str(args.get("video_id") or "").strip()
        query = str(args.get("query") or "").strip()
        if not vid and not query:
            return _err("give a video_id or a query")
        if hasattr(media, "video_blocked_reason"):
            blocked = media.video_blocked_reason()
            if blocked:
                return _err(blocked)
        if vid.lower().startswith("approved:"):
            return await self._play_approved(vid.split(":", 1)[1])
        if vid:
            vid = _video_id(vid) or ""
            if not vid:
                return _err("that isn't a YouTube video id; use one from search_youtube")
            found = next((r for r in self._found if r.get("video_id") == vid), None)
            if found is not None and found.get("approved_id") is not None:
                return await self._play_approved(str(found["approved_id"]))
            return await self._play_found(found or {"video_id": vid, "title": None},
                                          [r for r in self._found if r.get("video_id") != vid])
        results, problem = await self._search(query, 5)
        if problem:
            return _err(problem)
        if not results:
            return _err("no videos found for that; try other words")
        first = results[0]
        if first.get("approved_id") is not None:
            return await self._play_approved(str(first["approved_id"]))
        return await self._play_found(first, results[1:])

    async def _play_found(self, found: dict[str, Any], others: list[dict[str, Any]]) -> str:
        media = self._media()
        if not hasattr(media, "play_video") or not getattr(media, "search_allowed", lambda: False)():
            return _err("only the videos on the approved list can play here; list_approved_videos shows them")
        vid = found["video_id"]
        title = found.get("title")
        alternatives = [o for o in others if o.get("approved_id") is None][:5]

        async def play() -> bool:
            await media.play_video(vid, title=title, channel=found.get("channel"),
                                   duration_seconds=found.get("duration_seconds"), alternatives=alternatives)
            return True

        return await self._takeover(f"video {vid}", play, f"the video '{speakable_title(title)}'")

    async def _play_approved(self, raw_id: str) -> str:
        media = self._media()
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            return _err("unknown approved video")
        item = next((i for i in self._approved() if i.get("id") == item_id), None)
        if item is None:
            return _err("that video isn't on the approved list (or is switched off)")

        async def play() -> bool:
            await media.play(item_id)
            return True

        return await self._takeover(f"video approved:{item_id}", play,
                                    f"the video '{speakable_title(item.get('title'))}'")

    async def _stop_video(self, args: dict[str, Any]) -> str:
        media = self._media()
        if media is None:
            return _err("videos aren't available on this robot")
        dropped = self._drop_pending("video ")
        if (_safe(media.status) or {}).get("state") in (None, "idle"):
            return _ok("the video won't start") if dropped else _ok("no video is playing")
        await media.stop("stopped")
        self.done.append("video stopped")
        return _ok("the video stopped")

    async def _pause_video(self, args: dict[str, Any]) -> str:
        media = self._media()
        if media is None:
            return _err("videos aren't available on this robot")
        return _ok("the video is paused") if await media.pause() else _ok("no video is playing")

    async def _resume_video(self, args: dict[str, Any]) -> str:
        media = self._media()
        if media is None:
            return _err("videos aren't available on this robot")
        st = _safe(media.status) or {}
        if st.get("held"):
            # Paused for a chat (the face is showing): it needs the screen and speaker back, like a new video.
            async def resume() -> bool:
                return bool(await media.resume())

            title = speakable_title((st.get("item") or {}).get("title"))
            return await self._takeover("video resume", resume, f"the video '{title}' (from where it stopped)")
        return _ok("the video carries on") if await media.resume() else _ok("no video is paused")

    # ------------------------------------------------------------ games
    def _games(self) -> Any:
        return getattr(self.ctx, "games", None) or self.ctx.extras.get("games")

    def _game_list(self) -> list[dict[str, Any]]:
        games = self._games()
        items = games.list() if games is not None else []
        return [g for g in items or [] if isinstance(g, dict) and g.get("id")]

    async def _list_games(self, args: dict[str, Any]) -> str:
        if self._games() is None:
            return _err("games aren't available on this robot yet")
        items = [{"id": g.get("id"), "name": g.get("name"), "description": str(g.get("description") or "")[:120]}
                 for g in self._game_list()][:MAX_LISTED]
        return _json(items) if items else _ok("no games are installed")

    async def _start_game(self, args: dict[str, Any]) -> str:
        games = self._games()
        if games is None:
            return _err("games aren't available on this robot yet")
        wanted = str(args.get("game") or "").strip()
        game = _best_match(wanted, self._game_list()) if wanted else None
        if game is None:
            return _err(f"there is no game called {wanted[:60]}; list_games shows them" if wanted else "say which game")
        gid, name = game["id"], game.get("name") or game["id"]
        self.pending = [p for p in self.pending if not p.takeover]  # the game wins over a queued video
        try:
            started = games.start(gid, source=SOURCE)
        except TypeError:  # a games service without a source argument
            started = games.start(gid)
        if not await _maybe_await(started):
            why = (_safe(games.status) or {}).get("last_error")
            return _err(f"the game '{name}' couldn't start" + (f": {str(why)[:160]}" if why else ""))
        self.done.append(f"the game '{name}' started")
        return _ok(f"the game '{name}' started on the screen")

    async def _stop_game(self, args: dict[str, Any]) -> str:
        games = self._games()
        if games is None:
            return _err("games aren't available on this robot yet")
        if not _running(_safe(games.status)):
            return _ok("no game is running")
        await _maybe_await(games.stop())
        self.done.append("game stopped")
        return _ok("the game stopped")

    # ------------------------------------------------------------ camera
    async def _look_through_camera(self, args: dict[str, Any]) -> str:
        try:
            from littlebot.chatgpt.look import get_look  # noqa: PLC0415
        except ImportError:
            return _err("the camera can't be used for this")
        look = get_look(self.ctx)
        question = str(args.get("question") or "").strip()[:300] or "What do you see?"
        result = await look.look(question, asked_by="chat")
        if result.get("ok") and result.get("answer"):
            return _ok(f"the camera saw: {result['answer']}")
        return _err(str(result.get("detail") or "LittleBot couldn't look just now")
                    + " Don't guess what is there.")

    # ------------------------------------------------------------ live information (littlebot/info)
    async def _get_weather(self, args: dict[str, Any]) -> str:
        from littlebot.info.weather import WeatherError, get_weather  # noqa: PLC0415

        place = " ".join(str(args.get("place") or "").split())[:100] or None
        try:
            days = max(1, min(7, int(args.get("days") or 2)))
        except (TypeError, ValueError):
            days = 2
        try:
            report = await get_weather(self.ctx).report(place, days=days)
        except WeatherError as e:
            return _err(f"{e}. Say so briefly; don't guess the weather.")
        return _json(report)

    async def _get_time(self, args: dict[str, Any]) -> str:
        from littlebot.info.clock import now_info  # noqa: PLC0415
        from littlebot.info.weather import WeatherError, get_weather  # noqa: PLC0415

        place = " ".join(str(args.get("place") or "").split())[:100]
        info = getattr(self.ctx.config, "info", None)
        tz, where = (getattr(info, "timezone", "") or None), None
        if place:
            try:
                found = await get_weather(self.ctx).find_bounded(place)
            except WeatherError as e:
                return _err(str(e))
            if not found.timezone:
                return _err(f"LittleBot doesn't know the time zone of {found.label[:60]}")
            tz, where = found.timezone, found.label
        try:
            out = now_info(tz)
        except ValueError:
            if where:
                return _err(f"LittleBot doesn't know the time zone of {where[:60]}")
            # info.timezone isn't a real zone: the robot's own clock
            log.warning("info.timezone %r is not a known time zone; using the robot's own", str(tz)[:60])
            out = now_info(None)
        return _json({**({"place": where} if where else {}), **out})


# ---------------------------------------------------------------- helpers
def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _running(status: Any) -> bool:
    if not isinstance(status, dict):
        return bool(status)
    return bool(status.get("running") or status.get("active") or status.get("playing")
                or status.get("state") not in (None, "", "idle", "stopped", "finished"))


def _best_match(wanted: str, items: list[dict[str, Any]], *, extra: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """The item whose id / name best matches ``wanted`` ("peekaboo", "Old MacDonald", "the bus song")."""
    key = _norm(wanted)
    if not key:
        return None
    for item in items:  # exact id or name
        if key in (_norm(item.get("id")), _norm(item.get("name"))):
            return item
    words = set(key.split()) - {"the", "a", "an", "game", "activity", "song", "play", "lets", "start", "one"}
    best, score = None, 0.0
    for item in items:
        hay = " ".join([_norm(item.get("id")), _norm(item.get("name"))]
                       + [_norm(" ".join(map(str, item.get(e) or []))) for e in extra])
        if key in hay:
            s = 10.0 + len(key)
        else:
            hits = sum(1 for w in words if w in hay.split() or (len(w) > 3 and w in hay))
            s = hits / max(1, len(words)) if hits else 0.0
        if s > score:
            best, score = item, s
    return best if score >= 0.5 else None


_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _video_id(text: str) -> str | None:
    """An 11-character video id, also from a pasted YouTube link."""
    if _YT_ID.fullmatch(text):
        return text
    try:
        from littlebot.media.allowlist import parse_youtube_url  # noqa: PLC0415

        parsed = parse_youtube_url(text, kind="video")
    except Exception:  # noqa: BLE001
        return None
    return parsed.video_id if parsed.video_id and _YT_ID.fullmatch(parsed.video_id) else None


def _approved_result(item: dict[str, Any]) -> dict[str, Any]:
    return {"video_id": f"approved:{item.get('id')}", "approved_id": item.get("id"), "title": item.get("title"),
            "channel": None, "duration_seconds": None, "kind": item.get("kind")}


def _brief(r: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"video_id": r.get("video_id"), "title": r.get("title")}
    if r.get("channel"):
        out["channel"] = r["channel"]
    if r.get("duration_seconds"):
        out["minutes"] = round(float(r["duration_seconds"]) / 60, 1)
    if r.get("approved_id") is not None:
        out["approved"] = True
    if r.get("kind") == "playlist":
        out["playlist"] = True
    return out


def speakable_title(title: Any) -> str:
    """A short title that is safe to say out loud ("Baby Shark Dance | #babyshark ..." -> "Baby Shark Dance").

    Titles are cut at the first separator and hashtags are dropped; a title the reply checks
    would stop (brand or website words) becomes "the video"."""
    text = str(title or "").strip()
    text = re.split(r"\s+[|\-–—:•]\s+|[|(\[【]", text, maxsplit=1)[0]
    text = re.sub(r"#\S+", "", text)
    text = " ".join(text.split())[:60].strip(" -,.")
    if not text:
        return "the video"
    try:
        from littlebot.safety.content import find_blocked  # noqa: PLC0415

        if find_blocked(text):
            return "the video"
    except Exception:  # noqa: BLE001
        pass
    return text


def _machine_uptime_hours() -> float | None:
    try:
        return round(float(Path("/proc/uptime").read_text().split()[0]) / 3600, 1)
    except (OSError, ValueError, IndexError):
        return None


def wifi_status(path: str | Path = "/proc/net/wireless") -> dict[str, Any]:
    """Wi-Fi link from /proc/net/wireless (+ the network name from iwgetid, if installed). Blocking."""
    out: dict[str, Any] = {"connected": False}
    try:
        lines = Path(path).read_text().splitlines()[2:]
    except OSError:
        lines = []
    for line in lines:
        name, _, rest = line.partition(":")
        parts = rest.split()
        if not name.strip() or len(parts) < 3:
            continue
        try:
            quality = float(parts[1].rstrip("."))
            level = float(parts[2].rstrip("."))
        except ValueError:
            continue
        out = {"connected": True, "interface": name.strip(), "quality_percent": round(min(100.0, quality / 70 * 100)),
               "signal_dbm": level if level < 0 else None}
        break
    if not lines:
        out["detail"] = "no Wi-Fi adapter found (maybe a cable, or not a Pi)"
    exe = shutil.which("iwgetid")
    if out.get("connected") and exe:
        try:
            ssid = subprocess.run([exe, "-r"], capture_output=True, text=True, timeout=2, check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            ssid = ""
        if ssid:
            out["network"] = ssid[:64]
    return out


__all__ = ["Pending", "RobotToolbox", "TOOLS", "TOOL_NAMES", "speakable_title", "wifi_status"]
