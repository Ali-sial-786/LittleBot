"""The parent dashboard's JavaScript in headless Chromium, against the REAL robot.

The robot is built through the composition root (every real subsystem, mock devices) and serves
the dashboard on an ephemeral port, as in ``test_integration_dashboard.py``; a phone- or
desktop-sized browser then clicks through pages like a parent. These are regression tests for
UI behaviour that API tests cannot see. External requests (YouTube thumbnails) are blocked.

Skipped when Playwright or a Chromium build is not available.
"""

from __future__ import annotations

import glob
import os
import re
from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from littlebot.app.robot import LittleBot  # noqa: E402
from littlebot.chatgpt.auth import ChatGPTAuth  # noqa: E402
from littlebot.core.context import build_core_context  # noqa: E402
from littlebot.hardware.system import MockSystemMonitor  # noqa: E402
from littlebot.testing import make_test_config  # noqa: E402

pytestmark = pytest.mark.slow

PASSWORD = "a-good-parent-password"
PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1280, "height": 800}


def _chromium_candidates() -> list[str | None]:
    out: list[str | None] = [os.environ.get("LITTLEBOT_TEST_CHROMIUM") or None]
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), "/opt/pw-browsers", os.path.expanduser("~/.cache/ms-playwright")]
    for root in filter(None, roots):
        out += sorted(glob.glob(os.path.join(root, "chromium-*", "chrome-linux*", "chrome")), reverse=True)
        out += sorted(glob.glob(os.path.join(root, "chromium_headless_shell-*", "chrome-*", "*headless_shell")), reverse=True)
    return out


@pytest.fixture(scope="module")
def browser():
    pw = sync_playwright().start()
    launched, errors = None, []
    for exe in [None] + [c for c in _chromium_candidates() if c]:
        try:
            launched = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
            break
        except PlaywrightError as e:
            errors.append(str(e).splitlines()[0])
    if launched is None:
        pw.stop()
        pytest.skip(f"no Chromium available for Playwright: {errors[:2]}")
    yield launched
    launched.close()
    pw.stop()


@pytest.fixture(scope="module")
def robot(browser, tmp_path_factory):
    cfg = make_test_config(dashboard={"enabled": True, "port": 0})
    ctx = build_core_context(cfg, db_path=":memory:", monitor=MockSystemMonitor())
    ctx.clock.scale = 1.0  # activities run in real time, like for a parent
    ctx.extras["chatgpt_auth"] = ChatGPTAuth(tmp_path_factory.mktemp("chatgpt") / "chatgpt_auth.json")  # never a real sign-in
    bot = LittleBot(ctx)
    bot.build_subsystems()
    bot.start()
    base = f"http://127.0.0.1:{bot.dashboard.port}"
    # First-run setup once; every test's browser reuses the session cookie.
    setup = browser.new_context()
    r = setup.request.post(f"{base}/api/setup", data={"setup_code": bot.dashboard.auth.issue_setup_code(),
                                                       "password": PASSWORD})
    assert r.ok, r.text()
    state = setup.storage_state()
    setup.close()
    yield bot, base, state
    bot.shutdown("test end")


class Parent:
    """One browser (phone or desktop) logged in to the dashboard; collects JS errors."""

    def __init__(self, browser: Any, robot: Any, viewport: dict[str, int]) -> None:
        self.bot, self.base, state = robot
        mobile = viewport == PHONE
        self.context = browser.new_context(viewport=viewport, storage_state=state, is_mobile=mobile, has_touch=mobile)
        self.context.route(re.compile(r"^(?!http://127\.0\.0\.1[:/])"), lambda route: route.abort())
        self.page = self.context.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" and "status of" not in m.text else None)

    def open(self, page_id: str) -> Any:
        self.page.goto(f"{self.base}/#/{page_id}")
        expect(self.page.locator(".topbar")).to_be_visible()
        return self.page

    def api(self, method: str, path: str, body: Any = None) -> Any:
        """Same-origin API call from inside the page (session cookie + CSRF token)."""
        return self.page.evaluate(
            """async ([method, path, body]) => {
                 const r = await fetch('/api' + path, {method, credentials: 'same-origin', headers: {
                   'Content-Type': 'application/json', 'X-CSRF-Token': window.littlebot.session.csrf_token},
                   body: method === 'GET' ? undefined : JSON.stringify(body || {})});
                 return r.json(); }""", [method, path, body])

    def close(self) -> None:
        self.context.close()


@pytest.fixture
def phone(browser, robot):
    p = Parent(browser, robot, PHONE)
    yield p
    p.api("POST", "/stop")  # leave the shared robot idle for the next test
    assert p.errors == []
    p.close()


@pytest.fixture
def desktop(browser, robot):
    p = Parent(browser, robot, DESKTOP)
    yield p
    assert p.errors == []
    p.close()


def test_starting_an_activity_on_a_phone_brings_its_running_card_into_view(phone):
    page = phone.open("activities")
    phone.api("PUT", "/profile", {"age_months": None, "birth_month": ""})
    page.reload()
    expect(page.locator("article.act").first).to_be_visible()
    card = page.locator("article.act").last  # far down the list on a phone
    card.scroll_into_view_if_needed()
    card.get_by_role("button", name="Start").click()
    running = page.locator(".act-running")
    expect(running).to_be_visible()
    expect(running).to_be_in_viewport()  # with its Pause / Stop and caregiver answer buttons
    expect(running.get_by_role("button", name="Stop")).to_be_visible()


def test_suggested_activities_section(phone):
    page = phone.open("activities")
    phone.api("PUT", "/profile", {"nickname": "Sami", "age_months": 10})
    phone.api("PUT", "/activities/again_again/preferences", {"favourite": True})
    page.reload()
    section = page.get_by_role("region", name="Suggested activities")
    expect(section.get_by_role("heading", name="Suggested for Sami")).to_be_visible()
    expect(section).to_contain_text("Picked for 10 months")
    items = section.locator(".suggest-item")
    expect(items).to_have_count(6)
    expect(items.first).to_contain_text("Again! Again!")  # favourite first
    # Disallowing an activity in the list below removes it from the suggestions.
    card = page.locator("article.act", has=page.get_by_role("heading", name="Again! Again!", exact=True))
    card.get_by_role("checkbox").uncheck()
    expect(items.first).not_to_contain_text("Again! Again!")
    card.get_by_role("checkbox").check()
    expect(items.first).to_contain_text("Again! Again!")
    # Start straight from a suggestion.
    items.first.get_by_role("button").click()
    expect(page.locator(".act-running")).to_contain_text("Again! Again!")
    phone.api("POST", "/stop")
    # Without a nickname or age.
    phone.api("PUT", "/profile", {"nickname": "", "age_months": None, "birth_month": ""})
    phone.api("PUT", "/activities/again_again/preferences", {"favourite": False})
    page.reload()
    expect(section.get_by_role("heading", name="Suggested for your child")).to_be_visible()
    expect(section).to_contain_text("Add your child's age")


def test_running_activity_without_step_text_never_shows_null(desktop):
    """Right after Start (and in steps without words) the engine reports step_text "" - the cards
    must simply leave that line out, not print "null"."""
    import json

    def blank_step(route):
        resp = route.fetch()
        st = resp.json()
        if st.get("activities", {}).get("running"):
            st["activities"].update(step_text="", total_steps=0, prompt=None)
        route.fulfill(response=resp, body=json.dumps(st))

    page = desktop.open("home")
    page.route("**/api/status", blank_step)
    desktop.api("POST", "/activities/peekaboo/start")
    home_card = page.locator("section.card", has=page.get_by_role("heading", name="Activity"))
    expect(home_card).to_contain_text("Playing")
    assert "null" not in home_card.inner_text()
    page.get_by_role("link", name="Activities").first.click()
    running = page.locator(".act-running")
    expect(running).to_contain_text("Now playing")
    assert "null" not in running.inner_text()
    assert "null" not in page.locator("article.act").first.inner_text()
    desktop.api("POST", "/stop")


def test_devices_page_shows_the_mock_camera_as_simulated(desktop):
    page = desktop.open("devices")
    camera = page.locator("section.card", has=page.get_by_role("heading", name="Camera"))
    expect(camera.locator(".card-head .badge")).to_have_text("Simulated")  # not "Working"


def test_turning_videos_off_shows_why_they_will_not_play(desktop):
    page = desktop.open("media")
    switch = page.locator(".switch-row", has_text="Videos allowed")
    expect(switch.locator("input")).to_be_checked()
    switch.locator(".switch").click()
    expect(page.locator(".banner", has_text="Videos are turned off")).to_be_visible()
    switch.locator(".switch").click()
    expect(page.locator(".banner", has_text="Videos are turned off")).to_have_count(0)
    assert desktop.bot.ctx.settings.get("media.enabled") is True


def test_a_video_paused_for_a_chat_shows_on_home_and_videos_and_resumes(desktop):
    bot = desktop.bot
    media, settings = bot.ctx.media, bot.ctx.settings
    profile = settings.get("profile")
    settings.set("profile", {**(profile or {}), "age_months": 30})
    item = media.add_item("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Bus Song")
    try:
        bot.runtime.run(media.play(item["id"]))
        assert bot.runtime.run(media.hold("wake word", wait=0.0))  # "Hey LittleBot" while it played
        page = desktop.open("home")
        expect(page.get_by_text(re.compile(r"Paused at 0:00 for a chat\. It stops in [12] min"))).to_be_visible()
        page = desktop.open("media")
        held = page.locator(".now-playing").get_by_text(re.compile("Paused at 0:00 for a chat"))
        expect(held).to_be_visible()
        page.locator(".now-playing").get_by_role("button", name="Resume").click()
        expect(held).to_be_hidden()
        assert media.status()["state"] == "loading" and media.status()["held"] is None
    finally:
        bot.runtime.run(media.stop())
        media.remove_item(item["id"])
        settings.set("profile", profile)
    # the approved item's YouTube thumbnail is blocked on purpose (no external requests in these tests)
    desktop.errors[:] = [e for e in desktop.errors if e != "Failed to load resource: net::ERR_FAILED"]


def test_ai_page_offers_chatgpt_sign_in_and_live_voice(phone):
    page = phone.open("ai")
    expect(page.get_by_role("heading", name="ChatGPT account")).to_be_visible()
    expect(page.get_by_role("button", name="Sign in with ChatGPT")).to_be_visible()
    expect(page.get_by_role("heading", name="Live voice (GPT-Live)")).to_be_visible()
    expect(page.get_by_text("I am with my child and will stay for the whole conversation.")).to_be_visible()
    # Cloud off (the default): starting is refused with a plain explanation, and nothing is started.
    page.get_by_label("I am with my child and will stay for the whole conversation.").check()
    page.get_by_role("button", name="Start live voice").click()
    expect(page.get_by_text("Cloud communication is turned off in Privacy.").first).to_be_visible()
    assert phone.api("GET", "/chatgpt")["live"]["active"] is False
    if os.environ.get("LITTLEBOT_AI_SCREENSHOT"):  # for eyeballing the layout
        page.screenshot(path=os.environ["LITTLEBOT_AI_SCREENSHOT"], full_page=True)
    assert page.locator("#ai-provider option[value=chatgpt]").count() == 1


def test_ask_what_littlebot_sees_card_and_privacy_switch(phone):
    page = phone.open("ai")
    card = page.locator("section.card", has=page.get_by_role("heading", name="Ask what LittleBot sees"))
    card.scroll_into_view_if_needed()
    expect(card).to_be_visible()
    expect(card.get_by_label("Your question")).to_have_attribute("placeholder", "How many fingers am I holding up?")
    expect(card.get_by_label("Say the answer out loud")).not_to_be_checked()
    expect(card.get_by_text("Photos sent today: 0")).to_be_visible()
    expect(card.locator(".banner", has_text="Can’t look right now")).to_be_visible()  # off by default
    # Pressing the button anyway explains why, and no photo is taken.
    card.get_by_label("Your question").fill("How many fingers am I holding up?")
    card.get_by_role("button", name="Look and answer").click()
    expect(card.locator(".banner", has_text="No answer")).to_be_visible()
    expect(card.get_by_text("Photos sent today: 0")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")  # no sideways scroll
    page = phone.open("privacy")
    row = page.locator(".switch-row", has_text="Let the AI look through the camera when asked")
    expect(row).to_be_visible()
    expect(row.get_by_role("switch")).not_to_be_checked()  # off by default


def test_whos_who_on_the_profile_page_and_its_privacy_switch(phone):
    page = phone.open("profile")
    card = page.locator("section.card", has=page.get_by_role("heading", name="Who's who"))
    card.scroll_into_view_if_needed()
    expect(card.locator(".banner", has_text="Recognising family members is off")).to_be_visible()  # off by default
    expect(card.get_by_text("Nobody added yet.")).to_be_visible()
    expect(card.get_by_text("Never the photos.", exact=False)).to_be_visible()
    card.get_by_label("Name", exact=True).fill("Ali")
    card.get_by_label("Who they are").fill("dad")
    card.get_by_role("button", name="Add person").click()
    expect(card.get_by_text("Ali (dad)")).to_be_visible()
    expect(card.get_by_role("button", name="Take a sample")).to_be_visible()
    expect(card.locator(".banner", has_text="Recognising family voices is off")).to_be_visible()  # off by default
    expect(card.get_by_text("Voice: not set up.")).to_be_visible()
    expect(card.get_by_role("button", name="Record Ali's voice")).to_be_visible()
    expect(card.get_by_role("button", name="Forget Ali's voice")).to_be_hidden()  # nothing to forget
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")  # no sideways scroll
    (ali,) = phone.api("GET", "/people")["people"]
    assert ali["name"] == "Ali" and ali["samples"] == 0 and ali["voice_samples"] == 0
    phone.api("DELETE", f"/people/{ali['id']}")  # leave the shared robot as it was
    page = phone.open("privacy")
    row = page.locator(".switch-row", has_text="Recognise family members")
    expect(row).to_be_visible()
    expect(row.get_by_role("switch")).not_to_be_checked()
    row = page.locator(".switch-row", has_text="Recognise family voices")
    expect(row).to_be_visible()
    expect(row.get_by_role("switch")).not_to_be_checked()  # off by default


def test_settings_show_where_the_weather_is_for_and_let_the_parent_change_it(phone):
    page = phone.open("settings")
    card = page.locator("section.card", has=page.get_by_role("heading", name="Weather & time"))
    card.scroll_into_view_if_needed()
    # Cloud off (the default): nothing is looked up, and the card says why there's no weather.
    expect(card.get_by_text("Weather location: not known yet (from your internet connection, approximate)")).to_be_visible()
    expect(card.locator(".banner", has_text="Cloud communication is off in Privacy.")).to_be_visible()
    expect(card.get_by_text("Automatic (°")).to_be_visible()
    card.get_by_role("button", name="Change").click()
    card.get_by_label("Place").fill("43.65, -79.38")  # coordinates need no place search
    card.get_by_label("Units").select_option("celsius")
    card.get_by_role("button", name="Save").click()
    expect(card.get_by_text("Weather location: 43.65, -79.38 (set on the dashboard)")).to_be_visible()
    expect(card.get_by_role("button", name="Change")).to_be_visible()
    assert phone.bot.ctx.settings.get("info.location")["latitude"] == 43.65
    assert phone.bot.ctx.settings.get("info.units") == "celsius"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")  # no sideways scroll
    # A name needs the place search, so with cloud off the parent is told what to do.
    card.get_by_role("button", name="Change").click()
    card.get_by_label("Place").fill("Toronto")
    card.get_by_role("button", name="Save").click()
    expect(page.get_by_text("Turn on cloud communication in Privacy first").first).to_be_visible()
    card.get_by_role("button", name="Use my internet connection").click()
    expect(card.get_by_text("Weather location: not known yet (from your internet connection, approximate)")).to_be_visible()
    card.get_by_role("button", name="Change").click()
    card.get_by_label("Units").select_option("auto")
    card.get_by_role("button", name="Use my internet connection").click()
    expect(card.get_by_role("button", name="Change")).to_be_visible()
    assert phone.bot.ctx.settings.get("info.location") == {} and phone.bot.ctx.settings.get("info.units") == "auto"


def _wait(pred: Any, timeout: float = 5.0) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_camera_live_view_on_a_phone(phone):
    page = phone.open("more")
    page.get_by_role("link", name="Camera Live view from LittleBot").click()  # on a phone: More -> Camera
    expect(page.get_by_role("heading", name="Live view")).to_be_visible()
    stage = page.locator(".cam-stage")
    expect(stage.get_by_text("Press “Start live view” to see what LittleBot sees.")).to_be_visible()
    vision = phone.bot.ctx.vision
    assert vision.status()["live_view"]["viewers"] == 0  # nothing until the parent presses Start
    page.get_by_role("button", name="Start live view").click()
    expect(page.locator(".cam-live")).to_be_visible()
    expect(page.locator(".cam-fps")).to_have_text(re.compile(r"^\d+\.\d fps$"))
    assert vision.status()["live_view"]["viewers"] == 1
    painted = page.evaluate("""() => {
        const c = document.querySelector('.cam-canvas');
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
        let sum = 0; for (let i = 0; i < d.length; i += 397) sum += d[i];
        return [c.width, c.height, sum]; }""")
    assert 0 < painted[0] <= 480 and painted[0] * 3 == painted[1] * 4 and painted[2] > 0  # the mock camera's picture
    expect(page.get_by_text(re.compile(r"^1 device \(this one\)$"))).to_be_visible()
    with page.expect_download() as download:
        page.get_by_role("button", name="Take snapshot").click()
    assert re.fullmatch(r"littlebot-\d{8}-\d{6}\.jpg", download.value.suggested_filename)
    page.get_by_role("button", name="Stop live view").click()
    expect(page.get_by_role("button", name="Start live view")).to_be_visible()
    assert _wait(lambda: vision.status()["live_view"]["viewers"] == 0)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")  # no sideways scroll


def test_camera_view_stops_when_the_parent_leaves_the_page(phone):
    page = phone.open("camera")
    page.get_by_role("button", name="Start live view").click()
    expect(page.locator(".cam-live")).to_be_visible()
    vision = phone.bot.ctx.vision
    assert vision.status()["live_view"]["viewers"] == 1
    page.goto(f"{phone.base}/#/home")
    expect(page.get_by_role("heading", name="Devices & privacy")).to_be_visible()
    assert _wait(lambda: vision.status()["live_view"]["viewers"] == 0, timeout=2.0)  # told at once, not after the idle time


def test_camera_view_explains_privacy_off_and_video_pause(desktop):
    from littlebot.core.state import RobotState

    page = desktop.open("camera")
    stage = page.locator(".cam-stage")
    bot = desktop.bot
    try:
        page.get_by_role("button", name="Start live view").click()
        expect(page.locator(".cam-live")).to_be_visible()
        assert bot.ctx.state.transition(RobotState.PLAYING_MEDIA, "test video")
        expect(stage.get_by_text("Paused while a video plays on LittleBot")).to_be_visible(timeout=5000)
        bot.ctx.state.release(RobotState.PLAYING_MEDIA)
        expect(page.locator(".cam-live")).to_be_visible(timeout=8000)  # comes back by itself
        desktop.api("PUT", "/privacy", {"camera_enabled": False})
        expect(stage.get_by_text("Camera is off (Privacy)")).to_be_visible(timeout=5000)
        expect(page.locator(".cam-live")).to_be_hidden()
        expect(page.get_by_role("button", name="Start live view")).to_be_disabled()
        assert bot.ctx.vision.status()["live_view"]["viewers"] == 0
    finally:
        desktop.api("PUT", "/privacy", {"camera_enabled": True})
        if bot.ctx.state.state == RobotState.PLAYING_MEDIA:
            bot.ctx.state.release(RobotState.PLAYING_MEDIA)
    expect(page.get_by_role("button", name="Start live view")).to_be_enabled(timeout=5000)
def test_activity_voices_card_records_in_the_chatgpt_voice_with_the_parents_ok_for_the_name(phone, tmp_path):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent / "chatgpt"))
    from chatgpt_helpers import signed_in_auth

    ctx = phone.bot.ctx
    page = phone.open("activities")
    phone.api("PUT", "/profile", {"nickname": "Amara", "age_months": 10})
    ctx.settings.set("ai.share_child_name", False)  # on by default
    page.reload()
    card = page.get_by_role("region", name="Activity voices")
    expect(card).to_contain_text("lines LittleBot says in activities, games and chats are recorded in its ChatGPT voice")
    expect(card.locator(".banner")).to_contain_text("Turn on cloud communication in Privacy first")  # not yet
    signed_out = ctx.extras["chatgpt_auth"]
    ctx.extras["chatgpt_auth"] = signed_in_auth(tmp_path)  # (a stand-in sign-in: nothing is recorded here)
    phone.api("PUT", "/privacy", {"cloud_enabled": True})
    try:
        page.reload()
        expect(card.get_by_role("button", name="Record activity voices in the ChatGPT voice")).to_be_visible()
        box = card.get_by_role("checkbox", name=re.compile("Also record the .* lines with Amara's name"))
        expect(box).not_to_be_checked()  # the name is only sent to ChatGPT with the parent's OK
        count = card.get_by_text(re.compile(r"^\d+ lines to record"))
        without = count.inner_text()
        box.check()
        expect(count).not_to_have_text(without)
        expect(card).to_contain_text("The activities for Amara's age come first")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")  # no sideways scroll
    finally:
        phone.api("PUT", "/privacy", {"cloud_enabled": False})
        ctx.extras["chatgpt_auth"] = signed_out
        phone.api("PUT", "/profile", {"nickname": "", "age_months": None, "birth_month": ""})


def test_home_battery_card_on_mains_power_says_not_fitted(desktop):
    page = desktop.open("home")
    card = page.locator("section.card", has=page.get_by_role("heading", name="Battery"))
    expect(card).to_contain_text("Not fitted: LittleBot runs on mains power.")
    expect(card.get_by_role("button", name="Show on LittleBot")).to_be_hidden()
    assert "null" not in card.inner_text()


def test_home_battery_card_with_a_battery_fitted(phone):
    """The robot here has no battery monitor, so the status is rewritten to look like a fitted, low pack."""
    import json

    fitted = {"fitted": True, "connected": True, "status": "running", "detail": "ok", "kind": "serial",
              "percent": 18, "voltage": 7.43, "cells": [3.72, 3.71], "temperature_c": 31.5, "charging": False,
              "charged": False, "plugged_in": False, "time_left_minutes": 95, "time_to_full_minutes": None,
              "health": ["low"], "settings": {"low_percent": 20, "critical_percent": 7, "announce_low": True,
                                              "shutdown_when_empty": True, "poweroff_allowed": False}}

    def with_battery(route):
        resp = route.fetch()
        st = resp.json()
        st["power"] = fitted
        route.fulfill(response=resp, body=json.dumps(st))

    page = phone.open("home")
    page.route("**/api/status", with_battery)
    page.reload()
    card = page.locator("section.card", has=page.get_by_role("heading", name="Battery"))
    expect(card.locator(".battery-big")).to_have_text("18%")
    expect(card).to_contain_text("On battery")
    expect(card).to_contain_text("7.43 V (3.72 + 3.71)")
    expect(card).to_contain_text("31.5 °C")
    expect(card).to_contain_text("left")
    expect(card.locator(".banner", has_text="Battery low")).to_be_visible()
    expect(card.get_by_role("button", name="Show on LittleBot")).to_be_visible()
    card.locator("summary", has_text="Battery settings").click()
    expect(card.get_by_label("Low battery warning at")).to_have_value("20")
    assert "null" not in card.inner_text() and "undefined" not in card.inner_text()


def test_ai_page_fast_decisions_panel_saves_the_provider_and_modes(phone):
    page = phone.open("ai")
    panel = page.locator("section.card", has_text="Fast decisions")
    panel.scroll_into_view_if_needed()
    expect(panel).to_be_visible()
    expect(panel.locator("text=No TypeSafe key")).to_be_visible()
    page.select_option("#decide-provider", "jev")
    expect(panel.locator("text=TYPESAFE_API_KEY isn't set")).to_be_visible()  # the key belongs in .env
    page.select_option("#decide-cards", "on")
    expect(page.locator("#decide-cards")).to_have_value("on")
    d = phone.api("GET", "/decide")
    assert d["provider"] == "jev" and d["features"]["cards"]["chosen"] == "on"
    switch = panel.get_by_role("switch", name="Log what each decision was about")
    expect(switch).to_be_checked()
    switch.click(force=True)
    expect(switch).not_to_be_checked()
    assert phone.api("GET", "/decide")["log_context"] is False
    phone.api("PUT", "/decide/settings", {"provider": "off", "modes": {"cards": "shadow"}, "log_context": True})
