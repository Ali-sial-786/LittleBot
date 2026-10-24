"""A smoke run of the browser view against a real (headless) Chromium and a local page: the DevTools port file,
the standard-library WebSocket, the strip, zoom, read, scroll, results, back, new windows, the backstop and the
strip's ×. Skipped when no Chromium is installed (no internet needed: the page is served on 127.0.0.1)."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import glob
import http.server
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from littlebot.browser.cdp import CDPError
from littlebot.browser.service import BrowserView
from littlebot.testing import make_test_context

pytestmark = pytest.mark.slow


def _chromium() -> str | None:
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    for pattern in ("/opt/pw-browsers/chromium-*/chrome-linux/chrome",
                    str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux/chrome")):
        hits = sorted(glob.glob(pattern))
        if hits and os.access(hits[-1], os.X_OK):
            return hits[-1]
    return None


CHROMIUM = _chromium()
RESULTS = ("<!doctype html><title>Results</title><body><div id=rso>"
           "<div><a href='/two.html'><h3>First result</h3></a><p>The first snippet.</p></div>"
           "<div><a href='/three.html'><h3>Second result</h3></a></div></div>"
           "<p>" + "Lorem ipsum dolor sit amet. " * 400 + "</p></body>")


@pytest.fixture
def site(tmp_path: Path) -> Any:
    root = tmp_path / "site"
    root.mkdir()
    (root / "one.html").write_text(RESULTS)
    (root / "two.html").write_text("<title>Two</title><main>Page two says hello.</main>")
    (root / "three.html").write_text("<title>Three</title><main>Three</main>")
    handler = functools.partial(_Quiet, directory=str(root))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass


async def _until(cond: Callable[[], Any], timeout: float = 10.0) -> Any:
    end = asyncio.get_running_loop().time() + timeout
    while True:
        value = await cond()
        if value:
            return value
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.1)


@pytest.mark.skipif(CHROMIUM is None, reason="no Chromium installed")
@pytest.mark.timeout(90)
async def test_real_chromium_smoke(site: str, tmp_path: Path) -> None:
    ctx = make_test_context()
    ctx.privacy.set(cloud_enabled=True)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    view = BrowserView(ctx, browser_path=CHROMIUM, kiosk=False, insecure_hosts=("127.0.0.1",), tmp_root=profiles,
                       extra_args=("--headless=new", "--no-sandbox", "--disable-gpu"))
    await view.start()
    try:
        out = await view.open(url=f"{site}/one.html", source="dashboard")
        assert out["ok"], out
        s = view._session

        async def ev(expr: str) -> Any:
            res = await view._page(s, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
            return (res.get("result") or {}).get("value")

        await _until(lambda: ev("document.readyState === 'complete' && location.pathname === '/one.html'"))
        # the strip is in the page, its binding invisible to the page's own scripts; phone layout, zoomed
        assert await _until(lambda: ev("!!document.querySelector('littlebot-strip')"))
        assert await ev("typeof window.littlebotView") == "undefined"
        assert await ev("window.devicePixelRatio") == pytest.approx(1.4)
        assert "Mobile" in await ev("navigator.userAgent")
        assert await ev("window.open('/two.html')") is None  # no new windows

        read = await view.read()
        assert read["ok"] and read["text"].startswith("First result") and read["truncated"]
        assert len(read["text"]) <= ctx.config.browser.read_chars + 1

        assert (await view.control("scroll", {"direction": "down"}))["ok"]
        assert await _until(lambda: ev("scrollY > 0"))

        res = await view.control("open_result", {"n": 2})
        assert res["ok"] and res["opened"] == "Second result", res
        await _until(lambda: ev("location.pathname === '/three.html'"))
        await _until(lambda: asyncio.sleep(0, view.describe() == "Three (127.0.0.1)"))

        assert (await view.control("back", {}))["moved"] is True
        await _until(lambda: ev("location.pathname === '/one.html'"))

        # a page the policy refuses (plain http to a host that isn't allowed) is replaced
        await view._page(s, "Page.navigate", {"url": site.replace("127.0.0.1", "localhost") + "/two.html"})
        assert await _until(lambda: ev("document.body && document.body.innerText.includes(\"won't open\")"))
        assert (await view.control("back", {}))["ok"]
        await _until(lambda: ev("location.pathname === '/one.html' && !!document.querySelector('littlebot-strip')"))

        targets = (await s.cdp.send("Target.getTargets"))["targetInfos"]
        assert [t["type"] for t in targets].count("page") == 1

        # the strip's ×: a real (trusted) tap at its place, top right, in CSS pixels
        width = await ev("innerWidth")
        for kind in ("mousePressed", "mouseReleased"):
            with contextlib.suppress(CDPError):  # (the window may be gone by the release)
                await view._page(s, "Input.dispatchMouseEvent", {"type": kind, "x": width - 47, "y": 47,
                                                                  "button": "left", "clickCount": 1})
        await _until(lambda: asyncio.sleep(0, not view.active))
        assert view.status()["last"]["reason"] == "closed with the ×"
        await view._wait_teardowns()
        assert list(profiles.iterdir()) == []  # the throwaway profile is gone
    finally:
        await view.stop()
        ctx.db.close()
