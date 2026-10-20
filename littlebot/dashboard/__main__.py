"""Dashboard maintenance commands (run on the robot itself).

    python -m littlebot.dashboard reset-password [--data-dir DIR] [--port PORT] [--yes]

Forgets the parent password and deletes every dashboard session. This also works
while LittleBot is running: sessions are checked against the database on every
request, so every device is logged out at once, and the next visit to the
dashboard makes the running robot print (and show on its screen) a fresh setup
code. Otherwise the next start prints one.
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m littlebot.dashboard", description="LittleBot dashboard maintenance")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reset-password", help="forget the parent password and log out every device")
    r.add_argument("--data-dir", help="LittleBot data directory (default: from config, usually ./data)")
    r.add_argument("--port", type=int, help="dashboard port to check for a running LittleBot (default: from config)")
    r.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = p.parse_args(argv)

    from littlebot.core.config import load_config
    from littlebot.core.storage import Database
    from littlebot.dashboard.auth import AuthManager

    cfg = load_config(overrides={"data_dir": args.data_dir} if args.data_dir else None)
    db_path = Path(cfg.data_path) / "littlebot.db"
    if not db_path.exists():
        print(f"No LittleBot database at {db_path} - nothing to reset.")
        return 1
    if not args.yes:
        answer = input(f"Forget the dashboard password stored in {db_path}? Type 'reset' to confirm: ")
        if answer.strip().lower() != "reset":
            print("Cancelled.")
            return 1
    port = int(args.port or cfg.dashboard.port)
    running = _port_in_use(port)
    if running:
        print(f"Note: something is listening on the dashboard port {port} - LittleBot seems to be running.")
    db = Database(db_path)
    try:
        auth = AuthManager(db)
        auth.reset_password()  # removes the password hash and deletes every session row
        remaining = auth.active_sessions()
    finally:
        db.close()
    print(f"Dashboard password removed and every device logged out ({remaining} sessions left).")
    if running:
        print("Open the dashboard: the running LittleBot prints a new setup code in its console/log "
              "(and shows it on its screen). No restart needed.")
    else:
        print("Start LittleBot and look for the new setup code in its console/log.")
    return 0


def _port_in_use(port: int) -> bool:
    """True if something accepts connections on 127.0.0.1:``port`` (best effort, 0.3 s)."""
    if not 0 < port < 65536:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
