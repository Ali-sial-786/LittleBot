"""End-to-end test of the player page's JavaScript state machine in headless Chromium.

The real page (player.html/js/css) is served by uvicorn from the real router and driven by the
real MediaController, but ``https://www.youtube.com/iframe_api`` is intercepted and replaced
by a STUB ``window.YT`` (all other external requests are blocked). So this verifies our page
logic and the bridge - NOT that YouTube itself plays (see docs/YOUTUBE_ON_PI.md).

Skipped when Playwright or a Chromium build is not available.
"""

from __future__ import annotations

import glob
import os
import re
import time
from typing import Any, Callable

import pytest

pytest.importorskip("playwright.sync_api")

from media_testkit import VID_A, VID_B, LIST_A, _build  # type: ignore[import-not-found]  # noqa: E402
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from littlebot.core.events import Topics  # noqa: E402
from littlebot.core.runtime import AsyncRuntime  # noqa: E402
from littlebot.core.state import RobotState  # noqa: E402
from littlebot.media.display import NoneBackend  # noqa: E402

pytestmark = pytest.mark.slow

# Lets a test make a page hidden / visible again (headless pages are always "visible").
VISIBILITY_SHIM = r"""
(function () {
  window.__hiddenFlag = false;
  Object.defineProperty(document, 'visibilityState', {
    configurable: true, get: function () { return window.__hiddenFlag ? 'hidden' : 'visible'; } });
  window.__setHidden = function (hidden) {
    window.__hiddenFlag = !!hidden;
    document.dispatchEvent(new Event('visibilitychange'));
  };
})();
"""

YT_STUB = r"""
(function () {
  var rec = window.__ytStub = { players: [], created: 0, destroyed: 0, blockAutoplay: false };
  function Player(id, opts) {
    var el = document.getElementById(id);
    var frame = document.createElement('div');
    frame.className = 'yt-stub-frame';
    frame.id = id;
    el.parentNode.replaceChild(frame, el);
    this.el = frame; this.opts = opts; this.calls = []; this.state = -1; this.volume = 100;
    this.muted = false; this.destroyed = false;
    this.videoId = opts.videoId || null;
    var vars = opts.playerVars || {};
    this.playlist = vars.listType === 'playlist' ? (window.__ytStubPlaylist || ['aaaaaaaaaaa', 'bbbbbbbbbbb']) : null;
    this.listHidden = !!window.__ytStubHideList;  // getPlaylist() returns null (list "not loaded yet")
    this.noVideoId = !!window.__ytStubNoVideoId;  // the player doesn't tell which video it shows
    this.index = 0;
    if (this.playlist && !this.videoId) { this.videoId = this.playlist[0]; }
    rec.players.push(this); rec.created++;
    var self = this;
    setTimeout(function () { if (!self.destroyed) { opts.events.onReady({ target: self }); } }, 20);
  }
  Player.prototype._set = function (s) {
    this.state = s; var self = this;
    setTimeout(function () { if (!self.destroyed) { self.opts.events.onStateChange({ target: self, data: s }); } }, 0);
  };
  Player.prototype.playVideo = function () { this.calls.push('playVideo'); if (!rec.blockAutoplay) { this._set(1); } };
  Player.prototype.pauseVideo = function () { this.calls.push('pauseVideo'); this._set(2); };
  Player.prototype.stopVideo = function () { this.calls.push('stopVideo'); };
  Player.prototype.setVolume = function (v) { this.calls.push('setVolume:' + v); this.volume = v; };
  Player.prototype.getVolume = function () { return this.volume; };
  Player.prototype.mute = function () { this.calls.push('mute'); this.muted = true; };
  Player.prototype.unMute = function () { this.calls.push('unMute'); this.muted = false; };
  Player.prototype.isMuted = function () { return this.muted; };
  Player.prototype.getPlayerState = function () { return this.state; };
  Player.prototype.getCurrentTime = function () { return 1.5; };
  Player.prototype.getDuration = function () { return 60; };
  Player.prototype.getVideoUrl = function () {
    return this.noVideoId ? 'https://www.youtube.com/' : 'https://www.youtube.com/watch?v=' + this.videoId;
  };
  Player.prototype.getPlaylist = function () { return this.listHidden ? null : this.playlist; };
  Player.prototype.getPlaylistIndex = function () { return this.playlist ? this.index : -1; };
  Player.prototype.destroy = function () {
    this.calls.push('destroy'); this.destroyed = true; rec.destroyed++;
    if (this.el && this.el.parentNode) { this.el.parentNode.removeChild(this.el); }
  };
  rec.current = function () { return rec.players[rec.players.length - 1]; };
  rec.fire = function (s) { rec.current()._set(s); };
  rec.error = function (code) {
    var p = rec.current();
    setTimeout(function () { p.opts.events.onError({ target: p, data: code }); }, 0);
  };
  rec.navigate = function (vid) { var p = rec.current(); p.videoId = vid; p._set(1); };
  rec.revealList = function () { rec.current().listHidden = false; };
  rec.next = function () { var p = rec.current(); p.index++; p.videoId = p.playlist[p.index]; p._set(1); };
  rec.summary = function () {
    var p = rec.current();
    return p ? { created: rec.created, destroyed: rec.destroyed, calls: p.calls.slice(), videoId: p.videoId,
                 state: p.state,
                 opts: { videoId: p.opts.videoId || null, host: p.opts.host, playerVars: p.opts.playerVars } }
             : { created: 0, destroyed: 0, calls: [] };
  };
  window.YT = { Player: Player, loaded: 1,
                PlayerState: { UNSTARTED: -1, ENDED: 0, PLAYING: 1, PAUSED: 2, BUFFERING: 3, CUED: 5 } };
  setTimeout(function () { if (typeof window.onYouTubeIframeAPIReady === 'function') { window.onYouTubeIframeAPIReady(); } },
             window.__ytStubApiDelay || 0);
})();
"""


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
    launched = None
    errors = []
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


class Harness:
    """Controller + uvicorn server on one AsyncRuntime loop (like production), plus a browser page."""

    def __init__(self, browser: Any) -> None:
        import uvicorn
        from fastapi import FastAPI

        from littlebot.media import create_player_router

        self.env = _build(backend=NoneBackend(), tick=3600.0)
        self.media = self.env.media
        self.runtime = AsyncRuntime("media-page-test")
        self.runtime.start()
        self.runtime.run(self.media.start())
        app = FastAPI()
        app.include_router(create_player_router(self.media))
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                                    lifespan="off", access_log=False))
        self.runtime.submit(self.server.serve())
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.console: list[str] = []
        self.blocked: list[str] = []
        self.context = browser.new_context(viewport={"width": 480, "height": 320})
        self.page = self.context.new_page()
        self.page.on("console", lambda m: self.console.append(f"{m.type}: {m.text}"))
        # Only external URLs are intercepted (local bridge requests go straight to uvicorn).
        self.context.route(re.compile(r"^(?!http://127\.0\.0\.1[:/])"), self._route)

    def _route(self, route: Any) -> None:
        url = route.request.url
        if url.startswith("https://www.youtube.com/iframe_api"):
            route.fulfill(status=200, content_type="text/javascript", body=YT_STUB)
        else:
            self.blocked.append(url)
            route.abort()

    # -- helpers ------------------------------------------------------------
    def open(self, page: Any = None) -> None:
        page = page or self.page
        page.goto(f"http://127.0.0.1:{self.port}{self.media.player_path()}")
        page.wait_for_function("window.__littlebotPlayer !== undefined")

    def new_page(self, init_script: str = "") -> Any:
        """Another tab/window on the same player link (same YouTube stub routing)."""
        page = self.context.new_page()
        page.on("console", lambda m: self.console.append(f"{m.type}: {m.text}"))
        page.add_init_script(VISIBILITY_SHIM + init_script)
        return page

    def run(self, coro: Any) -> Any:
        return self.runtime.run(coro, timeout=10)

    def stub(self, page: Any = None) -> dict[str, Any]:
        return (page or self.page).evaluate(
            "window.__ytStub ? window.__ytStub.summary() : {created: 0, destroyed: 0, calls: []}")

    def snap(self, page: Any = None) -> dict[str, Any]:
        return (page or self.page).evaluate("window.__littlebotPlayer.snapshot()")

    def visible_screen(self, page: Any = None) -> str | None:
        return (page or self.page).evaluate(
            "(() => { const v = document.querySelector('.screen.visible'); return v ? v.id : null; })()")

    def wait(self, cond: Callable[[], bool], what: str, timeout: float = 6.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return
            self.pause(0.05)
        raise AssertionError(f"timed out waiting for {what}; stub={self.stub()} page={self.snap()} "
                             f"media={self.media.status()['state']} console={self.console[-5:]}")

    def pause(self, seconds: float) -> None:
        """Sleep while letting Playwright dispatch route handlers / events."""
        self.page.wait_for_timeout(seconds * 1000)

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            self.server.should_exit = True
            time.sleep(0.3)
            self.run(self.media.close())
            self.runtime.stop()
            self.env.ctx.db.close()

    def csp_violations(self) -> list[str]:
        return [m for m in self.console if "Content Security Policy" in m or "Refused to" in m]


@pytest.fixture
def h(browser):
    harness = Harness(browser)
    yield harness
    harness.close()


def _play(h: Harness, item: dict[str, Any]) -> None:
    h.run(h.media.play(item["id"]))
    h.wait(lambda: h.media.status()["state"] == "playing", "player to report playing")


def test_page_loads_idle_and_connects(h):
    h.open()
    h.wait(lambda: h.media.status()["player"]["connected"], "page to poll the bridge")
    assert h.visible_screen() == "screen-idle"
    assert h.stub()["created"] == 0  # nothing is loaded until the parent presses play
    assert h.csp_violations() == []


def test_plays_approved_video_with_safe_player_vars_then_ends(h):
    item = h.env.add(VID_A, "Twinkle")
    h.open()
    _play(h, item)
    s = h.stub()
    assert s["created"] == 1 and s["opts"]["videoId"] == VID_A
    assert s["opts"]["host"] == "https://www.youtube-nocookie.com"
    pv = s["opts"]["playerVars"]
    assert pv["rel"] == 0 and pv["controls"] == 0 and pv["disablekb"] == 1 and pv["fs"] == 0
    assert pv["iv_load_policy"] == 3 and pv["playsinline"] == 1 and pv["autoplay"] == 1
    assert pv["origin"] == f"http://127.0.0.1:{h.port}" and "list" not in pv
    assert "setVolume:50" in s["calls"] and "playVideo" in s["calls"]
    assert h.visible_screen() is None  # nothing is drawn over a live player
    # nothing else on the page overlaps the player area
    assert h.page.evaluate(
        "document.elementFromPoint(240, 160).closest('#yt-host') !== null") is True

    # pause / resume / volume commands reach the player
    h.run(h.media.pause())
    h.wait(lambda: "pauseVideo" in h.stub()["calls"], "pauseVideo")
    h.run(h.media.resume())
    h.wait(lambda: h.stub()["calls"].count("playVideo") >= 2, "playVideo after resume")
    h.media.set_volume(0.3)
    h.wait(lambda: "setVolume:30" in h.stub()["calls"], "setVolume:30")
    h.media.set_volume(0.0)
    h.wait(lambda: "mute" in h.stub()["calls"], "mute at volume 0")

    # ENDED -> player destroyed at once, calm "All done!" screen, robot back to the face
    h.page.evaluate("window.__ytStub.fire(0)")
    h.wait(lambda: h.env.last(Topics.MEDIA_STOPPED) is not None, "media.stopped")
    assert h.env.last(Topics.MEDIA_STOPPED).data["reason"] == "ended"
    assert h.env.ctx.state.state == RobotState.IDLE
    assert h.env.face_states()[-1] == "idle"
    assert h.visible_screen() == "screen-done"
    assert h.page.inner_text("#screen-done") == "All done!"
    assert h.stub()["destroyed"] == 1 and "destroy" in h.stub()["calls"]
    assert h.page.evaluate("document.getElementById('yt-host').childElementCount") == 0
    assert "ended" in h.snap()["events"]
    h.pause(0.7)  # a few more polls: the page stays on the finished screen, nothing new loads
    assert h.visible_screen() == "screen-done" and h.stub()["created"] == 1
    assert h.csp_violations() == []
    assert not [u for u in h.blocked if "youtube" in u]  # no other YouTube requests were attempted


def test_error_150_shows_calm_error_and_stops(h):
    item = h.env.add(VID_A, "Locked")
    h.open()
    _play(h, item)
    h.page.evaluate("window.__ytStub.error(150)")
    h.wait(lambda: h.env.last(Topics.MEDIA_STOPPED) is not None, "media.stopped")
    assert h.env.last(Topics.MEDIA_STOPPED).data["reason"] == "error"
    assert "Unsupported video" in h.env.last(Topics.MEDIA_ERROR).data["error"]
    assert "Unsupported video" in h.media.get_item(item["id"])["last_error"]
    assert h.visible_screen() == "screen-error" and h.stub()["destroyed"] == 1


def test_unapproved_video_is_torn_down(h):
    item = h.env.add(VID_A, "Approved")
    h.open()
    _play(h, item)
    h.page.evaluate("window.__ytStub.navigate('zzzzzzzzzzz')")  # e.g. YouTube switched to another video
    h.wait(lambda: h.env.last(Topics.MEDIA_ERROR) is not None, "media.error")
    assert h.env.last(Topics.MEDIA_ERROR).data["code"] == "unexpected_video"
    assert h.stub()["destroyed"] == 1 and h.visible_screen() == "screen-error"
    assert h.env.ctx.state.state == RobotState.IDLE


def test_parent_stop_and_skip(h):
    a, b = h.env.add(VID_A, "A"), h.env.add(VID_B, "B")
    h.open()
    _play(h, a)
    h.run(h.media.skip())
    h.wait(lambda: h.stub()["created"] == 2 and h.stub()["videoId"] == VID_B, "second player for item B")
    assert h.stub()["destroyed"] == 1  # the first player was torn down before loading B
    h.wait(lambda: h.media.status()["state"] == "playing", "B playing")
    h.run(h.media.stop())
    h.wait(lambda: h.stub()["destroyed"] == 2, "player destroyed after stop")
    assert h.visible_screen() == "screen-idle"  # a parent stop is a calm blank screen


def test_limit_fades_out_then_finishes(h):
    h.env.ctx.settings.set("media.session_limit_minutes", 1)
    item = h.env.add(VID_A, "A")
    h.open()
    _play(h, item)
    h.env.clock.advance(61)
    h.run(h.media.tick())
    assert h.env.last(Topics.MEDIA_STOPPED).data["reason"] == "limit"
    h.wait(lambda: h.visible_screen() == "screen-done", "finished screen after fade", timeout=8)
    vols = [int(c.split(":")[1]) for c in h.stub()["calls"] if c.startswith("setVolume:")]
    assert vols[-1] == 0 and vols.index(0) > vols.index(50)  # faded down from 50 to 0
    assert h.stub()["destroyed"] == 1


def test_playlist_continues_only_within_the_approved_playlist(h):
    pl = h.media.add_item(f"https://www.youtube.com/playlist?list={LIST_A}", "Songs")
    h.page.add_init_script(f"window.__ytStubPlaylist = ['{VID_A}', '{VID_B}'];")
    h.open()
    _play(h, pl)
    pv = h.stub()["opts"]["playerVars"]
    assert pv["listType"] == "playlist" and pv["list"] == LIST_A
    h.page.evaluate("window.__ytStub.fire(0)")  # first track ended, more to come
    h.wait(lambda: "track_ended" in h.snap()["events"], "track_ended")
    assert h.env.last(Topics.MEDIA_STOPPED) is None and h.stub()["destroyed"] == 0
    h.page.evaluate("window.__ytStub.next()")  # YouTube moves to track 2 (in the approved list)
    h.pause(0.3)
    assert h.env.last(Topics.MEDIA_ERROR) is None
    assert h.snap()["playlistIds"] == 2 and h.snap()["idsAcked"] == 2  # the robot knows the list too
    assert h.media.status()["player"]["info"]["video_id"] == VID_B
    h.page.evaluate("window.__ytStub.fire(0)")  # last track ended
    h.wait(lambda: h.env.last(Topics.MEDIA_STOPPED) is not None, "media.stopped")
    assert h.env.last(Topics.MEDIA_STOPPED).data["reason"] == "ended"
    assert h.visible_screen() == "screen-done"


def test_unapproved_video_is_reported_with_its_id(h):
    item = h.env.add(VID_A, "Approved")
    h.open()
    _play(h, item)
    h.page.evaluate("window.__ytStub.navigate('zzzzzzzzzzz')")
    h.wait(lambda: h.env.last(Topics.MEDIA_ERROR) is not None, "media.error")
    last = h.media.status()["player"]["last_event"]
    assert last["type"] == "error" and last["code"] == "unexpected_video" and last["video_id"] == "zzzzzzzzzzz"


def _start_unconfirmed(h: Harness, item: dict[str, Any]) -> None:
    """Play ``item`` and wait until the stub player is PLAYING (the page may not confirm it)."""
    h.run(h.media.play(item["id"]))
    h.wait(lambda: h.stub().get("state") == 1, "stub player to be playing")


def test_playlist_whose_video_list_never_arrives_is_stopped_after_grace(h):
    # The finding: getPlaylist() empty + an unapproved video in the player. Fail closed.
    h.media.bridge.verify_grace_ms = 800
    pl = h.media.add_item(f"https://www.youtube.com/playlist?list={LIST_A}", "Songs")
    h.page.add_init_script(f"window.__ytStubPlaylist = ['{VID_A}', '{VID_B}']; window.__ytStubHideList = true;")
    h.open()
    _start_unconfirmed(h, pl)
    h.page.evaluate("window.__ytStub.navigate('ZZZZZZZZZZZ')")
    h.wait(lambda: h.snap()["verifying"] or h.env.last(Topics.MEDIA_ERROR) is not None, "grace period")
    h.wait(lambda: h.env.last(Topics.MEDIA_ERROR) is not None, "media.error after the grace period", timeout=5)
    assert h.env.last(Topics.MEDIA_ERROR).data["code"] == "unexpected_video"
    assert h.env.last(Topics.MEDIA_STOPPED).data["reason"] == "error"
    events = h.snap()["events"]
    assert "playing" not in events and "error:unexpected_video" in events  # never reported as fine
    assert h.stub()["destroyed"] == 1 and h.visible_screen() == "screen-error"
    assert h.env.ctx.state.state == RobotState.IDLE


def test_playlist_video_list_arriving_late_is_used(h):
    h.media.bridge.verify_grace_ms = 3000
    pl = h.media.add_item(f"https://www.youtube.com/playlist?list={LIST_A}", "Songs")
    h.page.add_init_script(f"window.__ytStubPlaylist = ['{VID_A}', '{VID_B}']; window.__ytStubHideList = true;")
    h.open()
    _start_unconfirmed(h, pl)
    h.pause(0.4)
    assert h.snap()["verifying"] is True and h.media.status()["state"] == "loading"  # not confirmed yet
    h.page.evaluate("window.__ytStub.revealList()")
    h.wait(lambda: h.media.status()["state"] == "playing", "playing once the list is known")
    h.wait(lambda: h.snap()["idsAcked"] == 2, "robot to acknowledge the playlist's ids")
    assert h.snap()["playlistIds"] == 2 and h.snap()["verifying"] is False
    h.page.evaluate("window.__ytStub.next()")  # track 2 of the approved list
    h.pause(0.5)
    assert h.env.last(Topics.MEDIA_ERROR) is None and h.media.status()["state"] == "playing"
    h.page.evaluate("window.__ytStub.navigate('ZZZZZZZZZZZ')")  # not in the list: stopped at once
    h.wait(lambda: h.env.last(Topics.MEDIA_ERROR) is not None, "media.error", timeout=1.5)
    assert h.env.last(Topics.MEDIA_ERROR).data["code"] == "unexpected_video"
    assert h.stub()["destroyed"] == 1 and h.env.ctx.state.state == RobotState.IDLE


def test_single_video_with_unknown_id_is_stopped_after_grace(h):
    h.media.bridge.verify_grace_ms = 800
    item = h.env.add(VID_A, "Approved")
    h.page.add_init_script("window.__ytStubNoVideoId = true;")
    h.open()
    _start_unconfirmed(h, item)
    h.wait(lambda: h.env.last(Topics.MEDIA_ERROR) is not None, "media.error after the grace period", timeout=5)
    assert h.env.last(Topics.MEDIA_ERROR).data["code"] == "unexpected_video"
    assert "playing" not in h.snap()["events"] and h.stub()["destroyed"] == 1


# ------------------------------------------------------------------ one player page at a time
def test_leftover_background_tab_never_plays_along(h):
    # QA: desktop browser backend. After STOP the old tab stays open and keeps polling; the next
    # play must not create a second player there, and its "hidden" report must not kill the session.
    a_item, b_item = h.env.add(VID_A, "A"), h.env.add(VID_B, "B")
    old = h.new_page()
    h.open(old)
    h.run(h.media.play(a_item["id"]))
    h.wait(lambda: h.media.status()["state"] == "playing", "first session playing in the old tab")
    h.run(h.media.stop())
    h.wait(lambda: h.stub(old)["destroyed"] == 1, "old tab's player destroyed after stop")
    old.evaluate("window.__setHidden(true)")  # the parent switched to another tab
    new = h.new_page()
    h.open(new)
    h.run(h.media.play(b_item["id"]))
    h.wait(lambda: h.media.status()["state"] == "playing", "second session playing in the new tab")
    assert h.stub(new)["created"] == 1 and h.stub(new)["videoId"] == VID_B
    assert h.snap(new)["owner"] == h.snap(new)["pageId"]
    h.pause(0.8)  # several polls in both tabs
    assert h.stub(old)["created"] == 1  # still only the first session's (destroyed) player
    assert h.snap(old)["hasPlayer"] is False and h.snap(old)["owner"] == h.snap(new)["pageId"]
    old.evaluate("window.__setHidden(false)")
    old.evaluate("window.__setHidden(true)")  # visibility changes in the old tab change nothing
    h.pause(0.6)
    assert h.stub(old)["created"] == 1 and h.media.status()["state"] == "playing"
    assert h.env.last(Topics.MEDIA_ERROR) is None and h.env.ctx.state.state == RobotState.PLAYING_MEDIA


def test_two_visible_tabs_only_one_creates_a_player(h):
    item = h.env.add(VID_A, "A")
    one, two = h.new_page(), h.new_page()
    h.open(one)
    h.open(two)
    h.run(h.media.play(item["id"]))
    h.wait(lambda: h.media.status()["state"] == "playing", "playing")
    h.pause(0.8)
    created = [h.stub(one)["created"], h.stub(two)["created"]]
    assert sorted(created) == [0, 1]  # exactly one player, in the tab that claimed the video
    idle = one if created[0] == 0 else two
    assert h.visible_screen(idle) == "screen-idle" and h.snap(idle)["hasPlayer"] is False


def test_hidden_page_waits_until_visible(h):
    item = h.env.add(VID_A, "A")
    page = h.new_page()
    h.open(page)
    page.evaluate("window.__setHidden(true)")
    h.run(h.media.play(item["id"]))
    h.pause(0.8)
    assert h.stub(page)["created"] == 0 and h.media.bridge.owner is None  # a hidden page never claims
    assert h.media.status()["state"] == "loading"
    page.evaluate("window.__setHidden(false)")
    h.wait(lambda: h.media.status()["state"] == "playing", "playing once the page is visible")
    assert h.stub(page)["created"] == 1 and h.media.bridge.owner == h.snap(page)["pageId"]


def test_claimed_page_hidden_before_its_player_exists_creates_it_when_visible(h):
    item = h.env.add(VID_A, "A")
    page = h.new_page("window.__ytStubApiDelay = 900;")  # the YouTube API takes a moment to load
    h.open(page)
    h.run(h.media.play(item["id"]))
    h.wait(lambda: h.media.bridge.owner is not None, "the page to claim the video")
    page.evaluate("window.__setHidden(true)")
    h.wait(lambda: h.snap(page)["deferred"] is True, "player creation deferred while hidden", timeout=4)
    assert h.stub(page)["created"] == 0
    page.evaluate("window.__setHidden(false)")
    h.wait(lambda: h.media.status()["state"] == "playing", "playing once visible again")
    assert h.stub(page)["created"] == 1 and h.env.last(Topics.MEDIA_ERROR) is None


def test_page_that_loses_the_claim_tears_its_player_down(h):
    item = h.env.add(VID_A, "A")
    h.open()
    _play(h, item)
    bridge = h.media.bridge
    with bridge._lock:  # e.g. another page took over this load
        bridge._owner = "someotherpage01"
        bridge._seq += 1
    h.wait(lambda: h.stub()["destroyed"] == 1, "player torn down")
    assert h.visible_screen() == "screen-idle" and h.snap()["hasPlayer"] is False


def test_stale_token_page_shows_calm_message(h):
    item = h.env.add(VID_A, "A")
    h.page.goto(f"http://127.0.0.1:{h.port}/player?token=bad")
    assert "invalid player token" in h.page.content()
    h.open()
    h.media.bridge.token = "rotated-token-" + "x" * 30  # e.g. the robot restarted
    h.wait(lambda: h.visible_screen() == "screen-error", "out-of-date link message")
    assert "out of date" in h.page.inner_text("#screen-error")
    h.run(h.media.play(item["id"]))
    h.pause(0.6)
    assert h.stub()["created"] == 0  # an unauthorised page never loads anything


def test_hidden_page_stops_and_reports(h):
    item = h.env.add(VID_A, "A")
    h.open()
    _play(h, item)
    # e.g. a YouTube link opened a new tab in the kiosk, covering the player page
    h.page.evaluate("""() => {
        Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    h.wait(lambda: h.env.last(Topics.MEDIA_STOPPED) is not None, "media.stopped")
    assert h.env.last(Topics.MEDIA_STOPPED).data["reason"] == "error"
    assert "hidden or left" in h.env.last(Topics.MEDIA_ERROR).data["error"]
    assert h.stub()["destroyed"] == 1 and "hidden" in h.snap()["events"]
    assert h.env.ctx.state.state == RobotState.IDLE
