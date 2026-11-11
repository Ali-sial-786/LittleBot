"""scripts/check_whatsapp.py: asks the open WhatsApp page which selectors match, and prints keys and yes / no only."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from calls_testkit import PAGE_TARGET  # type: ignore[import-not-found]

from littlebot.calls import page_scripts as scripts
from littlebot.core.paths import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "scripts" / "check_whatsapp.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("check_whatsapp", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _answer(**more: Any) -> dict[str, Any]:
    keys = {k: [False] * len(v) for k, v in scripts.SELECTORS.items()}
    keys["chat_list"][0] = keys["search_box"][3] = keys["end_call"][1] = True
    return {"keys": keys, "linked": True, "qr": False, "call": "in_call", "container": 2, "videos": 2, "cover": True,
            "covered": True, "holes": 1, **more}


async def test_it_reports_keys_and_yes_no_only(env: Any, tmp_path: Path) -> None:
    mod = _load()
    dt = env.devtools
    dt.answers["Target.getTargets"] = {"targetInfos": [
        {"targetId": "T-OTHER", "type": "page", "url": "https://example.com/"},
        {"targetId": PAGE_TARGET, "type": "page", "url": "https://web.whatsapp.com/"}]}
    # a page that answered more than it was asked: none of it is passed on
    dt.eval_value = lambda expr: _answer(title="Ali Khan", text="a secret message", call="in_call")
    (tmp_path / "DevToolsActivePort").write_text(f"{dt.port}\n/devtools/browser/fake-id\n")
    report = await mod.check(tmp_path)
    assert set(report) == {"keys", "linked", "qr", "call", "container", "videos", "cover", "covered", "holes"}
    assert report["cover"] is True and report["covered"] is True and report["holes"] == 1
    assert report["container"] == 2 and report["call"] == "in_call"
    assert set(report["keys"]) == set(scripts.SELECTORS)
    assert all(isinstance(hit, bool) for hits in report["keys"].values() for hit in hits)
    assert dt.last("Target.attachToTarget") == {"targetId": PAGE_TARGET, "flatten": True}  # WhatsApp's page only
    assert dt.count("Input.dispatchMouseEvent") == 0  # it presses nothing
    text = mod.render(report)
    assert "Ali" not in text and "secret" not in text
    assert "cover in place: yes" in text and "hiding the page: yes" in text and "holes in it: 1" in text
    assert "call_container #2 matched" in text
    assert "search_box      FOUND      (first match: #3)" in text and "video_call      not found" in text
    for sel in scripts.SELECTORS["end_call"]:
        assert sel in text  # (LittleBot's own selectors, so a broken one can be named)


async def test_whatsapp_not_open(tmp_path: Path, env: Any) -> None:
    mod = _load()
    with pytest.raises(mod.NotOpen):
        await mod.check(tmp_path)  # no DevToolsActivePort
    (tmp_path / "DevToolsActivePort").write_text(f"{env.devtools.port}\n/devtools/browser/fake-id\n")
    with pytest.raises(mod.NotOpen, match="not on web.whatsapp.com"):
        await mod.check(tmp_path)  # Chromium is there, on another page


def test_from_the_command_line(tmp_path: Path) -> None:
    res = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60,
                         cwd=PROJECT_ROOT)
    assert res.returncode == 0 and "--json" in res.stdout and "--profile" in res.stdout
    res = subprocess.run([sys.executable, str(SCRIPT), "--profile", str(tmp_path)], capture_output=True, text=True,
                         timeout=60, cwd=PROJECT_ROOT)
    assert res.returncode == 2 and "isn't open" in res.stdout and "Traceback" not in res.stderr
