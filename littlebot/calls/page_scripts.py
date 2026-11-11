"""Everything LittleBot knows about WhatsApp Web's page, in ONE place: the selectors, the scripts that find and
report things, and LittleBot's cover over the page.

WhatsApp changes its page whenever it likes, and none of this could be tried against the real site when it was
written. So every thing LittleBot looks for has several ways to be found (``SELECTORS``: an aria-label, a
data-icon, a title, a role, a test id), tried in order; ``scripts/check_whatsapp.py`` prints which of them match on
the robot, so a broken one can be fixed here and nowhere else.

What the scripts hand back to LittleBot is only *whether* and *where* something is (true / false, which selector
matched, a point to press), never a word of the page: no chat names, no message text. The one script that looks at
names (``find_chat_script``) compares them inside the page with the contact asked for and answers with a point.

The cover (``cover_script``)
----------------------------
With the setting ``calls.hide_chats`` on (the default), LittleBot's own opaque sheet lies over the WHOLE WhatsApp
page from before its first paint (DevTools ``Page.addScriptToEvaluateOnNewDocument``), so the chat list, an open
chat, previews, search results and pop-ups never show on the robot's screen. Covered is the default; only two
things are let through, each found fresh five times a second by the cover itself:

* the QR code while WhatsApp isn't linked (redrawn big on the cover; if it can't be copied, a hole over it);
* a call: a hole over the call's own box when one is recognised (never a box that holds the chat list, the search
  box or a chat's header), else holes over the live video pictures only, with LittleBot's own big Answer / Decline /
  Hang up buttons on the cover.

The cover shows what is going on in big words (``cover_state``) and a big Close button. LittleBot's presses go
through it (``pass_script``: for the instant of a press the cover lets pointer events through while it stays
opaque). A failed step never uncovers anything: the cover says what went wrong. Off: only a small × stays.

Everything is built with DOM calls and inline CSSOM styles (no HTML strings, no <style>), so WhatsApp's content
security policy has nothing to refuse, and lives in a shadow root so the page's styles don't reach it.
"""

from __future__ import annotations

import html
import json
from typing import Any
from urllib.parse import quote

WORLD = "littlebot-calls"  # the isolated world the cover and LittleBot's scripts run in
BINDING = "littlebotCalls"  # Runtime.addBinding name, only in WORLD: the cover's buttons, touches
COVER_TAG = "littlebot-cover"

# ---------------------------------------------------------------- the selectors (the ONLY place they live)
# key -> ways to find it, tried in order; the first one with a visible match wins. Keep the most specific first.
SELECTORS: dict[str, tuple[str, ...]] = {
    # the QR code on the "link a device" page
    "qr": (
        'canvas[aria-label*="scan" i]',
        '[data-testid="qrcode"] canvas',
        "div[data-ref] canvas",
        '[data-testid="qrcode"]',
        "div[data-ref]",
        'canvas[role="img"]',
    ),
    # the list of chats: WhatsApp is linked and loaded once this shows
    "chat_list": (
        "#pane-side",
        '[data-testid="chat-list"]',
        '[aria-label="Chat list" i]',
        '[role="grid"][aria-rowcount]',
        "#side",
    ),
    # the search box above the chat list
    "search_box": (
        '#side [contenteditable="true"][role="textbox"]',
        '[data-testid="chat-list-search"]',
        'div[contenteditable="true"][data-tab="3"]',
        '[role="textbox"][aria-label*="search" i]',
        '[role="textbox"][aria-placeholder*="search" i]',
        '[title="Search input textbox" i]',
        'input[aria-label*="search" i]',
        '#side input[type="text"]',
    ),
    # a button that opens the search box, where it isn't always shown
    "search_button": (
        '#side button[aria-label*="search" i]',
        '#side [data-icon="search"]',
        '#side [data-icon*="search"]',
        '[role="button"][title*="search" i]',
    ),
    # the bar on top of an open chat
    "chat_header": (
        "#main header",
        '[data-testid="conversation-header"]',
        '#main [role="banner"]',
        '[data-testid="conversation-info-header"]',
    ),
    # the video-call button in an open chat (or the item in the call menu)
    "video_call": (
        '#main header [aria-label*="video call" i]',
        '[data-testid="video-call-btn"]',
        '#main header [data-icon*="video-call"]',
        '#main header [title*="video call" i]',
        '[role="menuitem"][aria-label*="video" i]',
        '[role="button"][aria-label*="video call" i]',
        'button[aria-label*="video call" i]',
        '[data-icon*="video-call"]',
        '[title*="video call" i]',
    ),
    # the voice-call button
    "voice_call": (
        '#main header [aria-label*="voice call" i]',
        '#main header [aria-label*="audio call" i]',
        '[data-testid="audio-call-btn"]',
        '[data-testid="voice-call-btn"]',
        '#main header [data-icon*="voice-call"]',
        '#main header [data-icon*="audio-call"]',
        '#main header [title*="voice call" i]',
        '[role="menuitem"][aria-label*="voice" i]',
        '[role="menuitem"][aria-label*="audio" i]',
        '[role="button"][aria-label*="voice call" i]',
        'button[aria-label*="voice call" i]',
        '[data-icon*="voice-call"]',
        '[data-icon*="audio-call"]',
        '[title*="voice call" i]',
    ),
    # one "Call" button that opens a small menu with the two kinds, where WhatsApp shows that instead
    "call_menu": (
        '#main header [aria-label="Call" i]',
        '#main header button[aria-label^="call" i]',
        '#main header [title="Call" i]',
        '#main header [data-icon="call"]',
        '#main header [data-icon*="call-refreshed"]',
        '#main header [aria-label*="get the app for calling" i]',
    ),
    # during a call: end it
    "end_call": (
        '[aria-label*="end call" i]',
        '[data-testid="end-call-btn"]',
        '[aria-label*="hang up" i]',
        '[aria-label*="leave call" i]',
        '[data-icon*="end-call"]',
        '[data-icon*="call-end"]',
        '[title*="end call" i]',
        '[aria-label*="cancel call" i]',
    ),
    # a call coming in: take it
    "accept_call": (
        '[aria-label*="accept call" i]',
        '[aria-label*="answer call" i]',
        '[data-testid="accept-call-btn"]',
        '[aria-label="Accept" i]',
        '[aria-label="Answer" i]',
        '[aria-label*="accept video" i]',
        '[aria-label*="accept voice" i]',
        '[data-icon*="accept-call"]',
        '[data-icon*="call-accept"]',
        '[title="Accept" i]',
    ),
    # ... or not
    "decline_call": (
        '[aria-label*="decline call" i]',
        '[aria-label*="reject call" i]',
        '[data-testid="decline-call-btn"]',
        '[aria-label="Decline" i]',
        '[aria-label="Reject" i]',
        '[aria-label="Ignore" i]',
        '[data-icon*="decline-call"]',
        '[data-icon*="reject-call"]',
        '[data-icon*="call-decline"]',
        '[title="Decline" i]',
    ),
    # the box a call lives in (the only part of the page the cover lets through, with the QR code)
    "call_container": (
        '[data-testid="call-container"]',
        '[data-testid*="voip" i]',
        '[data-testid*="call-window" i]',
        '[role="dialog"][aria-label*="call" i]',
        '[aria-label*="call" i][aria-modal="true"]',
        '[role="dialog"]:has(video)',
        '[role="dialog"]:has([aria-label*="end call" i])',
        '[role="dialog"]:has([aria-label*="decline" i])',
        '[role="alertdialog"]:has([aria-label*="decline" i])',
    ),
}

# The ones scripts/check_whatsapp.py lists (all of them), and those a step needs.
KEYS = tuple(SELECTORS)

# ---------------------------------------------------------------- shared helpers (JavaScript)
# H.q(key) -> the first visible element for a key; H.hit(key) -> {el, i} (which selector matched);
# H.call() -> what a call looks like right now. No text ever leaves these.
_HELPERS = r"""
const H = ((SEL) => {
  const vis = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    if (r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  };
  const hit = (key, base) => {
    const list = SEL[key] || [];
    for (let i = 0; i < list.length; i++) {
      let found;
      try { found = (base || document).querySelectorAll(list[i]); } catch (e) { continue; }
      for (const el of found) if (vis(el)) return {el, i};
    }
    return null;
  };
  const q = (key, base) => { const h = hit(key, base); return h ? h.el : null; };
  const call = () => {
    const linked = !!q('chat_list');
    const end = q('end_call'), accept = q('accept_call'), decline = q('decline_call');
    const videos = [];
    for (const v of document.querySelectorAll('video')) {
      if (v.srcObject && vis(v) && v.getBoundingClientRect().width >= 80) videos.push(v);  // live pictures only
    }
    let container = null, ci = -1;
    const list = SEL.call_container || [];
    outer: for (let i = 0; i < list.length; i++) {
      let found;
      try { found = document.querySelectorAll(list[i]); } catch (e) { continue; }
      for (const el of found) {
        if (!vis(el)) continue;
        if (q('chat_list', el) || q('search_box', el) || q('chat_header', el)) continue;  // never the chats
        const inside = (x) => !!x && el.contains(x);
        if (!(inside(end) || inside(accept) || inside(decline) || videos.some(inside))) continue;
        container = el; ci = i; break outer;
      }
    }
    const incoming = !!accept && (!!decline || !!container);
    const state = incoming ? 'incoming' : (end || videos.length || container) ? 'in_call' : 'none';
    return {linked, end, accept: incoming ? accept : null, decline, videos, container, ci, state};
  };
  const point = (el) => {
    const t = el.closest('button,[role="button"],[role="menuitem"]') || el;
    const r = (vis(t) ? t : el).getBoundingClientRect();
    return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
  };
  return {vis, hit, q, call, point};
})(SEL);
"""


def _sel_json() -> str:
    return json.dumps({k: list(v) for k, v in SELECTORS.items()}, separators=(",", ":"))


def state_script() -> str:
    """What the page shows now, as facts only: ``{keys: {key: index of the selector that matched, or -1},
    points: {key: {x, y}}, linked, qr, call: none | incoming | in_call, container: index or -1, videos: n,
    cover: is the cover there, covered: is it hiding the page, holes: n, ask: a cover button pressed or null}``."""
    return ("(() => { const SEL = " + _sel_json() + ";" + _HELPERS + r"""
  const keys = {}, points = {};
  for (const key of Object.keys(SEL)) {
    if (key === 'call_container') continue;
    const h = H.hit(key);
    keys[key] = h ? h.i : -1;
    if (h) points[key] = H.point(h.el);
  }
  const c = H.call();
  keys.call_container = c.ci;
  if (c.state !== 'incoming') { keys.accept_call = -1; delete points.accept_call; }
  const host = document.querySelector('""" + COVER_TAG + r"""');
  let ask = null;
  if (host) { ask = host.getAttribute('data-ask'); host.removeAttribute('data-ask'); }
  return {keys, points, linked: c.linked, qr: !c.linked && keys.qr >= 0, call: c.state, container: c.ci,
          videos: c.videos.length, cover: !!host, covered: !!host && host.getAttribute('data-on') === '1',
          holes: host ? Number(host.getAttribute('data-holes') || 0) : 0, ask};
})()""")


def check_script() -> str:
    """For scripts/check_whatsapp.py: every selector of every key, matched (a visible element) or not, and what
    the page counts as: ``{keys: {key: [true | false per selector]}, linked, qr, call, container: index or -1,
    videos, cover, covered, holes}``. True / false and numbers only; it changes nothing on the page."""
    return ("(() => { const SEL = " + _sel_json() + ";" + _HELPERS + r"""
  const keys = {};
  for (const key of Object.keys(SEL)) {
    keys[key] = SEL[key].map((sel) => {
      try { return [...document.querySelectorAll(sel)].some(H.vis); } catch (e) { return false; }
    });
  }
  const c = H.call();
  const host = document.querySelector('""" + COVER_TAG + r"""');
  return {keys, linked: c.linked, qr: !c.linked && keys.qr.some(Boolean), call: c.state, container: c.ci,
          videos: c.videos.length, cover: !!host, covered: !!host && host.getAttribute('data-on') === '1',
          holes: host ? Number(host.getAttribute('data-holes') || 0) : 0};
})()""")


def find_chat_script(name: str) -> str:
    """Where the chat called ``name`` is in the list (after a search): ``{found, x, y}``. The names are compared
    inside the page; nothing of them comes back."""
    return ("((want) => { const SEL = " + _sel_json() + ";" + _HELPERS + r"""
  const norm = (t) => String(t || '').replace(/\s+/g, ' ').trim().toLowerCase();
  want = norm(want);
  const scope = H.q('chat_list') || document;
  const exact = [], starts = [];
  for (const el of scope.querySelectorAll('[title], span[dir="auto"]')) {
    if (!H.vis(el)) continue;
    const t = norm(el.getAttribute('title')) || (el.childElementCount ? '' : norm(el.textContent));
    if (!t || t.length > 80) continue;
    const row = el.closest('[role="listitem"],[role="row"],[data-testid="cell-frame-container"],[role="gridcell"]') || el;
    if (t === want) exact.push(row);
    else if (t.startsWith(want + ' ') && !starts.includes(row)) starts.push(row);
  }
  const row = exact[0] || (starts.length === 1 ? starts[0] : null);
  if (!row) return {found: false};
  row.scrollIntoView({block: 'nearest'});
  const r = row.getBoundingClientRect();
  return {found: true, x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
})(""" + json.dumps(str(name)) + ")")


def header_script(name: str) -> str:
    """Is the open chat the one called ``name``? ``{header: a chat is open, match: it is that one}``."""
    return ("((want) => { const SEL = " + _sel_json() + ";" + _HELPERS + r"""
  const norm = (t) => String(t || '').replace(/\s+/g, ' ').trim().toLowerCase();
  want = norm(want);
  const head = H.q('chat_header');
  if (!head) return {header: false, match: false};
  for (const el of head.querySelectorAll('[title], span[dir="auto"]')) {
    const t = norm(el.getAttribute('title')) || (el.childElementCount ? '' : norm(el.textContent));
    if (t && (t === want || t.startsWith(want + ' '))) return {header: true, match: true};
  }
  return {header: true, match: false};
})(""" + json.dumps(str(name)) + ")")


def pass_script(on: bool) -> str:
    """For the instant of one of LittleBot's presses the cover lets pointer events through (it stays opaque)."""
    return ("((on) => { const host = document.querySelector('" + COVER_TAG + r"""');
  if (!host || !host.shadowRoot) return false;
  for (const el of host.shadowRoot.querySelectorAll('[data-pe]')) el.style.pointerEvents = on ? 'none' : el.getAttribute('data-pe');
  return true;
})(""" + ("true" if on else "false") + ")")


def cover_update_script(state: dict[str, Any]) -> str:
    """Hand the cover its new words (``cover_state``). False: there is no cover on the page (put one there)."""
    return ("((st) => { const host = document.querySelector('" + COVER_TAG + r"""');
  if (!host) return false;
  host.setAttribute('data-state', st);
  return true;
})(""" + json.dumps(json.dumps(state, separators=(",", ":"))) + ")")


_COVER = r"""
  if (window.__littlebotCover) return true;
  window.__littlebotCover = true;
  let host = null, bg, ui, qr, title, sub, row, bAnswer, bDecline, bHang, bClose, bX;
  const tell = (type) => {
    if (host) host.setAttribute('data-ask', type);
    try { window[BIND](JSON.stringify({type})); } catch (e) {}
  };
  const css = (el, style, pe) => {
    for (const k of Object.keys(style)) el.style[k] = style[k];
    if (pe) { el.style.pointerEvents = pe; el.setAttribute('data-pe', pe); }
    return el;
  };
  const button = (label, colour, ask) => {
    const b = css(document.createElement('button'), {
      font: '700 30px system-ui, sans-serif', border: '0', borderRadius: '22px', minWidth: '210px',
      minHeight: '92px', padding: '0 34px', color: '#fff', background: colour, touchAction: 'manipulation',
      cursor: 'pointer', margin: '0'}, 'auto');
    b.textContent = label;
    b.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); tell(ask); });
    return b;
  };
  const build = () => {
    host = document.createElement(TAG);
    host.setAttribute('data-state', JSON.stringify(INIT));
    css(host, {position: 'fixed', left: '0', top: '0', width: '100vw', height: '100vh', zIndex: '2147483647',
               display: 'block', margin: '0', padding: '0', border: '0', pointerEvents: 'none'});
    const root = host.attachShadow({mode: 'open'});
    bg = css(document.createElement('div'), {position: 'fixed', left: '0', top: '0', width: '100vw',
                                             height: '100vh'}, 'auto');
    ui = css(document.createElement('div'), {
      position: 'fixed', left: '0', top: '0', width: '100vw', height: '100vh', boxSizing: 'border-box',
      padding: '22px', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
      gap: '16px', textAlign: 'center', font: '600 30px/1.3 system-ui, sans-serif'}, 'none');
    qr = css(document.createElement('canvas'), {display: 'none', background: '#fff', borderRadius: '16px',
                                               imageRendering: 'pixelated'});
    title = css(document.createElement('div'), {fontSize: '44px', maxWidth: '920px'});
    sub = css(document.createElement('div'), {fontSize: '24px', fontWeight: '400', maxWidth: '880px'});
    row = css(document.createElement('div'), {display: 'flex', gap: '24px', marginTop: '8px'});
    bAnswer = button('Answer', '#2fa866', 'answer');
    bDecline = button('Decline', '#e5484d', 'decline');
    bHang = button('Hang up', '#e5484d', 'hangup');
    bClose = button('Close', '#623258', 'close');
    row.append(bAnswer, bDecline, bHang, bClose);
    ui.append(qr, title, sub, row);
    bX = button('×', '#623258', 'close');
    css(bX, {position: 'fixed', top: '10px', right: '10px', minWidth: '76px', width: '76px', minHeight: '76px',
             borderRadius: '38px', padding: '0', fontSize: '40px', display: 'none'});
    root.append(bg, ui, bX);
    for (const el of [bg, ui]) {  // nothing under the cover hears a touch on it
      for (const ev of ['pointerdown', 'pointerup', 'click', 'wheel', 'contextmenu']) {
        el.addEventListener(ev, (e) => { e.stopPropagation(); if (ev === 'contextmenu') e.preventDefault(); });
      }
    }
  };
  const attach = () => {
    const top = document.documentElement;
    if (!top) return false;
    if (!host) build();
    if (host.parentNode !== top || top.lastElementChild !== host) top.appendChild(host);  // always the last, on top
    return true;
  };
  const box = (el, pad) => {
    const r = el.getBoundingClientRect();
    const x = Math.max(0, Math.floor(r.left - pad)), y = Math.max(0, Math.floor(r.top - pad));
    const w = Math.min(innerWidth, Math.ceil(r.right + pad)) - x, h = Math.min(innerHeight, Math.ceil(r.bottom + pad)) - y;
    return w >= 8 && h >= 8 ? {x, y, w, h} : null;
  };
  const tick = () => {
    if (!attach()) return;
    let st = {};
    try { st = JSON.parse(host.getAttribute('data-state') || '{}'); } catch (e) {}
    const on = st.on !== false, dark = st.theme === 'dark';
    const paper = dark ? '#322a44' : '#ffd6e4', ink = dark ? '#f6e9f3' : '#623258';
    bg.style.background = paper;
    ui.style.color = ink;
    for (const b of [bClose, bX]) { b.style.background = ink; b.style.color = paper; }
    host.setAttribute('data-on', on ? '1' : '0');
    bg.style.display = on ? 'block' : 'none';
    ui.style.display = on ? 'flex' : 'none';
    if (!on) { bX.style.display = 'block'; host.setAttribute('data-holes', '0'); return; }
    const holes = [];
    let c = {state: 'none', videos: []};
    try { c = H.call(); } catch (e) {}
    let mirrored = false;
    const code = c.linked || c.state !== 'none' ? null : H.q('qr');
    if (code && code.tagName === 'CANVAS' && code.width > 0 && code.height > 0) {
      try {
        const size = Math.max(200, Math.min(innerHeight - 230, 430));
        if (qr.width !== size) { qr.width = size; qr.height = size; }
        const g = qr.getContext('2d');
        g.imageSmoothingEnabled = false;
        g.fillStyle = '#fff';
        g.fillRect(0, 0, size, size);
        g.drawImage(code, 14, 14, size - 28, size - 28);
        mirrored = true;
      } catch (e) { mirrored = false; }
    }
    if (code && !mirrored) { const b = box(code, 16); if (b) holes.push(b); }
    qr.style.display = mirrored ? 'block' : 'none';
    let own = false, boxed = false;
    if (c.state !== 'none') {
      const b = c.container ? box(c.container, 0) : null;
      if (b) { holes.push(b); boxed = true; }
      else { own = true; for (const v of c.videos) { const vb = box(v, 0); if (vb) holes.push(vb); } }
    }
    bAnswer.style.display = own && c.accept ? '' : 'none';
    bDecline.style.display = own && c.accept ? '' : 'none';
    bHang.style.display = own && !c.accept ? '' : 'none';
    bClose.style.display = c.state === 'none' ? '' : 'none';
    bX.style.display = boxed ? 'block' : 'none';
    const words = !(c.state !== 'none' && holes.length);  // a call's pictures: no words over them
    title.textContent = words ? String(st.title || '') : '';
    sub.textContent = words ? String(st.sub || '') : '';
    ui.style.justifyContent = c.state !== 'none' && holes.length ? 'flex-end' : 'center';
    if (holes.length) {
      let p = 'M0 0H' + innerWidth + 'V' + innerHeight + 'H0Z';
      for (const b of holes) p += 'M' + b.x + ' ' + b.y + 'h' + b.w + 'v' + b.h + 'h' + (-b.w) + 'Z';
      bg.style.clipPath = 'path(evenodd, "' + p + '")';
    } else {
      bg.style.clipPath = '';
    }
    host.setAttribute('data-holes', String(holes.length));
  };
  let touched = 0;
  document.addEventListener('pointerdown', () => {
    const now = Date.now();
    if (now - touched > 5000) { touched = now; try { window[BIND](JSON.stringify({type: 'touch'})); } catch (e) {} }
  }, true);
  try { new MutationObserver(tick).observe(document, {childList: true}); } catch (e) {}
  const watchTop = () => {
    if (!document.documentElement) return false;
    try { new MutationObserver(tick).observe(document.documentElement, {childList: true}); } catch (e) {}
    return true;
  };
  if (!watchTop()) { const t = setInterval(() => { if (watchTop()) clearInterval(t); }, 20); }
  tick();
  setInterval(tick, 200);
  return true;
"""


def cover_script(state: dict[str, Any]) -> str:
    """LittleBot's cover over WhatsApp's page, starting in ``state`` (``cover_state``). Run before the page's own
    scripts on every new document, in the world ``WORLD``; safe to run again (it then does nothing)."""
    return ("((SEL, INIT, BIND, TAG) => {" + _HELPERS + _COVER + "})(" + _sel_json() + ", "
            + json.dumps(state, separators=(",", ":")) + ", " + json.dumps(BINDING) + ", " + json.dumps(COVER_TAG)
            + ")")


# ---------------------------------------------------------------- the cover's words
LINK_TITLE = "Scan this with your phone"
LINK_SUB = "On your phone: WhatsApp, Settings, Linked devices, Link a device."
CLOSE_HINT = "Tap Close, or say “Hey LittleBot, close WhatsApp”."


def cover_state(*, hide: bool = True, theme: str = "light", phase: str = "starting", call: str = "none",
                name: str = "", answer: bool = False, problem: str = "") -> dict[str, Any]:
    """What the cover is and says: ``{on, theme, title, sub}``.

    ``phase``: starting | loading | linking | ready. ``call``: none | incoming | in_call. ``name``: the said name of
    who is being called (one on the parent's list). ``problem``: what went wrong; it stays covered and says so."""
    out: dict[str, Any] = {"on": bool(hide), "theme": "dark" if theme == "dark" else "light", "title": "", "sub": ""}
    if problem:
        out.update(title=str(problem)[:120], sub=CLOSE_HINT)
    elif call == "incoming":
        out.update(title="Someone is calling", sub="Tap Answer, or say “Hey LittleBot, answer”.")
    elif call == "in_call":
        out.update(title=f"On a call with {name}" if name else "On a call",
                   sub="Say “Hey LittleBot, hang up” to end it.")
    elif phase == "linking":
        out.update(title=LINK_TITLE, sub=LINK_SUB)
    elif phase in ("starting", "loading"):
        out.update(title="Opening WhatsApp…", sub="")
    elif name:
        out.update(title=f"Calling {name}…", sub="")
    elif answer:
        out.update(title="Waiting for the call…", sub="It shows here while your phone rings.")
    else:
        out.update(title="WhatsApp is ready", sub="Say “Hey LittleBot, call” and a name, or answer a call here. "
                                                  "Your chats stay hidden.")
    return out


def loading_url(theme: str = "light", text: str = "Opening WhatsApp…") -> str:
    """A calm page in LittleBot's colours while Chromium starts (the launch address; the only data: page shown)."""
    paper, ink = ("#322a44", "#f6e9f3") if theme == "dark" else ("#ffd6e4", "#623258")
    body = (f"<!doctype html><meta charset=utf-8><title>WhatsApp</title>"
            f"<body style=\"margin:0;height:100vh;display:flex;align-items:center;justify-content:center;"
            f"background:{paper};color:{ink};font:600 44px sans-serif\">{html.escape(text)}</body>")
    return "data:text/html;charset=utf-8," + quote(body)


__all__ = ["BINDING", "COVER_TAG", "KEYS", "SELECTORS", "WORLD", "check_script", "cover_script", "cover_state",
           "cover_update_script", "find_chat_script", "header_script", "loading_url", "pass_script", "state_script"]
