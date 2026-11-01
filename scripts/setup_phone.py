#!/usr/bin/env python3
"""Get LittleBot's heads-ups and reminders on your phone, through the free ntfy app (docs/CALENDAR.md, "Your phone").

    .venv/bin/python scripts/setup_phone.py           # make a private topic, show it, send a test
    .venv/bin/python scripts/setup_phone.py --test    # send another test
    .venv/bin/python scripts/setup_phone.py --new     # a new topic (phones on the old one stop getting them)
    .venv/bin/python scripts/setup_phone.py --remove  # stop sending to phones

Over ssh use -t: ssh -t alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/setup_phone.py".
The topic is long and random because it is the only thing that keeps the notifications private: anyone who knows it
can read them, so only put it on the family's phones. It is kept in data/ntfy_topic (readable only by you).
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TOPIC_FILE = "ntfy_topic"


def config():
    from littlebot.core.config import load_config

    return load_config(include_local=True)


def send_test(server: str, topic: str) -> bool:
    import httpx

    try:
        resp = httpx.post(server.rstrip("/") + "/", timeout=10, json={
            "topic": topic, "title": "LittleBot", "tags": ["robot"],
            "message": "Hello from LittleBot! Heads-ups and reminders will come here."})
        return resp.status_code < 400
    except httpx.HTTPError:
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--test", action="store_true")
    p.add_argument("--new", action="store_true")
    p.add_argument("--remove", action="store_true")
    args = p.parse_args(argv)
    cfg = config()
    path = cfg.data_path / TOPIC_FILE
    server = cfg.skills.ntfy_server or "https://ntfy.sh"
    if args.remove:
        if path.is_file():
            path.unlink()
            print("Phones won't get LittleBot's notifications any more. Restart LittleBot: "
                  "systemctl --user restart littlebot")
        else:
            print("No phone was set up.")
        return 0
    topic = path.read_text(encoding="utf-8").strip() if path.is_file() and not args.new else ""
    made = False
    if not topic:
        topic = "littlebot-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(topic + "\n")
        os.chmod(path, 0o600)
        made = True
    if not args.test:
        print("On each phone that should get LittleBot's heads-ups and reminders:")
        print("  1. Install the free 'ntfy' app (Google Play or the App Store).")
        print("  2. Tap +, and subscribe to this topic" + ("" if server.rstrip("/") == "https://ntfy.sh" else
                                                         f" on the server {server}") + ":")
        print(f"\n       {topic}\n")
        print("     Keep it to the family: anyone who knows it can read the notifications.")
        try:
            input("  3. Press Enter here to send a test notification ... ")
        except (EOFError, KeyboardInterrupt):
            print()
    if send_test(server, topic):
        print("Sent. It should be on the phone now.")
    else:
        print(f"The test didn't go (is the robot online? can it reach {server}?).")
    if made:
        print("Restart LittleBot so it uses the phone: systemctl --user restart littlebot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
