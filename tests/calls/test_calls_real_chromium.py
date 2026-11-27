"""A smoke run of the WhatsApp calls service against a real (headless) Chromium and a small LOCAL page that is shaped
like WhatsApp Web (a chat list, a search box, a chat header with a video button, a call box, a QR canvas): the cover
really hides the page from its first paint, lets only the QR code and the call through, LittleBot's presses go
through it, and Chromium is gone afterwards. Skipped when no Chromium is installed. It says nothing about the real
WhatsApp page (that needs scripts/check_whatsapp.py on the robot); no internet is used."""

from __future__ import annotations

import asyncio
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

from littlebot.calls import page_scripts as scripts
from littlebot.calls.service import CallsService
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

# A page with WhatsApp Web's shape (none of its code): #side with a search box, #pane-side with chat rows whose
# names sit in span[title], #main header with a "Video call" button, and a call box with an "End call" button.
FAKE = """<!doctype html><meta charset=utf-8><title>WhatsApp</title>
<style>body{margin:0;font:16px sans-serif} #side{position:absolute;left:0;top:0;width:340px;height:100vh}
#main{position:absolute;left:340px;top:0;right:0;height:100vh} [role=listitem]{padding:14px;border-bottom:1px solid #ccc}
[role=textbox]{border:1px solid #888;margin:8px;padding:8px;min-height:20px}
#call{position:fixed;left:200px;top:100px;width:500px;height:300px;background:#123;color:#fff}</style>
<div id=side><div contenteditable=true role=textbox aria-label="Search input textbox"></div>
<div id=pane-side aria-label="Chat list"></div></div><div id=main></div>
<script>
const chats = [['Somebody Else', 'the secret message about the surprise'], ['Ali Khan', 'another secret message'],
               ['Ali Khan Senior', 'hello']];
const list = document.getElementById('pane-side'), main = document.getElementById('main');
const box = document.querySelector('[role=textbox]');
window.pressed = [];
function draw() {
  const want = box.textContent.trim().toLowerCase();
  list.textContent = '';
  for (const [name, last] of chats) {
    if (want && !name.toLowerCase().includes(want)) continue;
    const row = document.createElement('div'); row.setAttribute('role', 'listitem');
    const a = document.createElement('span'); a.title = name; a.textContent = name;
    const b = document.createElement('span'); b.title = last; b.textContent = ' ' + last;
    row.append(a, b);
    row.addEventListener('click', (e) => { window.pressed.push('row:' + name + ':' + e.isTrusted); openChat(name); });
    list.append(row);
  }
}
function openChat(name) {
  main.textContent = '';
  const head = document.createElement('header');
  const t = document.createElement('span'); t.title = name; t.textContent = name;
  const v = document.createElement('button'); v.setAttribute('aria-label', 'Video call'); v.textContent = 'cam';
  v.addEventListener('click', () => { window.pressed.push('video:' + name); startCall(); });
  head.append(t, v); main.append(head);
  const msg = document.createElement('p'); msg.textContent = 'the whole secret conversation'; main.append(msg);
}
function startCall() {
  const c = document.createElement('div'); c.id = 'call'; c.setAttribute('data-testid', 'call-container');
  const e = document.createElement('button'); e.setAttribute('aria-label', 'End call'); e.textContent = 'end';
  e.addEventListener('click', () => { window.pressed.push('end'); c.remove(); });
  c.append('In a call', e); document.body.append(c);
}
box.addEventListener('input', draw);
draw();
</script>"""
QR = """<!doctype html><meta charset=utf-8><title>WhatsApp</title><body style="margin:0">
<p>Steps to log in</p><canvas aria-label="Scan this QR code to link a device!" width=228 height=228
style="position:absolute;left:600px;top:150px"></canvas>
<script>const g = document.querySelector('canvas').getContext('2d'); g.fillRect(20, 20, 60, 60);</script>"""


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def site(tmp_path: Path) -> Any:
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(FAKE)
    (root / "qr.html").write_text(QR)
    handler = functools.partial(_Quiet, directory=str(root))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


async def _until(cond: Callable[[], Any], timeout: float = 15.0) -> Any:
    end = asyncio.get_running_loop().time() + timeout
    while True:
        value = await cond()
        if value:
            return value
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.1)


def _service(site: str, tmp_path: Path, page: str) -> tuple[CallsService, Any]:
    ctx = make_test_context()
    ctx.privacy.set(cloud_enabled=True)
    ctx.settings.set("calls.contacts", {"Dad": "Ali Khan"})
    svc = CallsService(ctx, browser_path=CHROMIUM, kiosk=False, profile_dir=tmp_path / "whatsapp-profile",
                       home=f"{site}/{page}", origin=site, insecure_hosts=("127.0.0.1",), memory=lambda: 4000,
                       windows=lambda: None, poll=0.2, step_poll=0.1, settle=0.0,
                       extra_args=("--headless=new", "--no-sandbox", "--disable-gpu"))
    return svc, ctx


# What is on top at a point, seen from the page's own world: the cover's tag, or the page's element.
TOP = "((x, y) => { const e = document.elementFromPoint(x, y); return e ? e.tagName.toLowerCase() : null; })"


@pytest.mark.skipif(CHROMIUM is None, reason="no Chromium installed")
@pytest.mark.timeout(120)
async def test_a_call_under_the_cover(site: str, tmp_path: Path) -> None:
    svc, ctx = _service(site, tmp_path, "index.html")
    await svc.start()
    try:
        out = await svc.open(contact="Dad", source="dashboard")
        assert out["ok"], out
        s = svc._session

        async def ev(expr: str) -> Any:  # in the page's own world
            res = await svc._page(s, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
            return (res.get("result") or {}).get("value")

        await _until(lambda: _is(svc.in_call))
        # the automation ran underneath: the right chat (not "Ali Khan Senior"), with real presses
        assert await ev("window.pressed") == ["row:Ali Khan:true", "video:Ali Khan"]
        assert await ev("typeof window.littlebotCalls") == "undefined"  # the binding is invisible to the page
        st = await svc._state(s)
        assert st["cover"] and st["covered"] and st["call"] == "in_call" and st["keys"]["call_container"] == 0
        await _until(lambda: _holes(svc, s, 1))
        # the call's box shows through; the chat list, the open chat and its messages do not
        assert await ev(f"{TOP}(450, 250)") == "div"  # inside the call box
        assert await ev(f"{TOP}(100, 500)") == scripts.COVER_TAG  # the chat list
        assert await ev(f"{TOP}(800, 500)") == scripts.COVER_TAG  # the open chat
        res = await svc.hang_up("test")
        assert res["was_call"] is True and res["closed"] is True
    finally:
        await svc.stop()
        ctx.db.close()
    assert svc._leftovers() == []  # nothing of Chromium is left


@pytest.mark.skipif(CHROMIUM is None, reason="no Chromium installed")
@pytest.mark.timeout(120)
async def test_the_cover_hides_everything_and_shows_only_the_qr(site: str, tmp_path: Path) -> None:
    svc, ctx = _service(site, tmp_path, "qr.html")
    await svc.start()
    try:
        assert (await svc.open(source="dashboard"))["ok"]
        s = svc._session

        async def ev(expr: str) -> Any:
            res = await svc._page(s, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
            return (res.get("result") or {}).get("value")

        await _until(lambda: _is(svc.status()["state"] == "linking"))
        cover = f"document.querySelector('{scripts.COVER_TAG}')"
        assert await ev(f"{cover}.getAttribute('data-on')") == "1"
        # the QR code is redrawn big on the cover (no hole needed), with the words; the page itself is hidden
        assert await _until(lambda: ev(f"getComputedStyle({cover}.shadowRoot.querySelector('canvas')).display === 'block'"))
        assert await ev(f"{cover}.shadowRoot.querySelector('canvas').width") >= 200
        assert await _until(lambda: ev(f"{cover}.shadowRoot.textContent.includes('Scan this with your phone')"))
        assert await ev(f"{TOP}(700, 250)") == scripts.COVER_TAG
        assert await ev(f"{cover}.getAttribute('data-holes')") == "0"
        # a page script that removes the cover gets it back at once
        await ev(f"{cover}.remove()")
        assert await _until(lambda: ev(f"!!{cover} && {TOP}(700, 250) === '{scripts.COVER_TAG}'"))
        # the grown-up's switch: off leaves only a small × (and it is never switched off by anything else)
        ctx.settings.set("calls.hide_chats", False)
        assert await _until(lambda: ev(f"{cover}.getAttribute('data-on') === '0'"))
        assert await ev(f"{TOP}(700, 250)") == "canvas"
        ctx.settings.set("calls.hide_chats", True)
        assert await _until(lambda: ev(f"{TOP}(700, 250) === '{scripts.COVER_TAG}'"))
        # the cover's Close button closes WhatsApp
        await ev(f"[...{cover}.shadowRoot.querySelectorAll('button')].find((b) => b.textContent === 'Close').click()")
        await _until(lambda: _is(not svc.active))
    finally:
        await svc.stop()
        ctx.db.close()
    assert svc._leftovers() == []


async def _is(value: Any) -> Any:
    return value


async def _holes(svc: CallsService, s: Any, n: int) -> bool:
    return (await svc._state(s)).get("holes") == n


@pytest.mark.skipif(CHROMIUM is None, reason="no Chromium installed")
@pytest.mark.timeout(120)
async def test_check_whatsapp_script_against_the_open_page(site: str, tmp_path: Path) -> None:
    """scripts/check_whatsapp.py beside the running service: which selectors match, and the cover."""
    import importlib.util

    from littlebot.core.paths import PROJECT_ROOT

    spec = importlib.util.spec_from_file_location("check_whatsapp", PROJECT_ROOT / "scripts" / "check_whatsapp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    svc, ctx = _service(site, tmp_path, "index.html")
    await svc.start()
    try:
        assert (await svc.open(contact="Dad", source="dashboard"))["ok"]
        await _until(lambda: _is(svc.in_call))
        await _until(lambda: _holes(svc, svc._session, 1))
        report = await mod.check(tmp_path / "whatsapp-profile", any_page=True)
        assert report["cover"] and report["covered"] and report["holes"] == 1
        assert report["linked"] and report["call"] == "in_call" and report["container"] == 0
        assert report["keys"]["chat_list"][0] and report["keys"]["end_call"][0] and report["keys"]["video_call"][0]
        assert not any(report["keys"]["qr"])
        text = mod.render(report)
        assert "Ali" not in text and "secret" not in text
        assert svc.in_call  # the check changed nothing
    finally:
        await svc.stop()
        ctx.db.close()
