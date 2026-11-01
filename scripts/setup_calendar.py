#!/usr/bin/env python3
"""Connect LittleBot to your calendar (docs/CALENDAR.md).

    .venv/bin/python scripts/setup_calendar.py              # Google Calendar: see and add events (sign in once)
    .venv/bin/python scripts/setup_calendar.py --ics        # or a calendar's secret iCal address (see only)
    .venv/bin/python scripts/setup_calendar.py --check      # what LittleBot sees (never a token or the address)
    .venv/bin/python scripts/setup_calendar.py --disconnect # forget the Google sign-in
    .venv/bin/python scripts/setup_calendar.py --remove-ics # forget the iCal address(es)

Over ssh use -t so it can ask: ssh -t alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python
scripts/setup_calendar.py". The client secret and the iCal address are typed at a hidden prompt. The sign-in is kept in
data/google_calendar.json, the address in data/calendar_ics_url (both readable only by you). LittleBot picks it up
within 5 minutes, or at once after: systemctl --user restart littlebot
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import getpass
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from littlebot.skills.google_calendar import (  # noqa: E402
    REDIRECT_URI, CalendarError, GoogleAuth, GoogleCalendar)

ICS_FILE = "calendar_ics_url"
TOKEN_FILE = "google_calendar.json"


def data_dir() -> Path:
    try:
        from littlebot.core.config import load_config

        return load_config(include_local=True).data_path
    except Exception:  # noqa: BLE001 - a broken config: the default folder
        return REPO_ROOT / "data"


def home_zone() -> _dt.tzinfo:
    try:
        from zoneinfo import ZoneInfo

        from littlebot.core.config import load_config
        from littlebot.info.clock import local_zone_name

        name = load_config(include_local=True).info.timezone or local_zone_name()
        if name:
            return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        pass
    return _dt.datetime.now().astimezone().tzinfo or _dt.timezone.utc


def save_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    os.chmod(path, 0o600)


def ask(prompt: str, *, hidden: bool = False) -> str:
    try:
        return (getpass.getpass(prompt) if hidden else input(prompt)).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nStopped; nothing changed.")
        raise SystemExit(1) from None


async def today_count(auth: GoogleAuth) -> tuple[list[str], int]:
    cal = GoogleCalendar(auth)
    try:
        tz = home_zone()
        now = _dt.datetime.now(tz)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        names = [c["name"] or c["id"][:20] for c in await cal.calendars()]
        events = await cal.events(start, start + _dt.timedelta(days=7), tz)
        return names, len(events)
    finally:
        await cal.close()


def connect_google(folder: Path) -> int:
    print(__doc__.split("\n\n")[0])
    print("You need a Google Cloud 'Desktop app' OAuth client first (docs/CALENDAR.md, step 1).")
    auth = GoogleAuth(folder / TOKEN_FILE)
    client_id = ask("Client ID (ends in .apps.googleusercontent.com): ")
    secret = ask("Client secret (hidden): ", hidden=True)
    try:
        url = auth.begin(client_id, secret)
    except CalendarError as e:
        print(f"Not connected: {e}.")
        return 1
    print("\n1. Open this address on your phone or PC and sign in with the Google account that has the calendar:\n")
    print(url)
    print("\n2. If Google says it hasn't verified the app, press Advanced, then Go to (your app's name).")
    print("3. Tick both calendar permissions and press Continue.")
    print(f"4. The page then fails to load ({REDIRECT_URI}...): that is expected. Copy the WHOLE address from the "
          "address bar and paste it here.\n")
    pasted = ask("Address: ")
    try:
        asyncio.run(auth.finish(pasted))
    except CalendarError as e:
        print(f"Not connected: {e}.")
        return 1
    print("Connected. Checking what LittleBot can see ...")
    try:
        names, count = asyncio.run(today_count(auth))
        print(f"  calendars: {', '.join(names) or 'none'}")
        print(f"  events in the next 7 days: {count}")
    except CalendarError as e:
        print(f"  the check didn't work ({e}); the sign-in is saved anyway.")
    print("Done. LittleBot picks it up within 5 minutes, or now with: systemctl --user restart littlebot")
    return 0


def add_ics(folder: Path) -> int:
    print("Paste the calendar's secret iCal address (Google Calendar: Settings, your calendar, 'Secret address in")
    print("iCal format'; Outlook: Settings, Calendar, Shared calendars, Publish, ICS). It is hidden while you paste.")
    url = ask("Address (hidden): ", hidden=True)
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.lower().startswith(("https://", "http://")) or any(c.isspace() for c in url):
        print("That isn't a web address. Nothing saved.")
        return 1
    try:
        import httpx

        from littlebot.skills import ics

        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        feed = ics.parse(resp.content.decode("utf-8", errors="replace"))
        tz = home_zone()
        start = _dt.datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        found = ics.occurrences(feed, start, start + _dt.timedelta(days=7), tz)
        print(f"It works: {feed.name or 'the calendar'}, {len(found)} events in the next 7 days.")
    except Exception as e:  # noqa: BLE001 - say why, never the address
        print(f"That address didn't work ({type(e).__name__}). Nothing saved.")
        return 1
    path = folder / ICS_FILE
    old = path.read_text(encoding="utf-8").split() if path.is_file() else []
    if url in old:
        print("That address was already saved.")
        return 0
    save_secret(path, " ".join(old + [url]))
    print(f"Saved in data/{ICS_FILE} ({len(old) + 1} address{'es' if old else ''}; only you can read it).")
    print("LittleBot picks it up after: systemctl --user restart littlebot")
    return 0


def check(folder: Path) -> int:
    auth = GoogleAuth(folder / TOKEN_FILE)
    if auth.connected:
        try:
            names, count = asyncio.run(today_count(auth))
            print(f"Google Calendar: connected; calendars: {', '.join(names) or 'none'}; next 7 days: {count} events")
        except CalendarError as e:
            print(f"Google Calendar: connected, but it didn't answer ({e})")
    else:
        print("Google Calendar: not connected")
    path = folder / ICS_FILE
    n = len(path.read_text(encoding="utf-8").split()) if path.is_file() else 0
    print(f"iCal addresses: {n}" + (" (in .env too?)" if os.environ.get("CALENDAR_ICS_URL") else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ics", action="store_true", help="add a secret iCal address (read only)")
    p.add_argument("--check", action="store_true", help="what LittleBot sees")
    p.add_argument("--disconnect", action="store_true", help="forget the Google sign-in")
    p.add_argument("--remove-ics", action="store_true", help="forget the iCal address(es)")
    args = p.parse_args(argv)
    folder = data_dir()
    if args.check:
        return check(folder)
    if args.disconnect:
        had = GoogleAuth(folder / TOKEN_FILE).disconnect()
        print("Google Calendar disconnected." if had else "Google Calendar wasn't connected.")
        return 0
    if args.remove_ics:
        path = folder / ICS_FILE
        if path.is_file():
            path.unlink()
            print("The iCal address(es) are forgotten. Restart LittleBot: systemctl --user restart littlebot")
        else:
            print("There was no iCal address saved.")
        return 0
    return add_ics(folder) if args.ics else connect_google(folder)


if __name__ == "__main__":
    raise SystemExit(main())
