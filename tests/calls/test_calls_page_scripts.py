"""The WhatsApp page scripts (littlebot/calls/page_scripts.py): the selectors in one place with fallbacks, the
scripts as text (they parse, they hand back no page text), and the cover: what it says in each state, and that a
failure stays covered. (The cover really hiding a page is test_calls_real_chromium.py.)"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from littlebot.calls import page_scripts as scripts
from littlebot.calls import service as service_mod

NEEDED = ("qr", "chat_list", "search_box", "chat_header", "video_call", "voice_call", "call_menu", "end_call",
          "accept_call", "decline_call", "call_container")


def test_every_thing_has_several_ways_to_be_found() -> None:
    for key in NEEDED:
        assert len(scripts.SELECTORS[key]) >= 4, key
    joined = {k: " ".join(v) for k, v in scripts.SELECTORS.items()}
    for key in ("video_call", "voice_call", "end_call", "accept_call", "decline_call"):
        for kind in ("aria-label", "data-icon", "title"):
            assert kind in joined[key], (key, kind)
    assert "role=" in joined["video_call"] and "role=" in joined["call_container"] and "role=" in joined["search_box"]


def test_the_selectors_live_in_one_place() -> None:
    """No other module of LittleBot carries a WhatsApp selector."""
    root = Path(scripts.__file__).resolve().parents[1]
    marks = ("data-icon", "aria-label*=", "#pane-side", "data-testid")
    for path in root.rglob("*.py"):
        if path.name == "page_scripts.py" and path.parent.name == "calls":
            continue
        if path.parent.name == "browser":  # (the browser view has its own pages)
            continue
        text = path.read_text(encoding="utf-8")
        assert not any(m in text for m in marks), path


ALL = {
    "state": scripts.state_script(), "check": scripts.check_script(),
    "find": scripts.find_chat_script('Ali "the" Khan\\'), "header": scripts.header_script("Ali Khan"),
    "pass": scripts.pass_script(True), "update": scripts.cover_update_script(scripts.cover_state()),
    "cover": scripts.cover_script(scripts.cover_state()),
}


@pytest.mark.skipif(shutil.which("node") is None, reason="no node to parse JavaScript with")
@pytest.mark.parametrize("name", sorted(ALL))
def test_the_scripts_parse(name: str, tmp_path: Path) -> None:
    f = tmp_path / f"{name}.js"
    f.write_text("function f() { return " + ALL[name] + "; }", encoding="utf-8")
    res = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr[:400]


def test_the_scripts_hand_back_no_page_text() -> None:
    """What comes back is whether and where: the report scripts never return textContent, a title or a name."""
    for name in ("state", "check"):
        ret = ALL[name][ALL[name].rindex("return {"):]
        for leak in ("textContent", "innerText", "getAttribute('title')", "innerHTML", ".value", "aria-label"):
            assert leak not in ret, (name, leak)
        assert "textContent" not in ALL[name] and "innerText" not in ALL[name]
    for name in ("find", "header"):  # these compare names inside the page and answer found / a point / yes-no
        own = ALL[name].split("})(SEL);", 1)[1]  # (after the shared helpers, which return elements to each other)
        returns = [line for line in own.splitlines() if "return {" in line]
        assert returns and all(set(_fields(line)) <= {"found", "x", "y", "header", "match"} for line in returns), name


def _fields(line: str) -> list[str]:
    body = line[line.index("return {") + 8:line.rindex("}")]
    return [part.split(":")[0].strip() for part in body.split(",") if part.strip()]


def test_a_name_is_passed_as_data_never_as_code() -> None:
    script = scripts.find_chat_script('x"); alert(1); ("')
    assert script.endswith('("x\\"); alert(1); (\\"")')


# ---------------------------------------------------------------- the cover
def test_the_cover_script() -> None:
    src = scripts.cover_script(scripts.cover_state())
    assert src.startswith("((SEL, INIT, BIND, TAG) => {") and '"littlebot-cover"' in src and '"littlebotCalls"' in src
    assert "innerHTML" not in src and "createElement('style')" not in src  # nothing WhatsApp's CSP could refuse
    assert "attachShadow" in src and "2147483647" in src
    assert "MutationObserver" in src  # put back at once when the page removes it
    assert '"on":true' in src  # covered from the first paint
    # the two things let through, and never a box that holds the chats
    assert "q('chat_list', el) || q('search_box', el) || q('chat_header', el)" in src
    assert "v.srcObject" in src  # only live video pictures, never a video in a chat
    for label in ("'Answer'", "'Decline'", "'Hang up'", "'Close'"):
        assert label in src


def test_the_cover_is_on_unless_a_grown_up_switched_it_off() -> None:
    assert scripts.cover_state()["on"] is True
    assert scripts.cover_state(hide=False)["on"] is False
    assert scripts.cover_state(theme="dark")["theme"] == "dark" and scripts.cover_state(theme="x")["theme"] == "light"


@pytest.mark.parametrize("kw,title", [
    ({"phase": "starting"}, "Opening WhatsApp…"),
    ({"phase": "loading", "name": "Dad"}, "Opening WhatsApp…"),
    ({"phase": "linking", "name": "Dad"}, "Scan this with your phone"),
    ({"phase": "ready", "name": "Dad"}, "Calling Dad…"),
    ({"phase": "ready", "answer": True}, "Waiting for the call…"),
    ({"phase": "ready"}, "WhatsApp is ready"),
    ({"phase": "ready", "call": "incoming", "answer": True}, "Someone is calling"),
    ({"phase": "ready", "call": "in_call", "name": "Dad"}, "On a call with Dad"),
    ({"phase": "ready", "call": "in_call"}, "On a call"),
    ({"phase": "ready", "name": "Dad", "problem": "I couldn't find the call button."},
     "I couldn't find the call button."),
])
def test_what_the_cover_says(kw: dict, title: str) -> None:
    state = scripts.cover_state(**kw)
    assert state["title"] == title and state["on"] is True
    assert set(state) == {"on", "theme", "title", "sub"}


def test_a_failure_stays_covered_and_says_how_to_close() -> None:
    for step, words in service_mod.PROBLEMS.items():
        state = scripts.cover_state(phase="ready", problem=words.format(name="Dad"))
        assert state["on"] is True and state["title"] == words.format(name="Dad"), step
        assert "Close" in state["sub"]


def test_the_update_script_carries_the_state_as_data() -> None:
    state = scripts.cover_state(phase="ready", name='D"ad')
    src = scripts.cover_update_script(state)
    payload = json.loads(json.loads(src[src.rindex("})(") + 3:-1]))
    assert payload == state


def test_the_loading_page_is_littlebots_own() -> None:
    assert scripts.loading_url().startswith("data:text/html;charset=utf-8,")
    assert "ffd6e4" in scripts.loading_url("light") and "322a44" in scripts.loading_url("dark")
