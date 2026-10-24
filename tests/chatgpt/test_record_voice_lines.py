"""scripts/record_voice_lines.py: which lines, how long, the child's name, --missing / --only / --dry-run."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from chatgpt_helpers import signed_in_auth

import littlebot.chatgpt.voice_record as vr
from littlebot.app.spoken_lines import collect, estimate_seconds
from littlebot.audio.voice_lines import get_store
from littlebot.testing import make_test_context

spec = importlib.util.spec_from_file_location(
    "record_voice_lines", Path(__file__).resolve().parents[2] / "scripts" / "record_voice_lines.py")
rvl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rvl)


def args(**kw: Any) -> argparse.Namespace:
    base = dict(dry_run=False, only=None, missing=False, for_age=False, all_ages=False, with_name=None, retries=2,
                lines_per_call=40)
    return argparse.Namespace(**{**base, **kw})


@pytest.fixture
def rctx(tmp_path):
    c = make_test_context(data_dir=str(tmp_path))
    c.settings.set("profile", {**c.settings.get("profile"), "nickname": "Amara", "age_months": 10})
    c.privacy.set(cloud_enabled=True)
    c.extras["chatgpt_auth"] = signed_in_auth(tmp_path)
    yield c
    c.db.close()


@pytest.fixture
def fake_recording(monkeypatch):
    """record_lines without a call: every line comes back right, except the ones in ``wrong``."""
    asked: list[list[str]] = []
    wrong: set[str] = set()

    async def fake(auth, cfg, lines, *, on_take=None, on_status=None, stop=None, **kw):
        asked.append(list(lines))
        if on_status:
            on_status("connecting")
        takes = []
        for i, line in enumerate(lines):
            said = "Quack!" if line in wrong else line
            take = vr.Take(line, np.full(int(0.5 * vr.OUT_RATE), 0.3, np.float32), said, "bubbie",
                           vr.similarity(said, line), 1)
            takes.append(take)
            if on_take:
                await on_take(i, len(lines), take)
        return takes

    monkeypatch.setattr(vr, "record_lines", fake)
    return asked, wrong


def test_every_kind_of_line_is_collected_activities_for_her_age_first(rctx):
    lines = collect(rctx.config, rctx.settings)
    groups = {x.group for x in lines}
    assert groups == {"activities", "games", "replies", "robot"}
    texts = [x.text for x in lines]
    assert "Tap again, and I'll do my trick!" in texts and "Hi Amara! Let's play peekaboo!" in texts
    assert "Moooo!" in texts  # the peekaboo game's cow (host.say)
    assert "Hi hi! Wave wave!" in texts  # a scripted greeting, for a baby
    assert "I didn't hear that. Can you say it again?" in texts and "Let's find a grown-up right now." in texts
    assert "Hello! I'm LittleBot. Shall we play?" in texts
    first_older = next(i for i, x in enumerate(lines) if x.group == "activities" and not x.suitable)
    assert all(x.suitable for x in lines[:first_older] if x.group == "activities")
    assert len({x.hash for x in lines}) == len(lines)
    assert estimate_seconds(lines) > 30 * 60  # all of them take a while...
    assert estimate_seconds(lines[:10]) < 60


async def test_dry_run_counts_and_estimates_without_recording(rctx, fake_recording, capsys):
    asked, _wrong = fake_recording
    assert await rvl.main_async(args(dry_run=True, only=["again_again"]), rctx) == 0
    out = capsys.readouterr().out
    assert out.startswith("5 lines to record in the ChatGPT voice (5 activities; 23 words).")
    assert "That takes about 1 minute" in out and "[activity:again_again] Boing!" in out
    assert "Dry run: nothing was recorded." in out and asked == []
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary["lines"] == 5 and summary["dry_run"] is True and 0.2 < summary["minutes"] < 1.0


async def test_only_takes_names_and_groups_and_says_which_exist(rctx, fake_recording, capsys):
    assert await rvl.main_async(args(dry_run=True, only=["Again! Again!", "games"]), rctx) == 0
    assert "11 lines to record" in capsys.readouterr().out
    assert await rvl.main_async(args(dry_run=True, only=["nope"]), rctx) == 2
    out = capsys.readouterr().out
    assert "No activity called 'nope'" in out and "again_again" in out


async def test_the_childs_name_only_with_the_parents_ok(rctx, fake_recording, capsys):
    await rvl.main_async(args(dry_run=True, only=["peekaboo"]), rctx)
    out = capsys.readouterr().out
    assert "Hi Amara" not in out and "2 lines with Amara's name are left out" in out and "--with-name" in out
    await rvl.main_async(args(dry_run=True, only=["peekaboo"], with_name=True), rctx)
    out = capsys.readouterr().out
    assert "Hi Amara! Let's play peekaboo!" in out and "recorded with it" in out
    rctx.settings.set("ai.share_child_name", True)  # the AI page's sharing switch is the parent's OK too
    await rvl.main_async(args(dry_run=True, only=["peekaboo"]), rctx)
    assert "Hi Amara! Let's play peekaboo!" in capsys.readouterr().out


async def test_recording_saves_each_matched_line_and_lists_the_rest(rctx, fake_recording, capsys):
    asked, wrong = fake_recording
    wrong.add("Boing!")
    assert await rvl.main_async(args(only=["again_again"]), rctx) == 0
    out = capsys.readouterr().out
    assert asked == [["Tap again, and I'll do my trick!", "Boing!", "All done! That was fun!",
                      "Boing! Bye-bye for now!", "Boing, boing! What a fun game!"]]
    assert "[   1/5] ok   Tap again, and I'll do my trick!" in out
    assert "[   2/5] MISS Boing!  -> the voice said: Quack!" in out
    assert "Done: 4 of 5 lines recorded in the voice bubbie." in out
    assert "1 lines never matched" in out and "Boing!  (the voice said: Quack!)" in out
    store = get_store(rctx)
    assert store.has("Tap again, and I'll do my trick!") and not store.has("Boing!")
    assert (Path(rctx.config.data_path) / "voice_lines" / "index.json").is_file()
    # --missing: only what isn't recorded yet
    wrong.clear()
    assert await rvl.main_async(args(only=["again_again"], missing=True), rctx) == 0
    assert asked[-1] == ["Boing!"] and store.has("Boing!")
    assert await rvl.main_async(args(only=["again_again"], missing=True), rctx) == 0
    assert "Nothing to record" in capsys.readouterr().out and len(asked) == 2


@pytest.mark.parametrize("change, message", [
    (lambda c: c.privacy.set(cloud_enabled=False), "Cloud communication is turned off"),
    (lambda c: c.extras["chatgpt_auth"].store.clear(), "Not signed in with ChatGPT"),
])
async def test_it_says_what_to_do_first(rctx, fake_recording, capsys, change, message):
    change(rctx)
    assert await rvl.main_async(args(only=["again_again"]), rctx) == 1
    assert message in capsys.readouterr().out and fake_recording[0] == []
