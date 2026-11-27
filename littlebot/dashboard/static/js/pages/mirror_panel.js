// Phone mirroring (littlebot/mirror, docs/MIRRORING.md): an iPhone by AirPlay or an Android phone by scrcpy on
// LittleBot's screen. Start, stop, pair and forget Android phones, the parent's switch, and the install command
// when a program is missing. The AirPlay receiver only runs while asked for.

import { get, post, put } from '../api.js';
import { append, banner, button, clear, confirmDialog, h, icon, switchRow, toast } from '../ui.js';

const field = (id, label, input, hint) => h('div', { class: 'field' }, h('label', { for: id, text: label }), input,
  hint ? h('span', { class: 'hint', text: hint }) : null);
const ADDRESS_RE = /^(\d{1,3}\.){3}\d{1,3}:\d{1,5}$/;
const CODE_RE = /^\d{6}$/;

export function mirrorPanel(app) {
  const body = h('div', { class: 'stack-sm' });
  let data = null;
  let shownKey = '';
  let pairing = false;

  const enabled = switchRow({
    title: 'Allow phone mirroring',
    text: 'A grown-up’s iPhone or Android phone on LittleBot’s screen, only when someone asks for it.',
    onChange: async (v) => {
      const r = await app.act(() => put('/mirror/settings', { enabled: v }), v ? 'Phone mirroring allowed.' : 'Phone mirroring switched off.');
      if (r) { data = r; render(true); }
      return r !== null;
    },
  });

  async function act(fn, done) {
    const r = await app.act(fn, done);
    if (r) { data = r.status || r; render(true); }
    return r;
  }

  function installBox(kind, label) {
    const k = (data.kinds || {})[kind] || {};
    if (k.installed !== false) return null;
    return h('div', { class: 'stack-sm' },
      banner('warn', 'alert', `${label}: not installed`, 'Run this once in a terminal on LittleBot (it asks for your password), or run bash scripts/setup_mirroring.sh in the LittleBot folder:'),
      h('pre', { class: 'json', text: k.install || data.setup_script || '' }),
      button('Copy the command', { kind: 'soft', small: true, onClick: async () => {
        try { await navigator.clipboard.writeText(k.install || ''); toast('Copied.', 'ok'); } catch { toast('Select the command and copy it.', 'info'); }
      } }));
  }

  async function start(phone) {
    const r = await app.act(() => post('/mirror/start', { phone }));
    if (!r) return;
    data = r.status || data;
    if (r.ok) toast(phone === 'iphone' ? 'Ready: now pick LittleBot on the iPhone.' : 'The phone’s screen is coming up on LittleBot.', 'ok');
    else toast(r.detail || 'Mirroring didn’t start.', 'error');
    render(true);
  }

  function nowBox() {
    if (!data.active) {
      const last = data.last;
      return last ? h('p', { class: 'muted small', text: `Last: ${last.kind === 'iphone' ? 'iPhone' : 'Android'}, ${Math.round(last.seconds || 0)} s, stopped: ${last.reason}.` }) : null;
    }
    const phone = data.kind === 'iphone' ? 'an iPhone' : 'an Android phone';
    const text = data.state === 'waiting'
      ? `Waiting for ${phone} (about ${Math.round(data.waits_left || 0)} s left), then the receiver closes again.`
      : `Showing ${phone} on LittleBot’s screen.`;
    return h('div', { class: 'stack-sm' }, banner('info', 'wifi', 'Mirroring is on', text),
      h('div', { class: 'btn-row' }, button('Stop mirroring', { kind: 'danger-outline', iconName: 'square', onClick: () => act(() => post('/mirror/stop'), 'Stopped; the face is back.') })));
  }

  function iphoneBox() {
    const name = data.airplay_name || 'LittleBot';
    return h('div', { class: 'skill-row stack-sm' },
      h('h3', { class: 'small-head', text: 'iPhone or iPad (AirPlay)' }),
      installBox('iphone', 'AirPlay (uxplay)'),
      h('ol', {},
        h('li', { text: 'Tap “Mirror an iPhone” (or say “Hey LittleBot, mirror my iPhone”).' }),
        h('li', { text: 'On the iPhone, open Control Center (swipe down from the top-right corner) and tap Screen Mirroring.' }),
        h('li', { text: `Pick “${name}”. It must be on the same Wi-Fi as LittleBot.` })),
      h('p', { class: 'muted small', text: `LittleBot waits ${Math.round(data.wait_seconds || 90)} seconds for the phone, then closes the receiver again. To stop: Stop Mirroring on the iPhone, “stop mirroring”, or Stop here.` }),
      h('div', { class: 'btn-row' }, button('Mirror an iPhone', { kind: 'primary', iconName: 'play', onClick: () => start('iphone') })));
  }

  function pairForm() {
    const addr = h('input', { class: 'input', id: 'mirror-addr', type: 'text', inputmode: 'decimal', autocomplete: 'off', spellcheck: 'false', maxlength: 21, placeholder: '192.168.1.23:37099' });
    const code = h('input', { class: 'input', id: 'mirror-code', type: 'text', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: 6, placeholder: '123456' });
    const name = h('input', { class: 'input', id: 'mirror-name', type: 'text', autocomplete: 'off', maxlength: 40, placeholder: 'e.g. Ali’s phone (optional)' });
    return h('div', { class: 'stack-sm' },
      h('ol', {},
        h('li', { text: 'On the phone: Settings → About phone → tap Build number 7 times (Developer options on).' }),
        h('li', { text: 'Settings → Developer options → Wireless debugging: on (same Wi-Fi as LittleBot).' }),
        h('li', { text: 'Tap “Pair device with pairing code”, and type the address and code it shows here.' })),
      h('div', { class: 'form-grid two' },
        field(addr.id, 'IP address & port', addr), field(code.id, 'Wi-Fi pairing code', code)),
      field(name.id, 'Name', name),
      h('div', { class: 'btn-row' },
        button('Pair', { kind: 'primary', onClick: async () => {
          const a = addr.value.trim(), c = code.value.trim();
          if (!ADDRESS_RE.test(a)) { toast('The address looks like 192.168.1.23:37099.', 'error'); addr.focus(); return; }
          if (!CODE_RE.test(c)) { toast('The pairing code is six digits.', 'error'); code.focus(); return; }
          const r = await app.act(() => post('/mirror/android/pair', { address: a, code: c, name: name.value.trim() }));
          code.value = '';
          if (!r) return;
          data = r.status || data;
          if (r.ok) { toast(r.connected ? 'Paired and connected.' : 'Paired. Keep Wireless debugging on to mirror.', 'ok'); pairing = false; }
          else toast(r.detail || 'Pairing didn’t work.', 'error');
          render(true);
        } }),
        button('Cancel', { kind: 'soft', onClick: () => { pairing = false; render(true); } })));
  }

  function androidBox() {
    const phones = data.android_phones || [];
    return h('div', { class: 'skill-row stack-sm' },
      h('h3', { class: 'small-head', text: 'Android phone (scrcpy over Wi-Fi)' }),
      installBox('android', 'Android mirroring (scrcpy, adb)'),
      h('p', { class: 'muted small', text: 'Pair each phone once (Android 11 or newer). Then switch on Wireless debugging on the phone whenever you want to mirror it.' }),
      phones.length
        ? h('ul', { class: 'plain-list' }, phones.map((p) => h('li', { class: 'row between' },
          h('span', { text: p.name || p.address || 'Android phone' }),
          button('Forget', { kind: 'soft', small: true, onClick: async () => {
            if (await confirmDialog({ title: `Forget ${p.name || 'this phone'}?`, confirm: 'Forget', body: 'LittleBot takes it off its list and disconnects it. To remove LittleBot from the phone too: Wireless debugging → Paired devices → LittleBot’s computer → Forget.' })) {
              act(() => post('/mirror/android/forget', { key: p.guid || p.address }), 'Forgotten.');
            }
          } }))))
        : h('p', { class: 'muted', text: 'No phone paired yet.' }),
      pairing ? pairForm() : null,
      h('div', { class: 'btn-row' },
        pairing ? null : button('Pair a phone', { kind: 'soft', iconName: 'link', onClick: () => { pairing = true; render(true); } }),
        button('Mirror an Android', { kind: 'primary', iconName: 'play', onClick: () => start('android') })));
  }

  function render(force) {
    const key = JSON.stringify([data, pairing]);
    if (key === shownKey && !force) return;
    shownKey = key;
    clear(body);
    if (!data || data.error) {
      body.append(h('p', { class: 'muted', text: (data && data.error) || 'Loading…' }));
      return;
    }
    enabled.el.querySelector('input').checked = !!data.enabled;
    append(body, [
      enabled.el,
      data.available === false ? banner('info', 'info', 'Not available on this robot', data.detail || '') : null,
      nowBox(),
      iphoneBox(),
      androidBox(),
      h('p', { class: 'muted small', text: data.sound ? 'The phone’s sound plays from LittleBot’s speaker (config mirror.sound).' : 'The phone’s sound stays on the phone (config mirror.sound turns it on).' }),
    ]);
  }

  async function refresh() {
    try { data = await get('/mirror'); } catch (e) { data = { error: e.message || 'Phone mirroring isn’t available.' }; }
    render();
  }

  const el = h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('wifi'), 'Phone mirroring')), body);
  refresh();
  const timer = setInterval(() => { if (!document.hidden && !pairing && !el.contains(document.activeElement)) refresh(); }, 5000);
  return { el, destroy() { clearInterval(timer); } };
}
