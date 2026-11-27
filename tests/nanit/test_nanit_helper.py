"""scripts/nanit_helper.py: the Nanit sign-in (a fake Nanit server: the code step, wrong password, wrong code), the
session file tidy, and the camera list holding names and uids only. Nothing secret is printed."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

HELPER = Path(__file__).resolve().parents[2] / "scripts" / "nanit_helper.py"
PASSWORD = "pa55-word-never-printed"
REFRESH = "refresh-token-never-printed"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("nanit_helper_under_test", HELPER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeNanit:
    """POST /login: email + password -> 482 + mfa_token (a code is emailed); + mfa_token + mfa_code -> 201."""

    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, str], dict[str, Any]]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                fake.requests.append(({k.lower(): v for k, v in self.headers.items()}, body))
                if self.path != "/login" or body.get("password") != PASSWORD:
                    code, out = 401, {"message": "Unauthorized"}
                elif "mfa_code" not in body:
                    code, out = 482, {"mfa_token": "mfa-123", "phone_suffix": ""}
                elif body.get("mfa_token") == "mfa-123" and body.get("mfa_code") == "0123":
                    code, out = 201, {"access_token": "access-x", "refresh_token": REFRESH}
                else:
                    code, out = 403, {"message": "bad code"}
                raw = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def nanit() -> Any:
    f = FakeNanit()
    yield f
    f.close()


def _sign_in(mod: Any, monkeypatch: Any, nanit: FakeNanit, answers: list[str], password: str = PASSWORD) -> None:
    mod.API = nanit.url
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    monkeypatch.setattr(mod.getpass, "getpass", lambda prompt="": password)


def test_sign_in_with_the_emailed_code(tmp_path: Path, monkeypatch: Any, nanit: FakeNanit, capsys: Any) -> None:
    mod = _load()
    session = tmp_path / "data" / "nanit" / "session.json"
    _sign_in(mod, monkeypatch, nanit, ["me@example.com", "0123"])
    assert mod.main(["login", str(session)]) == 0
    assert json.loads(session.read_text()) == {"revision": 3, "authToken": "", "refreshToken": REFRESH}
    if os.name == "posix":
        assert session.stat().st_mode & 0o777 == 0o600 and session.parent.stat().st_mode & 0o777 == 0o700
    first, second = nanit.requests
    assert first[0]["nanit-api-version"] == "1" and first[1] == {"email": "me@example.com", "password": PASSWORD,
                                                                 "channel": "email"}
    assert second[1]["mfa_token"] == "mfa-123" and second[1]["mfa_code"] == "0123"
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err and REFRESH not in out.out + out.err and "mfa-123" not in out.out
    assert [p.name for p in session.parent.iterdir()] == ["session.json"]  # no temporary file left


def test_a_wrong_password_or_code_writes_nothing(tmp_path: Path, monkeypatch: Any, nanit: FakeNanit,
                                                 capsys: Any) -> None:
    mod = _load()
    session = tmp_path / "nanit" / "session.json"
    _sign_in(mod, monkeypatch, nanit, ["me@example.com"], password="wrong")
    assert mod.main(["login", str(session)]) == 1 and not session.exists()
    assert "didn't accept the sign-in" in capsys.readouterr().out
    _sign_in(mod, monkeypatch, nanit, ["me@example.com", "9999"])
    assert mod.main(["login", str(session)]) == 1 and not session.exists()
    assert "didn't accept the code" in capsys.readouterr().out
    _sign_in(mod, monkeypatch, nanit, ["me@example.com", "not a code"])
    assert mod.main(["login", str(session)]) == 1 and not session.exists()
    _sign_in(mod, monkeypatch, nanit, [""])
    assert mod.main(["login", str(session)]) == 1 and not session.exists()


def test_tidy_cuts_junk_after_the_json(tmp_path: Path, capsys: Any) -> None:
    mod = _load()
    session = tmp_path / "session.json"
    good = {"revision": 3, "authToken": "a", "refreshToken": REFRESH, "babies": []}
    session.write_text(json.dumps(good) + 'en":"leftover"}')  # the bridge rewrote it without shortening it
    assert mod.main(["tidy", str(session)]) == 0
    assert json.loads(session.read_text()) == good
    assert mod.main(["tidy", str(session)]) == 0  # already tidy: unchanged
    session.write_text("not json")
    assert mod.main(["tidy", str(session)]) == 1 and "--login-again" in capsys.readouterr().err
    assert mod.main(["tidy", str(tmp_path / "none.json")]) == 1
    assert REFRESH not in capsys.readouterr().err


def test_the_camera_list_has_names_and_uids_only(tmp_path: Path) -> None:
    mod = _load()
    session = tmp_path / "session.json"
    out = tmp_path / "cameras.json"
    session.write_text(json.dumps({"revision": 3, "authToken": "secret-a", "refreshToken": REFRESH}))
    assert mod.main(["cameras", str(session), str(out)]) == 1 and not out.exists()  # not listed yet
    session.write_text(json.dumps({"revision": 3, "authToken": "secret-a", "refreshToken": REFRESH, "babies": [
        {"uid": "a1b2c3d4", "name": "Leo", "camera_uid": "N301AXXX"},
        {"uid": "../bad", "name": "Bad"}, {"uid": "e5f6", "name": ""}]}) + "junk")
    assert mod.main(["cameras", str(session), str(out)]) == 0
    text = out.read_text()
    assert json.loads(text) == {"cameras": [{"uid": "a1b2c3d4", "name": "Leo"}, {"uid": "e5f6", "name": "Camera 2"}]}
    assert REFRESH not in text and "secret-a" not in text and "N301AXXX" not in text


def test_has_session_and_usage(tmp_path: Path) -> None:
    mod = _load()
    session = tmp_path / "session.json"
    assert mod.main(["has-session", str(session)]) == 1
    session.write_text(json.dumps({"revision": 3, "refreshToken": ""}))
    assert mod.main(["has-session", str(session)]) == 1
    session.write_text(json.dumps({"revision": 3, "refreshToken": REFRESH}))
    assert mod.main(["has-session", str(session)]) == 0
    assert mod.main(["bogus"]) == 2


def test_the_helper_is_plain_python() -> None:
    text = HELPER.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env python3") and "\r\n" not in text
    assert "print(refresh" not in text and "print(password" not in text
    assert sys.version_info >= (3, 9)
