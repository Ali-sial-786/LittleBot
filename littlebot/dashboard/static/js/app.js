// LittleBot parent dashboard: boot, login / first-run setup, shell, router, status polling.

import { ApiError, get, onUnauthorized, post, setCsrf } from './api.js';
import { button, clear, h, icon, miniFace, setText, stateLabel, toast } from './ui.js';

import home from './pages/home.js';
import activities from './pages/activities.js';
import media from './pages/media.js';
import face from './pages/face.js';
import profile from './pages/profile.js';
import ai from './pages/ai.js';
import privacy from './pages/privacy.js';
import motion from './pages/motion.js';
import devices from './pages/devices.js';
import simulator from './pages/simulator.js';
import settings from './pages/settings.js';
import more from './pages/more.js';

export const NAV = [
  { id: 'home', label: 'Home', icon: 'home', tab: true, page: home },
  { id: 'activities', label: 'Activities', icon: 'shapes', tab: true, page: activities },
  { id: 'media', label: 'Videos', icon: 'film', tab: true, page: media },
  { id: 'face', label: 'Face', icon: 'face', tab: true, page: face },
  { id: 'profile', label: 'Child profile', icon: 'user', page: profile, blurb: 'Age, name and favourites' },
  { id: 'ai', label: 'AI', icon: 'sparkles', page: ai, blurb: 'Optional cloud conversation' },
  { id: 'privacy', label: 'Privacy', icon: 'shield', page: privacy, blurb: 'Camera, microphone, cloud' },
  { id: 'motion', label: 'Motion', icon: 'robot', page: motion, blurb: 'Gestures and emergency stop' },
  { id: 'devices', label: 'Audio & devices', icon: 'speaker', page: devices, blurb: 'Speaker, microphone, camera' },
  { id: 'simulator', label: 'Simulator', icon: 'gamepad', page: simulator, blurb: 'Pretend speech and faces' },
  { id: 'settings', label: 'Settings', icon: 'sliders', page: settings, blurb: 'Password, access, system' },
];
const PAGES = Object.fromEntries(NAV.map((n) => [n.id, n]));
PAGES.more = { id: 'more', label: 'More', icon: 'grid', page: more };

const POLL_MS = 1500;
const root = document.getElementById('app');

const app = {
  status: null,
  session: null,
  online: true,
  listeners: new Set(),
  navigate(id) { location.hash = `#/${id}`; },
  refresh() { schedulePoll(0); },
  /** Run an API action: toast errors, optional success toast, refresh status. Returns null on failure. */
  async act(fn, okMessage) {
    try {
      const res = await fn();
      if (okMessage) toast(typeof okMessage === 'function' ? okMessage(res) : okMessage, 'ok');
      schedulePoll(150);
      return res ?? {};
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) toast(e.message || 'Something went wrong.', 'error');
      return null;
    }
  },
  subscribe(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); },
};
window.littlebot = app; // handy for debugging from the browser console

// ------------------------------------------------------------------ boot
async function boot() {
  onUnauthorized(() => sessionEnded());
  let session;
  try {
    session = await get('/session');
  } catch (e) {
    renderFatal(e.message);
    return;
  }
  root.classList.remove('booting');
  root.removeAttribute('aria-busy');
  if (session.authenticated) startShell(session);
  else if (session.setup_required) renderSetup(session);
  else renderLogin();
}

function renderFatal(message) {
  clear(root);
  root.classList.remove('booting');
  root.append(h('div', { class: 'auth' }, h('div', { class: 'auth-card card' },
    h('div', { class: 'logo' }, miniFace({ expression: 'sad', size: 88 }), h('h1', { text: "Can't reach LittleBot" }), h('p', { text: message })),
    button('Try again', { kind: 'primary block', iconName: 'refresh', onClick: () => location.reload() }))));
}

function sessionEnded() {
  if (!app.session) return;
  stopShell();
  renderLogin('Your session has ended. Please log in again.');
}

// ------------------------------------------------------------------ auth screens
function passwordField({ id, label, autocomplete, hint }) {
  const input = h('input', { type: 'password', id, name: id, autocomplete, required: true, maxlength: 256, spellcheck: 'false' });
  const toggle = h('button', { type: 'button', class: 'btn ghost icon small', 'aria-label': 'Show password', 'aria-pressed': 'false' }, icon('eye'));
  toggle.addEventListener('click', () => {
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    toggle.setAttribute('aria-pressed', String(show));
    toggle.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
    clear(toggle).append(icon(show ? 'eyeOff' : 'eye'));
  });
  const field = h('div', { class: 'field' }, h('label', { for: id, text: label }), h('div', { class: 'pw-wrap' }, input, toggle), hint || null);
  return { field, input };
}

function authShell(title, text, ...content) {
  clear(root);
  stopShell();
  const card = h('div', { class: 'auth-card' },
    h('div', { class: 'logo' }, miniFace({ expression: 'happy', size: 96, label: 'LittleBot' }), h('h1', { text: title }), text ? h('p', { text }) : null),
    h('div', { class: 'card' }, content),
    h('p', { class: 'footnote', text: 'LittleBot keeps this dashboard on your home network. Nothing here is sent to the internet.' }));
  root.append(h('main', { class: 'auth' }, card));
  return card;
}

function renderLogin(message) {
  history.replaceState(null, '', location.pathname.startsWith('/setup') || location.pathname.startsWith('/login') ? '/' + location.hash : location.href);
  const err = h('div', { class: 'error-box', role: 'alert', hidden: !message, text: message || '' });
  const pw = passwordField({ id: 'password', label: 'Parent password', autocomplete: 'current-password' });
  const submit = h('button', { type: 'submit', class: 'btn primary block big' }, icon('lock'), h('span', { text: 'Log in' }));
  const form = h('form', { novalidate: true }, err, pw.field, submit);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!pw.input.value) { pw.input.focus(); return; }
    submit.disabled = true;
    err.hidden = true;
    try {
      const s = await post('/login', { password: pw.input.value });
      startShell(s);
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
      pw.input.select();
    } finally {
      submit.disabled = false;
    }
  });
  authShell('Welcome back', 'Log in to look after LittleBot.', form);
  pw.input.focus();
}

function strengthOf(pw) {
  let score = 0;
  if (pw.length >= 8) score++;
  if (pw.length >= 12) score++;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) score++;
  if (/\d/.test(pw) || /[^\w\s]/.test(pw)) score++;
  if (pw.length >= 16) score++;
  return Math.min(4, score);
}

function renderSetup(session) {
  const minLen = session?.min_password_length || 8;
  const err = h('div', { class: 'error-box', role: 'alert', hidden: true });
  const code = h('input', { id: 'setup-code', type: 'text', required: true, maxlength: 12, autocomplete: 'one-time-code', autocapitalize: 'characters', spellcheck: 'false', placeholder: 'ABCD-EFGH', inputmode: 'text' });
  const bar = h('span');
  const strengthText = h('span', { class: 'hint', text: `At least ${minLen} characters. A short sentence is easy to remember.` });
  const pw = passwordField({ id: 'new-password', label: 'Choose a parent password', autocomplete: 'new-password', hint: h('div', { class: 'stack-sm' }, h('div', { class: 'strength', 'aria-hidden': 'true' }, bar), strengthText) });
  const pw2 = passwordField({ id: 'confirm-password', label: 'Type it again', autocomplete: 'new-password' });
  const colors = ['var(--danger)', 'var(--danger)', 'var(--warn)', 'var(--ok)', 'var(--ok)'];
  pw.input.addEventListener('input', () => {
    const sc = strengthOf(pw.input.value);
    bar.style.width = `${pw.input.value ? (sc + 1) * 20 : 0}%`;
    bar.style.backgroundColor = colors[sc];
  });
  const submit = h('button', { type: 'submit', class: 'btn primary block big' }, icon('check'), h('span', { text: 'Finish setup' }));
  const form = h('form', { novalidate: true },
    h('div', { class: 'banner info' }, icon('info'), h('div', {},
      h('strong', { text: 'Find your setup code' }),
      h('span', { text: 'LittleBot printed a one-time code in the window or terminal where it is running (and in its log file). Only someone with access to the robot can see it.' }))),
    err,
    h('div', { class: 'field setup-code' }, h('label', { for: 'setup-code', text: 'Setup code' }), code),
    pw.field, pw2.field, submit);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    err.hidden = true;
    const fail = (m, el) => { err.textContent = m; err.hidden = false; el?.focus(); };
    if (!code.value.trim()) return fail('Enter the setup code from the robot.', code);
    if (pw.input.value.length < minLen) return fail(`Your password needs at least ${minLen} characters.`, pw.input);
    if (pw.input.value !== pw2.input.value) return fail("The two passwords don't match.", pw2.input);
    submit.disabled = true;
    try {
      const s = await post('/setup', { setup_code: code.value.trim(), password: pw.input.value });
      toast('All set! Your dashboard is ready.', 'ok');
      startShell(s);
    } catch (ex) {
      if (ex.status === 409) { renderLogin(ex.message); return; }
      fail(ex.message, ex.status === 401 ? code : null);
    } finally {
      submit.disabled = false;
    }
  });
  authShell('Set up your dashboard', "Let's make LittleBot yours. This takes a minute.", form);
  code.focus();
}

// ------------------------------------------------------------------ shell
let shell = null;
let current = null; // {id, ctl}
let pollTimer = null;
let polling = false;

function startShell(session) {
  setCsrf(session.csrf_token);
  app.session = session;
  if (location.pathname !== '/') history.replaceState(null, '', '/' + (location.hash || '#/home'));
  clear(root);
  const face0 = miniFace({ expression: 'neutral', size: 44, label: 'LittleBot face' });
  const nameEl = h('span', { class: 'brand-name', text: session.robot_name || 'LittleBot' });
  const dot = h('span', { class: 'conn-dot', title: 'Connected' });
  const stateEl = h('span', { text: 'Connecting…' });
  const stopBtn = h('button', { type: 'button', class: 'btn-stop-top', 'aria-label': 'Stop everything now' }, icon('stop'), h('span', { text: 'STOP' }));
  stopBtn.addEventListener('click', () => stopEverything());
  const topbar = h('header', { class: 'topbar' },
    h('a', { class: 'topbar-brand', href: '#/home', 'aria-label': 'Home' }, face0, h('span', { class: 'brand-text' }, nameEl, h('span', { class: 'brand-state' }, dot, stateEl))),
    stopBtn);
  const offline = h('div', { class: 'offline-bar', role: 'alert', hidden: true, text: "Can't reach LittleBot right now - retrying…" });
  const promptBar = h('a', { class: 'prompt-bar', hidden: true, href: '#/home' });
  const side = h('nav', { class: 'sidenav', 'aria-label': 'Sections' },
    NAV.slice(0, 4).map(navLink), h('div', { class: 'nav-sep' }), NAV.slice(4).map(navLink));
  const view = h('main', { class: 'view', id: 'view', tabindex: '-1' });
  const tabs = h('nav', { class: 'tabbar', 'aria-label': 'Main' },
    [...NAV.filter((n) => n.tab), PAGES.more].map((n) => h('a', { class: 'tab', href: `#/${n.id}`, dataset: { id: n.id } }, icon(n.icon), h('span', { text: n.id === 'more' ? 'More' : n.label }))));
  root.append(h('div', { class: 'shell' }, topbar, offline, promptBar, h('div', { class: 'layout' }, side, view), tabs));
  shell = { face0, nameEl, dot, stateEl, view, side, tabs, offline, promptBar };
  window.addEventListener('hashchange', route);
  document.addEventListener('visibilitychange', onVisibility);
  route();
  schedulePoll(0);
}

function navLink(n) {
  return h('a', { href: `#/${n.id}`, dataset: { id: n.id } }, icon(n.icon), h('span', { text: n.label }));
}

function stopShell() {
  window.removeEventListener('hashchange', route);
  document.removeEventListener('visibilitychange', onVisibility);
  clearTimeout(pollTimer);
  pollTimer = null;
  if (current?.ctl?.destroy) { try { current.ctl.destroy(); } catch { /* ignore */ } }
  current = null;
  shell = null;
  app.session = null;
  app.status = null;
  setCsrf(null);
}

function route() {
  if (!shell) return;
  const id = (location.hash.match(/^#\/([a-z]+)/) || [])[1] || 'home';
  const entry = PAGES[id] || PAGES.home;
  if (current && current.id === entry.id) return;
  if (current?.ctl?.destroy) { try { current.ctl.destroy(); } catch { /* ignore */ } }
  clear(shell.view);
  for (const a of [...shell.side.querySelectorAll('a'), ...shell.tabs.querySelectorAll('a')]) {
    const isTabMore = a.dataset.id === 'more' && !NAV.find((n) => n.id === entry.id)?.tab && entry.id !== 'home';
    if (a.dataset.id === entry.id || isTabMore) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  document.title = `${entry.label} · LittleBot`;
  let ctl = null;
  try {
    ctl = entry.page.mount(shell.view, app) || {};
  } catch (e) {
    console.error(e);
    shell.view.append(h('p', { class: 'empty', text: 'This page failed to load.' }));
  }
  current = { id: entry.id, ctl };
  if (app.status && ctl?.update) safeUpdate(ctl, app.status);
  window.scrollTo(0, 0);
  shell.view.focus({ preventScroll: true });
  updatePromptBar();
}

function safeUpdate(ctl, st) {
  try { ctl.update(st); } catch (e) { console.error('page update failed', e); }
}

// ------------------------------------------------------------------ polling
function onVisibility() {
  if (document.hidden) { clearTimeout(pollTimer); pollTimer = null; }
  else schedulePoll(0);
}

function schedulePoll(delay = POLL_MS) {
  if (!shell) return;
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, delay);
}

async function poll() {
  if (!shell || document.hidden) return;
  if (polling) { schedulePoll(200); return; }
  polling = true;
  try {
    const st = await get('/status', { timeout: 8000 });
    setOnline(true);
    applyStatus(st);
  } catch (e) {
    if (e.status === 401) return;
    setOnline(false);
  } finally {
    polling = false;
  }
  schedulePoll(app.online ? POLL_MS : 4000);
}

function setOnline(ok) {
  if (!shell || app.online === ok) return;
  app.online = ok;
  shell.offline.hidden = ok;
  shell.dot.classList.toggle('offline', !ok);
  shell.dot.title = ok ? 'Connected' : 'Disconnected';
}

function applyStatus(st) {
  app.status = st;
  if (!shell) return;
  const fs = st.face || {};
  const asleep = st.state?.state === 'SLEEPING';
  shell.face0.update(asleep ? 'sleepy' : fs.expression || 'neutral', fs.gaze, !!fs.eyes_closed || asleep);
  setText(shell.nameEl, st.name || 'LittleBot');
  setText(shell.stateEl, stateLabel(st));
  if (current?.ctl?.update) safeUpdate(current.ctl, st);
  for (const fn of app.listeners) { try { fn(st); } catch { /* ignore */ } }
  updatePromptBar();
}

function updatePromptBar() {
  if (!shell) return;
  const prompt = app.status?.activities?.prompt;
  const show = !!(prompt && prompt.text) && current && current.id !== 'home' && current.id !== 'activities';
  shell.promptBar.hidden = !show;
  if (show) setText(shell.promptBar, `LittleBot is asking you: “${prompt.text}” - tap to answer`);
}

async function stopEverything() {
  const res = await app.act(() => post('/stop'), 'Stopped. LittleBot is calm and idle.');
  if (res?.status) applyStatus({ ...app.status, ...res.status });
}
app.stopEverything = stopEverything;

boot();
