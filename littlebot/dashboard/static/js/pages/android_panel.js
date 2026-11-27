// Android on LittleBot's screen (littlebot/android, docs/ANDROID_ON_PI.md): Waydroid's Android with Google Play,
// only while it is on the screen. Set up? (the next step to run on the robot), Open (Android or one of its apps),
// Close, the parent's switch, and what the last run freed. The Google sign-in stays inside Android.

import { get, post, put } from '../api.js';
import { append, banner, button, clear, h, icon, switchRow, toast } from '../ui.js';

const STEP_WORDS = {
  kernel: 'Step 1: the Pi’s 4K-page kernel (Android needs it), then a restart.',
  install: 'Step 2: Waydroid and Android itself (about 1 GB to download).',
  container: 'Android’s container service isn’t running.',
};

export function androidPanel(app) {
  const body = h('div', { class: 'stack-sm' });
  let data = null;
  let shownKey = '';

  const enabled = switchRow({
    title: 'Allow Android',
    text: 'Android with Google Play on LittleBot’s screen, when someone asks for it.',
    onChange: async (v) => {
      const r = await app.act(() => put('/android/settings', { enabled: v }), v ? 'Android allowed.' : 'Android switched off.');
      if (r) { data = r; render(true); }
      return r !== null;
    },
  });

  async function act(fn, done) {
    const r = await app.act(fn, done);
    if (r) { data = r.status || r; render(true); }
    return r;
  }

  async function open(name) {
    const r = await app.act(() => post('/android/open', name ? { app: name } : {}));
    if (!r) return;
    data = r.status || data;
    if (r.ok) toast(r.already ? 'Android is on already.' : 'Android is starting on LittleBot’s screen (a minute or two the first time).', 'ok');
    else toast(r.detail || 'Android didn’t start.', 'error');
    render(true);
  }

  function setupBox() {
    const cmd = data.setup_command || `${data.setup_script || '~/LittleBot/scripts/setup_waydroid.sh'}`;
    return h('div', { class: 'stack-sm' },
      banner('warn', 'alert', 'Not set up yet', `${STEP_WORDS[data.setup_step] || 'Something is missing.'} Run this in a terminal on LittleBot (it asks for your password itself):`),
      h('pre', { class: 'json', text: cmd }),
      button('Copy the command', { kind: 'soft', small: true, onClick: async () => {
        try { await navigator.clipboard.writeText(cmd); toast('Copied.', 'ok'); } catch { toast('Select the command and copy it.', 'info'); }
      } }));
  }

  function nowBox() {
    if (!data.active) {
      const last = data.last;
      if (!last) return null;
      const freed = last.freed_mb != null ? `, ${last.freed_mb >= 0 ? '+' : ''}${last.freed_mb} MB of memory back` : '';
      return h('p', { class: 'muted small', text: `Last: ${Math.round(last.seconds || 0)} s, shut down: ${last.reason}${freed}.` });
    }
    const text = data.state === 'starting'
      ? 'Starting… its window comes in a minute or so (longer the first time).'
      : 'Android is on LittleBot’s screen. It shuts down by itself when its window is closed or nobody touches it for a few minutes.';
    return h('div', { class: 'stack-sm' }, banner('info', 'grid', 'Android is on', text),
      h('div', { class: 'btn-row' }, button('Close Android', { kind: 'danger-outline', iconName: 'square', onClick: () => act(() => post('/android/close'), 'Android shut down; the face is back.') })));
  }

  function openBox() {
    if (data.active) return null;
    const apps = Object.keys(data.apps || {});
    return h('div', { class: 'btn-row' },
      button('Open Android', { kind: 'primary', iconName: 'play', onClick: () => open(null) }),
      apps.map((a) => button(`Open ${a}`, { kind: 'soft', iconName: 'play', onClick: () => open(a) })));
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
    const free = data.memory_free_mb != null ? ` Memory free now: ${data.memory_free_mb} MB (it starts with at least ${data.min_free_mb} MB).` : '';
    append(body, [
      enabled.el,
      data.available === false ? banner('info', 'info', 'Not available on this robot', data.detail || '') : null,
      data.available !== false && !data.set_up ? setupBox() : null,
      nowBox(),
      data.set_up ? openBox() : null,
      h('p', { class: 'muted small', text: `Say “Hey LittleBot, open Android” (or “open the Aosu app”, “close Android”). Android runs only while it is on the screen: closing its window, ${data.off_screen_seconds || 10} s behind another window, the screen going dark after the quiet minutes, a video, game or STOP shut it down and give its 1 to 1.5 GB of memory back.${free}` }),
      h('p', { class: 'muted small', text: 'Your Google and app sign-ins stay inside the robot’s Android. Anyone who can open Android on the robot can use them: switch it off here when it shouldn’t be used.' }),
    ]);
  }

  async function refresh() {
    try { data = await get('/android'); } catch (e) { data = { error: e.message || 'Android isn’t available.' }; }
    render();
  }

  const el = h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('grid'), 'Android')), body);
  refresh();
  const timer = setInterval(() => { if (!document.hidden && !el.contains(document.activeElement)) refresh(); }, 5000);
  return { el, destroy() { clearInterval(timer); } };
}
