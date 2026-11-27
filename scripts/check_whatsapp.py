#!/usr/bin/env python3
"""Which of LittleBot's WhatsApp selectors match right now? Run on the robot WHILE WhatsApp is open on its screen.

    cd ~/LittleBot && .venv/bin/python scripts/check_whatsapp.py            # a table
    .venv/bin/python scripts/check_whatsapp.py --json                       # the same as JSON (to paste in a chat)

WhatsApp changes its page whenever it likes, and LittleBot finds the search box, the call buttons and the call's
box by a list of selectors each (littlebot/calls/page_scripts.py ``SELECTORS``). This prints, for every thing
LittleBot looks for, which of its selectors match a visible element on the page now: the selector's key, its number
and yes / no. It also says whether LittleBot's cover is in place and hiding the page, how many holes it has (the QR
code, a call), what the page counts as (linked? a call?), and which call-box selector matched.

It prints NOTHING from the page: no names, no messages, no titles. Only LittleBot's own selector keys, true / false
and counts. It changes nothing on the page and presses nothing. It connects to Chromium's DevTools on this
computer only (the port Chromium wrote into the WhatsApp profile folder).

Try it in each of these states and keep the output: the QR page (before linking), the chat list, a chat open, a
call ringing, a call on. A thing that is on the screen with every selector "no" needs a new selector.

Exit code: 0 fine, 2 WhatsApp isn't open (or its DevTools didn't answer).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from littlebot.browser.cdp import CDPConnection, CDPError  # noqa: E402
from littlebot.calls import page_scripts as scripts  # noqa: E402
from littlebot.calls import policy  # noqa: E402

# What each state of the page should find (the rest is expected to say "no" there).
EXPECTED = {
    "the QR page": ("qr",),
    "the chat list": ("chat_list", "search_box"),
    "a chat open": ("chat_header", "video_call", "voice_call"),
    "a call ringing": ("accept_call", "decline_call", "call_container"),
    "a call on": ("end_call", "call_container"),
}


class NotOpen(Exception):
    """WhatsApp isn't open on the robot (or isn't reachable)."""


def default_profile() -> Path:
    """The WhatsApp profile folder of this install (``calls.profile_dir`` under the data folder)."""
    from littlebot.core.config import load_config  # noqa: PLC0415

    cfg = load_config()
    return cfg.data_path / str(cfg.calls.profile_dir)


def devtools_address(profile: Path) -> str:
    try:
        lines = (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
    except OSError:
        raise NotOpen(f"WhatsApp isn't open: there is no DevToolsActivePort in {profile}") from None
    if len(lines) < 2 or not lines[0].strip().isdigit() or not lines[1].startswith("/devtools/"):
        raise NotOpen("WhatsApp's DevTools address couldn't be read")
    return f"ws://127.0.0.1:{int(lines[0])}{lines[1].strip()}"


async def check(profile: Path, *, connect: Any = CDPConnection.connect, any_page: bool = False) -> dict[str, Any]:
    """Ask the open WhatsApp page (``page_scripts.check_script``). ``any_page``: a test's local page counts."""
    try:
        cdp = await connect(devtools_address(profile))
    except (OSError, ConnectionError, TimeoutError) as e:
        raise NotOpen(f"WhatsApp's Chromium didn't answer ({type(e).__name__}); is WhatsApp open?") from None
    try:
        infos = (await cdp.send("Target.getTargets")).get("targetInfos") or []
        pages = [t for t in infos if t.get("type") == "page"]
        page = next((t for t in pages if policy.host_of(str(t.get("url") or "")) == policy.HOST), None)
        if page is None and any_page and pages:
            page = pages[0]
        if page is None:
            raise NotOpen("Chromium is open, but not on web.whatsapp.com")
        tid = str(page["targetId"])
        session = (await cdp.send("Target.attachToTarget", {"targetId": tid, "flatten": True}))["sessionId"]
        world = await cdp.send("Page.createIsolatedWorld", {"frameId": tid, "worldName": "littlebot-check"},
                               session=session)
        res = await cdp.send("Runtime.evaluate", {"expression": scripts.check_script(), "returnByValue": True,
                                                  "contextId": world["executionContextId"], "timeout": 5000},
                             session=session)
        if res.get("exceptionDetails"):
            raise NotOpen("the check script failed on the page")
        value = (res.get("result") or {}).get("value")
        if not isinstance(value, dict):
            raise NotOpen("the page gave no answer")
        return _clean(value)
    except CDPError as e:
        raise NotOpen(f"DevTools refused ({str(e)[:80]})") from None
    finally:
        await cdp.close()


def _clean(value: dict[str, Any]) -> dict[str, Any]:
    """Only what this script may print: our keys, true / false, numbers and the three states. Anything else the
    page might have answered is dropped (belt and braces: the script asks for nothing else)."""
    keys = value.get("keys") if isinstance(value.get("keys"), dict) else {}
    call = value.get("call") if value.get("call") in ("none", "incoming", "in_call") else "none"
    container = value.get("container") if isinstance(value.get("container"), int) else -1
    return {
        "keys": {k: [bool(x) for x in (keys.get(k) or [])][:len(scripts.SELECTORS[k])] for k in scripts.SELECTORS},
        "linked": bool(value.get("linked")), "qr": bool(value.get("qr")), "call": call, "container": int(container),
        "videos": int(value.get("videos") or 0), "cover": bool(value.get("cover")),
        "covered": bool(value.get("covered")), "holes": int(value.get("holes") or 0),
    }


def render(report: dict[str, Any]) -> str:
    yes_no = lambda v: "yes" if v else "no"  # noqa: E731
    out = ["WhatsApp page check (LittleBot's own selector keys and yes / no only; nothing from the page)", ""]
    out.append(f"cover in place: {yes_no(report['cover'])}   hiding the page: {yes_no(report['covered'])}   "
               f"holes in it: {report['holes']}")
    out.append(f"linked (chat list found): {yes_no(report['linked'])}   QR page: {yes_no(report['qr'])}   "
               f"call: {report['call']}   live video pictures: {report['videos']}")
    c = report["container"]
    out.append("call box: " + (f"call_container #{c} matched" if c >= 0 else "none recognised (during a call the "
                               "cover then shows only the video pictures and LittleBot's own buttons)"))
    out.append("")
    for key, selectors in scripts.SELECTORS.items():
        hits = report["keys"].get(key) or []
        first = next((i for i, hit in enumerate(hits) if hit), None)
        out.append(f"{key:<15} {'FOUND' if first is not None else 'not found':<10} "
                   + (f"(first match: #{first})" if first is not None else ""))
        for i, sel in enumerate(selectors):
            out.append(f"    #{i} {yes_no(i < len(hits) and hits[i]):<4} {sel}")
    out.append("")
    out.append("What each state should find:")
    for state, keys in EXPECTED.items():
        out.append(f"  {state}: " + ", ".join(keys))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Which of LittleBot's WhatsApp selectors match on the open page "
                                             "(keys and yes / no only; nothing from the page is printed).")
    ap.add_argument("--profile", type=Path, default=None, help="the WhatsApp profile folder (default: this "
                                                               "install's data/whatsapp-profile)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    profile = args.profile or default_profile()
    try:
        report = asyncio.run(check(profile))
    except NotOpen as e:
        print(f"{e}\nOpen WhatsApp on the robot first (say \"Hey LittleBot, open WhatsApp\", or the dashboard's "
              "Assistant page), then run this again.")
        return 2
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
