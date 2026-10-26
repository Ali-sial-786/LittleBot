"""What LittleBot puts into the pages its browser view shows, and the two small pages of its own.

* ``strip_script(binding, eye)`` - LittleBot's floating strip, added to every page (DevTools
  ``Page.addScriptToEvaluateOnNewDocument`` in an isolated world, so the page's own scripts can't see or call
  into it): the face's mini eyes (rim, gradient, big pupil, two catchlights, a blink now and then: the look of
  ``littlebot/games/art.py`` ``draw_eyes``) and a big × that closes the view. It also tells LittleBot about
  real touches (for the idle timer), opens links meant for a new window in the same view, and keeps the
  long-press menu away.
* ``MAIN_WORLD_SCRIPT``          - in the page's own world: no new windows, no print dialog.
* ``READ_SCRIPT``, ``RESULTS_SCRIPT``, ``SCROLL_SCRIPT`` - run on demand (``read()``, ``open_result``, ``scroll``)
* ``loading_url()``, ``blocked_page(why)`` - the calm pink page while Chromium starts, and the page shown
  instead of one LittleBot won't open.
"""

from __future__ import annotations

import html
import json
from urllib.parse import quote

BINDING = "littlebotView"  # Runtime.addBinding name, only in WORLD
WORLD = "littlebot"  # the isolated world the strip runs in

PINK = "#ffd6e4"
INK = "#623258"  # games' art.INK (98, 50, 88)
PUPIL = (10, 22, 40)  # art.PUPIL


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> str:
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def _lighten(a: tuple[int, int, int], t: float) -> str:
    return _mix(a, (255, 255, 255), t)


def eyes_svg(eye: tuple[int, int, int], *, w: int = 22, h: int = 29, gap: int = 14) -> str:
    """Two eyes as SVG, the way the games draw them (art.eye_body + pupil + catchlights)."""
    rim, top, bottom = _mix(eye, PUPIL, 0.42), _mix(eye, PUPIL, 0.18), _lighten(eye, 0.32)
    pupil = _mix(PUPIL, PUPIL, 0)  # as a colour
    total = w * 2 + gap
    parts = [f'<svg class="eyes" viewBox="0 0 {total} {h}" width="{total}" height="{h}" aria-hidden="true">',
             (f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{top}"/>'
              f'<stop offset="1" stop-color="{bottom}"/></linearGradient></defs>')]
    r = min(w, h) * 0.48
    inset = w * 0.1
    for i in range(2):
        x = i * (w + gap)
        cx, cy = x + w / 2, h / 2
        parts.append(
            f'<g class="eye"><rect x="{x}" y="0" width="{w}" height="{h}" rx="{r:.1f}" fill="{rim}"/>'
            f'<rect x="{x + inset / 2:.1f}" y="{inset / 2:.1f}" width="{w - inset:.1f}" height="{h - inset:.1f}" '
            f'rx="{r - w * 0.05:.1f}" fill="url(#g)"/>'
            f'<circle class="pupil" cx="{cx:.1f}" cy="{cy:.1f}" r="{w * 0.34:.1f}" fill="{pupil}"/>'
            f'<circle cx="{cx - w * 0.2:.1f}" cy="{cy - h * 0.22:.1f}" r="{w * 0.15:.1f}" fill="#fff"/>'
            f'<circle cx="{cx + w * 0.17:.1f}" cy="{cy + h * 0.12:.1f}" r="{w * 0.07:.1f}" fill="#fff"/></g>')
    parts.append("</svg>")
    return "".join(parts)


_STRIP_CSS = f"""
:host {{ all: initial; }}
.strip {{ position: fixed; top: 6px; right: 6px; z-index: 2147483647; display: flex; align-items: center;
  gap: 8px; padding: 6px 6px 6px 12px; border-radius: 40px; background: {PINK}; border: 3px solid #fff;
  box-shadow: 0 2px 10px rgba(98, 50, 88, .35); font-family: sans-serif; touch-action: manipulation;
  -webkit-user-select: none; user-select: none; }}
.eyes {{ display: block; }}
.eye {{ transform-box: fill-box; transform-origin: center; animation: blink 5.5s infinite; }}
.pupil {{ animation: glance 9s infinite ease-in-out; }}
@keyframes blink {{ 0%, 94%, 100% {{ transform: scaleY(1); }} 97% {{ transform: scaleY(.12); }} }}
@keyframes glance {{ 0%, 40%, 100% {{ transform: translate(0, 0); }} 50%, 70% {{ transform: translate(-3px, 2px); }}
  80%, 90% {{ transform: translate(2px, 1px); }} }}
button {{ all: initial; box-sizing: border-box; width: 64px; height: 64px; border-radius: 50%; background: #fff;
  border: 3px solid {INK}; color: {INK}; font: 700 44px/56px sans-serif; text-align: center; cursor: pointer; }}
button:active {{ background: {INK}; color: #fff; }}
"""


def strip_script(eye: tuple[int, int, int] = (90, 210, 255), *, binding: str = BINDING) -> str:
    """The strip with LittleBot's eyes and a close button, plus touch reporting (runs in ``WORLD``)."""
    markup = f'<style>{_STRIP_CSS}</style><div class="strip">{eyes_svg(eye)}' \
             '<button type="button" aria-label="Close">×</button></div>'
    return """(() => {
  if (window.top !== window) return;
  const send = (type) => { try { window[%(binding)s](JSON.stringify({type})); } catch (e) {} };
  let host = null;
  const mount = () => {
    if (host && host.isConnected) return;
    host = document.createElement('littlebot-strip');
    const root = host.attachShadow({mode: 'closed'});
    root.innerHTML = %(markup)s;
    root.querySelector('button').addEventListener('click', (e) => {
      if (e.isTrusted) { e.preventDefault(); e.stopPropagation(); send('close'); }
    }, true);
    (document.body || document.documentElement).appendChild(host);
  };
  const start = () => { mount(); setInterval(mount, 2000); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
  let last = 0;
  const touched = (e) => {
    if (!e.isTrusted) return;
    const now = Date.now();
    if (now - last > 2000) { last = now; send('touch'); }
  };
  for (const t of ['pointerdown', 'touchstart', 'wheel', 'keydown'])
    addEventListener(t, touched, {capture: true, passive: true});
  addEventListener('click', (e) => {
    if (!e.isTrusted || !e.target || !e.target.closest) return;
    const a = e.target.closest('a[target]');
    if (a && a.href && a.target.toLowerCase() !== '_self' && a.target.toLowerCase() !== '_top') {
      e.preventDefault(); location.href = a.href;
    }
  }, true);
  addEventListener('contextmenu', (e) => e.preventDefault(), true);
})();""" % {"binding": json.dumps(binding), "markup": json.dumps(markup)}


MAIN_WORLD_SCRIPT = """(() => {
  try {
    Object.defineProperty(window, 'open', {value: function () { return null; }, configurable: false});
    Object.defineProperty(window, 'print', {value: function () {}, configurable: false});
  } catch (e) {}
})();"""

# The page's main text for the voice: on Google (or Bing) the results column, else <main> / <article> / the body.
READ_SCRIPT = """(() => {
  const google = /(^|\\.)google\\.[a-z.]+$/.test(location.hostname);
  const bing = /(^|\\.)bing\\.com$/.test(location.hostname);
  const pick = (sels) => { for (const s of sels) { const el = document.querySelector(s); if (el) return el; } return null; };
  const el = (google && pick(['#center_col', '#search', '#rso', '#main'])) || (bing && pick(['#b_results'])) ||
    pick(['main', 'article', '[role=main]']) || document.body;
  const text = el ? (el.innerText || el.textContent || '') : '';
  return {title: document.title || '', host: location.hostname, google, text};
})()"""

# The organic results on a Google (or Bing) results page, in order (ads left out): [{href, title}].
RESULTS_SCRIPT = """(() => {
  const out = [], seen = new Set();
  const heads = document.querySelectorAll('#rso a h3, #search a h3, #rso a [role=heading], #main a h3, ' +
                                          '#main a [role=heading], #b_results li.b_algo h2 a');
  for (const h of heads) {
    const a = h.closest('a');
    if (!a || !a.href || a.closest('#tads, #tadsb, #bottomads, [data-text-ad], [aria-label="Ads"]')) continue;
    let u; try { u = new URL(a.href, location.href); } catch (e) { continue; }
    if (!/^https?:$/.test(u.protocol) || seen.has(u.href)) continue;
    seen.add(u.href);
    out.push({href: u.href, title: (h.innerText || '').trim().slice(0, 120)});
    if (out.length >= 20) break;
  }
  return out;
})()"""

# Scroll by a share of the screen (negative: up), or to the top / bottom; says where it ended up.
SCROLL_SCRIPT = """((share, edge) => {
  const el = document.scrollingElement || document.documentElement;
  const max = Math.max(0, el.scrollHeight - el.clientHeight);
  let top = edge === 'top' ? 0 : edge === 'bottom' ? max : el.scrollTop + share * window.innerHeight;
  top = Math.max(0, Math.min(max, top));
  el.scrollTo({top, behavior: 'smooth'});
  return {top: Math.round(top), max: Math.round(max)};
})"""


def loading_url(text: str = "Looking it up…") -> str:
    """A calm pink page while Chromium starts (the launch address; the only data: page ever shown)."""
    body = (f"<!doctype html><meta charset=utf-8><title>LittleBot</title>"
            f"<body style=\"margin:0;height:100vh;display:flex;align-items:center;justify-content:center;"
            f"background:{PINK};color:{INK};font:600 34px sans-serif\">{html.escape(text)}</body>")
    return "data:text/html;charset=utf-8," + quote(body)


def blocked_page(why: str) -> str:
    """The page shown instead of one LittleBot won't open (an http page, a local address ...)."""
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content=\"width=device-width\">"
            f"<title>LittleBot</title><body style=\"margin:0;padding:48px 24px;background:{PINK};color:{INK};"
            f"font:600 26px/1.4 sans-serif\"><p>LittleBot won't open that page.</p>"
            f"<p style=\"font-weight:400;font-size:20px\">{html.escape(why)} Say “go back”, or tap "
            f"× to close the browser.</p></body>")


__all__ = ["BINDING", "MAIN_WORLD_SCRIPT", "READ_SCRIPT", "RESULTS_SCRIPT", "SCROLL_SCRIPT", "WORLD",
           "blocked_page", "eyes_svg", "loading_url", "strip_script"]
