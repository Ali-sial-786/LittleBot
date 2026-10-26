"""The browser view (littlebot/browser/service.py) against a fake Chromium launcher and a fake DevTools peer
(a real localhost WebSocket): what it starts, the DevTools calls it makes, who may open it, and when it closes."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import pytest
from browser_testkit import (  # type: ignore[import-not-found]
    CONTEXT_ID,
    PAGE_SESSION,
    PAGE_TARGET,
)

from littlebot.browser import page_scripts as scripts
from littlebot.browser import service as service_mod
from littlebot.browser.policy import google_url
from littlebot.browser.service import (
    CLOSED,
    KEEP_AWAKE,
    NAVIGATED,
    OPENED,
    PRIORITY,
    BrowserView,
    trim_text,
)
from littlebot.core.arbiter import Priority, Resource
from littlebot.core.events import Topics
from littlebot.core.state import RobotState

QUERY = "how tall are giraffes"


def _navigations(env: Any) -> list[str]:
    return [p["url"] for p in env.devtools.params("Page.navigate")]


# ---------------------------------------------------------------- opening
async def test_open_searches_google_with_safe_search(env: Any) -> None:
    out = await env.open(QUERY)
    assert out["ok"] is True and out["already"] is False
    assert out["showing"] == f"Google results for “{QUERY}”"
    assert _navigations(env) == [google_url(QUERY)]
    assert google_url(QUERY) == "https://www.google.com/search?q=how+tall+are+giraffes&safe=active"
    assert env.browser.active
    opened = env.last(OPENED)
    assert opened is not None and opened.data == {"source": "voice", "host": "www.google.com", "search": True}


async def test_no_query_opens_googles_start_page(env: Any) -> None:
    out = await env.open(None)
    assert out["ok"] and _navigations(env) == ["https://www.google.com/?safe=active"]
    assert env.browser.describe() == "Google"


async def test_the_window_is_chromeless_with_localhost_only_devtools(env: Any) -> None:
    await env.open()
    argv = env.launcher.argv
    assert argv[0] == "/usr/bin/chromium"
    assert "--kiosk" in argv and any(a.startswith("--app=data:text/html") for a in argv)
    assert "--remote-debugging-address=127.0.0.1" in argv and "--remote-debugging-port=0" in argv
    assert "--incognito" in argv and "--mute-audio" in argv and "--deny-permission-prompts" in argv
    data_dirs = [a.split("=", 1)[1] for a in argv if a.startswith("--user-data-dir=")]
    assert len(data_dirs) == 1 and data_dirs[0].startswith(str(env.tmp / "littlebot-browser-"))
    assert env.devtools.paths == ["/devtools/browser/fake-id"]  # the address from DevToolsActivePort
    assert env.profiles() and str(env.profiles()[0]) == data_dirs[0]


async def test_no_kiosk_gives_an_app_window(make_env: Any) -> None:
    env = await make_env(browser_kw={"kiosk": False})
    await env.open()
    assert "--kiosk" not in env.launcher.argv
    assert any(a.startswith("--app=") for a in env.launcher.argv)


async def test_setup_locks_the_page_down_before_it_loads(env: Any) -> None:
    await env.open()
    dt = env.devtools
    calls = dt.methods()
    attach = dt.last("Target.attachToTarget")
    assert attach == {"targetId": PAGE_TARGET, "flatten": True}
    # new windows are watched only once our own page is known
    assert calls.index("Target.setDiscoverTargets") > calls.index("Target.attachToTarget")
    downloads = dt.params("Browser.setDownloadBehavior")
    assert {"behavior": "deny"} in downloads and {"behavior": "deny", "browserContextId": CONTEXT_ID} in downloads
    denied = {p["permission"]["name"] for p in dt.params("Browser.setPermission") if p["setting"] == "denied"}
    assert {"notifications", "videoCapture", "audioCapture", "geolocation"} <= denied
    page_calls = dt.methods(PAGE_SESSION)
    assert dt.last("Runtime.addBinding") == {"name": scripts.BINDING, "executionContextName": scripts.WORLD}
    injected = dt.params("Page.addScriptToEvaluateOnNewDocument")
    assert [p.get("worldName") for p in injected] == [scripts.WORLD, None]
    assert "littlebot-strip" in injected[0]["source"] and "(window, 'open'" in injected[1]["source"]
    assert dt.last("Fetch.enable") == {"patterns": service_mod.FETCH_PATTERNS}
    assert dt.last("Page.setInterceptFileChooserDialog") == {"enabled": True}
    assert dt.last("Emulation.setDeviceMetricsOverride") == {"width": 0, "height": 0, "deviceScaleFactor": 1.4,
                                                             "mobile": False}
    assert dt.last("Emulation.setUserAgentOverride") is None  # it says what it is (browser.mobile_layout off)
    # everything for the page is in place before the first real page is asked for
    assert page_calls.index("Fetch.enable") < page_calls.index("Page.navigate")
    assert page_calls.index("Page.addScriptToEvaluateOnNewDocument") < page_calls.index("Page.navigate")


async def test_it_takes_the_screen_like_a_video_then_holds_it_lower(env: Any) -> None:
    card = env.ctx.arbiter.acquire("screens", [Resource.SCREEN], Priority.CONVERSATION)
    assert card is not None
    await env.open()
    holder = env.ctx.arbiter.holder(Resource.SCREEN)
    assert holder.owner == "browser" and holder.priority == PRIORITY == 45
    assert not card.active  # the card went


async def test_a_more_important_screen_holder_means_busy(env: Any) -> None:
    env.ctx.arbiter.acquire("parent", [Resource.SCREEN], Priority.PARENT)
    out = await env.open()
    assert out == {**out, "ok": False, "error": "busy"}
    assert env.launcher.argvs == [] and not env.browser.active


async def test_logs_never_hold_the_query_or_the_page(env: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="littlebot.browser")
    await env.open("purple dinosaur facts")
    await env.devtools.emit("Page.frameNavigated", {"frame": {"id": PAGE_TARGET, "url": "https://example.com/dino?secret=1"}})
    await env.until(lambda: env.last(NAVIGATED) is not None)
    env.devtools.eval_value = lambda expr: {"text": "Dinosaurs were purple", "title": "Dino page"}
    await env.browser.read()
    env.browser.close("test")
    await env.settle()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "purple" not in text and "secret" not in text and "Dino" not in text
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO and r.name == "littlebot.browser"]
    assert any(m.startswith("browser: opened (from voice; a search)") for m in infos)
    assert "browser: page opened (example.com)" in infos
    assert any(m.startswith("browser: closed (test)") for m in infos)


# ---------------------------------------------------------------- refusals
async def test_a_blocked_query_is_refused_without_starting(env: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="littlebot.browser")
    out = await env.open("naked women")
    assert out["ok"] is False and out["error"] == "blocked"
    assert out["detail"] == service_mod.ERRORS["blocked"]
    assert env.launcher.argvs == [] and not env.browser.active
    assert "naked" not in caplog.text


async def test_cloud_off_means_no_browser(make_env: Any) -> None:
    env = await make_env(cloud=False)
    out = await env.open()
    assert out["error"] == "cloud_off" and env.launcher.argvs == []
    assert env.browser.blocked_reason() == "cloud_off"


async def test_the_parents_switch_off_means_no_browser(env: Any) -> None:
    env.ctx.settings.set("browser.enabled", False)
    out = await env.open()
    assert out["error"] == "disabled" and env.launcher.argvs == []
    assert env.browser.offered() is False


async def test_the_child_speaking_is_refused_for_the_voice_and_tools(env: Any) -> None:
    env.ctx.bus.publish(service_mod.VOICE_SPEAKER, {"is_child": True, "name": "Mia"})
    assert (await env.open(source="voice"))["error"] == "child"
    assert (await env.open(source="tool"))["error"] == "child"
    assert env.launcher.argvs == []
    env.clock.advance(service_mod.CHILD_RECENT_S + 1)  # a while ago: a grown-up may ask now
    assert (await env.open(source="voice"))["ok"]


async def test_a_grown_up_speaking_clears_the_child(env: Any) -> None:
    env.ctx.bus.publish(service_mod.VOICE_SPEAKER, {"is_child": True})
    env.ctx.bus.publish(service_mod.VOICE_SPEAKER, {"is_child": False})
    assert (await env.open())["ok"]


async def test_no_chromium_or_headless_is_unavailable(make_env: Any) -> None:
    env = await make_env(browser_kw={"browser_path": None})
    assert env.browser.unavailable_reason() == "no screen (headless run)"
    assert (await env.open())["error"] == "unavailable"
    assert env.browser.offered() is False


def test_the_default_test_context_has_no_browser(ctx: Any) -> None:
    view = BrowserView(ctx)  # headless test config: nothing is ever launched
    assert view.unavailable_reason() is not None and not view.offered()


async def test_asleep_means_no_browser(env: Any) -> None:
    env.ctx.state.transition(RobotState.SLEEPING, "test", force=True)
    assert (await env.open())["error"] == "asleep"


async def test_bad_urls_are_refused(env: Any) -> None:
    for url in ("http://example.com", "file:///etc/passwd", "chrome://settings", "data:text/html,hi",
                "https://localhost/x", "https://192.168.1.1/", "https://printer.local/"):
        out = await env.browser.open(url=url)
        assert out["error"] == "bad_url", url
    assert env.launcher.argvs == []


async def test_a_url_is_opened_and_a_google_url_gets_safe_search(env: Any) -> None:
    await env.browser.open(url="https://www.google.com/search?q=cats&safe=off")
    assert _navigations(env) == ["https://www.google.com/search?q=cats&safe=active"]


async def test_chromium_not_starting_cleans_up(make_env: Any) -> None:
    env = await make_env(launcher_kw={"fail": OSError("no such file")})
    out = await env.open()
    await env.settle()
    assert out["error"] == "failed" and not env.browser.active
    assert env.profiles() == [] and env.ctx.arbiter.holder(Resource.SCREEN) is None


async def test_devtools_never_answering_times_out_and_cleans_up(make_env: Any) -> None:
    env = await make_env(launcher_kw={"write_port": False}, browser={"start_timeout_seconds": 2})
    task = asyncio.create_task(env.open())
    await env.until(lambda: bool(env.launcher.procs))
    for _ in range(30):
        env.clock.advance(1)
        await asyncio.sleep(0.02)
        if task.done():
            break
    out = await task
    await env.settle()
    assert out["error"] == "failed" and env.launcher.proc.terminated and env.profiles() == []


async def test_closed_while_starting_stops_chromium_again(make_env: Any) -> None:
    env = await make_env(launcher_kw={"write_port": False})
    task = asyncio.create_task(env.open())
    await env.until(lambda: bool(env.launcher.procs))
    env.ctx.bus.publish(Topics.STOP_ALL, {"source": "parent"})
    out = await task
    await env.settle()
    assert out["ok"] is False and env.launcher.proc.terminated and env.profiles() == []


SORRY = "https://www.google.com/sorry/index?continue=https://www.google.com/search%3Fq%3Dgiraffes&q=EgQ"


def _ran(env: Any, script: str) -> list[str]:
    return [p["expression"] for p in env.devtools.params("Runtime.evaluate") if p["expression"].startswith(script)]


async def test_the_phone_layout_is_asked_for_only_when_switched_on(tmp_path: Any) -> None:
    from browser_testkit import build_env, close_env  # type: ignore[import-not-found]

    env = await build_env(tmp_path, browser={"mobile_layout": True})
    try:
        await env.open(QUERY)
        ua = env.devtools.last("Emulation.setUserAgentOverride")
        assert "Mobile" in ua["userAgent"] and "Chrome/141.0.0.0" in ua["userAgent"]
        assert ua["userAgentMetadata"]["mobile"] is True
    finally:
        await close_env(env)


async def test_googles_captcha_is_shown_big_for_a_grownup(env: Any, caplog: pytest.LogCaptureFixture) -> None:
    """From a real call: "it showed Google, which brought up a captcha", a small box on the 3.5" screen."""
    caplog.set_level(logging.INFO, logger="littlebot.browser")
    found = {"kind": "box", "w": 304, "h": 78, "iw": 514, "ih": 343}  # in CSS pixels, before the zoom

    def page(expr: str) -> Any:  # what the page reports: the frame as big as the zoom makes it
        if not expr.startswith(scripts.CAPTCHA_SCRIPT):
            return True
        z = env.browser._session.captcha_zoom
        return {**found, "w": found["w"] * z, "h": found["h"] * z}

    env.devtools.eval_value = page
    await env.open(QUERY)
    await env.devtools.emit("Page.frameNavigated", {"frame": {"id": PAGE_TARGET, "url": SORRY}})
    await env.until(lambda: bool(_ran(env, scripts.CAPTCHA_ZOOM_SCRIPT)))
    assert _ran(env, scripts.CAPTCHA_ZOOM_SCRIPT) == [f"{scripts.CAPTCHA_ZOOM_SCRIPT}(1.59)"]  # 304 px -> the width
    note = env.last(service_mod.SCREEN_NEWS)
    assert note is not None and note.data["text"] == service_mod.CAPTCHA_NOTE and "I'm not a robot" in note.data["text"]
    assert env.browser.describe() == "Google's “I'm not a robot” check"
    assert "waiting for a grown-up" in caplog.text and "giraffes" not in caplog.text
    await env.until(lambda: any(e.endswith("(true)") for e in _ran(env, scripts.CAPTCHA_SCRIPT)))  # centred
    # the picture challenge opens: it is fitted instead (smaller, so all of it shows)
    found.update(kind="challenge", w=400, h=580)
    await env.until(lambda: len(_ran(env, scripts.CAPTCHA_ZOOM_SCRIPT)) == 2)
    assert _ran(env, scripts.CAPTCHA_ZOOM_SCRIPT)[-1] == f"{scripts.CAPTCHA_ZOOM_SCRIPT}(0.6)"
    # solved: Google goes on to the results, and nothing falls back to Bing
    await env.devtools.emit("Page.frameNavigated", {"frame": {"id": PAGE_TARGET, "url": google_url(QUERY)}})
    await env.until(lambda: not env.browser._session.captcha)
    assert env.browser.describe() == f"Google results for “{QUERY}”"
    env.clock.advance(service_mod.CAPTCHA_TAP_S + 1)
    await asyncio.sleep(service_mod.CAPTCHA_POLL_S * 2)
    assert "bing" not in _navigations(env)[-1] and "the captcha was solved" in caplog.text


async def test_nobody_ticks_the_box_so_bing_shows_the_results(env: Any) -> None:
    from littlebot.browser.policy import bing_url

    env.devtools.eval_value = lambda expr: ({"kind": "box", "w": 304, "h": 78, "iw": 514, "ih": 343}
                                            if expr.startswith(scripts.CAPTCHA_SCRIPT) else True)
    await env.open(QUERY)
    await env.devtools.emit("Page.frameNavigated", {"frame": {"id": PAGE_TARGET, "url": SORRY}})
    await env.until(lambda: env.browser._session.captcha)
    env.clock.advance(service_mod.CAPTCHA_TAP_S - 5)
    await asyncio.sleep(service_mod.CAPTCHA_POLL_S * 2)
    assert _navigations(env)[-1] == google_url(QUERY)  # still waiting
    env.clock.advance(6)
    await env.until(lambda: _navigations(env)[-1] == bing_url(QUERY), timeout=3)
    assert bing_url(QUERY) == "https://www.bing.com/search?q=how+tall+are+giraffes&adlt=strict"
    assert env.last(service_mod.SCREEN_NEWS).data["text"] == service_mod.CAPTCHA_GAVE_UP_NOTE
    await env.devtools.emit("Page.frameNavigated", {"frame": {"id": PAGE_TARGET, "url": bing_url(QUERY)}})
    await env.until(lambda: env.browser.describe() == f"Bing results for “{QUERY}”")
    env.clock.advance(service_mod.DEDUPE_S + 1)
    await env.open("why is the sky blue")  # Google rests a while: straight to Bing
    assert _navigations(env)[-1] == bing_url("why is the sky blue")
    env.clock.advance(service_mod.GOOGLE_RESTS_S)
    await env.open("cats")
    assert _navigations(env)[-1] == google_url("cats")


async def test_a_child_talking_never_gets_googles_captcha(env: Any) -> None:
    from littlebot.browser.policy import bing_url

    await env.open(QUERY)
    env.ctx.bus.publish("voice.speaker", {"is_child": True})
    await env.until(lambda: env.browser._child_spoke())
    await env.devtools.emit("Page.frameNavigated", {"frame": {"id": PAGE_TARGET, "url": SORRY}})
    await env.until(lambda: _navigations(env)[-1] == bing_url(QUERY))
    assert not env.browser._session.captcha and env.last(service_mod.SCREEN_NEWS) is None


@pytest.mark.parametrize(("found", "zoom", "want"), [
    ({"kind": "box", "w": 304, "h": 78, "iw": 514, "ih": 343}, 1.0, 1.59),  # the box fills the width
    ({"kind": "box", "w": 483, "h": 124, "iw": 514, "ih": 343}, 1.59, None),  # near enough already
    ({"kind": "challenge", "w": 400, "h": 580, "iw": 720, "ih": 480}, 1.0, 0.78),  # all of it fits the height
    ({"kind": "box", "w": 30, "h": 10, "iw": 720, "ih": 480}, 1.0, service_mod.CAPTCHA_MAX_ZOOM),
    ({"kind": "box", "w": 0, "h": 78, "iw": 514, "ih": 343}, 1.0, None),
    ({"kind": "box"}, 1.0, None),
])
def test_how_big_the_captcha_is_made(found: dict[str, Any], zoom: float, want: float | None) -> None:
    assert service_mod.captcha_zoom(found, zoom) == want


async def test_the_saved_browser_is_kept_and_forgotten(env: Any) -> None:
    saved = env.tmp / "saved-browser"
    assert env.browser.status()["keep_signins"] is False and env.browser.status()["saved"] is False
    env.ctx.settings.set("browser.keep_signins", True)
    saved.mkdir()
    (saved / "DevToolsActivePort").write_text("1\n/devtools/browser/stale\n")  # left by an earlier run
    (saved / "Cookies").write_text("google")
    out = await env.open(QUERY)
    assert out["ok"], out
    argv = env.launcher.argv
    assert "--incognito" not in argv and f"--user-data-dir={saved}" in argv
    env.browser.close("test")
    await env.settle()
    assert (saved / "Cookies").read_text() == "google" and env.profiles() == []  # kept; no throwaway one made
    assert env.browser.status()["saved"] is True
    await env.open(QUERY)
    assert await env.browser.forget() is True  # closes the browser that uses it, then deletes it
    assert not env.browser.active and not saved.exists() and await env.browser.forget() is False
    env.ctx.settings.set("browser.keep_signins", False)
    await env.open(QUERY)
    assert "--incognito" in env.launcher.argv and not saved.exists()


# ---------------------------------------------------------------- open again, dedupe# ---------------------------------------------------------------- open again, dedupe
async def test_the_director_and_a_tool_asking_together_open_once(env: Any) -> None:
    await env.open(QUERY, source="director")
    again = await env.open(QUERY, source="tool")
    assert again["ok"] and again["already"] is True
    assert len(env.launcher.argvs) == 1 and len(_navigations(env)) == 1


async def test_a_new_search_reuses_the_window(env: Any) -> None:
    await env.open(QUERY)
    out = await env.open("why is the sky blue")
    assert out["ok"] and not out["already"]
    assert len(env.launcher.argvs) == 1
    assert _navigations(env)[-1] == google_url("why is the sky blue")
    env.clock.advance(service_mod.DEDUPE_S + 1)
    assert (await env.open("why is the sky blue", source="tool"))["already"] is True  # it shows that already


# ---------------------------------------------------------------- control
def _evals(env: Any) -> list[str]:
    return [p["expression"] for p in env.devtools.params("Runtime.evaluate")]


async def test_scroll_runs_in_littlebots_own_world(env: Any) -> None:
    await env.open()
    env.devtools.eval_value = lambda expr: {"top": 300, "max": 300}
    out = await env.browser.control("scroll", {"direction": "down", "amount": "page"})
    assert out["ok"] and out["at_bottom"] is True and out["at_top"] is False
    world = env.devtools.last("Page.createIsolatedWorld")
    assert world == {"frameId": PAGE_TARGET, "worldName": "littlebot-control"}
    ev = env.devtools.last("Runtime.evaluate")
    assert ev["contextId"] == 42 and ev["returnByValue"] is True
    assert ev["expression"].endswith('(0.85, "")')
    env.clock.advance(1)
    await env.browser.control("scroll", {"direction": "up", "amount": "half"})
    await env.browser.control("scroll", {"direction": "top"})
    await env.browser.control("scroll", {"direction": "down", "amount": 2})
    tails = [e.rsplit(")(", 1)[1] for e in _evals(env)]
    assert tails == ['0.85, "")', '-0.5, "")', '0.85, "top")', '1.7, "")']
    assert env.devtools.count("Page.createIsolatedWorld") == 1  # one world per page
    assert (await env.browser.control("scroll", {"direction": "sideways"}))["error"] == "bad_action"


async def test_open_result_goes_to_the_nth_result(env: Any) -> None:
    await env.open()
    results = [{"href": "https://a.example/1", "title": "First"}, {"href": "https://b.example/2", "title": "Two"},
               {"href": "http://plain.example/", "title": "Plain"}]
    env.devtools.eval_value = lambda expr: results
    out = await env.browser.control("open_result", {"n": 2})
    assert out["ok"] and out["opened"] == "Two" and out["n"] == 2
    assert _navigations(env)[-1] == "https://b.example/2"
    assert scripts.RESULTS_SCRIPT in _evals(env)
    assert (await env.browser.control("open_result", {"n": "last"}))["error"] == "bad_url"  # http: refused
    assert (await env.browser.control("open_result", {"n": 7}))["error"] == "no_result"
    assert (await env.browser.control("open_result", {"n": "first"}))["opened"] == "First"
    assert _navigations(env)[-1] == "https://a.example/1"


async def test_back_and_forward_use_the_history_skipping_the_loading_page(env: Any) -> None:
    await env.open()
    env.devtools.history = {"currentIndex": 2, "entries": [
        {"id": 1, "url": scripts.loading_url()}, {"id": 2, "url": "https://www.google.com/search?q=x&safe=active"},
        {"id": 3, "url": "https://a.example/"}]}
    out = await env.browser.control("back")
    assert out["moved"] is True and env.devtools.last("Page.navigateToHistoryEntry") == {"entryId": 2}
    env.devtools.history["currentIndex"] = 1
    env.clock.advance(service_mod.DEDUPE_S + 1)
    out = await env.browser.control("back")
    assert out["moved"] is False and env.devtools.count("Page.navigateToHistoryEntry") == 1
    out = await env.browser.control("forward")
    assert out["moved"] is True and env.devtools.last("Page.navigateToHistoryEntry") == {"entryId": 3}


async def test_reload_and_zoom(env: Any) -> None:
    await env.open()
    await env.browser.control("reload")
    assert env.devtools.last("Page.reload") == {"ignoreCache": False}
    assert (await env.browser.control("zoom", {"level": "in"}))["zoom"] == 1.55
    assert env.devtools.last("Emulation.setDeviceMetricsOverride")["deviceScaleFactor"] == 1.55
    assert (await env.browser.control("zoom", {"level": "150%"}))["zoom"] == 1.5
    assert (await env.browser.control("zoom", {"level": 9}))["zoom"] == 2.5
    assert (await env.browser.control("zoom", {"level": "reset"}))["zoom"] == 1.4
    assert (await env.browser.control("zoom", {"level": "huge"}))["error"] == "bad_action"
    assert (await env.browser.control("fly"))["error"] == "bad_action"


async def test_control_without_a_browser_is_not_open(env: Any) -> None:
    assert (await env.browser.control("scroll", {}))["error"] == "not_open"
    assert (await env.browser.read())["error"] == "not_open"


async def test_the_same_control_from_two_sources_runs_once(env: Any) -> None:
    await env.open()
    env.devtools.eval_value = lambda expr: {"top": 0, "max": 100}
    await env.browser.control("scroll", {"direction": "down"}, source="director")
    again = await env.browser.control("scroll", {"direction": "down"}, source="tool")
    assert again["already"] is True and env.devtools.count("Runtime.evaluate") == 1


async def test_a_page_script_error_is_a_failure_not_a_crash(env: Any) -> None:
    await env.open()
    env.devtools.answers["Runtime.evaluate"] = {"exceptionDetails": {"text": "boom"}}
    assert (await env.browser.control("scroll", {"direction": "down"}))["error"] == "failed"
    assert env.browser.active


async def test_a_fresh_world_after_the_page_changed(env: Any) -> None:
    await env.open()
    fails = [True]

    def evaluate(params: dict[str, Any]) -> dict[str, Any]:
        if fails and fails.pop():
            raise env.devtools.Refuse("Cannot find context with specified id")
        return {"result": {"value": {"top": 0, "max": 0}}}

    env.devtools.answers["Runtime.evaluate"] = evaluate
    assert (await env.browser.control("scroll", {"direction": "down"}))["ok"]
    assert env.devtools.count("Page.createIsolatedWorld") == 2


# ---------------------------------------------------------------- read
async def test_read_returns_the_trimmed_main_text(env: Any) -> None:
    await env.open()
    env.devtools.eval_value = lambda expr: {"title": "  Giraffe \n facts ", "host": "en.wikipedia.org",
                                            "text": "Giraffes  are\n\n tall.\x07 " + "word " * 2000}
    out = await env.browser.read()
    assert out["ok"] and out["title"] == "Giraffe facts" and out["host"] == "en.wikipedia.org"
    assert out["text"].startswith("Giraffes are tall. word word") and out["truncated"] is True
    assert len(out["text"]) <= env.ctx.config.browser.read_chars + 1 and out["text"].endswith("word…")
    assert _evals(env) == [scripts.READ_SCRIPT]
    again = await env.browser.control("read")  # the director and a tool asking together: read once
    assert again == out and env.devtools.count("Runtime.evaluate") == 1
    env.clock.advance(service_mod.READ_CACHE_S + 1)
    await env.browser.read()
    assert env.devtools.count("Runtime.evaluate") == 2


async def test_read_again_after_a_new_page(env: Any) -> None:
    await env.open()
    env.devtools.eval_value = lambda expr: {"text": "one"}
    assert (await env.browser.read())["text"] == "one"
    env.devtools.eval_value = lambda expr: {"text": "two"}
    await env.devtools.emit("Page.frameNavigated", {"frame": {"id": PAGE_TARGET, "url": "https://b.example/"}})
    await env.until(lambda: env.last(NAVIGATED) is not None)
    assert (await env.browser.read())["text"] == "two"


@pytest.mark.parametrize(("text", "limit", "expected"), [
    ("short  text", 100, ("short text", False)),
    ("one two three four five", 16, ("one two three…", True)),
    ("one two three four five", 12, ("one two thre…", True)),  # no word end near the limit: cut there
    ("abcdefghijklmnop", 10, ("abcdefghij…", True)),
    ("a\u200bb\x00c", 10, ("a b c", False)),
])
def test_trim_text(text: str, limit: int, expected: tuple[str, bool]) -> None:
    assert trim_text(text, limit) == expected


# ---------------------------------------------------------------- the strip's × and touches
async def test_the_strips_close_button_closes_the_view(env: Any) -> None:
    await env.open()
    dirs = env.profiles()
    await env.devtools.emit("Runtime.bindingCalled", {"name": scripts.BINDING, "payload": json.dumps({"type": "close"}),
                                                      "executionContextId": 9})
    await env.until(lambda: not env.browser.active)
    await env.settle()
    assert env.last(CLOSED).data == {"reason": "closed with the ×"}
    assert env.launcher.proc.terminated == 1
    assert dirs and env.profiles() == []  # the throwaway profile is gone
    assert env.ctx.arbiter.holder(Resource.SCREEN) is None
    assert env.browser.status()["last"]["reason"] == "closed with the ×"


async def test_another_binding_or_garbage_is_ignored(env: Any) -> None:
    await env.open()
    await env.devtools.emit("Runtime.bindingCalled", {"name": "somethingElse", "payload": '{"type": "close"}'})
    await env.devtools.emit("Runtime.bindingCalled", {"name": scripts.BINDING, "payload": "not json"})
    await env.settle()
    assert env.browser.active


async def test_a_touch_restarts_the_idle_wait_and_keeps_littlebot_awake(env: Any) -> None:
    await env.open()
    env.events.clear()
    env.clock.advance(100)
    await env.devtools.emit("Runtime.bindingCalled", {"name": scripts.BINDING, "payload": '{"type": "touch"}'})
    await env.until(lambda: env.last(KEEP_AWAKE) is not None)
    assert env.last(KEEP_AWAKE).data == {"kind": "browser"}
    assert env.browser.status()["closes_in"] == env.ctx.config.browser.idle_seconds


# ---------------------------------------------------------------- the idle timer
async def test_it_closes_when_nobody_used_it(env: Any) -> None:
    await env.open()
    env.clock.advance(env.ctx.config.browser.idle_seconds - 5)
    await asyncio.sleep(0.05)
    assert env.browser.active and env.browser.status()["closes_in"] == 5
    env.clock.advance(6)
    await env.until(lambda: not env.browser.active)
    assert env.last(CLOSED).data == {"reason": "nobody used it"}


async def test_the_idle_wait_starts_when_the_voice_chat_ends(env: Any) -> None:
    voice = env.ctx.arbiter.acquire("live_voice", [Resource.FACE, Resource.AUDIO_OUT, Resource.MICROPHONE],
                                    Priority.PARENT)
    await env.open()
    assert env.browser.status()["closes_in"] is None and env.browser.status()["voice_on"] is True
    env.clock.advance(1000)
    await asyncio.sleep(0.05)
    assert env.browser.active  # a long chat with the browser up: it stays
    voice.release()
    await asyncio.sleep(0.05)
    env.clock.advance(env.ctx.config.browser.idle_seconds - 1)
    await asyncio.sleep(0.05)
    assert env.browser.active
    env.clock.advance(2)
    await env.until(lambda: not env.browser.active)


async def test_it_closes_a_while_after_they_moved_on_in_the_chat(env: Any) -> None:
    env.ctx.arbiter.acquire("live_voice", [Resource.FACE, Resource.AUDIO_OUT, Resource.MICROPHONE], Priority.PARENT)
    await env.open()
    wait = env.ctx.config.browser.moved_on_seconds
    env.browser.moved_on()  # just opened: that turn is about it (the voice's tool may have come first)
    assert env.browser.status()["closes_in"] is None
    env.clock.advance(service_mod.MOVED_ON_GRACE_S)
    env.browser.moved_on()
    assert env.browser.status()["closes_in"] == wait
    env.clock.advance(wait - 5)
    env.browser.moved_on()  # another turn about something else doesn't start the wait again
    await asyncio.sleep(0.05)
    assert env.browser.active and env.browser.status()["closes_in"] == 5
    await env.devtools.emit("Runtime.bindingCalled", {"name": scripts.BINDING, "payload": '{"type": "touch"}'})
    await env.until(lambda: env.browser.status().get("closes_in") is None)  # used again: it stays
    env.clock.advance(service_mod.MOVED_ON_GRACE_S)
    env.browser.moved_on()
    env.clock.advance(wait + 1)
    await env.until(lambda: not env.browser.active)
    assert env.last(CLOSED).data == {"reason": "they moved on to something else"}


# ---------------------------------------------------------------- the rest of its lifetime
async def test_a_video_taking_the_screen_closes_the_browser(env: Any) -> None:
    await env.open()
    lease = env.ctx.arbiter.acquire("media", [Resource.SCREEN], Priority.MEDIA)
    assert lease is not None
    await env.until(lambda: not env.browser.active)
    await env.settle()
    assert env.last(CLOSED).data["reason"] == "media took the screen"
    assert env.launcher.proc.terminated and env.profiles() == []
    assert env.ctx.arbiter.holder(Resource.SCREEN).owner == "media"  # closing didn't drop the video's lease


async def test_a_game_takes_the_screen_but_a_card_does_not(env: Any) -> None:
    await env.open()
    assert env.ctx.arbiter.acquire("screens", [Resource.SCREEN], Priority.CONVERSATION) is None
    assert env.browser.active
    assert env.ctx.arbiter.acquire("games", [Resource.SCREEN], Priority.ACTIVITY) is not None
    await env.until(lambda: not env.browser.active)


@pytest.mark.parametrize(("topic", "data", "reason"), [
    (Topics.STOP_ALL, {"source": "parent"}, "stop all"),
    (Topics.MEDIA_STARTED, {"item_id": 1}, "a video started"),
    (Topics.ACTIVITY_STARTED, {"id": "x"}, "an activity started"),
    ("game.started", {"game": "bubbles"}, "a game started"),
    (Topics.SETTINGS_CHANGED, {"key": "browser.enabled", "value": False}, "switched off"),
])
async def test_closing_events(env: Any, topic: str, data: dict[str, Any], reason: str) -> None:
    await env.open()
    env.ctx.bus.publish(topic, data)
    assert not env.browser.active
    await env.settle()
    assert env.last(CLOSED).data == {"reason": reason} and env.profiles() == []


async def test_other_settings_leave_it_open(env: Any) -> None:
    await env.open()
    env.ctx.bus.publish(Topics.SETTINGS_CHANGED, {"key": "browser.enabled", "value": True})
    env.ctx.bus.publish(Topics.SETTINGS_CHANGED, {"key": "volume", "value": 0})
    assert env.browser.active


async def test_cloud_off_closes_it(env: Any) -> None:
    await env.open()
    env.ctx.privacy.set(cloud_enabled=False)
    assert not env.browser.active
    assert env.last(CLOSED).data == {"reason": "cloud communication switched off"}


async def test_sleep_closes_it(env: Any) -> None:
    await env.open()
    env.ctx.state.transition(RobotState.SLEEPING, "bedtime", force=True)
    assert not env.browser.active and env.last(CLOSED).data == {"reason": "LittleBot went to sleep"}


async def test_chromium_exiting_closes_it(env: Any) -> None:
    await env.open()
    env.launcher.proc.exit(1)
    await env.until(lambda: not env.browser.active)
    assert env.last(CLOSED).data == {"reason": "the browser window closed"}


async def test_losing_devtools_closes_it(env: Any) -> None:
    await env.open()
    await env.devtools.drop()
    await env.until(lambda: not env.browser.active)
    assert env.last(CLOSED).data == {"reason": "lost the connection to the browser"}


async def test_its_window_going_away_closes_it(env: Any) -> None:
    await env.open()
    await env.devtools.emit("Target.targetDestroyed", {"targetId": PAGE_TARGET}, session=None)
    await env.until(lambda: not env.browser.active)


async def test_close_by_voice_and_aclose(env: Any) -> None:
    await env.open()
    assert (await env.browser.control("close", source="tool")) == {"ok": True, "closed": True}
    assert env.last(CLOSED).data == {"reason": "asked in the voice chat"}
    await env.open()
    assert await env.browser.aclose("a video started") is True
    assert env.launcher.procs[-1].terminated and env.profiles() == []
    assert await env.browser.aclose("again") is False


async def test_close_from_another_thread(env: Any) -> None:
    await env.open()
    assert await asyncio.to_thread(env.browser.close, "parent") is True
    await env.settle()
    assert env.launcher.proc.terminated and env.profiles() == []


async def test_old_profiles_are_swept_on_start(make_env: Any, tmp_path: Any) -> None:
    stale = tmp_path / "littlebot-browser-old"
    stale.mkdir()
    (stale / "Cookies").write_text("x")
    await make_env()
    assert not stale.exists()


# ---------------------------------------------------------------- one Chromium at a time
class FakeMedia:
    def __init__(self, status: dict[str, Any]) -> None:
        self._status = status
        self.stopped: list[str] = []

    def status(self) -> dict[str, Any]:
        return self._status

    async def stop(self, reason: str = "stopped") -> None:
        self.stopped.append(reason)


async def test_a_playing_video_stops_before_the_browser_opens(env: Any) -> None:
    env.ctx.media = FakeMedia({"state": "playing", "held": False, "player": {"running": True}})
    await env.open()
    assert env.ctx.media.stopped == ["stopped"] and env.browser.active


async def test_a_video_paused_for_the_chat_is_kept(env: Any) -> None:
    env.ctx.media = FakeMedia({"state": "paused", "held": True, "player": {"running": False}})
    await env.open()
    assert env.ctx.media.stopped == [] and env.browser.active


async def test_nothing_playing_is_left_alone(env: Any) -> None:
    env.ctx.media = FakeMedia({"state": "idle", "held": False, "player": {"running": False}})
    await env.open()
    assert env.ctx.media.stopped == []


# ---------------------------------------------------------------- every page is checked
async def _paused(env: Any, url: str, *, kind: str = "Document", frame: str = PAGE_TARGET) -> tuple[str, dict]:
    before = len(env.devtools.calls)
    await env.devtools.emit("Fetch.requestPaused", {"requestId": "R1", "request": {"url": url}, "frameId": frame,
                                                    "resourceType": kind})
    await env.until(lambda: any(m.startswith("Fetch.") and m != "Fetch.enable"
                                for m, _p, _s in env.devtools.calls[before:]))
    method, params, _s = next(c for c in env.devtools.calls[before:] if c[0].startswith("Fetch."))
    return method, params


async def test_requests_are_let_through_rewritten_or_refused(env: Any) -> None:
    await env.open()
    assert await _paused(env, "https://en.wikipedia.org/wiki/Giraffe") == ("Fetch.continueRequest", {"requestId": "R1"})

    method, params = await _paused(env, "https://www.google.com/search?q=cats")
    assert method == "Fetch.fulfillRequest" and params["responseCode"] == 302
    assert params["responseHeaders"] == [{"name": "Location", "value": "https://www.google.com/search?q=cats&safe=active"}]

    method, params = await _paused(env, "https://www.google.com/search?q=cats&safe=off", kind="XHR")
    assert (method, params["url"]) == ("Fetch.continueRequest", "https://www.google.com/search?q=cats&safe=active")

    method, params = await _paused(env, "http://insecure.example/")
    assert method == "Fetch.fulfillRequest" and params["responseCode"] == 200
    page = base64.b64decode(params["body"]).decode("utf-8")
    assert "LittleBot" in page and "https" in page

    method, params = await _paused(env, "http://ads.example/frame", frame="SUBFRAME")
    assert (method, params["errorReason"]) == ("Fetch.failRequest", "BlockedByClient")


async def test_the_page_backstop(env: Any) -> None:
    await env.open()
    dt = env.devtools
    await dt.emit("Page.frameNavigated", {"frame": {"id": PAGE_TARGET, "url": "file:///etc/passwd"}})
    await env.until(lambda: dt.count("Page.setDocumentContent") == 1)
    assert dt.last("Page.setDocumentContent")["frameId"] == PAGE_TARGET
    await dt.emit("Page.frameNavigated", {"frame": {"id": PAGE_TARGET, "url": "https://www.google.com/search?q=x"}})
    await env.until(lambda: _navigations(env)[-1] == "https://www.google.com/search?q=x&safe=active")
    await dt.emit("Page.frameNavigated", {"frame": {"id": "CHILD", "parentId": PAGE_TARGET, "url": "file:///x"}})
    await dt.emit("Page.frameNavigated", {"frame": {"id": PAGE_TARGET, "url": "https://en.wikipedia.org/wiki/G"}})
    await env.until(lambda: env.last(NAVIGATED) is not None)
    assert env.last(NAVIGATED).data == {"host": "en.wikipedia.org"}
    assert dt.count("Page.setDocumentContent") == 1  # a sub-frame isn't the page
    await dt.emit("Target.targetInfoChanged", {"targetInfo": {
        "targetId": PAGE_TARGET, "title": "en.wikipedia.org/wiki/G", "url": "https://en.wikipedia.org/wiki/G"}},
        session=None)  # Chromium's stand-in title (the address) isn't a title
    await env.until(lambda: env.browser.describe() == "en.wikipedia.org")
    dt.answers["Target.getTargetInfo"] = {"targetInfo": {"targetId": PAGE_TARGET, "title": "Giraffe -  Wiki",
                                                         "url": "https://en.wikipedia.org/wiki/G"}}
    await dt.emit("Page.loadEventFired", {"timestamp": 1.0})
    await env.until(lambda: env.browser.describe() == "Giraffe - Wiki (en.wikipedia.org)")
    assert env.browser.status()["showing"] == "Giraffe - Wiki (en.wikipedia.org)"


async def test_new_windows_dialogs_and_downloads(env: Any) -> None:
    await env.open()
    dt = env.devtools
    await dt.emit("Target.targetCreated", {"targetInfo": {"targetId": "POPUP", "type": "page"}}, session=None)
    await dt.emit("Target.targetCreated", {"targetInfo": {"targetId": "W1", "type": "worker"}}, session=None)
    await dt.emit("Target.targetCreated", {"targetInfo": {"targetId": PAGE_TARGET, "type": "page"}}, session=None)
    await env.until(lambda: dt.count("Target.closeTarget") == 1)
    assert dt.last("Target.closeTarget") == {"targetId": "POPUP"}
    await dt.emit("Page.javascriptDialogOpening", {"type": "alert", "message": "hi"})
    await env.until(lambda: dt.count("Page.handleJavaScriptDialog") == 1)
    assert dt.last("Page.handleJavaScriptDialog") == {"accept": False}
    await dt.emit("Page.javascriptDialogOpening", {"type": "beforeunload"})
    await env.until(lambda: dt.count("Page.handleJavaScriptDialog") == 2)
    assert dt.last("Page.handleJavaScriptDialog") == {"accept": True}
    await dt.emit("Browser.downloadWillBegin", {"url": "https://x.example/f.exe"}, session=None)
    await dt.ping()
    await env.settle()
    assert env.browser.active and dt.count("Target.closeTarget") == 1


# ---------------------------------------------------------------- status
async def test_status(env: Any) -> None:
    st = env.browser.status()
    assert st["available"] and st["enabled"] and st["active"] is False and st["showing"] is None
    await env.open(QUERY, source="dashboard")
    env.clock.advance(3)
    st = env.browser.status()
    assert st["active"] and st["showing"] == f"Google results for “{QUERY}”" and st["source"] == "dashboard"
    assert st["seconds"] == 3 and st["zoom"] == 1.4 and st["closes_in"] == st["idle_seconds"] - 3
    env.browser.close("parent")
    st = env.browser.status()
    assert st["active"] is False and st["last"]["reason"] == "parent"
