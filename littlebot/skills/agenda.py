"""Calendar: "what's on today?", "when is the dentist?", "am I free Friday afternoon?", "add the dentist on Tuesday
at 3", "move it to 4", "cancel it".

Where the events come from (either or both, docs/CALENDAR.md):

* **Google Calendar**, signed in once with ``scripts/setup_calendar.py`` (littlebot/skills/google_calendar.py): every
  calendar ticked in Google Calendar's own list is read, and events can be added, moved, renamed and cancelled.
* **iCal addresses** (``CALENDAR_ICS_URL`` in .env or ``data/calendar_ics_url``, several separated by spaces):
  Google's, Outlook's or iCloud's "secret address", read only (littlebot/skills/ics.py). The address is a secret: it
  is never logged or shown.

The next ``AHEAD_DAYS`` days are fetched in the background every ``REFRESH_S`` (while cloud communication is allowed)
and kept in memory only: the turn's context (what's on today and tomorrow, so "what's next?" needs no tool), the
morning briefing, the day at a glance and the heads-ups (littlebot/skills/heads_up.py) read that copy. The tools ask
Google fresh, and fall back to that copy when it can't answer.

The brain refers to an event by a short id ("e3") that stays the same while LittleBot runs. Cancelling one needs the
user's yes (``confirmed``). A repeating event is moved or cancelled only that once.

Whose calendar (each grown-up on Who's who can have their own):

* **Accounts**: the household's (``data/google_calendar.json``, the iCal addresses above) and one per person
  (``data/google_calendar_p<id>.json``, ``data/calendar_ics_url_p<id>``), connected on the dashboard or with
  ``scripts/setup_calendar.py --person NAME``. A person's calendars are theirs; the household's are the family's,
  except one named after someone ("Sara's calendar"); the dashboard can say otherwise (``calendar.owners``). The same
  event on two people's calendars (a shared family calendar) is the family's.
* **Who is talking** (voice and face recognition, littlebot/assistant/who.py): "what's on today?" is their own events
  and the family's; "add the dentist on Tuesday" goes on their own calendar. Not sure who it is: everyone's events,
  each with whose it is, and an add asks whose calendar when there is more than one. "What's on Sara's calendar?"
  and "put it on the family calendar" say whose.
* **Heads-ups** name the person whose event it is when there are several ("Heads up, Ali: ...") and go to their
  phone (littlebot/skills/push.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import difflib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from littlebot.skills import when as W
from littlebot.skills.base import BOOL, INT, STR, Skill, ToolSpec, as_json, err, ok, tool
from littlebot.skills.google_calendar import CalendarError, GoogleAuth, GoogleCalendar, event_body

log = logging.getLogger("littlebot.skills.calendar")

TOKEN_FILE = "google_calendar.json"  # the household's Google sign-in
PERSON_TOKEN = "google_calendar_p{id}.json"  # a person's own (Who's who id)
PERSON_ICS = "calendar_ics_url_p{id}"  # a person's own iCal address(es), chmod 600
HOME = "home"  # the household's account
FAMILY_CALENDAR = re.compile(r"\b(family|home|household|shared|holidays?)\b")  # a calendar for everyone, by its name
FAMILY_WORDS = frozenset({"family", "home", "household", "everyone", "everybody", "all", "shared", "the family",
                          "our", "ours", "us"})
MINE_WORDS = frozenset({"me", "my", "mine", "myself", "i"})
REFRESH_S = 300.0  # the background copy is fetched again this often
FIRST_REFRESH_S = 20.0  # after start
STALE_S = 1800.0  # an older copy isn't used for the context, the briefing or heads-ups
AHEAD_DAYS = 8
ICS_TTL_S = 900.0  # an iCal feed is downloaded again after this long
ICS_TIMEOUT_S = 10.0
ICS_MAX_BYTES = 5_000_000
SEARCH_DAYS = 180  # "when is the dentist?" looks this far ahead
MAX_DAYS = 31
MAX_LISTED = 25  # events in one answer
CONTEXT_EVENTS = 6
DEFAULT_MINUTES = 60
MAX_KEYS = 400
CARD_LINGER_S = 15.0  # the card after adding / changing an event (a short look)
MAX_TITLE = 100

_ISO_DATE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")
_CLOCK_ONLY = re.compile(r"^\s*(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?\s*$", re.I)


class AgendaError(Exception):
    """Why there's no answer, in plain words for the model."""


@dataclass
class Event:
    source: str  # google | ics
    id: str
    calendar_id: str
    calendar: str
    title: str
    start: _dt.datetime  # aware
    end: _dt.datetime  # aware, not included
    all_day: bool = False
    location: str = ""
    color: str = ""
    can_edit: bool = False
    recurring: bool = False
    key: str = ""  # the short id the brain uses ("e3")
    extra: dict[str, Any] = field(default_factory=dict)
    account: str = HOME  # whose sign-in / address it came from ("home", "p3")
    owner: int | None = None  # whose event it is (Who's who id); None: the family's

    @property
    def ident(self) -> tuple[str, str, str, str]:
        return self.source, self.account, self.calendar_id, self.id


@dataclass
class Account:
    """One place events come from: the household's, or one person's (their Google sign-in, their iCal addresses)."""

    key: str  # "home" | "p<id>"
    person_id: int | None
    auth: GoogleAuth
    google: GoogleCalendar


@dataclass(frozen=True)
class Speaker:
    """Who is talking (from voice / face recognition), as the calendar needs it."""

    person_id: int
    name: str
    sure: bool


class Calendar(Skill):
    name = "calendar"

    def __init__(self, ctx: Any, *, now: Callable[[], float] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(ctx, **({"now": now} if now is not None else {}))
        self._transport = transport
        token_file = Path(ctx.extras.get("google_calendar_token_file") or ctx.config.data_path / TOKEN_FILE)  # tests
        self._dir = token_file.parent  # the people's own sign-ins and addresses are kept next to it
        self.home = self._make_account(HOME, None, token_file)
        self.auth, self.google = self.home.auth, self.home.google  # the household's (setup script, tests)
        self._accounts: dict[str, Account] = {HOME: self.home}
        self._people_cache: tuple[float, list[dict[str, Any]]] = (-1e9, [])
        self._tz: _dt.tzinfo | None | bool = False
        self._cache: list[Event] = []
        self._cache_range: tuple[_dt.datetime, _dt.datetime] | None = None
        self._cache_t = -1e9
        self._ics: dict[str, tuple[float, Any]] = {}
        self._keys: dict[tuple[str, str, str], str] = {}
        self._by_key: dict[str, Event] = {}
        self._next_key = 1
        self._added: dict[tuple[str, str, str], float] = {}  # events added here (no heads-up right after)
        self._task: asyncio.Task[None] | None = None
        self._problem: str | None = None
        self._add_note = ""  # said with an add made for someone recognised only "probably"
        self.version = 0

    # ------------------------------------------------------------ settings
    def _cfg(self) -> Any:
        return getattr(self.ctx.config, "skills", None)

    def ics_urls(self, account: Account | None = None) -> list[str]:
        """An account's iCal addresses (default: the household's)."""
        if account is None or account.person_id is None:
            raw = str(self.ctx.config.secret("calendar_ics_url") or "")
        else:
            from littlebot.core.keys import read_private  # noqa: PLC0415

            raw = read_private(self._ics_path(account.person_id)) or ""
        return ics_list(raw)

    # ------------------------------------------------------------ whose: people and their accounts
    def _make_account(self, key: str, person_id: int | None, token_file: Path) -> Account:
        auth = GoogleAuth(token_file, transport=self._transport, now=self.now)
        google = GoogleCalendar(auth, transport=self._transport, now=self.now,
                                timeout=lambda: float(getattr(self._cfg(), "calendar_timeout_seconds", 6.0)))
        return Account(key, person_id, auth, google)

    def _token_path(self, person_id: int) -> Path:
        return self._dir / PERSON_TOKEN.format(id=int(person_id))

    def _ics_path(self, person_id: int) -> Path:
        return self._dir / PERSON_ICS.format(id=int(person_id))

    def people(self) -> list[dict[str, Any]]:
        """The grown-ups on Who's who (id, name, role); the little one is left out. Read again every minute."""
        at, cached = self._people_cache
        if self.now() - at < 60.0:
            return cached
        people: list[dict[str, Any]] = []
        try:
            from littlebot.vision.faces import get_faces, is_child  # noqa: PLC0415

            child = None
            with contextlib.suppress(Exception):
                profile = self.ctx.settings.get("profile") or {}
                child = str(profile.get("nickname") or "") or None if isinstance(profile, dict) else None
            for p in get_faces(self.ctx).store.people():
                if not is_child(str(p.get("name") or ""), str(p.get("role") or ""), child):
                    people.append({"id": int(p["id"]), "name": str(p["name"]), "role": str(p.get("role") or "")})
        except Exception:  # noqa: BLE001 - no Who's who: one household calendar
            log.debug("calendar: no Who's who", exc_info=True)
        self._people_cache = (self.now(), people)
        return people

    def forget_people_cache(self) -> None:
        self._people_cache = (-1e9, [])

    def person(self, person_id: int | None) -> dict[str, Any] | None:
        return next((p for p in self.people() if person_id is not None and p["id"] == int(person_id)), None)

    def person_named(self, words: Any) -> dict[str, Any] | None:
        """ "Sara", "Sara's", "her mom" (a role) -> that person on Who's who, or None."""
        said = " ".join(re.sub(r"'s\b|’s\b", "", str(words or "").lower()).split())
        if not said:
            return None
        people = self.people()
        for p in people:
            if p["name"].lower() == said or re.search(rf"\b{re.escape(p['name'].lower())}\b", said):
                return p
        for p in people:
            if p["role"] and re.search(rf"\b{re.escape(p['role'].lower())}\b", said):
                return p
        names = {p["name"].lower(): p for p in people}
        close = difflib.get_close_matches(said, list(names), n=1, cutoff=0.8)
        return names[close[0]] if close else None

    def account(self, person_id: int | None, *, create: bool = False) -> Account | None:
        """The household's account (None) or a person's: theirs once they have a sign-in or an address (``create``:
        also before, for connecting one)."""
        if person_id is None:
            return self.home
        key = f"p{int(person_id)}"
        acc = self._accounts.get(key)
        if acc is None and (create or self._token_path(person_id).exists() or self._ics_path(person_id).exists()):
            acc = self._accounts[key] = self._make_account(key, int(person_id), self._token_path(person_id))
        return acc

    def accounts(self) -> list[Account]:
        """The household's and every grown-up's on Who's who that has a calendar (or is connecting one)."""
        out = [self.home]
        for p in self.people():
            acc = self.account(p["id"])
            if acc is not None:
                out.append(acc)
        return out

    def _sources(self) -> list[tuple[Account, bool, list[str]]]:
        """(account, Google connected, iCal addresses) for every account with something to read."""
        out = []
        for acc in self.accounts():
            urls = self.ics_urls(acc)
            if acc.auth.connected or urls:
                out.append((acc, acc.auth.connected, urls))
        return out

    def owner_of(self, acc: Account, calendar_id: str, calendar_name: str) -> int | None:
        """Whose a calendar is: what the dashboard said, else one named after someone ("Sara's calendar") is theirs,
        one called "Family", "Home" or "Holidays" is everyone's, else the account's person (None: the family's)."""
        owners = {}
        with contextlib.suppress(Exception):
            owners = dict(self.ctx.settings.get("calendar.owners") or {})
        for k in (f"{acc.key}:{calendar_id}", acc.key):
            if k in owners:
                try:
                    pid = int(owners[k] or 0)
                except (TypeError, ValueError):
                    pid = 0
                return pid if pid and self.person(pid) is not None else None
        name = str(calendar_name or "").lower()
        for p in self.people():
            if p["id"] != acc.person_id and re.search(rf"\b{re.escape(p['name'].lower())}\b", name):
                return p["id"]
        if FAMILY_CALENDAR.search(name):
            return None
        return acc.person_id

    def owner_name(self, owner: int | None) -> str | None:
        p = self.person(owner)
        return p["name"] if p is not None else None

    def several_owners(self, events: list[Event] | None = None) -> bool:
        """More than one person has their own events: then each event says whose it is."""
        evs = self._cache if events is None else events
        owners = {e.owner for e in evs if e.owner is not None}
        owners |= {a.person_id for a in self.accounts() if a.person_id is not None}
        return len(owners) >= 2

    def speaker(self) -> Speaker | None:
        """Who is talking now (the assistant's voice and face recognition), when they are on Who's who."""
        svc = getattr(self.ctx, "assistant", None)
        try:
            who = svc.speaker() if svc is not None and hasattr(svc, "speaker") else None
        except Exception:  # noqa: BLE001
            who = None
        if who is None or self.person(getattr(who, "person_id", None)) is None:
            return None
        return Speaker(int(who.person_id), str(who.name), bool(getattr(who, "sure", False)))

    def for_person(self, events: list[Event], person_id: int | None) -> tuple[list[Event], dict[int, int]]:
        """``person_id``'s own events and the family's, and how many of each other person's were left out."""
        if person_id is None:
            return events, {}
        mine, others = [], {}
        for e in events:
            if e.owner is None or e.owner == person_id:
                mine.append(e)
            else:
                others[e.owner] = others.get(e.owner, 0) + 1
        return mine, others

    @property
    def can_write(self) -> bool:
        return any(a.auth.connected for a in self.accounts())

    def set_up(self) -> bool:
        return bool(self._sources())

    def available(self) -> bool:
        return bool(getattr(self._cfg(), "calendar", True)) and self.set_up()

    def blocked_reason(self) -> str | None:
        if self.ctx.config.is_offline:
            return "LittleBot is in offline mode, so it can't reach the calendar"
        if not self.ctx.privacy.cloud_enabled:
            return "cloud communication is off in Privacy, so LittleBot can't reach the calendar"
        return None

    def zone(self) -> _dt.tzinfo:
        """The home's time zone (``info.timezone``, else the system's); the robot's fixed offset when unknown."""
        if self._tz is False:
            from littlebot.info.clock import local_zone_name  # noqa: PLC0415

            tz: _dt.tzinfo | None = None
            for name in (str(getattr(self.ctx.config.info, "timezone", "") or "").strip(), local_zone_name() or ""):
                if name:
                    try:
                        tz = ZoneInfo(name)
                        break
                    except (ZoneInfoNotFoundError, ValueError):
                        continue
            self._tz = tz
        return self._tz or _dt.datetime.now().astimezone().tzinfo or _dt.timezone.utc  # type: ignore[return-value]

    def zone_name(self) -> str:
        return getattr(self.zone(), "key", "") or ""

    def local(self, ts: float | None = None) -> _dt.datetime:
        return _dt.datetime.fromtimestamp(self.now() if ts is None else ts, self.zone())

    def _day_start(self, day: _dt.date) -> _dt.datetime:
        return _dt.datetime(day.year, day.month, day.day, tzinfo=self.zone())

    def _twelve(self) -> bool:
        try:
            from littlebot.screens.cards import candy  # noqa: PLC0415

            return bool(candy.uses_12h(self.ctx))
        except Exception:  # noqa: BLE001
            return True

    # ------------------------------------------------------------ fetching
    async def fetch(self, start: _dt.datetime, end: _dt.datetime, query: str | None = None) -> list[Event]:
        """Every source's events overlapping [start, end), soonest first. Raises AgendaError when none answered."""
        why = self.blocked_reason()
        if why:
            raise AgendaError(why)
        jobs: list[Any] = []
        for acc, google, urls in self._sources():
            if google:
                jobs.append(self._google_events(acc, start, end, query))
            for i, url in enumerate(urls):
                jobs.append(self._ics_events(acc, i, url, start, end, query))
        if not jobs:
            raise AgendaError("the calendar isn't set up (docs/CALENDAR.md)")
        results = await asyncio.gather(*jobs, return_exceptions=True)
        events: list[Event] = []
        problems: list[str] = []
        for res in results:
            if isinstance(res, BaseException):
                if isinstance(res, asyncio.CancelledError):
                    raise res
                problems.append(str(res) if isinstance(res, (CalendarError, AgendaError)) else
                                "the calendar couldn't be read")
                if not isinstance(res, (CalendarError, AgendaError)):
                    log.warning("calendar: a source failed (%s)", type(res).__name__)
            else:
                events += res
        self._note_problem(problems[0] if problems else None)
        if problems and len(problems) == len(results):
            raise AgendaError(problems[0])
        return self._sorted(_dedupe(events))

    async def _google_events(self, acc: Account, start: _dt.datetime, end: _dt.datetime,
                             query: str | None) -> list[Event]:
        found = await acc.google.events(start, end, self.zone(), query=query)
        return [self._google_event(acc, e) for e in found]

    def _google_event(self, acc: Account, e: dict[str, Any]) -> Event:
        ev = Event(source="google", account=acc.key, **{k: v for k, v in e.items() if k in _EVENT_FIELDS})
        ev.owner = self.owner_of(acc, ev.calendar_id, ev.calendar)
        return self._remember(ev)

    async def _ics_events(self, acc: Account, index: int, url: str, start: _dt.datetime, end: _dt.datetime,
                          query: str | None) -> list[Event]:
        from littlebot.skills import ics  # noqa: PLC0415

        feed = await self._ics_feed(index, url)
        out = []
        for occ in ics.occurrences(feed, start, end, self.zone(), limit=500):
            if query and not _matches(query, f"{occ.title} {occ.location}"):
                continue
            ev = Event(source="ics", account=acc.key, id=f"{occ.uid}@{occ.start.isoformat()}",
                       calendar_id=f"ics{index}", calendar=occ.calendar or "Calendar",
                       title=" ".join(occ.title.split())[:MAX_TITLE] or "(no title)", start=occ.start, end=occ.end,
                       all_day=occ.all_day, location=occ.location)
            ev.owner = self.owner_of(acc, ev.calendar_id, ev.calendar)
            out.append(self._remember(ev))
        return out

    async def _ics_feed(self, index: int, url: str) -> Any:
        from littlebot.skills import ics  # noqa: PLC0415

        hit = self._ics.get(url)
        if hit is not None and self.now() - hit[0] < ICS_TTL_S:
            return hit[1]
        try:
            async with httpx.AsyncClient(transport=self._transport, follow_redirects=True, max_redirects=3,
                                         timeout=httpx.Timeout(ICS_TIMEOUT_S, connect=5.0)) as client:
                resp = await client.get(url, headers={"Accept": "text/calendar"})
        except httpx.TimeoutException:
            raise AgendaError("the iCal calendar didn't answer in time") from None
        except httpx.HTTPError:
            raise AgendaError("the iCal calendar couldn't be reached") from None
        if resp.status_code >= 400:
            log.info("calendar: iCal feed %d answered HTTP %d", index + 1, resp.status_code)
            raise AgendaError("the iCal calendar's address didn't work (it may have been reset)")
        if len(resp.content) > ICS_MAX_BYTES:
            raise AgendaError("the iCal calendar is too big to read")
        feed = await asyncio.to_thread(ics.parse, resp.content.decode("utf-8", errors="replace"))
        self._ics[url] = (self.now(), feed)
        return feed

    def _note_problem(self, problem: str | None) -> None:
        if problem != self._problem:
            if problem:
                log.info("calendar: %s", problem)
            elif self._problem:
                log.info("calendar: working again")
        self._problem = problem

    def _remember(self, ev: Event) -> Event:
        """Give the event its short id (the same one each time it is seen)."""
        key = self._keys.get(ev.ident)
        if key is None:
            key = f"e{self._next_key}"
            self._next_key += 1
            self._keys[ev.ident] = key
            if len(self._keys) > MAX_KEYS:
                old = next(iter(self._keys))
                self._by_key.pop(self._keys.pop(old), None)
        ev.key = key
        self._by_key[key] = ev
        return ev

    def _sorted(self, events: list[Event]) -> list[Event]:
        return sorted(events, key=lambda e: (e.start, not e.all_day, e.title.lower()))

    def event(self, key: Any) -> Event | None:
        return self._by_key.get(str(key or "").strip().lower())

    # ------------------------------------------------------------ the background copy
    async def refresh(self) -> bool:
        """Fetch today and the next days into the copy. True when it worked."""
        today = self.local().date()
        start, end = self._day_start(today), self._day_start(today + _dt.timedelta(days=AHEAD_DAYS))
        try:
            events = await self.fetch(start, end)
        except AgendaError:
            return False
        self._cache, self._cache_range, self._cache_t = events, (start, end), self.now()
        self.version += 1
        return True

    def fresh(self, max_age: float = STALE_S) -> bool:
        return self._cache_range is not None and self.now() - self._cache_t < max_age

    def cached(self, start: _dt.datetime, end: _dt.datetime) -> list[Event] | None:
        """The copy's events overlapping [start, end); None when the copy doesn't cover that or is too old."""
        r = self._cache_range
        if r is None or not self.fresh() or start < r[0] or end > r[1]:
            return None
        return [e for e in self._cache
                if (e.end > start and e.start < end) or (e.start == e.end and start <= e.start < end)]

    async def events_in(self, start: _dt.datetime, end: _dt.datetime, query: str | None = None) -> list[Event]:
        """Fresh from the sources (the copy when it was fetched a moment ago, or when they don't answer)."""
        if not query and self.fresh(60.0):
            got = self.cached(start, end)
            if got is not None:
                return got
        try:
            return await self.fetch(start, end, query)
        except AgendaError:
            got = None if query else self.cached(start, end)
            if got is None:
                raise
            return got

    def soon(self, seconds: float) -> list[Event]:
        """Events with a time starting in the next ``seconds`` (from the copy), for heads-ups."""
        now = self.local()
        out = []
        for e in self._cache if self.fresh() else []:
            if e.all_day or not now < e.start <= now + _dt.timedelta(seconds=seconds):
                continue
            if self.now() - self._added.get(e.ident, -1e9) < 600:
                continue  # added here a moment ago: they know
            out.append(e)
        return out

    async def start(self) -> None:
        if bool(getattr(self._cfg(), "calendar", True)):
            self._task = asyncio.get_running_loop().create_task(self._loop(), name="calendar")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for acc in list(self._accounts.values()):
            await acc.google.close()

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_REFRESH_S)
        while True:
            try:
                for acc in self.accounts():
                    if not acc.auth.connected:
                        acc.auth.reload()  # scripts/setup_calendar.py may have connected it since
                if self.available() and self.blocked_reason() is None:
                    await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - try again next time
                log.warning("calendar: the background refresh failed", exc_info=True)
            await asyncio.sleep(REFRESH_S)

    # ------------------------------------------------------------ words
    def day_label(self, day: _dt.date, *, short: bool = False) -> str:
        today = self.local().date()
        if day == today:
            return "Today" if short else "today"
        if day == today + _dt.timedelta(days=1):
            return "Tomorrow" if short else "tomorrow"
        if short:
            return f"{day:%a} {day.day} {day:%b}"
        year = f" {day.year}" if day.year != today.year else ""
        return f"{day:%A} {day.day} {day:%B}{year}"

    def time_words(self, e: Event) -> str:
        """ "3 pm to 4 pm", "all day": for the brain and for speaking (always 12 hours; the card follows the home)."""
        twelve = True
        if e.all_day:
            last = (e.end - _dt.timedelta(seconds=1)).date()
            return "all day" if last <= e.start.date() else f"all day, until {self.day_label(last)}"
        start, end = e.start.astimezone(self.zone()), e.end.astimezone(self.zone())
        if end <= start:
            return W.clock_words(start, twelve)
        if end.date() != start.date():
            return f"{W.clock_words(start, twelve)} until {self.day_label(end.date())} {W.clock_words(end, twelve)}"
        return f"{W.clock_words(start, twelve)} to {W.clock_words(end, twelve)}"

    def whose(self, e: Event) -> str:
        """ "Ali's" / "the family's", for an event when several people have their own calendars."""
        name = self.owner_name(e.owner)
        return f"{name}'s" if name else "the family's"

    def _for_model(self, e: Event, now: _dt.datetime, *, whose: bool = False) -> dict[str, Any]:
        day = e.start.astimezone(self.zone()).date()
        if e.all_day:  # one that began before today is on today
            day = max(day, now.date())
        out: dict[str, Any] = {"id": e.key, "title": e.title, "day": self.day_label(day), "time": self.time_words(e)}
        if e.location:
            out["where"] = e.location
        if e.calendar:
            out["calendar"] = e.calendar
        if whose:
            out["whose"] = self.whose(e)
        if e.end <= now and e.end > e.start or (e.end == e.start and e.start <= now):
            out["over"] = True
        elif e.start <= now < e.end and not e.all_day:
            out["now"] = True
        if e.recurring:
            out["repeats"] = True
        return out

    def card_spec(self, events: list[Event], title: str, *, empty: str = "", highlight: str = "",
                  linger: float | None = None) -> dict[str, Any]:
        twelve = self._twelve()
        today = self.local().date()
        whose = self.several_owners()
        items = []
        for e in events[:60]:
            day = max(e.start.astimezone(self.zone()).date(), today) if e.all_day else \
                e.start.astimezone(self.zone()).date()
            name = self.owner_name(e.owner) if whose else None
            items.append({"id": e.key, "day": self.day_label(day, short=True),
                          "when": "All day" if e.all_day else W.clock_words(e.start.astimezone(self.zone()), twelve),
                          "text": f"{e.title} · {name}" if name else e.title, "where": e.location, "color": e.color})
        spec: dict[str, Any] = {"card": "calendar", "title": title, "items": items}
        if empty:
            spec["empty"] = empty
        if highlight:
            spec["highlight"] = highlight
        if linger:
            spec["linger"] = linger
        return spec

    # ------------------------------------------------------------ notes for the brain and the screen
    def prompt_note(self) -> str:
        write = ("To add one: add_calendar_event, with the start as an ISO-8601 local time ('2026-11-04T15:00', or "
                 "a date with all_day for a whole day; no time given: all day, without asking); confirm it in a few "
                 "words from the result. To move or rename one: change_calendar_event (give the new start with its "
                 "date). To cancel one: delete_calendar_event, which first gives the question to ask them; never "
                 "say it is cancelled (or being cancelled) before they said yes and the result says so. "
                 if self.can_write else "The calendar is read-only here: LittleBot can't add or change events. ")
        people = ""
        if self.several_owners():
            people = ("Each grown-up has their own calendar. Without 'person', calendar_events gives the person "
                      "talking's own events and the family's, and add_calendar_event puts it on their own calendar; "
                      "pass person for someone else's ('Sara') or 'family'. When you don't know who is talking, "
                      "say whose each event is, and ask whose calendar an event goes on. ")
        return ("Calendar: the family's calendar. Today's and tomorrow's events are in the context: a quick "
                "question about one of them ('what's next?', 'when's the dentist?') is answered from it, with no "
                "tool. To go through a day or more (what's on today, Friday, this week), or whether they're free, "
                "call calendar_events first (from: an ISO date or words like 'tomorrow' or 'this week'; days; or "
                "search: a few words; it shows the events on the screen) and then say the answer once, from its "
                "result: never answer first and look it up after. " + people + write +
                "'Remind me' is set_reminder; 'put it on the calendar' is the calendar.")

    def context_note(self) -> str:
        if not self.available() or not self.fresh():
            return ""
        try:
            return self._context()
        except Exception:  # noqa: BLE001
            log.debug("calendar: no context", exc_info=True)
            return ""

    def _whose_view(self, events: list[Event]) -> tuple[list[Event], str, bool]:
        """The events for the person talking (their own and the family's), a few words on whose they are, and
        whether each event should say whose it is (several people's calendars, and not sure who is talking)."""
        if not self.several_owners(events):
            return events, "", False
        who = self.speaker()
        if who is None:
            return events, " (everyone's; not sure who is talking, so say whose)", True
        mine, others = self.for_person(events, who.person_id)
        hidden = ", ".join(f"{self.owner_name(pid) or 'someone'}'s {n}" for pid, n in others.items())
        probably = "" if who.sure else "probably "
        return mine, (f" ({probably}{who.name} is talking: their own and the family's"
                      + (f"; not shown: {hidden}" if hidden else "") + ")"), False

    def _context(self) -> str:
        now = self.local()
        today = now.date()
        start, end = self._day_start(today), self._day_start(today + _dt.timedelta(days=2))
        events = [e for e in self.cached(start, end) or [] if e.end > now or e.all_day and e.end > start]
        events, whose_words, label = self._whose_view(events)
        if not events:
            parts = ["nothing more today or tomorrow"]
        else:
            parts = []
            tomorrow = today + _dt.timedelta(days=1)
            for e in events[:CONTEXT_EVENTS]:  # "tomorrow (Monday)": the brain matches "Monday" to it
                day = f"today ({today:%A})" if e.start.astimezone(self.zone()).date() <= today else \
                    f"tomorrow ({tomorrow:%A})"
                bits = [f"{day} {self.time_words(e)} {e.title} ({e.key}"]
                if label:
                    bits.append(f", {self.whose(e)}")
                if e.location:
                    bits.append(f", at {e.location}")
                if not e.all_day and e.start <= now < e.end:
                    bits.append(", happening now")
                parts.append("".join(bits) + ")")
            if len(events) > CONTEXT_EVENTS:
                parts.append(f"and {len(events) - CONTEXT_EVENTS} more")
        tail = "" if self.can_write else " (read-only)"
        return "Calendar" + tail + whose_words + ": " + "; ".join(parts) + "."

    def briefing_note(self, kind: str = "morning") -> str:
        """Today's events (tomorrow's for the evening), for the briefing: the person talking's and the family's."""
        if not self.available() or not self.fresh():
            return ""
        day = self.local().date() + _dt.timedelta(days=1 if kind == "evening" else 0)
        events = self.cached(self._day_start(day), self._day_start(day + _dt.timedelta(days=1))) or []
        events, whose_words, label = self._whose_view(events)
        label_day = "tomorrow" if kind == "evening" else "today"
        if not events:
            return f"Calendar {label_day}{whose_words}: nothing."
        items = [f"{self.time_words(e)}: {e.title}" + (f" ({self.whose(e)})" if label else "")
                 + (f" at {e.location}" if e.location else "") for e in events[:8]]
        return f"Calendar {label_day}{whose_words}: " + "; ".join(items) + "."

    def glance_lines(self) -> list[str]:
        """The next two events today (else tomorrow's first), for the day at a glance."""
        if not self.available() or not self.fresh():
            return []
        now = self.local()
        start = self._day_start(now.date())
        events = self.cached(start, self._day_start(now.date() + _dt.timedelta(days=2))) or []
        twelve = self._twelve()
        out = []
        for e in events:
            if e.all_day or e.end <= now:
                continue
            day = e.start.astimezone(self.zone()).date()
            prefix = "" if day == now.date() else "Tomorrow "
            if prefix and out:
                break
            out.append(f"{prefix}{W.clock_words(e.start.astimezone(self.zone()), twelve)} {e.title}")
            if len(out) == 2 or prefix:
                break
        return out

    def status(self) -> dict[str, Any]:
        return {**super().status(), "google": self.auth.status(), "ics_feeds": len(self.ics_urls()),
                "people": {p["name"]: {"google": bool(a.auth.connected), "ics_feeds": len(self.ics_urls(a))}
                           for p in self.people() if (a := self.account(p["id"])) is not None},
                "cached": len(self._cache), "cache_age_s": round(self.now() - self._cache_t) if self._cache_range
                else None, "problem": self._problem}

    # ------------------------------------------------------------ setting up (the dashboard, setup_calendar.py)
    def known_client(self) -> tuple[str, str] | None:
        """The Google Cloud client (ID, secret) a connected account was made with: the next person reuses it."""
        for acc in self.accounts():
            t = acc.auth.tokens()
            if t is not None and t.client_id and t.client_secret:
                return t.client_id, t.client_secret
        return None

    def _check_person(self, person_id: int | None) -> None:
        if person_id is not None and self.person(person_id) is None:
            raise AgendaError("that person isn't on Who's who")

    def begin_connect(self, person_id: int | None, client_id: str | None = None,
                      client_secret: str | None = None) -> str:
        """Google's sign-in address for the household (None) or a person. The client comes from the last sign-in
        when none is given. Raises CalendarError (bad_client_id, bad_secret) or AgendaError."""
        self._check_person(person_id)
        if not client_id or not client_secret:
            known = self.known_client()
            if known is None:
                raise CalendarError("bad_client_id")
            client_id, client_secret = known
        acc = self.account(person_id, create=True)
        assert acc is not None
        return acc.auth.begin(client_id, client_secret)

    async def finish_connect(self, person_id: int | None, pasted: str) -> None:
        self._check_person(person_id)
        acc = self.account(person_id, create=True)
        assert acc is not None
        await acc.auth.finish(pasted)
        self._cache_t = -1e9
        await self.refresh()

    def disconnect(self, person_id: int | None) -> bool:
        acc = self.account(person_id)
        if acc is None:
            return False
        had = acc.auth.disconnect()
        self._drop_account(acc.key, source="google")
        return had

    def _ics_file(self, person_id: int | None) -> Path:
        return self._dir / "calendar_ics_url" if person_id is None else self._ics_path(person_id)

    async def set_ics(self, person_id: int | None, text: str) -> int:
        """Save the household's or a person's iCal address(es) once each one has been read: how many events they
        have in the next days. Raises AgendaError (not an address, it didn't work)."""
        from littlebot.core.keys import write_private  # noqa: PLC0415
        from littlebot.skills import ics  # noqa: PLC0415

        self._check_person(person_id)
        urls = ics_list(text)
        if not urls:
            raise AgendaError("that isn't an iCal address (it starts with https:// or webcal://)")
        why = self.blocked_reason()
        if why:
            raise AgendaError(why)
        today = self.local().date()
        start, end = self._day_start(today), self._day_start(today + _dt.timedelta(days=AHEAD_DAYS))
        found = 0
        for i, url in enumerate(urls):
            self._ics.pop(url, None)
            found += len(ics.occurrences(await self._ics_feed(i, url), start, end, self.zone(), limit=500))
        path = self._ics_file(person_id)
        write_private(path.parent, path.name, " ".join(urls))
        if person_id is None:
            from pydantic import SecretStr  # noqa: PLC0415

            self.ctx.config.calendar_ics_url = SecretStr(" ".join(urls))
        else:
            self.account(person_id, create=True)
        log.info("calendar: an iCal address was saved (%d event(s) in the next days)", found)
        self._cache_t = -1e9
        await self.refresh()
        return found

    def remove_ics(self, person_id: int | None) -> bool:
        path = self._ics_file(person_id)
        had = path.exists()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        if person_id is None:
            from pydantic import SecretStr  # noqa: PLC0415

            from littlebot.core.keys import current_env  # noqa: PLC0415

            left = current_env().get("CALENDAR_ICS_URL", "").strip()  # the .env one, if any, is used again
            self.ctx.config.calendar_ics_url = SecretStr(left) if left else None
        self._drop_account(f"p{int(person_id)}" if person_id is not None else HOME, source="ics")
        return had

    def forget_person(self, person_id: int) -> None:
        """Someone was taken off Who's who: their sign-in, addresses and calendar owners go too."""
        acc = self._accounts.get(f"p{int(person_id)}")
        if acc is not None:
            acc.auth.disconnect()
        for path in (self._token_path(person_id), self._ics_path(person_id)):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        self._accounts.pop(f"p{int(person_id)}", None)
        with contextlib.suppress(Exception):
            owners = {k: v for k, v in dict(self.ctx.settings.get("calendar.owners") or {}).items()
                      if not k.startswith(f"p{int(person_id)}:") and k != f"p{int(person_id)}"
                      and str(v) != str(int(person_id))}
            self.ctx.settings.set("calendar.owners", owners, source="dashboard")
        self.forget_people_cache()
        self._drop_account(f"p{int(person_id)}")
        log.info("calendar: a person's calendar sign-in and addresses were removed")

    def set_owner(self, key: str, person_id: int | None) -> None:
        """Whose a calendar ("<account>:<calendar id>") or a whole account ("home") is (None or 0: the family's)."""
        person_id = int(person_id or 0) or None
        self._check_person(person_id)
        owners = dict(self.ctx.settings.get("calendar.owners") or {})
        owners[str(key)[:300]] = int(person_id or 0)
        self.ctx.settings.set("calendar.owners", owners, source="dashboard")
        for e in self._cache:  # the copy says it at once
            acc = self._accounts.get(e.account)
            if acc is not None:
                e.owner = self.owner_of(acc, e.calendar_id, e.calendar)
        self.version += 1

    def _drop_account(self, key: str, *, source: str | None = None) -> None:
        self._cache = [e for e in self._cache if not (e.account == key and (source is None or e.source == source))]
        self.version += 1

    async def calendars_view(self) -> list[dict[str, Any]]:
        """Every calendar read, with whose it is (for the dashboard). Never an address or a token."""
        out: list[dict[str, Any]] = []
        for acc, google, urls in self._sources():
            if google:
                try:
                    cals = await acc.google.calendars()
                except (CalendarError, AgendaError):
                    cals = []
                out += [{"account": acc.key, "id": c["id"], "name": c["name"], "can_edit": bool(c.get("can_edit")),
                         "owner": self.owner_of(acc, c["id"], c["name"]), "source": "google"} for c in cals]
            for i, url in enumerate(urls):
                hit = self._ics.get(url)
                name = getattr(hit[1], "name", "") if hit else ""
                out.append({"account": acc.key, "id": f"ics{i}", "name": name or f"iCal calendar {i + 1}",
                            "can_edit": False, "owner": self.owner_of(acc, f"ics{i}", name), "source": "ics"})
        return out

    # ------------------------------------------------------------ tools
    def tools(self) -> list[ToolSpec]:
        out = [tool("calendar_events", "The family calendar's events for a day or a few days, or the ones matching "
                    "some words, and they are shown on the screen. Use for 'what's on today / tomorrow / Friday / "
                    "this week', 'when is the dentist?', 'am I free Saturday afternoon?'. Each event has an id for "
                    "the other calendar tools.",
                    {"from": {**STR, "description": "the first day: an ISO date ('2026-11-06') or words ('today', "
                                                    "'tomorrow', 'friday', 'this week', 'next week', 'this "
                                                    "weekend'). Default today."},
                     "days": {**INT, "minimum": 1, "maximum": MAX_DAYS,
                              "description": "how many days from 'from' (default 1, or the week's days)"},
                     "search": {**STR, "description": "optional: a few words to find ('dentist'); alone, looks "
                                                      f"{SEARCH_DAYS} days ahead"}})]
        people = self.several_owners()
        if people:
            out[0]["parameters"]["properties"]["person"] = {
                **STR, "description": "optional: whose ('Sara', 'mine', 'everyone'); default the person talking's "
                                      "own and the family's (everyone's when it isn't known who is talking)"}
        if not self.can_write:
            return out
        when_help = ("ISO-8601 local date-time ('2026-11-04T15:00'); or a date ('2026-11-04') with all_day; the "
                     "user's words ('Tuesday at 3pm') work too")
        out += [
            tool("add_calendar_event", "Add an event to the family calendar (Google Calendar). 'Add the dentist on "
                 "Tuesday at 3', 'put Zayn's birthday on the 12th' (no time said: all_day, don't ask for one). The "
                 "result says exactly what was added.",
                 {"title": {**STR, "description": "short, as it should appear: 'Dentist', 'Zayn's birthday'"},
                  "start": {**STR, "description": when_help},
                  "end": {**STR, "description": "optional end, same format"},
                  "minutes": {**INT, "minimum": 5, "maximum": 1440,
                              "description": f"optional length instead of end (default {DEFAULT_MINUTES})"},
                  "all_day": BOOL,
                  "where": {**STR, "description": "optional place"},
                  "calendar": {**STR, "description": "optional: which calendar, by name (default the main one)"}},
                 ("title", "start")),
            tool("change_calendar_event", "Move, rename or change the place of an event (by its id from "
                 "calendar_events or the context). A repeating event is changed only that once.",
                 {"id": STR, "start": {**STR, "description": "new start, with its date (" + when_help + ")"},
                  "end": STR, "minutes": {**INT, "minimum": 5, "maximum": 1440}, "title": STR, "where": STR},
                 ("id",)),
            tool("delete_calendar_event", "Cancel (delete) an event by its id. Without confirmed it only gives the "
                 "question to ask: call it first, say nothing about cancelling before, then just ask that question; "
                 "after they say yes, call again with confirmed true. A repeating event is cancelled only that once.",
                 {"id": STR, "confirmed": BOOL}, ("id",)),
        ]
        if people:
            out[1]["parameters"]["properties"]["person"] = {
                **STR, "description": "optional: whose calendar ('Sara', 'family'); default the person talking's own"}
        return out

    async def run(self, name: str, args: dict[str, Any]) -> str:
        args = args if isinstance(args, dict) else {}
        if not self.available():
            return err("the calendar isn't set up (docs/CALENDAR.md)")
        try:
            if name == "calendar_events":
                return await self._list(args)
            if name == "add_calendar_event":
                return await self._add(args)
            if name == "change_calendar_event":
                return await self._change(args)
            if name == "delete_calendar_event":
                return await self._delete(args)
        except (AgendaError, CalendarError, W.WhenError) as e:
            return err(str(e))
        return err(f"calendar has no tool called {str(name)[:40]}")

    # ------------------------------------------------------------ calendar_events
    def day_range(self, words: Any, days: Any = None) -> tuple[_dt.date, int]:
        """The first day and how many days ``words`` (and ``days``) mean."""
        today = self.local().date()
        said = " ".join(str(words or "").lower().split()).strip(" .?!")
        s = re.sub(r"^(?:on|for|this coming|the)\s+", "", said.replace("'", ""))
        first, count = today, 1
        m = _ISO_DATE.match(s)
        if not s or s in ("today", "tonight", "this evening", "this afternoon", "this morning", "now"):
            pass
        elif m:
            try:
                first = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                raise AgendaError(f"{s[:20]} isn't a date") from None
        elif s == "tomorrow":
            first = today + _dt.timedelta(days=1)
        elif s == "yesterday":
            first = today - _dt.timedelta(days=1)
        elif s in ("this week", "week", "the week", "rest of the week"):
            count = 7 - today.weekday()
        elif s in ("next week", "the next week"):
            first, count = today + _dt.timedelta(days=7 - today.weekday()), 7
        elif s in ("weekend", "this weekend", "the weekend"):
            first = today if today.weekday() >= 5 else today + _dt.timedelta(days=5 - today.weekday())
            count = 7 - first.weekday()
        elif s == "next weekend":
            first, count = today + _dt.timedelta(days=12 - today.weekday()), 2
        elif s in ("this month", "month", "the month", "rest of the month"):
            nxt = (today.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
            count = (nxt - today).days
        elif s in ("next 7 days", "the next 7 days", "next few days", "coming days", "the coming week"):
            count = 7
        else:
            day = s.removeprefix("next ")
            if day in W.WEEKDAYS:
                ahead = (W.WEEKDAYS.index(day) - today.weekday()) % 7
                if s.startswith("next ") and ahead == 0:
                    ahead = 7
                first = today + _dt.timedelta(days=ahead)
            else:
                try:
                    got = W.parse_when(said, self.local(), tz=self.zone())
                except W.WhenError:
                    raise AgendaError(f"I couldn't tell which day '{str(words)[:40]}' is; give an ISO date "
                                      "like 2026-11-06") from None
                first = got.at.date()
        if days not in (None, ""):
            try:
                count = max(1, min(MAX_DAYS, int(days)))
            except (TypeError, ValueError):
                pass
        return first, max(1, min(MAX_DAYS, count))

    def range_words(self, first: _dt.date, count: int) -> str:
        if count == 1:
            label = self.day_label(first)
            return label if label not in ("today", "tomorrow") else f"{label} ({first:%A} {first.day} {first:%B})"
        last = first + _dt.timedelta(days=count - 1)
        return f"{self.day_label(first)} to {self.day_label(last)}"

    async def _list(self, args: dict[str, Any]) -> str:
        search = " ".join(w for w in str(args.get("search") or "").split()
                          if w.lower().strip(".,?!") not in _NOT_SEARCH)[:60]
        now = self.local()
        if search and not args.get("from"):
            first, count = now.date(), SEARCH_DAYS
        else:
            first, count = self.day_range(args.get("from"), args.get("days"))
        start, end = self._day_start(first), self._day_start(first + _dt.timedelta(days=count))
        events = await self.events_in(start, end, search or None)
        events, whose, label, left_out, name = self._whose_list(events, args.get("person"))
        span = self.range_words(first, count) if not (search and not args.get("from")) else \
            f"the next {SEARCH_DAYS} days"
        title = (search.capitalize() if search else
                 self.range_words(first, count).split(" (")[0].capitalize() if count == 1 else
                 ("This week" if first == now.date() and count == 7 - now.weekday() else "Coming up"))
        if name and len(title) + len(name) < 30:
            title = f"{name} · {title}"
        empty = f"Nothing on the calendar {span.split(' (')[0]}" if not search else f"No “{search}” coming up"
        await self.show(self.card_spec(events, title, empty=empty))
        if not events:
            what = f"nothing matching '{search}'" if search else "nothing on the calendar"
            return ok(f"{what} {span}" + (f" ({whose})" if whose else "")
                      + (f"; not shown: {left_out}" if left_out else "")
                      + ("" if self.can_write else " (read-only calendar)"))
        out: dict[str, Any] = {"range": span, "now": W.clock_words(now),
                               "events": [self._for_model(e, now, whose=label) for e in events[:MAX_LISTED]],
                               "shown_on_screen": True}
        if whose:
            out["whose"] = whose
        if left_out:
            out["not_shown"] = left_out
        if len(events) > MAX_LISTED:
            out["more"] = len(events) - MAX_LISTED
        if not self.can_write:
            out["read_only"] = True
        return as_json(out)

    def _whose_list(self, events: list[Event], person: Any) -> tuple[list[Event], str, bool, str, str]:
        """For calendar_events: (the events, whose they are in words, whether each says whose, what was left out,
        the person's name for the card)."""
        said = " ".join(str(person or "").lower().split()).strip(" .?!")
        if not self.several_owners(events) and not said:
            return events, "", False, "", ""
        who = self.speaker()
        unsure = "everyone's (not sure who is talking: say whose each one is)"
        if said in ("everyone", "everybody", "all", "everyone's", "all of us", "the whole family", "anyone"):
            return events, "everyone's", True, "", ""
        if said in FAMILY_WORDS or said in ("family's", "the family's"):
            return [e for e in events if e.owner is None], "the family's", False, "", "Family"
        if said in MINE_WORDS or not said:
            target = self.person(who.person_id) if who is not None else None
            if target is None:
                return events, unsure, True, "", ""
        else:
            target = self.person_named(said)
            if target is None:
                names = ", ".join(p["name"] for p in self.people()) or "nobody yet"
                raise AgendaError(f"'{str(person)[:30]}' isn't on Who's who (these are: {names})")
        mine, others = self.for_person(events, target["id"])
        left = ", ".join(f"{self.owner_name(pid) or 'someone'}'s {n}" for pid, n in others.items())
        words = f"{target['name']}'s and the family's"
        if who is not None and who.person_id == target["id"]:
            words += f" ({'' if who.sure else 'probably '}{target['name']} is talking)"
        return mine, words, False, left, target["name"]

    # ------------------------------------------------------------ add / change / delete
    def _when(self, value: Any, *, all_day: bool = False, base: _dt.date | None = None) -> tuple[_dt.datetime, bool]:
        """(the time, whether it is a whole day) from an ISO date-time, an ISO date or the user's words. ``base``:
        a time of day alone ("4 pm") is on that day."""
        s = " ".join(str(value or "").split())[:80]
        if not s:
            raise AgendaError("give the start: " + W.HINT)
        m = _ISO_DATE.match(s)
        if m and (all_day or len(s.strip()) == 10):
            try:
                d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                raise AgendaError(f"{s[:20]} isn't a date") from None
            return self._day_start(d), True
        if base is not None and _CLOCK_ONLY.match(s):  # "move it to 4 pm": the same day
            hour, minute = _clock(s)
            return W.local_dt(base, hour, minute, self.zone()), False
        now = self.local()
        try:
            got = W.parse_when(s, now, tz=self.zone())
        except W.WhenError:
            if not all_day:
                raise
            got = W.parse_when(s + " at 11:59 pm", now, tz=self.zone())  # a whole day that is today
        at = got.at.astimezone(self.zone())
        if all_day:
            return self._day_start(at.date()), True
        return at, False

    def _calendar_for(self, cals: list[dict[str, Any]], name: Any) -> dict[str, Any] | None:
        writable = [c for c in cals if c.get("can_edit")]
        want = " ".join(str(name or getattr(self._cfg(), "calendar_default", "") or "").lower().split())
        if want:
            names = {c["name"].lower(): c for c in writable}
            if want in names:
                return names[want]
            close = difflib.get_close_matches(want, list(names), n=1, cutoff=0.6)
            if close:
                return names[close[0]]
            hit = next((c for c in writable if want in c["name"].lower()), None)
            if hit is not None:
                return hit
            if name:
                return None
        return next((c for c in writable if c.get("primary")), writable[0] if writable else None)

    async def _add(self, args: dict[str, Any]) -> str:
        if not self.can_write:
            return err("the calendar is read-only here (connect Google Calendar to add events)")
        title = _clean_title(args.get("title"))
        if not title:
            return err("say what the event is (title)")
        all_day = bool(args.get("all_day"))
        start, whole = self._when(args.get("start"), all_day=all_day)
        all_day = all_day or whole
        end = self._end(start, args, all_day)
        self._add_note = ""
        acc, whose = self._account_to_add(args.get("person"))
        cals = await acc.google.calendars()
        cal = self._calendar_for(cals, args.get("calendar"))
        if cal is None:
            names = ", ".join(c["name"] for c in cals if c.get("can_edit")) or "none"
            return err(f"there's no calendar called '{str(args.get('calendar'))[:30]}' LittleBot can add to "
                       f"(these: {names})")
        same = next((e for e in self._cache if e.title.lower() == title.lower() and e.start == start
                     and e.account == acc.key), None)
        if same is not None:
            return ok(f"that's already on the calendar: {self._describe(same)} (id {same.key})")
        where = " ".join(str(args.get("where") or "").split())[:120] or None
        body = event_body(title, start, end, all_day=all_day, zone_name=self.zone_name(), location=where)
        got = await acc.google.add(cal, body, self.zone())
        ev = self._google_event(acc, got)
        self._added[ev.ident] = self.now()
        self._into_cache(ev)
        log.info("calendar: an event was added (%s)", "all day" if all_day else "timed")
        await self._show_day(ev)
        place = f"{self.whose(ev)} calendar" if whose else f"the {cal['name'] or 'main'} calendar"
        return ok(f"added {self._describe(ev)} to {place} (id {ev.key}){self._add_note}")

    def _account_to_add(self, person: Any) -> tuple[Account, str]:
        """Which sign-in a new event goes on, and whose it is in words ("" when there is only one calendar):
        ``person`` if given, else the person talking's own, else the household's, else the only one there is.
        AgendaError (for the model to ask) when that isn't clear."""
        writable = [a for a in self.accounts() if a.auth.connected]
        if not writable:
            raise AgendaError("the calendar is read-only here (connect Google Calendar to add events)")
        several = self.several_owners()
        said = " ".join(str(person or "").lower().split()).strip(" .?!")
        who = self.speaker()

        def named(acc: Account) -> str:
            if not several:
                return ""
            name = self.owner_name(acc.person_id)
            return f"{name}'s" if name else "the family"

        if said in FAMILY_WORDS or said in ("family's", "the family's"):
            if not self.home.auth.connected:
                raise AgendaError("there's no family calendar connected; put it on someone's own calendar (person)")
            return self.home, named(self.home)
        if said and said not in MINE_WORDS:
            p = self.person_named(said)
            if p is None:
                names = ", ".join(x["name"] for x in self.people()) or "nobody yet"
                raise AgendaError(f"'{str(person)[:30]}' isn't on Who's who (these are: {names})")
            acc = self.account(p["id"])
            if acc is None or not acc.auth.connected:
                raise AgendaError(f"{p['name']} has no Google Calendar connected here (the dashboard's Calendars "
                                  "card connects one)")
            return acc, named(acc)
        if who is not None:
            acc = self.account(who.person_id)
            if acc is not None and acc.auth.connected:
                self._add_note = "" if who.sure or not several else \
                    f" (by voice it's probably {who.name} talking: say whose calendar it went on)"
                return acc, named(acc)
        if said in MINE_WORDS and who is None and len(writable) > 1:
            raise AgendaError("not sure who is talking: ask whose calendar it goes on ("
                              + " or ".join(named(a) or a.key for a in writable) + "), then call again with person")
        if self.home.auth.connected:
            return self.home, named(self.home)
        if len(writable) == 1:
            return writable[0], named(writable[0])
        raise AgendaError("ask whose calendar it goes on (" + " or ".join(named(a) for a in writable)
                          + "), then call again with person")

    def _end(self, start: _dt.datetime, args: dict[str, Any], all_day: bool,
             keep: _dt.timedelta | None = None) -> _dt.datetime:
        if args.get("end") not in (None, ""):
            end, _ = self._when(args.get("end"), all_day=all_day, base=start.date())
            if all_day:
                end += _dt.timedelta(days=1)  # the last day is included when they say it
            if end <= start:
                raise AgendaError("the end must be after the start")
            return end
        if args.get("minutes") not in (None, ""):
            try:
                return start + _dt.timedelta(minutes=max(5, min(1440, int(args["minutes"]))))
            except (TypeError, ValueError):
                raise AgendaError("minutes must be a number") from None
        if keep is not None:
            return start + keep
        return start + (_dt.timedelta(days=1) if all_day else _dt.timedelta(minutes=DEFAULT_MINUTES))

    def _editable(self, key: Any) -> tuple[Event, Account]:
        ev = self.event(key)
        if ev is None:
            raise AgendaError(f"there's no event '{str(key)[:12]}' (look it up with calendar_events first)")
        acc = self._accounts.get(ev.account)
        if ev.source != "google" or not ev.can_edit or acc is None or not acc.auth.connected:
            raise AgendaError(f"'{ev.title}' is on a calendar LittleBot can only read")
        return ev, acc

    async def _change(self, args: dict[str, Any]) -> str:
        if not self.can_write:
            return err("the calendar is read-only here (connect Google Calendar to change events)")
        ev, acc = self._editable(args.get("id"))
        title = _clean_title(args.get("title")) or None
        where = None if args.get("where") is None else " ".join(str(args.get("where")).split())[:120]
        start = end = None
        all_day = ev.all_day
        if args.get("start") not in (None, ""):
            start, all_day = self._when(args.get("start"), all_day=ev.all_day and not _has_clock(args.get("start")),
                                        base=ev.start.astimezone(self.zone()).date())
            end = self._end(start, args, all_day, keep=None if all_day != ev.all_day else ev.end - ev.start)
        elif args.get("end") not in (None, "") or args.get("minutes") not in (None, ""):
            start = ev.start
            end = self._end(start, args, ev.all_day)
        if title is None and where is None and start is None:
            return err("say what to change: start, end, minutes, title or where")
        cal = next((c for c in await acc.google.calendars() if c["id"] == ev.calendar_id),
                   {"id": ev.calendar_id, "name": ev.calendar, "color": ev.color, "can_edit": True})
        body = event_body(title, start, end, all_day=all_day, zone_name=self.zone_name(), location=where)
        got = await acc.google.change(cal, ev.id, body, self.zone())
        new = Event(source="google", account=acc.key, **{k: v for k, v in got.items() if k in _EVENT_FIELDS})
        new.recurring = new.recurring or ev.recurring
        new.owner = ev.owner
        self._drop_from_cache(ev)
        self._keys[new.ident] = ev.key
        new = self._remember(new)
        self._added[new.ident] = self.now()
        self._into_cache(new)
        log.info("calendar: an event was changed")
        await self._show_day(new)
        once = " (only this time; it repeats)" if new.recurring else ""
        return ok(f"changed: now {self._describe(new)}{once} (id {new.key})")

    async def _delete(self, args: dict[str, Any]) -> str:
        if not self.can_write:
            return err("the calendar is read-only here (connect Google Calendar to cancel events)")
        ev, acc = self._editable(args.get("id"))
        once = " (only this time; it repeats)" if ev.recurring else ""
        whose = f" from {self.whose(ev)} calendar" if self.several_owners() else ""
        if args.get("confirmed") is not True:
            return ok(f"not deleted yet. Ask them: cancel {self._describe(ev)}{whose}{once}? If they say yes, call "
                      f"delete_calendar_event again with confirmed true.")
        cal = {"id": ev.calendar_id, "name": ev.calendar}
        try:
            await acc.google.delete(cal, ev.id)
        except CalendarError as e:
            if e.kind != "not_found":
                raise
        self._drop_from_cache(ev)
        log.info("calendar: an event was cancelled")
        return ok(f"cancelled {self._describe(ev)}{once}")

    def _describe(self, e: Event) -> str:
        day = self.day_label(e.start.astimezone(self.zone()).date())
        day = f"on {day}" if day not in ("today", "tomorrow") else day
        where = f" at {e.location}" if e.location else ""
        return f"'{e.title}' {day}, {self.time_words(e)}{where}"

    def _into_cache(self, ev: Event) -> None:
        r = self._cache_range
        if r is not None and ev.end > r[0] and ev.start < r[1]:
            self._cache = self._sorted([e for e in self._cache if e.ident != ev.ident] + [ev])
        self.version += 1

    def _drop_from_cache(self, ev: Event) -> None:
        self._cache = [e for e in self._cache if e.ident != ev.ident]
        self.version += 1

    async def _show_day(self, ev: Event) -> None:
        day = ev.start.astimezone(self.zone()).date()
        events = self.cached(self._day_start(day), self._day_start(day + _dt.timedelta(days=1)))
        if events is None or not any(e.ident == ev.ident for e in events):
            events = [ev]
        await self.show(self.card_spec(events, self.day_label(day).capitalize(), highlight=ev.key,
                                       linger=CARD_LINGER_S))


# Words that are about when or whether, not what: searching for them finds nothing ("am I free Monday afternoon?").
_NOT_SEARCH = frozenset({"morning", "afternoon", "evening", "night", "tonight", "today", "tomorrow", "free", "busy",
                         "anything", "something", "events", "event", "appointments", "appointment", "plans", "plan",
                         "schedule", "calendar", "meetings", "the", "my", "any", "on", "in", "at"})

_EVENT_FIELDS = frozenset({"id", "calendar_id", "calendar", "title", "start", "end", "all_day", "location", "color",
                           "can_edit", "recurring"})


def _dedupe(events: list[Event]) -> list[Event]:
    """The same event from Google and from an iCal address of the same calendar: Google's is kept. On two people's
    calendars (a shared family calendar, an invitation to both): the family's."""
    seen: dict[tuple[str, _dt.datetime], Event] = {}
    for e in sorted(events, key=lambda e: e.source != "google"):
        k = (e.title.lower(), e.start)
        if k not in seen:
            seen[k] = e
        elif seen[k].owner != e.owner:
            seen[k].owner = None
    return list(seen.values())


def ics_list(raw: str) -> list[str]:
    """iCal addresses from text (several separated by spaces or commas; webcal:// is https://), at most 5."""
    out = []
    for part in str(raw or "").replace(",", " ").split():
        if part.lower().startswith("webcal://"):
            part = "https://" + part[len("webcal://"):]
        if part.lower().startswith(("https://", "http://")):
            out.append(part)
    return out[:5]


def _matches(query: str, text: str) -> bool:
    words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if w not in ("the", "a", "my", "an")]
    hay = text.lower()
    return bool(words) and all(w in hay for w in words)


def _clean_title(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    s = " ".join(str(value).split()).strip(" \"'“”‘’.,;:")
    s = re.sub(r"^(?:add|put|schedule)\s+", "", s, flags=re.I)
    return s[:MAX_TITLE]


def _has_clock(value: Any) -> bool:
    s = str(value or "")
    return bool(re.search(r"T\d{1,2}:\d{2}|\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|\d{1,2}:\d{2}", s, re.I))


def _clock(s: str) -> tuple[int, int]:
    """ "4 pm" -> (16, 0), "at 9:30" -> (9, 30), "4" -> (16, 0) (an afternoon guess, as reminders do)."""
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", s, re.I)
    if m is None:
        raise AgendaError("give a time like 4 pm")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower().replace(".", "")
    if h > 23 or mi > 59:
        raise AgendaError(f"{m.group(0)} isn't a time of day")
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    elif not ampm and 1 <= h < 7:
        h += 12
    return h, mi


__all__ = ["AgendaError", "Calendar", "Event"]
