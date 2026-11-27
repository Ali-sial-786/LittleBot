// A live camera picture for any page (Who's who uses it to line people up for face samples): the same robot
// endpoints as the Camera page. One stream carries JPEG after JPEG (/api/camera/stream: 8 bytes - length and
// sequence number, big-endian - then the JPEG), so there's no Wi-Fi round trip per frame; the newest complete
// frame is drawn and the rest skipped. When the stream ends or isn't there (an older robot), one plain
// /api/camera/frame request brings a frame or says why there is none. Nothing is kept. The robot shows its
// camera sign while this runs; it stops when the page is hidden or left, and tells the robot at once.

const FRAME_TIMEOUT_MS = 8000;
const RETRY_MS = { paused: 3000, no_camera: 3000, timeout: 300, network: 2500 };
const NEEDS_PARENT = new Set(['camera_off', 'busy', 'unavailable']); // stop trying by ourselves

function newViewerId() {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

// canvas: where frames are drawn. onState(code, detail): 'connecting' | 'live' | 'stopped' | a reason code from the
// robot (camera_off, paused, no_camera, busy, timeout, network, unavailable). onFrame(): after each frame drawn.
export function liveStream(app, canvas, { onState = () => {}, onFrame = () => {} } = {}) {
  const g = canvas.getContext('2d');
  let viewer = newViewerId();
  let seq = 0;
  let running = false;
  let looping = false;
  let canStream = true;
  let inflight = null;
  let wake = null;

  const supported = typeof window.createImageBitmap === 'function' && typeof AbortController === 'function';

  async function draw(blob) {
    const bmp = await createImageBitmap(blob);
    if (canvas.width !== bmp.width || canvas.height !== bmp.height) { canvas.width = bmp.width; canvas.height = bmp.height; }
    g.drawImage(bmp, 0, 0);
    if (bmp.close) bmp.close();
  }

  function shown(n) {
    seq = Number.isFinite(n) && n > 0 ? n : seq + 1;
    onState('live');
    onFrame();
  }

  async function streamFrames() {
    const ctl = new AbortController();
    inflight = ctl;
    let timer = setTimeout(() => ctl.abort(), FRAME_TIMEOUT_MS);
    const kick = () => { clearTimeout(timer); timer = setTimeout(() => ctl.abort(), FRAME_TIMEOUT_MS); };
    let count = 0;
    try {
      const res = await fetch(`/api/camera/stream?viewer=${viewer}&after=${seq}`, { credentials: 'same-origin', cache: 'no-store', signal: ctl.signal });
      if (res.status === 404 || res.status === 405) return null;
      if (!res.ok) return 0;
      if (!res.body || !(res.headers.get('content-type') || '').startsWith('application/octet-stream')) return null;
      const reader = res.body.getReader();
      let buf = new Uint8Array(0);
      for (;;) {
        const { value, done } = await reader.read();
        if (done || !running || document.hidden) break;
        kick();
        const joined = new Uint8Array(buf.length + value.length);
        joined.set(buf);
        joined.set(value, buf.length);
        buf = joined;
        let newest = null;
        while (buf.length >= 8) {
          const dv = new DataView(buf.buffer, buf.byteOffset, 8);
          const len = dv.getUint32(0);
          if (buf.length < 8 + len) break;
          newest = { jpeg: buf.slice(8, 8 + len), n: dv.getUint32(4) };
          buf = buf.slice(8 + len);
        }
        if (!newest) continue;
        try { await draw(new Blob([newest.jpeg], { type: 'image/jpeg' })); } catch { continue; }
        shown(newest.n);
        count += 1;
      }
      reader.cancel().catch(() => {});
    } catch { /* aborted or the connection dropped */ } finally {
      clearTimeout(timer);
      if (inflight === ctl) inflight = null;
    }
    return count;
  }

  async function loop() {
    if (looping) return;
    looping = true;
    try {
      while (running && !document.hidden) {
        if (canStream) {
          const n = await streamFrames();
          if (!running || document.hidden) break;
          if (n === null) canStream = false;
        }
        const ctl = new AbortController();
        inflight = ctl;
        const timer = setTimeout(() => ctl.abort(), FRAME_TIMEOUT_MS);
        let res = null;
        try {
          res = await fetch(`/api/camera/frame?viewer=${viewer}&after=${seq}`, { credentials: 'same-origin', cache: 'no-store', signal: ctl.signal });
        } catch { res = null; } finally { clearTimeout(timer); inflight = null; }
        if (!running || document.hidden) break;
        if (res && res.ok) {
          try { await draw(await res.blob()); shown(Number(res.headers.get('X-Frame-Seq'))); } catch { /* next one */ }
          continue;
        }
        let code = 'network', why = '';
        if (res) {
          let data = null;
          try { data = await res.json(); } catch { data = null; }
          if (res.status === 401) { running = false; onState('stopped'); app.refresh(); break; }
          code = data?.code || 'unavailable';
          why = data?.detail || '';
          if (code === 'stopped') { viewer = newViewerId(); seq = 0; continue; }
        }
        onState(code, why);
        if (NEEDS_PARENT.has(code)) { running = false; break; }
        await new Promise((resolve) => { const t = setTimeout(resolve, RETRY_MS[code] ?? 2000); wake = () => { clearTimeout(t); resolve(); }; });
        wake = null;
      }
    } finally {
      looping = false;
    }
  }

  function tellRobot() {
    const token = app.session?.csrf_token;
    try {
      fetch('/api/camera/stop', {
        method: 'POST', credentials: 'same-origin', keepalive: true, body: JSON.stringify({ viewer }),
        headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...(token ? { 'X-CSRF-Token': token } : {}) },
      }).catch(() => {});
    } catch { /* the robot forgets the viewer after a few seconds anyway */ }
  }

  function interrupt() {
    if (inflight) inflight.abort();
    if (wake) wake();
  }

  function onVisibility() {
    if (!running) return;
    if (document.hidden) { interrupt(); tellRobot(); viewer = newViewerId(); seq = 0; } else { onState('connecting'); loop(); }
  }
  function onPageHide() { if (running) tellRobot(); }
  document.addEventListener('visibilitychange', onVisibility);
  window.addEventListener('pagehide', onPageHide);

  return {
    supported,
    get running() { return running; },
    start() {
      if (!supported || running) return;
      viewer = newViewerId();
      seq = 0;
      running = true;
      onState('connecting');
      loop();
    },
    stop() {
      if (!running) return;
      running = false;
      interrupt();
      tellRobot();
      onState('stopped');
    },
    destroy() {
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('pagehide', onPageHide);
      if (running) { running = false; interrupt(); tellRobot(); }
    },
  };
}
