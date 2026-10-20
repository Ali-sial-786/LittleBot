"""Validate activity scripts: ``python -m littlebot.activities [FILE_OR_DIR ...]``.

With no arguments, checks the built-in scripts (assets/activities) plus any
parent folders configured in ``activities.custom_dirs``. Exit code 1 if any
script has a problem, so it can be used before copying a new script to the robot.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from littlebot.activities.model import load_directories
from littlebot.core.paths import ACTIVITY_SCRIPTS_DIR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m littlebot.activities", description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="YAML files or folders (default: built-in + custom folders)")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every activity")
    args = parser.parse_args(argv)
    if args.paths:
        paths = [Path(p) for p in args.paths]
        # Shared word lists (_data.yaml) are always available to custom scripts.
        if not any(p.resolve() == ACTIVITY_SCRIPTS_DIR.resolve() for p in paths):
            paths = [ACTIVITY_SCRIPTS_DIR / "_data.yaml", *paths]
    else:
        from littlebot.core.config import load_config

        cfg = load_config(use_dotenv=False)
        paths = [ACTIVITY_SCRIPTS_DIR]
        for d in cfg.activities.custom_dirs:
            p = Path(d).expanduser()
            paths.append(p if p.is_absolute() else cfg.data_path / p)
    report = load_directories(paths)
    if args.verbose:
        for act in sorted(report.activities.values(), key=lambda a: (a.min_age_months, a.id)):
            print(f"  {act.id:<28} {act.age_range_label():<20} {act.category:<11} {act.name}")
    print(f"{len(report.activities)} activities OK from {len(report.files)} file(s)")
    for err in report.errors:
        print(f"PROBLEM: {err}", file=sys.stderr)
    return 1 if report.errors else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
