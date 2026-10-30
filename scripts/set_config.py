#!/usr/bin/env python3
"""Change one setting in config/local.yaml, keeping the rest of the file (comments too) as it is.

    .venv/bin/python scripts/set_config.py chatgpt.live_barge_in_max_pitch_hz 220
    .venv/bin/python scripts/set_config.py audio.echo_cancel auto
    .venv/bin/python scripts/set_config.py --unset chatgpt.live_barge_in_speech_ms     # back to the default
    .venv/bin/python scripts/set_config.py --show chatgpt.live_barge_in_max_pitch_hz   # what it is now

No quotes needed, so the same command works over ssh from PowerShell and in a shell on the robot (editing
by hand with sed or printf went wrong both ways, 2026-10-29: a second "audio:" section appended at the end
replaced the first one completely). The setting goes into the section's last copy in the file (the one that
counts) and a section used twice is pointed out. The new file is checked against LittleBot's settings before
it is saved: a misspelt name or a wrong value changes nothing. Restart LittleBot after:
systemctl --user restart littlebot
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LOCAL = REPO_ROOT / "config" / "local.yaml"
_KEY = re.compile(r"^(\s*)([A-Za-z_][\w-]*):(\s*)(.*)$")
# YAML reads these as true/false; in LittleBot's settings they are words ("echo_cancel: off"), so they are quoted
_YAML_BOOL_WORDS = {"on", "off", "yes", "no", "y", "n"}


def _content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find(lines: list[str], start: int, end: int, indent: int, key: str) -> int | None:
    """The last line in [start, end) that is ``key:`` at ``indent`` (the copy that counts), or None."""
    found = None
    for i in range(start, end):
        m = _KEY.match(lines[i])
        if m and _content(lines[i]) and len(m.group(1)) == indent and m.group(2) == key:
            found = i
    return found


def _block_end(lines: list[str], header: int, end: int) -> int:
    """Where the block under the mapping header at ``header`` ends."""
    own = _indent(lines[header])
    for i in range(header + 1, end):
        if _content(lines[i]) and _indent(lines[i]) <= own:
            return i
    return end


def _child_indent(lines: list[str], start: int, end: int, default: int) -> int:
    return next((_indent(lines[i]) for i in range(start, end) if _content(lines[i])), default)


def yaml_value(raw: str) -> str:
    """The value as it is written in the file."""
    v = raw.strip()
    if v.lower() in _YAML_BOOL_WORDS:
        return f'"{v}"'
    return v


def set_value(lines: list[str], path: list[str], value: str | None) -> tuple[list[str], int | None]:
    """``lines`` with ``path`` set to ``value`` (None: removed), and the line it is on (None when removed)."""
    lines = list(lines)
    start, end, indent = 0, len(lines), -1
    for depth, key in enumerate(path):
        child = _child_indent(lines, start, end, indent + 2) if depth else 0
        i = _find(lines, start, end, child, key)
        leaf = depth == len(path) - 1
        if i is None:
            if value is None:
                return lines, None  # not set: nothing to remove
            new, ind = [], child
            for j, k in enumerate(path[depth:]):
                new.append(" " * ind + (f"{k}: {value}" if depth + j == len(path) - 1 else f"{k}:"))
                ind += 2
            at = start if depth else end  # under its section's header; a new section at the end
            if not depth and lines and lines[-1].strip():
                new.insert(0, "")
                at = len(lines)
            lines[at:at] = new
            return lines, at + len(new) - 1
        m = _KEY.match(lines[i])
        assert m is not None
        if leaf:
            rest = m.group(4)
            if not rest.strip() or rest.strip().startswith("#"):
                if _block_end(lines, i, end) > i + 1:
                    raise SystemExit(f"{'.'.join(path)} is a section, not a setting: name one of its settings.")
            if value is None:
                del lines[i]
                return lines, None
            comment = re.search(r"\s+#.*$", rest) if not rest.lstrip().startswith(("'", '"')) else None
            lines[i] = f"{m.group(1)}{key}: {value}{comment.group(0) if comment else ''}"
            return lines, i
        start, end, indent = i + 1, _block_end(lines, i, end), child
    return lines, None


def duplicates(lines: list[str]) -> dict[str, list[int]]:
    """Top-level sections that are in the file more than once (YAML keeps only the last), with line numbers."""
    seen: dict[str, list[int]] = {}
    for n, line in enumerate(lines, 1):
        m = _KEY.match(line)
        if m and _content(line) and not m.group(1):
            seen.setdefault(m.group(2), []).append(n)
    return {k: v for k, v in seen.items() if len(v) > 1}


def check(text: str, mode: str) -> str | None:
    """Why LittleBot wouldn't start with ``text`` as config/local.yaml, or None when it would."""
    from littlebot.core.config import load_config

    with tempfile.TemporaryDirectory() as d:
        candidate = Path(d) / "local.yaml"
        candidate.write_text(text, encoding="utf-8")
        try:
            load_config(mode=mode, config_file=candidate, include_local=False, use_dotenv=False, environ={})
        except Exception as e:  # noqa: BLE001 - YAML or settings errors: shown, nothing saved
            return str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return None


def effective(path: list[str], mode: str) -> object:
    from littlebot.core.config import load_config

    value: object = load_config(mode=mode, use_dotenv=False, environ={})
    for key in path:
        value = getattr(value, key)
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("setting", help="section.setting, e.g. chatgpt.live_barge_in_max_pitch_hz")
    ap.add_argument("value", nargs="?", help="the new value")
    ap.add_argument("--unset", action="store_true", help="remove it from local.yaml (the default applies again)")
    ap.add_argument("--show", action="store_true", help="only say what it is now")
    ap.add_argument("--mode", default="pi", help="the mode LittleBot runs in (default: pi)")
    ap.add_argument("--file", type=Path, default=LOCAL, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    path = [p for p in args.setting.strip().split(".") if p]
    if not path:
        ap.error("name a setting, e.g. chatgpt.live_barge_in_max_pitch_hz")
    if args.show:
        print(f"{'.'.join(path)} = {effective(path, args.mode)!r}")
        return 0
    if args.unset == (args.value is not None):
        ap.error("give a value, or --unset (or --show)")
    old = args.file.read_text(encoding="utf-8") if args.file.is_file() else ""
    lines = old.splitlines()
    for key, where in duplicates(lines).items():
        print(f"Note: '{key}:' is in {args.file.name} {len(where)} times (lines {', '.join(map(str, where))}); only "
              f"the last one counts, so the settings under the others are ignored. Merge them into one.")
    new_lines, at = set_value(lines, path, None if args.unset else yaml_value(args.value))
    if new_lines == lines:
        print(f"{'.'.join(path)} isn't set in {args.file.name}: nothing to change.")
        return 0
    text = "\n".join(new_lines) + "\n"
    why = check(text, args.mode)
    if why is not None:
        print(f"Not changed: LittleBot wouldn't accept that ({why}).")
        return 1
    args.file.parent.mkdir(parents=True, exist_ok=True)
    args.file.write_text(text, encoding="utf-8")
    done = f"line {at + 1}: {new_lines[at].strip()}" if at is not None else "removed (the default applies)"
    print(f"{'.'.join(path)}: {done}. Restart LittleBot: systemctl --user restart littlebot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
