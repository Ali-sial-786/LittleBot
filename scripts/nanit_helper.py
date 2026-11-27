#!/usr/bin/env python3
"""LittleBot's helper for the Nanit bridge (docs/NANIT.md). Run by scripts/setup_nanit.sh and scripts/nanit_bridge.sh,
never by LittleBot itself, and never prints a password or a token.

    nanit_helper.py login SESSION_FILE      sign in to Nanit on the terminal (email, password, the emailed code)
                                            and write the bridge's session file (mode 600)
    nanit_helper.py tidy SESSION_FILE       repair the session file before the bridge starts (the bridge rewrites
                                            it without shortening it, which can leave junk after the JSON)
    nanit_helper.py cameras SESSION_FILE OUT    write OUT with each camera's name and baby uid and nothing else
                                            (exit 0 once the bridge has listed at least one camera, else 1)
    nanit_helper.py has-session SESSION_FILE    exit 0 if the file holds a sign-in (prints nothing)

The sign-in is the same as the bridge's own scripts/init-nanit.sh (indiefan/home_assistant_nanit): POST
https://api.nanit.com/login with the email and password; Nanit answers with an ``mfa_token`` and emails a code;
the same request again with ``mfa_token`` and ``mfa_code`` gives a ``refresh_token``. The session file is the
bridge's own (revision 3): ``{"revision": 3, "authToken": "", "refreshToken": "..."}``; the bridge trades the
refresh token for a fresh one at each start and keeps it there. Unlike init-nanit.sh the password never goes on a
command line, nothing is printed, and a failed sign-in writes nothing.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API = os.environ.get("LB_NANIT_API", "https://api.nanit.com")  # tests point this at a fake server
REVISION = 3  # the bridge's pkg/session Revision
UID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")  # the bridge's own rule for rtmp://.../local/<uid>
TIMEOUT_S = 30
MAX_SESSION_BYTES = 1024 * 1024


def _post(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(f"{API}/login", data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "nanit-api-version": "1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 - a fixed https address
            code, raw = resp.status, resp.read(65536)
    except urllib.error.HTTPError as e:  # 482 = "a code was emailed", 401 = wrong email or password
        code, raw = e.code, e.read(65536)
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        data = {}
    return code, data if isinstance(data, dict) else {}


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with mode 600, all at once (a temporary file in the same folder, then renamed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def login(session: Path) -> int:
    print("Sign in to your Nanit account. LittleBot keeps only the sign-in the bridge needs, in")
    print(f"{session} (only you can read it); your password is not kept.")
    email = input("Nanit email: ").strip()
    password = getpass.getpass("Nanit password (not shown): ")
    if not email or not password:
        print("Nothing written: the email and the password are both needed.")
        return 1
    code, data = _post({"email": email, "password": password, "channel": "email"})
    refresh = str(data.get("refresh_token") or "")
    if not refresh:
        mfa = str(data.get("mfa_token") or "")
        if not mfa:
            print(f"Nanit didn't accept the sign-in (HTTP {code}). Check the email and the password, and try again.")
            return 1
        print("Nanit has emailed you a code.")
        mfa_code = input("Code from the email: ").strip()
        if not re.fullmatch(r"\d{4,8}", mfa_code):
            print("Nothing written: the code is the digits from Nanit's email.")
            return 1
        code, data = _post({"email": email, "password": password, "channel": "email", "mfa_token": mfa,
                            "mfa_code": mfa_code})
        refresh = str(data.get("refresh_token") or "")
        if not refresh:
            print(f"Nanit didn't accept the code (HTTP {code}). Run it again for a new code.")
            return 1
    password = ""
    _write_private(session, json.dumps({"revision": REVISION, "authToken": "", "refreshToken": refresh}))
    print("Signed in. The sign-in is saved for the bridge.")
    return 0


def _first_json(path: Path) -> tuple[dict[str, Any] | None, int, int]:
    """The session's JSON object (the first one in the file), where it ends, and the file's size."""
    raw = path.read_bytes()[:MAX_SESSION_BYTES].decode("utf-8", "replace")
    text = raw.lstrip()
    try:
        obj, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None, 0, len(raw)
    return (obj if isinstance(obj, dict) else None), end + (len(raw) - len(text)), len(raw)


def tidy(session: Path) -> int:
    if not session.is_file():
        print("nanit: no sign-in yet (run scripts/setup_nanit.sh)", file=sys.stderr)
        return 1
    obj, end, size = _first_json(session)
    if obj is None:
        print("nanit: the sign-in file can't be read; sign in again: scripts/setup_nanit.sh --login-again",
              file=sys.stderr)
        return 1
    if session.read_bytes()[end:].strip():  # junk after the JSON: keep only the JSON
        _write_private(session, json.dumps(obj))
        print("nanit: tidied the sign-in file", file=sys.stderr)
    os.chmod(session, 0o600)
    return 0


def cameras(session: Path, out: Path) -> int:
    try:
        obj, _end, _size = _first_json(session)
    except OSError:
        return 1
    babies = obj.get("babies") if isinstance(obj, dict) else None
    found = []
    for b in babies if isinstance(babies, list) else []:
        if not isinstance(b, dict):
            continue
        uid = str(b.get("uid") or "")
        if UID_RE.match(uid):
            name = re.sub(r"[^\w '\-.]", "", str(b.get("name") or ""))[:40].strip()
            found.append({"uid": uid, "name": name or f"Camera {len(found) + 1}"})
    if not found:
        return 1
    _write_private(out, json.dumps({"cameras": found}, indent=1) + "\n")
    print(f"nanit: {len(found)} camera(s): {', '.join(c['name'] for c in found)}", file=sys.stderr)
    return 0


def has_session(session: Path) -> int:
    try:
        obj, _end, _size = _first_json(session)
    except OSError:
        return 1
    return 0 if isinstance(obj, dict) and obj.get("refreshToken") else 1


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "login":
        return login(Path(argv[1]))
    if len(argv) == 2 and argv[0] == "tidy":
        return tidy(Path(argv[1]))
    if len(argv) == 3 and argv[0] == "cameras":
        return cameras(Path(argv[1]), Path(argv[2]))
    if len(argv) == 2 and argv[0] == "has-session":
        return has_session(Path(argv[1]))
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nStopped; nothing written.")
        sys.exit(130)
