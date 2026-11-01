"""Heads-ups: LittleBot speaks up by itself, briefly, when something is coming, and tells the family's phones.

* **Calendar**: "Heads up: Dentist at 3 pm, in 30 minutes." ``heads_up.calendar_minutes`` before an event with a time
  (from the calendar skill's copy, littlebot/skills/agenda.py), once per event; not for one added a moment ago.
* **Weather**: "Heads up: rain is likely around 3 pm. Take an umbrella if you go out." Once when rain, snow, freezing
  rain or storms are likely (``WET_CHANCE``) to start within ``WEATHER_AHEAD_H`` hours while it is dry now; at most
  once every ``WEATHER_GAP_S``, between ``WEATHER_HOURS``. Checked every ``WEATHER_EVERY_S`` from Open-Meteo.
* **Reminders**: a reminder that rings (the reminders skill already says it) goes to the phones too.

Where it goes: out loud (a chime, the sentence, a card; through the conversation when one is on) unless it is quiet
hours (``heads_up.quiet_hours``), LittleBot is asleep, or the speaker is switched off for heads-ups; and to the phones
(ntfy, littlebot/skills/push.py) once ``scripts/setup_phone.py`` has set them up. A weather heads-up that comes while
a video plays or LittleBot is busy talking waits for a quiet moment (up to ``WEATHER_WAIT_S``).

The settings (``heads_up.*`` in the dashboard's settings) change by voice: ``heads_up_settings`` ("stop telling me
about the weather", "tell me an hour before", "don't speak up after 9"). ``send_to_phone`` sends what they ask
("send the shopping list to my phone").
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
from typing import Any, Callable

from littlebot.skills import when as W
from littlebot.skills.base import BOOL, INT, STR, Skill, ToolSpec, err, ok, tool
from littlebot.skills.push import Push

log = logging.getLogger("littlebot.skills.heads_up")

TICK_S = 30.0
FIRST_TICK_S = 45.0
WEATHER_EVERY_S = 20 * 60.0
WEATHER_GAP_S = 6 * 3600.0
WEATHER_AHEAD_H = 3
WEATHER_HOURS = (7, 20)  # local hours a weather heads-up may be given (from, to)
WEATHER_WAIT_S = 30 * 60.0
WET_CHANCE = 60  # percent
SOON_MARGIN_S = 90.0  # an event this much inside its heads-up time still gets one (the check runs every TICK_S)
MAX_REMEMBERED = 200
WET_WORDS = ("thunder", "freezing", "snow", "rain", "drizzle", "showers", "hail")
QUIET_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*$")
KEYS = ("heads_up.enabled", "heads_up.calendar_minutes", "heads_up.weather", "heads_up.speaker", "heads_up.phone",
        "heads_up.quiet_hours")


def parse_quiet(text: Any) -> tuple[int, int] | None:
    """ "21:30-07:00" -> (1290, 420) minutes after midnight; None for none ("", "off") or junk."""
    m = QUIET_RE.match(str(text or ""))
    if m is None:
        return None
    a = int(m.group(1)) * 60 + int(m.group(2) or 0)
    b = int(m.group(3)) * 60 + int(m.group(4) or 0)
    if a >= 24 * 60 or b >= 24 * 60 or a == b:
        return None
    return a, b


def in_quiet(local: _dt.datetime, quiet: tuple[int, int] | None) -> bool:
    if quiet is None:
        return False
    m = local.hour * 60 + local.minute
    a, b = quiet
    return a <= m < b if a < b else (m >= a or m < b)


def wet_kind(conditions: Any) -> str | None:
    """ "light rain showers" -> "rain"; None when it isn't rain, snow or a storm."""
    c = str(conditions or "").lower()
    if "thunder" in c:
        return "thunderstorms"
    if "freezing" in c and "fog" not in c:
        return "freezing rain"
    if "snow" in c:
        return "snow"
    if any(w in c for w in ("rain", "drizzle", "showers")):
        return "rain"
    return None


def weather_words(kind: str, at: str, *, within_hour: bool) -> str:
    when = "within the hour" if within_hour else f"around {at}"
    if kind == "thunderstorms":
        return f"Heads up: thunderstorms are likely {when}."
    if kind == "freezing rain":
        return f"Heads up: freezing rain is likely {when}, so roads and paths may be icy."
    if kind == "snow":
        return f"Heads up: snow is likely {when}. Dress warmly if you go out."
    return f"Heads up: rain is likely {when}. Take an umbrella if you go out."


class HeadsUp(Skill):
    name = "heads_up"

    def __init__(self, ctx: Any, *, now: Callable[[], float] | None = None, push: Push | None = None) -> None:
        super().__init__(ctx, **({"now": now} if now is not None else {}))
        self.push = push or Push(ctx)
        self._told: dict[str, float] = {}  # what was already given (event ids, "weather")
        self._weather_t = -1e9
        self._weather_wait: tuple[float, str, str] | None = None  # (since, words, phone title)
        self._last_weather: float = -1e9
        self._task: asyncio.Task[None] | None = None
        self._subs: list[Any] = []
        self._jobs: set[asyncio.Task[Any]] = set()
        self.sent: list[dict[str, Any]] = []  # the last few, for the status and tests

    # ------------------------------------------------------------ settings
    def _cfg(self) -> Any:
        return getattr(self.ctx.config, "skills", None)

    def setting(self, key: str) -> Any:
        default = {"enabled": getattr(self._cfg(), "heads_up", True),
                   "calendar_minutes": getattr(self._cfg(), "heads_up_calendar_minutes", 30),
                   "weather": getattr(self._cfg(), "heads_up_weather", True), "speaker": True, "phone": True,
                   "quiet_hours": getattr(self._cfg(), "heads_up_quiet_hours", "21:30-07:00")}[key]
        settings = getattr(self.ctx, "settings", None)
        try:
            return settings.get(f"heads_up.{key}", default) if settings is not None else default
        except Exception:  # noqa: BLE001
            return default

    def enabled(self) -> bool:
        return bool(self.setting("enabled"))

    def lead_minutes(self) -> int:
        try:
            return max(0, min(240, int(self.setting("calendar_minutes") or 0)))
        except (TypeError, ValueError):
            return 30

    def quiet(self) -> tuple[int, int] | None:
        return parse_quiet(self.setting("quiet_hours"))

    def phone_on(self) -> bool:
        return bool(self.setting("phone")) and self.push.set_up()

    def available(self) -> bool:
        return True  # the settings tool is always there; what's given depends on the settings

    def local(self) -> _dt.datetime:
        cal = self._calendar()
        if cal is not None:
            return cal.local()
        from littlebot.info.clock import zone  # noqa: PLC0415

        try:
            tz = zone(getattr(self.ctx.config.info, "timezone", "") or None)
        except ValueError:
            tz = None
        return W.now_local(self.now(), tz)

    def _calendar(self) -> Any:
        hub = getattr(self.ctx, "skills", None)
        try:
            cal = hub.get("calendar") if hub is not None and hasattr(hub, "get") else None
            return cal if cal is not None and cal.available() else None
        except Exception:  # noqa: BLE001
            return None

    def _twelve(self) -> bool:
        try:
            from littlebot.screens.cards import candy  # noqa: PLC0415

            return bool(candy.uses_12h(self.ctx))
        except Exception:  # noqa: BLE001
            return True

    # ------------------------------------------------------------ background
    async def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="heads-up")
        try:
            from littlebot.skills.reminders import RINGING  # noqa: PLC0415

            self._subs.append(self.ctx.bus.subscribe(RINGING, self._on_reminder))
        except Exception:  # noqa: BLE001
            log.debug("heads-up: no reminders feed", exc_info=True)

    async def stop(self) -> None:
        for s in self._subs:
            s.cancel()
        self._subs.clear()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_TICK_S)
        while True:
            await self.tick()
            await asyncio.sleep(TICK_S)

    async def tick(self) -> None:
        """One look at what's coming. Never raises."""
        if not self.enabled():
            return
        for step in (self._calendar_step, self._weather_step):
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the next tick tries again
                log.warning("heads-up: a check failed", exc_info=True)

    async def drain(self) -> None:
        """Wait for the phone notifications it started (tests)."""
        while self._jobs:
            await asyncio.gather(*list(self._jobs), return_exceptions=True)

    # ------------------------------------------------------------ calendar
    async def _calendar_step(self) -> None:
        lead = self.lead_minutes()
        cal = self._calendar()
        if not lead or cal is None:
            return
        now = self.local()
        for ev in cal.soon(lead * 60 + SOON_MARGIN_S):
            key = f"cal:{ev.source}:{ev.calendar_id}:{ev.id}:{ev.start.isoformat()}"
            if key in self._told:
                continue
            self._remember(key)
            minutes = max(1, round((ev.start - now).total_seconds() / 60))
            clock = W.clock_words(ev.start.astimezone(now.tzinfo))  # spoken: "3 pm" (the card follows the home)
            where = f", at {ev.location}" if ev.location else ""
            title = ev.title[:1].upper() + ev.title[1:]
            text = f"Heads up: {title} at {clock}, in {_minutes(minutes)}{where}."
            card = None
            try:
                day = ev.start.astimezone(now.tzinfo).date()
                events = cal.cached(cal._day_start(day), cal._day_start(day + _dt.timedelta(days=1))) or [ev]  # noqa: SLF001
                card = cal.card_spec(events, "Coming up", highlight=ev.key, linger=20.0)
            except Exception:  # noqa: BLE001
                card = None
            self.give(text, kind="calendar", card=card, phone_title=f"{title} in {_minutes(minutes)}",
                      phone_text=f"{clock}{where}".lstrip(", "), tags=("calendar",))

    # ------------------------------------------------------------ weather
    async def _weather_step(self) -> None:
        now_t = self.now()
        if self._weather_wait is not None:
            since, words, title = self._weather_wait
            if now_t - since > WEATHER_WAIT_S:
                self._weather_wait = None
                log.info("heads-up: the weather heads-up waited too long for a quiet moment; dropped")
            elif not self._busy():
                self._weather_wait = None
                self.give(words, kind="weather", phone_title=title, phone_text=words.removeprefix("Heads up: "),
                          tags=("umbrella",), phone=False)  # the phone had it when it was found
            return
        if not self.setting("weather") or now_t - self._weather_t < WEATHER_EVERY_S:
            return
        local = self.local()
        if not WEATHER_HOURS[0] <= local.hour < WEATHER_HOURS[1] or now_t - self._last_weather < WEATHER_GAP_S:
            return
        self._weather_t = now_t
        found = await self.wet_soon()
        if found is None:
            return
        kind, at, within_hour = found
        self._last_weather = now_t
        words = weather_words(kind, at, within_hour=within_hour)
        title = f"{kind.capitalize()} {'soon' if within_hour else 'around ' + at}"
        if self._busy() and self._speaker_ok():
            self._weather_wait = (now_t, words, title)
            if self.phone_on():
                self._spawn(self.push.send(title, words.removeprefix("Heads up: "), tags=("umbrella",)))
            return
        self.give(words, kind="weather", phone_title=title, phone_text=words.removeprefix("Heads up: "),
                  tags=("umbrella",))

    async def wet_soon(self) -> tuple[str, str, bool] | None:
        """(kind, "3 pm", within the hour) when rain / snow / storms are likely to start soon while it's dry now."""
        from littlebot.info.weather import get_weather  # noqa: PLC0415

        svc = get_weather(self.ctx)
        if getattr(svc, "blocked_reason", lambda: None)() is not None:
            return None
        try:
            report = await svc.report(None, days=1, hourly=True)
        except Exception as e:  # noqa: BLE001 - try again next time
            log.info("heads-up: no forecast (%s)", getattr(e, "kind", type(e).__name__))
            return None
        if wet_kind((report.get("now") or {}).get("conditions")) or (report.get("now") or {}).get("precipitation_mm"):
            return None  # it's wet already: they know
        for i, hour in enumerate((report.get("hour_by_hour") or [])[:WEATHER_AHEAD_H]):
            kind = wet_kind(hour.get("conditions"))
            chance = hour.get("chance_of_rain_or_snow_percent")
            if kind and isinstance(chance, (int, float)) and chance >= WET_CHANCE:
                return kind, _clock_text(str(hour.get("time") or ""), True), i == 0
        return None

    # ------------------------------------------------------------ reminders
    def _on_reminder(self, event: Any) -> None:
        if not self.enabled() or not self.phone_on() or event.get("kind") != "reminder":
            return
        label = str(event.get("label") or "reminder")
        self._spawn_threadsafe(self.push.send(f"Reminder: {label}"[:120], label, tags=("bell",), priority="high"))

    # ------------------------------------------------------------ giving it
    def _speaker_ok(self) -> bool:
        if not self.setting("speaker") or in_quiet(self.local(), self.quiet()):
            return False
        try:
            from littlebot.core.state import RobotState  # noqa: PLC0415

            if self.ctx.state.state == RobotState.SLEEPING:
                return False
        except Exception:  # noqa: BLE001
            pass
        return True

    def _busy(self) -> bool:
        """A video playing or a conversation going on: a weather heads-up waits."""
        media = getattr(self.ctx, "media", None)
        try:
            if media is not None and (media.status() or {}).get("state") in ("loading", "playing"):
                return True
        except Exception:  # noqa: BLE001
            pass
        assistant = getattr(self.ctx, "assistant", None)
        try:
            return bool(assistant is not None and assistant.status().get("active"))
        except Exception:  # noqa: BLE001
            return False

    def give(self, text: str, *, kind: str, card: dict[str, Any] | None = None, phone_title: str = "",
             phone_text: str = "", tags: tuple[str, ...] = (), phone: bool = True) -> None:
        spoken = self._speaker_ok()
        if spoken:
            self._sound("chime")
            self.announce(text, kind="heads_up", card=card)
        to_phone = phone and self.phone_on()
        if to_phone:
            self._spawn(self.push.send(phone_title or text, phone_text or text, tags=tags))
        self.sent = (self.sent + [{"kind": kind, "at": self.now(), "spoken": spoken, "phone": to_phone}])[-10:]
        log.info("heads-up: %s (%s)", kind, ", ".join(w for w, on in (("said", spoken), ("phone", to_phone)) if on)
                 or "nowhere: quiet hours and no phone")

    def _remember(self, key: str) -> None:
        self._told[key] = self.now()
        if len(self._told) > MAX_REMEMBERED:
            for old in sorted(self._told, key=self._told.get)[:MAX_REMEMBERED // 4]:  # type: ignore[arg-type]
                self._told.pop(old, None)

    def _sound(self, name: str) -> None:
        audio = getattr(self.ctx, "audio", None)
        if audio is None:
            return
        try:
            self._spawn(audio.play_sound(name, wait=False))
        except Exception:  # noqa: BLE001
            log.debug("heads-up: no sound", exc_info=True)

    def _spawn(self, coro: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    def _spawn_threadsafe(self, coro: Any) -> None:
        try:
            asyncio.get_running_loop()
            self._spawn(coro)
            return
        except RuntimeError:
            pass
        runtime = getattr(self.ctx, "runtime", None)
        if runtime is not None and getattr(runtime, "running", False):
            runtime.submit(coro)
        else:
            coro.close()

    # ------------------------------------------------------------ for the brain
    def tools(self) -> list[ToolSpec]:
        out = [tool("heads_up_settings", "Change how LittleBot speaks up by itself (calendar heads-ups before events, "
                    "rain or snow coming, and copies to the phone). Only pass what they want changed. 'Stop telling "
                    "me about the weather', 'tell me an hour before my meetings', 'don't speak up after 9 pm', "
                    "'stop the heads-ups'. With nothing passed, it says the current settings.",
                    {"enabled": {**BOOL, "description": "all heads-ups on or off"},
                     "calendar_minutes": {**INT, "minimum": 0, "maximum": 240,
                                          "description": "minutes before a calendar event (0: none)"},
                     "weather": {**BOOL, "description": "rain / snow / storm heads-ups"},
                     "speaker": {**BOOL, "description": "said out loud (false: only the phone)"},
                     "phone": {**BOOL, "description": "also sent to the phone"},
                     "quiet_hours": {**STR, "description": "'21:30-07:00' (nothing said out loud then), or 'off'"}})]
        if self.push.set_up():
            out.append(tool("send_to_phone", "Send a message to the family's phones (a notification): 'send the "
                            "shopping list to my phone', 'text me that recipe'. Write the full text to send.",
                            {"title": {**STR, "description": "a short title"},
                             "text": {**STR, "description": "what to send, in full"}}, ("text",)))
        return out

    def prompt_note(self) -> str:
        return ("Heads-ups: LittleBot speaks up by itself before calendar events and when rain or snow is coming "
                "(lines starting 'Heads up:' were said by LittleBot on its own). Change that with heads_up_settings."
                + (" send_to_phone sends a notification to their phone." if self.push.set_up() else ""))

    def context_note(self) -> str:
        if not self.enabled():
            return "Heads-ups: off."
        parts = []
        lead = self.lead_minutes()
        if lead and self._calendar() is not None:
            parts.append(f"calendar {lead} minutes before")
        if self.setting("weather"):
            parts.append("rain/snow coming")
        where = []
        if self.setting("speaker"):
            q = self.quiet()
            where.append("said out loud" + (f" (not {_quiet_text(q)})" if q else ""))
        if self.phone_on():
            where.append("sent to the phone")
        return f"Heads-ups: {', '.join(parts) or 'none'}; {' and '.join(where) or 'given nowhere'}."

    def status(self) -> dict[str, Any]:
        return {**super().status(), "enabled": self.enabled(), "phone_set_up": self.push.set_up(),
                "phone_problem": self.push.last_error, "sent": list(self.sent)}

    async def run(self, name: str, args: dict[str, Any]) -> str:
        args = args if isinstance(args, dict) else {}
        if name == "heads_up_settings":
            return self._settings(args)
        if name == "send_to_phone":
            text = str(args.get("text") or "").strip()
            if not text:
                return err("say what to send (text)")
            if not self.push.set_up():
                return err("the phone isn't set up yet (scripts/setup_phone.py)")
            title = " ".join(str(args.get("title") or "From LittleBot").split())[:80]
            if await self.push.send(title, text, tags=("robot",)):
                return ok("sent to the phone")
            return err(f"it didn't go ({self.push.last_error or 'unknown'})")
        return err(f"heads_up has no tool called {str(name)[:40]}")

    def _settings(self, args: dict[str, Any]) -> str:
        settings = getattr(self.ctx, "settings", None)
        changed = []
        for key in ("enabled", "calendar_minutes", "weather", "speaker", "phone", "quiet_hours"):
            if key not in args or args[key] is None:
                continue
            value = args[key]
            if key == "quiet_hours":
                text = str(value).strip().lower()
                if text in ("off", "none", "never", "no", ""):
                    value = ""
                elif parse_quiet(text) is None:
                    return err("quiet_hours is like '21:30-07:00' (24-hour times), or 'off'")
                else:
                    value = text.replace(" ", "")
            elif key == "calendar_minutes":
                try:
                    value = max(0, min(240, int(value)))
                except (TypeError, ValueError):
                    return err("calendar_minutes must be a number")
            else:
                value = bool(value)
            try:
                if settings is not None:
                    settings.set(f"heads_up.{key}", value, source="voice")
            except (KeyError, ValueError):
                return err("that setting can't be changed here")
            changed.append(key)
        now = self.context_note().removeprefix("Heads-ups: ")
        if args.get("phone") is True and not self.push.set_up():
            now += " (the phone isn't set up yet: scripts/setup_phone.py)"
        return ok(("changed. Now: " if changed else "now: ") + now)


def _minutes(n: int) -> str:
    if n >= 60 and n % 60 == 0:
        h = n // 60
        return f"{h} hour{'s' if h != 1 else ''}"
    if n > 60:
        return f"{n // 60} hour{'s' if n // 60 != 1 else ''} {n % 60} minutes"
    return f"{n} minute{'s' if n != 1 else ''}"


def _clock_text(hhmm: str, twelve: bool) -> str:
    m = re.match(r"(\d{1,2}):(\d{2})", hhmm)
    if m is None:
        return hhmm or "later"
    return W.clock_words(_dt.datetime(2000, 1, 1, int(m.group(1)), int(m.group(2))), twelve)


def _quiet_text(q: tuple[int, int]) -> str:
    def t(m: int) -> str:
        return W.clock_words(_dt.datetime(2000, 1, 1, m // 60, m % 60))
    return f"{t(q[0])} to {t(q[1])}"


__all__ = ["HeadsUp", "in_quiet", "parse_quiet", "weather_words", "wet_kind"]
