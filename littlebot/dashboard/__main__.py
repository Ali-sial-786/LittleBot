"""Dashboard maintenance commands (run on the robot itself).

    python -m littlebot.dashboard reset-password [--data-dir DIR] [--yes]

Forgets the parent password and every dashboard session. Stop LittleBot first;
the next start prints a fresh first-run setup code to the console / log.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m littlebot.dashboard", description="LittleBot dashboard maintenance")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reset-password", help="forget the parent password and log out every device")
    r.add_argument("--data-dir", help="LittleBot data directory (default: from config, usually ./data)")
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
    db = Database(db_path)
    try:
        AuthManager(db).reset_password()
    finally:
        db.close()
    print("Dashboard password removed. Start LittleBot again and look for the new setup code in its console/log.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
