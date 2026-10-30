#!/usr/bin/env python3
"""Save an API key for LittleBot in its own key file (data/<name>_api_key, readable only by you: chmod 600).

    .venv/bin/python scripts/set_key.py deepgram       # asks for the key (hidden), saves it
    .venv/bin/python scripts/set_key.py cartesia
    .venv/bin/python scripts/set_key.py elevenlabs
    .venv/bin/python scripts/set_key.py anthropic
    .venv/bin/python scripts/set_key.py home_assistant  # the Home Assistant long-lived access token
    .venv/bin/python scripts/set_key.py --check         # which keys are set (never the keys)
    .venv/bin/python scripts/set_key.py --remove deepgram

Over ssh, use -t so it can ask: ssh -t alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/set_key.py
deepgram". The key is typed or pasted at a hidden prompt: it never appears on the screen, in the shell history or in
LittleBot's logs. A key in .env (DEEPGRAM_API_KEY=...) wins over the file. Restart LittleBot after:
systemctl --user restart littlebot
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# name -> (key file under data/, environment variable)
KEYS = {
    "deepgram": ("deepgram_api_key", "DEEPGRAM_API_KEY"),
    "cartesia": ("cartesia_api_key", "CARTESIA_API_KEY"),
    "elevenlabs": ("elevenlabs_api_key", "ELEVENLABS_API_KEY"),
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    "home_assistant": ("home_assistant_token", "HOME_ASSISTANT_TOKEN"),
}


def data_dir() -> Path:
    try:
        from littlebot.core.config import load_config

        return load_config(include_local=True).data_path
    except Exception:  # noqa: BLE001 - a broken config: the default folder
        return REPO_ROOT / "data"


def save(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key + "\n")
    os.chmod(path, 0o600)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", nargs="?", choices=sorted(KEYS), help="which key")
    p.add_argument("--check", action="store_true", help="say which keys are set")
    p.add_argument("--remove", action="store_true", help="delete the key file")
    args = p.parse_args(argv)
    folder = data_dir()
    if args.check or not args.name:
        for name, (file_name, env) in sorted(KEYS.items()):
            where = []
            if (folder / file_name).is_file():
                where.append(f"data/{file_name}")
            if any(line.split("=", 1)[0].strip() == env and line.split("=", 1)[1].strip()
                   for line in _env_lines()):
                where.append(".env")
            print(f"  {name:15s} {'set (' + ', '.join(where) + ')' if where else 'not set'}")
        return 0
    file_name, env = KEYS[args.name]
    path = folder / file_name
    if args.remove:
        if path.is_file():
            path.unlink()
            print(f"Removed data/{file_name}. Restart LittleBot: systemctl --user restart littlebot")
        else:
            print(f"There was no data/{file_name}.")
        return 0
    try:
        key = getpass.getpass(f"Paste the {args.name} key (hidden) and press Enter: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nNothing saved.")
        return 1
    if len(key) < 8 or any(c.isspace() for c in key):
        print("That doesn't look like a key (too short, or it has spaces). Nothing saved.")
        return 1
    save(path, key)
    print(f"Saved in data/{file_name} (only you can read it). Restart LittleBot: systemctl --user restart littlebot")
    return 0


def _env_lines() -> list[str]:
    env = REPO_ROOT / ".env"
    try:
        return [ln for ln in env.read_text(encoding="utf-8").splitlines() if "=" in ln and not ln.startswith("#")]
    except OSError:
        return []


if __name__ == "__main__":
    raise SystemExit(main())
