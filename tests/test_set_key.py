"""scripts/set_key.py: keys saved with mode 600, never printed."""

from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "set_key.py"


def _load():
    spec = importlib.util.spec_from_file_location("set_key", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_saves_hidden_key_with_600(tmp_path, monkeypatch, capsys) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(mod.getpass, "getpass", lambda prompt: "dg-secret-123456")
    assert mod.main(["deepgram"]) == 0
    f = tmp_path / "deepgram_api_key"
    assert f.read_text().strip() == "dg-secret-123456"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "dg-secret" not in out
    assert mod.main(["--check"]) == 0
    assert "deepgram        set" in capsys.readouterr().out
    assert mod.main(["--remove", "deepgram"]) == 0 and not f.exists()


@pytest.mark.parametrize("bad", ["", "short", "has space in it"])
def test_refuses_things_that_are_not_keys(tmp_path, monkeypatch, bad) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(mod.getpass, "getpass", lambda prompt: bad)
    assert mod.main(["cartesia"]) == 1
    assert not (tmp_path / "cartesia_api_key").exists()
