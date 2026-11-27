// The Nanit baby camera (littlebot/nanit, docs/NANIT.md): its live picture full screen on LittleBot's screen
// through an unofficial bridge on the robot. Set up? (the one command to run), the bridge, Show (one button per
// camera), Close, the parent's switch and the sound switch. The Nanit sign-in itself never comes here.

import { get, post, put } from '../api.js';
import { append, banner, button, clear, h, icon, switchRow, toast } from '../ui.js';

const BRIDGE_WORDS = {
  active: 'running', inactive: 'stopped', failed: 'stopped after an error', activating: 'starting (or restarting after an error)',
  deactivating: 'stopping', missing: 'not set up', unknown: 'unknown',
};

export function nanitPanel(app) {
  const body = h('div', { class: 'stack-sm' });
  let data = null;
  let shownKey = '';

  const enabled = switchRow({
    title: 'Allow the baby camera',
    text: 'The Nanit camera’s live picture on LittleBot’s screen, when a grown-up asks for it.',
    onChange: async (v) => {
      const r = await app.act(() => put('/nanit/settings', { enabled: v }), v ? 'Baby camera allowed.' : 'Baby camera switched off.');
      if (r) { data = r; render(true); }
      return r !== null;
    },
  });

  const sound = switchRow({
    title: 'Baby camera sound',
    text: 'The baby’s sounds through LittleBot’s speaker while the camera shows (starts with the next view).',
    onChange: async (v) => {
      const r = await app.act(() => put('/nanit/settings', { sound: v }), v ? 'Sound on.' : 'Sound off.');
      if (r) { data = r; render(true); }
      return r !== null;
    },
  });

  async function act(fn, done) {
    const r = await app.act(fn, done);
    if (r) { data = r.status || r; render(true); }
    return r;
  }

  async function show(camera) {
    const r = await app.act(() => post('/nanit/show', camera ? { camera } : {}));
    if (!r) return;
    data = r.status || data;
    if (r.ok) toast(`${r.camera || 'The camera'} is coming up on LittleBot’s screen.`, 'ok');
    else toast(r.detail || 'The baby camera didn’t open.', 'error');
    render(true);
  }

  function setupBox() {
    const parts = data.setup || {};
    const missing = [];
    if (!parts.bridge) missing.push('the bridge program');
    if (!parts.signed_in) missing.push('the Nanit sign-in');
    if (!parts.service) missing.push('the bridge’s service');
    const cmd = data.setup_command || 'bash ~/LittleBot/scripts/setup_nanit.sh';
    return h('div', { class: 'stack-sm' },
      banner('warn', 'alert', 'Not set up yet', `Missing: ${missing.join(', ') || 'something'}. Run this once in a terminal on LittleBot (no password needed):`),
      h('pre', { class: 'json', text: cmd }),
      button('Copy the command', { kind: 'soft', small: true, onClick: async () => {
        try { await navigator.clipboard.writeText(cmd); toast('Copied.', 'ok'); } catch { toast('Select the command and copy it.', 'info'); }
      } }),
      h('p', { class: 'muted small', text: 'It builds the bridge and asks you to sign in to Nanit right there in the terminal: you type your own email, password and the code Nanit emails you. LittleBot never sees your password, and the dashboard never shows the sign-in.' }));
  }

  function bridgeBox() {
    const state = data.bridge || 'unknown';
    const words = BRIDGE_WORDS[state] || state;
    const cams = data.cameras || [];
    if (data.bridge_running) {
      return h('p', { class: 'muted small', text: `The bridge is running. ${cams.length ? `Cameras: ${cams.map((c) => c.name).join(', ')}.` : 'No camera found yet (give it a minute after it starts).'}` });
    }
    return h('div', { class: 'stack-sm' },
      banner('warn', 'alert', `The bridge is ${words}`, 'To see why, run this on LittleBot:'),
      h('pre', { class: 'json', text: `${data.status_command || 'systemctl --user status littlebot-nanit'}\njournalctl --user -u littlebot-nanit -n 50` }),
      h('p', { class: 'muted small', text: `If the Nanit sign-in has run out, sign in again with: ${data.setup_command || 'bash ~/LittleBot/scripts/setup_nanit.sh'} --login-again` }));
  }

  function nowBox() {
    if (!data.active) {
      const last = data.last;
      return last ? h('p', { class: 'muted small', text: `Last: ${last.camera}, ${Math.round(last.seconds || 0)} s, closed: ${last.reason}.` }) : null;
    }
    const text = data.state === 'starting'
      ? `Waiting for the picture from ${data.camera}…`
      : `Showing ${data.camera} on LittleBot’s screen${data.minutes_left != null ? ` (closes by itself in about ${Math.max(1, Math.round(data.minutes_left))} min)` : ''}.`;
    return h('div', { class: 'stack-sm' }, banner('info', 'camera', 'The baby camera is on', text),
      h('div', { class: 'btn-row' }, button('Close the camera', { kind: 'danger-outline', iconName: 'square', onClick: () => act(() => post('/nanit/close'), 'Closed; the face is back.') })));
  }

  function camerasBox() {
    const cams = data.cameras || [];
    if (!cams.length) return null;
    return h('div', { class: 'btn-row' }, cams.map((c) => button(`Show ${c.name}`, { kind: 'primary', iconName: 'play', onClick: () => show(c.name) })));
  }

  function render(force) {
    const key = JSON.stringify(data);
    if (key === shownKey && !force) return;
    shownKey = key;
    clear(body);
    if (!data || data.error) {
      body.append(h('p', { class: 'muted', text: (data && data.error) || 'Loading…' }));
      return;
    }
    enabled.el.querySelector('input').checked = !!data.enabled;
    sound.el.querySelector('input').checked = !!data.sound;
    append(body, [
      enabled.el,
      data.available === false ? banner('info', 'info', 'Not available on this robot', data.detail || '') : null,
      data.set_up === false ? setupBox() : null,
      data.set_up ? bridgeBox() : null,
      data.set_up && data.player === false ? banner('warn', 'alert', 'ffplay is missing', 'Install it on LittleBot with: sudo apt install ffmpeg') : null,
      nowBox(),
      data.set_up ? camerasBox() : null,
      sound.el,
      h('p', { class: 'muted small', text: `Say “Hey LittleBot, show the baby camera” (or “close the camera”). It closes by itself after ${data.max_minutes ? `${data.max_minutes} minutes` : 'never (config nanit.max_minutes is 0)'}, and when a video, game or STOP comes.` }),
      h('p', { class: 'muted small', text: 'Unofficial: this uses a community bridge, not an app from Nanit, and a Nanit update can break it. While LittleBot shows the local picture, the Nanit app may switch to its cloud stream. The picture stays on your home network.' }),
    ]);
  }

  async function refresh() {
    try { data = await get('/nanit'); } catch (e) { data = { error: e.message || 'The baby camera isn’t available.' }; }
    render();
  }

  const el = h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('camera'), 'Baby camera (Nanit)')), body);
  refresh();
  const timer = setInterval(() => { if (!document.hidden && !el.contains(document.activeElement)) refresh(); }, 5000);
  return { el, destroy() { clearInterval(timer); } };
}
