// WhatsApp calls on LittleBot's screen (littlebot/calls, docs/CALLS.md): WhatsApp Web in Chromium, only while it
// is on the screen. What's on (linked? a call? memory), Open / Close, the parent's switches (calls allowed; the
// chats hidden behind LittleBot's cover), who can be called, and signing out on the robot. Nothing of the chats
// comes here: only states and the names on this list.

import { get, post, put } from '../api.js';
import { append, banner, button, clear, confirmDialog, h, icon, switchRow, toast } from '../ui.js';

const MAX_NAME = 40;

export function callsPanel(app) {
  const body = h('div', { class: 'stack-sm' });
  let data = null;
  let shownKey = '';
  let rows = null; // the contacts being edited: [{name, whatsapp}] (null: not edited, follows the robot)

  const take = (r) => { if (r) { data = r.status || r; render(true); } return r; };

  const enabled = switchRow({
    title: 'Allow WhatsApp calls',
    text: 'Video and voice calls through WhatsApp on LittleBot’s screen, when a grown-up asks.',
    onChange: async (v) => take(await app.act(() => put('/calls/settings', { enabled: v }), v ? 'WhatsApp calls allowed.' : 'WhatsApp calls switched off.')) !== null,
  });

  const hide = switchRow({
    title: 'Hide my chats on the robot',
    text: 'LittleBot covers WhatsApp’s page: only the QR code and a call show. Off: your chat list and messages are visible on the robot’s screen while WhatsApp is open.',
    onChange: async (v) => {
      if (!v && !(await confirmDialog({ title: 'Show your chats on the robot?', warn: true, confirm: 'Show them', body: 'Anyone looking at LittleBot’s screen while WhatsApp is open will see your chat list and messages, and can tap into them. Notifications with message previews can show too.' }))) return false;
      return take(await app.act(() => put('/calls/settings', { hide_chats: v }), v ? 'Your chats are hidden on the robot.' : 'Your chats now show on the robot while WhatsApp is open.')) !== null;
    },
  });

  async function open(extra, done) {
    const r = await app.act(() => post('/calls/open', extra || {}));
    if (!r) return;
    data = r.status || data;
    if (r.ok) toast(done || (r.already ? 'WhatsApp is open already.' : 'WhatsApp is opening on LittleBot’s screen.'), 'ok');
    else toast(r.detail || 'WhatsApp didn’t open.', 'error');
    render(true);
  }

  function nowBox() {
    if (!data.active) {
      const last = data.last;
      if (!last) return null;
      const freed = last.freed_mb != null ? `, ${last.freed_mb >= 0 ? '+' : ''}${last.freed_mb} MB of memory back` : '';
      return h('p', { class: 'muted small', text: `Last: ${Math.round(last.seconds || 0)} s, shut down: ${last.reason}${freed}.` });
    }
    let title = 'WhatsApp is open';
    let text = 'It shuts down by itself when the call ends or nobody uses it.';
    if (data.call === 'in_call') { title = 'In a call'; text = 'Say “Hey LittleBot, hang up”, or press Close.'; }
    else if (data.call === 'incoming') { title = 'A call is ringing'; text = 'Tap Answer on LittleBot’s screen, or say “Hey LittleBot, answer”.'; }
    else if (data.problem) { title = 'Something didn’t work'; text = data.problem; }
    else if (data.state === 'linking') { title = 'Not linked yet'; text = 'The QR code is on LittleBot’s screen. On your phone: WhatsApp → Settings → Linked devices → Link a device, and scan it.'; }
    else if (data.state !== 'ready') { title = 'Opening…'; text = 'WhatsApp is loading on LittleBot’s screen.'; }
    else if (data.calling) { title = `Calling ${data.calling}…`; }
    else if (data.closes_in != null) { text = `Linked. It closes in ${Math.round(data.closes_in)} s if nobody uses it.`; }
    return h('div', { class: 'stack-sm' }, banner(data.problem ? 'warn' : 'info', data.problem ? 'alert' : 'camera', title, text),
      h('div', { class: 'btn-row' }, button('Close WhatsApp', { kind: 'danger-outline', iconName: 'stop', onClick: async () => take(await app.act(() => post('/calls/close'), 'WhatsApp is closed; the face is back.')) })));
  }

  function openBox() {
    const people = (data.contacts || []).map((c) => c.name);
    const busy = data.active && data.call !== 'none';
    return h('div', { class: 'btn-row' },
      data.active ? null : button('Open WhatsApp', { kind: 'primary', iconName: 'play', onClick: () => open({}) }),
      data.active ? null : button('Open to answer a call', { kind: 'soft', iconName: 'play', onClick: () => open({ answer: true }) }),
      busy ? null : people.map((name) => button(`Video call ${name}`, { kind: 'soft', iconName: 'camera', onClick: () => open({ contact: name, kind: 'video' }, `Calling ${name} from LittleBot.`) })));
  }

  function contactsBox() {
    const list = rows || (data.contacts || []).map((c) => ({ name: c.name, whatsapp: c.whatsapp }));
    const edit = (i, key) => (e) => { if (!rows) rows = list.map((c) => ({ ...c })); rows[i][key] = e.target.value; };
    const line = (c, i) => h('div', { class: 'btn-row' },
      h('input', { type: 'text', maxlength: MAX_NAME, value: c.name, placeholder: 'What you say (Dad)', 'aria-label': 'The name you say', autocomplete: 'off', oninput: edit(i, 'name') }),
      h('input', { type: 'text', maxlength: MAX_NAME, value: c.whatsapp, placeholder: 'Name in WhatsApp (Ali Khan)', 'aria-label': 'The name in WhatsApp', autocomplete: 'off', oninput: edit(i, 'whatsapp') }),
      button('Remove', { kind: 'soft', small: true, onClick: () => { rows = list.filter((_, j) => j !== i); render(true); } }));
    return h('div', { class: 'stack-sm' },
      h('h3', { text: 'Who can be called' }),
      h('p', { class: 'muted small', text: 'Only these people can be called from LittleBot. The first name is what you say (“call Dad”); the second is exactly how that person’s chat is named in your WhatsApp.' }),
      list.length ? list.map(line) : h('p', { class: 'muted', text: 'Nobody yet.' }),
      h('div', { class: 'btn-row' },
        button('Add a person', { kind: 'soft', iconName: 'plus', onClick: () => { rows = [...list, { name: '', whatsapp: '' }]; render(true); } }),
        rows ? button('Save the list', { kind: 'primary', iconName: 'check', onClick: async () => {
          const r = await app.act(() => put('/calls/contacts', { contacts: rows.filter((c) => c.name.trim() || c.whatsapp.trim()) }), 'Saved.');
          if (r) { rows = null; take(r); }
        } }) : null,
        rows ? button('Cancel', { kind: 'ghost', onClick: () => { rows = null; render(true); } }) : null));
  }

  function signOutBox() {
    if (!data.profile) return h('p', { class: 'muted small', text: 'WhatsApp isn’t signed in on the robot. The first time it opens, it shows a QR code to scan with your phone.' });
    return h('div', { class: 'btn-row' }, button('Sign out of WhatsApp on the robot', { kind: 'danger-outline', iconName: 'lock', onClick: async () => {
      if (!(await confirmDialog({ title: 'Sign out of WhatsApp on the robot?', danger: true, confirm: 'Sign out', body: 'LittleBot closes WhatsApp and deletes its saved sign-in (data/whatsapp-profile). To use it again you scan the QR code again. Also remove “LittleBot” (it shows as a Chrome browser) on your phone: WhatsApp → Settings → Linked devices.' }))) return;
      take(await app.act(() => post('/calls/signout'), 'Signed out: the saved sign-in was deleted.'));
    } }));
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
    hide.el.querySelector('input').checked = data.hide_chats !== false;
    const usable = data.available !== false;
    const free = data.memory_free_mb != null ? ` Memory free now: ${data.memory_free_mb} MB (it opens with at least ${data.min_free_mb} MB).` : '';
    append(body, [
      enabled.el,
      hide.el,
      usable ? null : banner('info', 'info', 'Not available on this robot', data.detail || ''),
      usable ? nowBox() : null,
      usable && data.enabled ? openBox() : null,
      contactsBox(),
      usable ? signOutBox() : null,
      h('p', { class: 'muted small', text: `Say “Hey LittleBot, video call Dad”, “open WhatsApp”, “answer on WhatsApp”, “hang up”. The robot doesn’t ring by itself: WhatsApp runs only while it is on the screen, and is shut down ${data.idle_seconds || 120} s after the last use, when the call ends, or when something else takes the screen, to give its memory back.${free}` }),
      h('p', { class: 'muted small', text: 'It is your own WhatsApp, linked like WhatsApp Web on a computer. LittleBot never reads, sends or keeps messages. Not tried on the robot yet; it works WhatsApp’s page for you, which can break when WhatsApp changes it.' }),
    ]);
  }

  async function refresh() {
    try { data = await get('/calls'); } catch (e) { data = { error: e.message || 'WhatsApp calls aren’t available.' }; }
    render();
  }

  const el = h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('camera'), 'WhatsApp calls')), body);
  refresh();
  const timer = setInterval(() => { if (!document.hidden && !rows && !el.contains(document.activeElement)) refresh(); }, 5000);
  return { el, destroy() { clearInterval(timer); } };
}
