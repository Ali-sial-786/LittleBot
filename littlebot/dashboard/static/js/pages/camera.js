// Camera: a live view from LittleBot's camera, for the logged-in parent only.
// The page asks for one small JPEG after another (each request waits for the next new frame), draws it on a
// canvas and lets it go: nothing is saved, on the robot or here. LittleBot shows a small camera sign on its own
// screen while anyone watches. The view stops when you press Stop, leave the page, switch apps or lock the phone.

import { get } from '../api.js';
import { button, clear, h, icon, pageHead, setText, toast } from '../ui.js';

const FPS_WINDOW_MS = 3000;
const FRAME_TIMEOUT_MS = 8000; // the robot answers within ~2 s (a frame, or why there is none)
const RETRY_MS = { paused: 3000, no_camera: 3000, timeout: 300, network: 2500 };
const NEEDS_PARENT = new Set(['camera_off', 'busy', 'unavailable']); // stop trying by ourselves
const STATUS_POLL_MS = 3000;

// Messages shown over the picture area: [icon, title, text]
const MESSAGES = {
  idle: ['camera', 'Press “Start live view” to see what LittleBot sees.', 'Nothing is recorded.'],
  connecting: ['camera', 'Connecting to the camera…', ''],
  camera_off: ['lock', 'Camera is off (Privacy)', 'Turn the camera on in Privacy to watch.'],
  paused: ['pause', 'Paused while a video plays on LittleBot', 'The picture comes back by itself when the video ends.'],
  no_camera: ['alert', 'No camera connected', 'Check the camera’s USB cable. LittleBot keeps trying.'],
  busy: ['alert', 'Too many devices are watching', 'Close the camera view on another phone or computer.'],
  timeout: ['clock', 'Waiting for the camera…', ''],
  network: ['wifi', 'Can’t reach LittleBot', 'Retrying…'],
  unavailable: ['alert', 'The live view isn’t available on this robot', ''],
  unsupported: ['alert', 'This browser can’t show the live view', 'Try a recent Chrome, Samsung Internet, Firefox or Safari.'],
};

function newViewerId() {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes); // fine over plain http on the home network (randomUUID is not)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

const sleep = (ms, onTimer) => new Promise((resolve) => { onTimer(setTimeout(resolve, ms), resolve); });

export default {
  mount(view, app) {
    let viewer = newViewerId(); // a fresh id for every Start, so a stopped view can't come back by itself
    let running = false; // the parent pressed Start (and not Stop)
    let looping = false;
    let seq = 0;
    let state = 'idle';
    let inflight = null; // AbortController of the pending frame request
    let waitTimer = null, wakeWait = null;
    let info = null;
    const stamps = [];
    const supported = typeof createImageBitmap === 'function' && typeof fetch === 'function';

    const canvas = h('canvas', { class: 'cam-canvas', width: 480, height: 360, role: 'img', 'aria-label': 'Live picture from LittleBot’s camera' });
    const g = canvas.getContext('2d');
    const msgIcon = h('span', { class: 'cam-msg-icon' });
    const msgTitle = h('strong');
    const msgText = h('span');
    const msgWhy = h('span', { class: 'cam-why' });
    const msgLink = h('a', { href: '#/privacy', text: 'Open Privacy', hidden: true });
    const overlay = h('div', { class: 'cam-msg', role: 'status', 'aria-live': 'polite' }, msgIcon, msgTitle, msgText, msgWhy, msgLink);
    const liveTag = h('span', { class: 'cam-live', hidden: true }, h('span', { class: 'cam-dot' }), 'LIVE');
    const fpsTag = h('span', { class: 'cam-fps tnum', hidden: true, 'aria-label': 'Frame rate' });
    const stage = h('div', { class: 'cam-stage' }, canvas, overlay, liveTag, fpsTag);
    const badge = h('span', { class: 'badge' });
    const startBtn = button('Start live view', { kind: 'primary', iconName: 'play', onClick: () => start() });
    const stopBtn = button('Stop live view', { kind: 'soft', iconName: 'square', onClick: () => stop() });
    const snapBtn = button('Take snapshot', { kind: 'soft', iconName: 'camera', onClick: () => snapshot() });
    const facts = h('dl', { class: 'kv' });

    view.append(
      pageHead('Camera', 'See what LittleBot sees, live, on this phone or computer. Nothing is recorded.'),
      h('div', { class: 'grid two' },
        h('section', { class: 'card cam-card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('camera'), 'Live view'), badge),
          stage,
          h('div', { class: 'btn-row mt-2' }, startBtn, stopBtn, snapBtn)),
        h('section', { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('shield'), 'While you watch')),
          h('ul', { class: 'cam-notes' },
            h('li', { text: 'LittleBot shows a small camera sign in the corner of its screen while anyone watches, so watching is never hidden.' }),
            h('li', { text: 'Nothing is recorded. Each picture goes only to this page and is thrown away. A snapshot is saved on this device only, and only when you tap the button.' }),
            h('li', { text: 'The view stops by itself when you leave this page, switch apps or lock your phone.' }),
            h('li', { text: 'While a video plays on LittleBot, the view pauses so the video stays smooth.' })),
          h('div', { class: 'divider mt-2' }),
          h('div', { class: 'mt-2' }, facts),
          h('p', { class: 'muted mt-2' }, 'To stop all camera use, turn the camera off in ', h('a', { href: '#/privacy', text: 'Privacy' }), '.'))));

    // ------------------------------------------------------------- picture area
    function show(next, why = '') {
      state = next;
      const live = next === 'live';
      overlay.hidden = live;
      liveTag.hidden = !live;
      fpsTag.hidden = !live;
      if (!live) {
        g.clearRect(0, 0, canvas.width, canvas.height); // never leave an old picture looking "live"
        stamps.length = 0;
        const [ico, title, text] = MESSAGES[next] || MESSAGES.unavailable;
        clear(msgIcon).append(icon(ico));
        setText(msgTitle, title);
        setText(msgText, text);
        setText(msgWhy, why);
        msgWhy.hidden = !why;
        msgLink.hidden = next !== 'camera_off';
      }
      sync();
    }

    function sync() {
      startBtn.hidden = running;
      stopBtn.hidden = !running;
      snapBtn.disabled = !(running && state === 'live');
      startBtn.disabled = !supported || info?.blocked === 'camera_off' || info?.available === false;
      if (running && state === 'live') setBadge('ok', 'Live');
      else if (info) setBadge(...badgeFor(info));
    }

    function setBadge(cls, label) {
      badge.className = `badge ${cls}`;
      setText(badge, label);
    }

    function badgeFor(d) {
      if (d.blocked === 'camera_off') return ['', 'Turned off'];
      if (d.blocked === 'paused') return ['warn', 'Paused'];
      if (d.blocked === 'no_camera') return ['danger', d.camera === 'disconnected' ? 'Disconnected' : 'No camera'];
      if (d.blocked === 'unavailable') return ['', 'Not available'];
      if (d.simulated) return ['warn', 'Simulated'];
      if (d.camera === 'ok') return ['ok', 'Ready'];
      return ['', 'Starting…'];
    }

    function tick() {
      const now = performance.now();
      stamps.push(now);
      while (stamps.length && now - stamps[0] > FPS_WINDOW_MS) stamps.shift();
      const span = (stamps[stamps.length - 1] - stamps[0]) / 1000;
      setText(fpsTag, stamps.length > 1 && span > 0 ? `${((stamps.length - 1) / span).toFixed(1)} fps` : '… fps');
    }

    async function draw(blob) {
      const bmp = await createImageBitmap(blob);
      if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
        canvas.width = bmp.width;
        canvas.height = bmp.height;
      }
      g.drawImage(bmp, 0, 0);
      if (bmp.close) bmp.close();
    }

    // ------------------------------------------------------------- frame loop
    async function loop() {
      if (looping) return;
      looping = true;
      try {
        while (running && !document.hidden) {
          const ctl = new AbortController();
          inflight = ctl;
          const timer = setTimeout(() => ctl.abort(), FRAME_TIMEOUT_MS);
          let res = null;
          try {
            res = await fetch(`/api/camera/frame?viewer=${viewer}&after=${seq}`, { credentials: 'same-origin', cache: 'no-store', signal: ctl.signal });
          } catch { res = null; } finally { clearTimeout(timer); inflight = null; }
          if (!running || document.hidden) break;
          if (res && res.ok) {
            try {
              const blob = await res.blob();
              if (!running) break;
              await draw(blob);
              const n = Number(res.headers.get('X-Frame-Seq'));
              seq = Number.isFinite(n) && n > 0 ? n : seq + 1;
              if (state !== 'live') { show('live'); setTimeout(refresh, 400); } // "Watching now" right away
              tick();
            } catch { /* one broken frame: just ask for the next */ }
            continue;
          }
          let code = 'network', why = '';
          if (res) {
            let data = null;
            try { data = await res.json(); } catch { data = null; }
            if (res.status === 401) { running = false; show('idle'); app.refresh(); break; } // logs in again
            code = data?.code || 'unavailable';
            why = code === 'no_camera' || code === 'busy' ? data?.why || '' : '';
            if (code === 'stopped') { viewer = newViewerId(); seq = 0; continue; } // an old request: start afresh
          }
          show(code, why);
          if (NEEDS_PARENT.has(code)) { running = false; break; }
          await sleep(RETRY_MS[code] ?? 2000, (t, wake) => { waitTimer = t; wakeWait = wake; });
          waitTimer = null;
          wakeWait = null;
        }
      } finally {
        looping = false;
        sync();
      }
    }

    function start() {
      if (!supported) { show('unsupported'); return; }
      if (running) return;
      viewer = newViewerId();
      seq = 0;
      running = true;
      show('connecting');
      loop();
    }

    function interrupt() {
      if (inflight) inflight.abort();
      if (waitTimer) clearTimeout(waitTimer);
      if (wakeWait) wakeWait();
    }

    function tellRobot() {
      // keepalive: still delivered when the page is being hidden or closed
      const token = app.session?.csrf_token;
      try {
        fetch('/api/camera/stop', {
          method: 'POST', credentials: 'same-origin', keepalive: true, body: JSON.stringify({ viewer }),
          headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...(token ? { 'X-CSRF-Token': token } : {}) },
        }).catch(() => {});
      } catch { /* the robot forgets the viewer after a few seconds anyway */ }
    }

    function stop(message = 'idle') {
      const was = running;
      running = false;
      interrupt();
      if (was) { tellRobot(); setTimeout(refresh, 400); }
      show(message === 'idle' && info?.blocked === 'camera_off' ? 'camera_off' : message);
    }

    async function snapshot() {
      if (!(running && state === 'live')) { toast('Start the live view first.', 'error'); return; }
      const token = app.session?.csrf_token;
      let res;
      try {
        res = await fetch('/api/camera/snapshot', {
          method: 'POST', credentials: 'same-origin', cache: 'no-store', body: JSON.stringify({ viewer }),
          headers: { 'Content-Type': 'application/json', ...(token ? { 'X-CSRF-Token': token } : {}) },
        });
      } catch { toast('Can’t reach LittleBot.', 'error'); return; }
      if (!res.ok) {
        let data = null;
        try { data = await res.json(); } catch { data = null; }
        toast(data?.detail || 'LittleBot couldn’t take a snapshot right now.', 'error');
        return;
      }
      const blob = await res.blob();
      const name = ((res.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/) || [])[1] || 'littlebot-snapshot.jpg';
      const url = URL.createObjectURL(blob);
      const a = h('a', { href: url, download: name, hidden: true });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      toast('Snapshot saved on this device (not on LittleBot).', 'ok');
    }

    // ------------------------------------------------------------- status
    function fill(d) {
      info = d;
      const lv = d.live_view || {};
      const others = Math.max(0, (lv.viewers || 0) - (running ? 1 : 0));
      const rows = [
        ['Camera', d.simulated ? 'Pretend camera (no real camera connected)' : badgeFor(d)[1]],
        ['Picture', `${lv.width || 480} px wide, up to ${lv.max_fps || 8} frames a second`],
        ['Watching now', lv.viewers ? `${lv.viewers} device${lv.viewers === 1 ? '' : 's'}${running && others ? ` (you and ${others} more)` : running ? ' (this one)' : ''}` : 'Nobody'],
      ];
      if (running && lv.encode_ms != null) rows.push(['Work on LittleBot', `${lv.encode_ms} ms per picture${lv.frame_kb ? `, ${lv.frame_kb} KB` : ''}`]);
      clear(facts);
      for (const [k, v] of rows) facts.append(h('dt', { text: k }), h('dd', { text: v }));
      if (!running) {
        if (d.blocked === 'camera_off') show('camera_off');
        else if (d.available === false && state === 'idle') show('unavailable');
        else if (state === 'camera_off') show('idle');
        if (state === 'idle') {
          setText(msgText, d.blocked === 'paused' ? 'A video is playing on LittleBot: the picture shows when it ends.'
            : d.blocked === 'no_camera' ? 'No camera is connected right now.' : MESSAGES.idle[2]);
        }
      }
      sync();
    }

    async function refresh() {
      if (document.hidden) return;
      try { fill(await get('/camera', { timeout: 8000 })); } catch { /* the shell shows when LittleBot is unreachable */ }
    }

    // ------------------------------------------------------------- page life
    function onVisibility() {
      if (document.hidden) {
        if (running) { interrupt(); tellRobot(); viewer = newViewerId(); seq = 0; } // stop sending while unseen
      } else {
        refresh();
        if (running) { show('connecting'); loop(); }
      }
    }
    function onPageHide() { if (running) tellRobot(); }
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('pagehide', onPageHide);
    const statusTimer = setInterval(refresh, STATUS_POLL_MS);

    show(supported ? 'idle' : 'unsupported');
    refresh();

    return {
      destroy() {
        clearInterval(statusTimer);
        document.removeEventListener('visibilitychange', onVisibility);
        window.removeEventListener('pagehide', onPageHide);
        if (running) { running = false; interrupt(); tellRobot(); }
      },
    };
  },
};
