// Drive: LittleBot's wheels from the dashboard, beside the live camera (pages/camera.js).
// Hold an arrow (or W A S D / the arrow keys) to drive: the page re-sends the command every 250 ms and the robot
// stops by itself within a second when it stops coming (a closed tab, a lost Wi-Fi, a locked phone). Letting go
// sends Stop at once. Quick moves drive a set distance or turn a set angle. Real wheels move only once a parent
// has armed the motors on the Motion page; the emergency stop here is the same as there.

import { ApiError, get, post } from '../api.js';
import { button, clear, h, icon, setText, toast } from '../ui.js';

const RESEND_MS = 250;
const STATUS_MS = 2000;
// [linear, angular] for each direction: + forwards, + turning left (the robot's own left)
const DIRS = {
  up: [1, 0], down: [-1, 0], left: [0, 1], right: [0, -1],
  upleft: [1, 0.6], upright: [1, -0.6], downleft: [-1, -0.6], downright: [-1, 0.6],
};
const KEYS = { ArrowUp: 'up', KeyW: 'up', ArrowDown: 'down', KeyS: 'down', ArrowLeft: 'left', KeyA: 'left', ArrowRight: 'right', KeyD: 'right' };

export function drivePanel(app) {
  let speed = 0.6; // share of the speed limit
  let held = null; // the direction held now
  let timer = null;
  let warned = false; // one toast per hold, not one every 250 ms
  const keys = new Set();

  const badge = h('span', { class: 'badge' });
  const note = h('p', { class: 'muted small' });
  const armLink = h('a', { href: '#/motion', text: 'Arm the motors on the Motion page', hidden: true });
  const pose = h('p', { class: 'muted small tnum' });

  const pad = h('div', { class: 'drive-pad', role: 'group', 'aria-label': 'Drive LittleBot: hold a button' });
  const padBtn = (dir, ico, label) => {
    const b = h('button', { type: 'button', class: `drive-btn drive-${dir}`, 'aria-label': label }, icon(ico));
    b.addEventListener('pointerdown', (e) => { e.preventDefault(); b.setPointerCapture?.(e.pointerId); hold(dir); });
    for (const ev of ['pointerup', 'pointercancel', 'lostpointercapture']) b.addEventListener(ev, () => release(dir));
    b.addEventListener('contextmenu', (e) => e.preventDefault()); // a long press on a phone isn't a menu
    return b;
  };
  const stopBtn = h('button', { type: 'button', class: 'drive-btn drive-stop', 'aria-label': 'Stop' }, icon('square'));
  stopBtn.addEventListener('click', () => { held = null; halt(); });
  pad.append(
    padBtn('upleft', 'arrowUpLeft', 'Forward and left'), padBtn('up', 'arrowUp', 'Forward'), padBtn('upright', 'arrowUpRight', 'Forward and right'),
    padBtn('left', 'rotateLeft', 'Turn left'), stopBtn, padBtn('right', 'rotateRight', 'Turn right'),
    padBtn('downleft', 'arrowDownLeft', 'Back and left'), padBtn('down', 'arrowDown', 'Back'), padBtn('downright', 'arrowDownRight', 'Back and right'));

  const speedOut = h('output', { class: 'tnum' });
  const speedIn = h('input', { type: 'range', min: 20, max: 100, step: 5, value: speed * 100, 'aria-label': 'Speed' });
  const showSpeed = () => setText(speedOut, `${Math.round(speed * 100)}%`);
  speedIn.addEventListener('input', () => { speed = Number(speedIn.value) / 100; showSpeed(); });
  showSpeed();

  const cmIn = h('input', { type: 'number', min: 1, max: 200, step: 1, value: 50, class: 'drive-cm', 'aria-label': 'Distance in centimetres' });
  const move = (m) => app.act(() => post('/motion/drive/move', { meters: m }));
  const turn = (d) => app.act(() => post('/motion/drive/turn', { degrees: d }));
  const quick = h('div', { class: 'stack-sm' },
    h('div', { class: 'btn-row' },
      button('Forward', { kind: 'soft', iconName: 'arrowUp', onClick: () => move(Math.min(200, Math.max(1, Number(cmIn.value) || 50)) / 100) }),
      button('Back', { kind: 'soft', iconName: 'arrowDown', onClick: () => move(-Math.min(200, Math.max(1, Number(cmIn.value) || 50)) / 100) }),
      h('label', { class: 'drive-cm-wrap' }, cmIn, ' cm')),
    h('div', { class: 'btn-row' },
      button('Left 90°', { kind: 'soft', iconName: 'rotateLeft', onClick: () => turn(90) }),
      button('Right 90°', { kind: 'soft', iconName: 'rotateRight', onClick: () => turn(-90) }),
      button('Turn around', { kind: 'soft', iconName: 'refresh', onClick: () => turn(180) })));

  const estop = h('button', { type: 'button', class: 'estop-big' }, icon('stop'), 'EMERGENCY STOP');
  estop.addEventListener('click', () => { held = null; halt(); app.act(() => post('/motion/estop'), 'Emergency stop: motors off.').then(refresh); });

  const el = h('section', { class: 'card drive-card' },
    h('div', { class: 'card-head' }, h('h2', {}, icon('robot'), 'Drive'), badge),
    note, armLink,
    pad,
    h('div', { class: 'field mt-2' }, h('label', {}, 'Speed ', speedOut), speedIn),
    h('p', { class: 'muted small', text: 'Hold a button (or W A S D / the arrow keys) to drive; let go to stop.' }),
    h('div', { class: 'divider mt-2' }),
    h('h3', { class: 'mt-2', text: 'Move a set amount' }), quick,
    pose,
    h('div', { class: 'mt-2' }, estop));

  // ----------------------------------------------------------- holding
  async function send() {
    if (!held) return;
    const [lin, ang] = DIRS[held];
    try {
      await post('/motion/drive', { linear: lin * speed, angular: ang * speed });
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401) && !warned) toast(e.message || 'LittleBot can’t drive right now.', 'error');
      warned = true;
      held = null;
      stopTimer();
      refresh();
    }
  }
  function hold(dir) {
    if (held === dir) return;
    held = dir;
    warned = false;
    stopTimer();
    send();
    timer = setInterval(send, RESEND_MS);
  }
  function release(dir) {
    if (held !== dir) return;
    held = null;
    halt();
  }
  function stopTimer() { if (timer) { clearInterval(timer); timer = null; } }
  function halt() {
    stopTimer();
    post('/motion/drive/stop').catch(() => {});
  }

  // keyboard: arrows / W A S D (two at once: an arc)
  const keyDir = () => {
    const up = keys.has('up'), down = keys.has('down'), left = keys.has('left'), right = keys.has('right');
    if (up && left) return 'upleft'; if (up && right) return 'upright';
    if (down && left) return 'downleft'; if (down && right) return 'downright';
    return up ? 'up' : down ? 'down' : left ? 'left' : right ? 'right' : null;
  };
  function onKey(e) {
    const dir = KEYS[e.code];
    if (!dir || e.target?.closest?.('input, textarea, select')) return;
    e.preventDefault();
    if (e.type === 'keydown') keys.add(dir); else keys.delete(dir);
    const next = keyDir();
    if (next) hold(next); else if (held) { held = null; halt(); }
  }
  document.addEventListener('keydown', onKey);
  document.addEventListener('keyup', onKey);
  const onBlur = () => { keys.clear(); if (held) { held = null; halt(); } };
  window.addEventListener('blur', onBlur);
  const onVisibility = () => { if (document.hidden) onBlur(); else refresh(); };
  document.addEventListener('visibilitychange', onVisibility);

  // ----------------------------------------------------------- status
  function fill(d) {
    const st = d.drive || {};
    const why = d.refusal || '';
    let cls = 'ok', label = 'Ready';
    if (!st.enabled) { cls = ''; label = 'Off'; }
    else if (st.estop) { cls = 'danger'; label = 'Emergency stop'; }
    else if (why.includes('not armed')) { cls = 'warn'; label = 'Locked'; }
    else if (why) { cls = 'warn'; label = 'Can’t move'; }
    else if (!st.physical) { cls = 'warn'; label = st.moving ? 'Moving (simulated)' : 'Simulated'; }
    else if (st.moving) { cls = 'ok'; label = 'Moving'; }
    badge.className = `badge ${cls}`;
    setText(badge, label);
    let text = '';
    if (!st.enabled) text = 'The wheels are switched off (motion.drive.enabled).';
    else if (why) text = why.charAt(0).toUpperCase() + why.slice(1) + '.';
    else if (!st.physical) text = `Simulated wheels: nothing really moves${st.note ? ` (${st.note})` : ''}.`;
    else text = 'Real wheels: keep the floor clear and stay close.';
    setText(note, text);
    armLink.hidden = !why.includes('not armed');
    const p = st.pose || {};
    setText(pose, st.enabled ? `Since start: ${((p.x || 0) * 100).toFixed(0)} cm forward, ${((p.y || 0) * 100).toFixed(0)} cm left, facing ${Math.round(p.heading || 0)}°` : '');
    pad.classList.toggle('disabled', !st.enabled);
  }
  async function refresh() {
    if (document.hidden) return;
    try { fill(await get('/motion/drive', { timeout: 6000 })); } catch (e) {
      if (e instanceof ApiError && e.status === 503) { clear(note).append(document.createTextNode('LittleBot has no wheels (motion system unavailable).')); badge.className = 'badge'; setText(badge, 'Unavailable'); }
    }
  }
  const statusTimer = setInterval(refresh, STATUS_MS);
  refresh();

  return {
    el,
    destroy() {
      clearInterval(statusTimer);
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('keyup', onKey);
      window.removeEventListener('blur', onBlur);
      document.removeEventListener('visibilitychange', onVisibility);
      if (held || timer) { held = null; halt(); }
    },
  };
}
