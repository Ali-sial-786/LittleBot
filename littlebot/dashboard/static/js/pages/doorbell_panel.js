// Doorbell and camera alerts (littlebot/skills/doorbell.py, docs/DOORBELL.md): LittleBot says when the doorbell
// rings or a camera sees someone. A phone or Home Assistant calls LittleBot's secret address; this card shows that
// address, how to set each one up, a test ring, a new address, and what is said.

import { get, post, put } from '../api.js';
import { append, banner, button, clear, confirmDialog, h, icon, switchRow, toast } from '../ui.js';

const KIND_LABELS = {
  doorbell: 'Doorbell presses',
  person: 'A person seen',
  package: 'A package seen',
  motion: 'Movement',
  vehicle: 'Cars',
  animal: 'Animals',
  other: 'Anything else',
};
const WHY = {
  kind_off: 'that kind isn’t said out loud (see the switches)',
  quiet_hours: 'it’s quiet hours (the heads-ups’ quiet hours)',
  asleep: 'LittleBot is asleep',
  busy: 'a video or a conversation is on',
  too_many: 'too many alerts this minute',
  off: 'door alerts are off',
};

const pre = (text) => h('pre', { class: 'json', text });

function macrodroidSteps(url) {
  return h('ol', { class: 'stack-sm' },
    h('li', {}, 'On an Android phone that has the Aosu app and stays at home on the Wi-Fi (an old phone is fine), install MacroDroid (free) from the Play Store and give it notification access when it asks.'),
    h('li', {}, 'Add a macro. Trigger: ', h('strong', { text: 'Notification → Notification Received' }), ', Application: ', h('strong', { text: 'aosu' }), ' (any text).'),
    h('li', {}, 'Action: ', h('strong', { text: 'Connectivity → HTTP Request' }), '. Method POST, this address:', pre(url)),
    h('li', {}, 'Body (content type application/json):', pre('{"title":"[notification_title]","text":"[notification]","app":"aosu"}'),
      h('span', { class: 'muted small', text: 'The words in square brackets are MacroDroid “magic text”: pick them from its magic-text list (the … button) so the names match your version; the title and the notification’s text are the ones needed.' })),
    h('li', {}, 'Save, then ring your doorbell. LittleBot says “Someone’s at the front door.”'));
}

function companionSteps(url) {
  return h('div', { class: 'stack-sm' },
    h('ol', { class: 'stack-sm' },
      h('li', {}, 'In the Home Assistant Companion app on the Android phone with Aosu: Settings → Companion app → Manage sensors → ', h('strong', { text: 'Last notification' }), ': turn it on, and add the Aosu app to its allow list.'),
      h('li', {}, 'In Home Assistant, add this to configuration.yaml (restart Home Assistant after):'),
      pre(`rest_command:\n  littlebot_door:\n    url: "${url}"\n    method: POST\n    content_type: "application/json"\n    payload: >-\n      {"event": {{ event | default('') | to_json }}, "camera": {{ camera | default('') | to_json }},\n       "title": {{ title | default('') | to_json }}, "text": {{ text | default('') | to_json }}}`),
      h('li', {}, 'And an automation (change sensor.my_phone_last_notification to your phone’s sensor):'),
      pre(`automation:\n  - alias: "Aosu to LittleBot"\n    trigger:\n      - platform: state\n        entity_id: sensor.my_phone_last_notification\n    condition:\n      - "{{ 'aosu' in (trigger.to_state.attributes.package | default('')) }}"\n    action:\n      - service: rest_command.littlebot_door\n        data:\n          title: "{{ trigger.to_state.attributes['android.title'] | default('') }}"\n          text: "{{ trigger.to_state.state }}"`)));
}

function haSteps() {
  return h('div', { class: 'stack-sm' },
    h('p', { text: 'For a doorbell Home Assistant already knows (Nest, Eufy, Ring, Reolink …): add the rest_command above, then one automation per thing to say. Pick your doorbell’s own entity in the trigger (Settings → Devices).' }),
    pre(`automation:\n  - alias: "Doorbell to LittleBot"\n    trigger:\n      - platform: state\n        entity_id: binary_sensor.front_door_ding   # your doorbell's press\n        to: "on"\n    action:\n      - service: rest_command.littlebot_door\n        data:\n          event: doorbell\n          camera: Front Door\n  - alias: "Package to LittleBot"\n    trigger:\n      - platform: state\n        entity_id: binary_sensor.front_door_package   # your camera's package or person sensor\n        to: "on"\n    action:\n      - service: rest_command.littlebot_door\n        data:\n          event: package          # or person, motion, vehicle, animal\n          camera: Front Door`),
    h('p', { class: 'muted small', text: 'Newer Ring and Nest set-ups have an event entity (event.front_door_ding) instead: trigger on any change of its state.' }));
}

export function doorbellPanel(app) {
  const body = h('div', { class: 'stack-sm' });
  const urlInput = h('input', { class: 'input', id: 'doorbell-url', readonly: true, spellcheck: 'false', autocomplete: 'off' });
  const enabledRow = switchRow({ title: 'Door and camera alerts', text: 'LittleBot says it out loud and shows it on its screen.', onChange: (v) => save({ enabled: v }) });
  const nightRow = switchRow({ title: 'Doorbell presses at night too', text: 'A ring is said even in the heads-ups’ quiet hours or while LittleBot sleeps. Everything else waits for the day.', onChange: (v) => save({ at_night: v }) });
  const kindRows = {};
  const kindsBox = h('div', { class: 'stack-sm' });
  let kinds = [];
  let shownKey = '';

  for (const [kind, label] of Object.entries(KIND_LABELS)) {
    kindRows[kind] = switchRow({ title: label, onChange: (v) => save({ events: kinds.filter((k) => k !== kind).concat(v ? [kind] : []) }) });
    kindsBox.append(kindRows[kind].el);
  }

  async function save(change) {
    const r = await app.act(() => put('/doorbell/settings', change), 'Saved.');
    if (r) render(r, true);
    return r !== null;
  }

  const copyBtn = button('Copy', { kind: 'soft small', iconName: 'link', onClick: async () => {
    try { await navigator.clipboard.writeText(urlInput.value); toast('Copied.', 'ok'); } catch {
      urlInput.focus(); urlInput.select(); toast('Selected: copy it with your phone’s or computer’s copy.', 'ok');
    }
  } });
  const testBtn = button('Send a test', { kind: 'primary', iconName: 'speaker', onClick: async () => {
    const r = await app.act(() => post('/doorbell/test'));
    if (r) toast(r.said ? 'LittleBot is saying it.' : `Not said: ${WHY[r.why] || r.why || 'unknown'}.`, r.said ? 'ok' : 'error');
  } });
  const rotateBtn = button('New address', { kind: 'soft small', iconName: 'refresh', onClick: async () => {
    const yes = await confirmDialog({ title: 'Make a new address?', warn: true, confirm: 'New address',
      body: 'The old address stops working at once: put the new one in the phone or Home Assistant.' });
    if (!yes) return;
    const r = await app.act(() => post('/doorbell/rotate'), 'New address made.');
    if (r) render(r, true);
  } });

  function render(d, force) {
    const key = JSON.stringify(d);
    if (key === shownKey && !force) return;
    shownKey = key;
    clear(body);
    if (!d.available) {
      body.append(h('p', { class: 'muted', text: d.error || 'Door alerts are switched off in LittleBot’s config (skills.doorbell).' }));
      return;
    }
    kinds = d.events || [];
    enabledRow.set(d.enabled);
    nightRow.set(d.at_night);
    for (const [kind, row] of Object.entries(kindRows)) row.set(kinds.includes(kind));
    urlInput.value = d.url || '';
    const last = (d.last || []).map((e) => h('li', {}, `${new Date(e.at * 1000).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' })}: ${KIND_LABELS[e.kind] || e.kind}${e.camera ? ` · ${e.camera}` : ''}${e.said ? '' : ' (not said)'}`));
    append(body, [
      h('p', { class: 'muted', text: 'Aosu doorbells and cameras can’t be reached directly, so a phone (or Home Assistant) tells LittleBot when something happens, by calling this secret address on your home Wi-Fi. No video: only what happened. Nothing leaves LittleBot. Set-up help: docs/DOORBELL.md.' }),
      d.reachable ? null : banner('warn', 'wifi', 'The phone can’t reach LittleBot yet', d.lan_help || ''),
      h('div', { class: 'field' }, h('label', { for: 'doorbell-url', text: 'LittleBot’s doorbell address (keep it secret, like a password)' }), h('div', { class: 'row gap' }, urlInput, copyBtn)),
      h('div', { class: 'btn-row' }, testBtn, rotateBtn),
      enabledRow.el,
      h('h3', { class: 'small-head', text: 'Said out loud' }), kindsBox, nightRow.el,
      last.length ? h('div', {}, h('h3', { class: 'small-head', text: 'Lately' }), h('ul', { class: 'plain-list' }, last)) : null,
      h('details', { class: 'disclosure' }, h('summary', { text: 'Set up: Android phone with MacroDroid (Aosu)' }), macrodroidSteps(d.url || '')),
      h('details', { class: 'disclosure' }, h('summary', { text: 'Set up: Android phone with the Home Assistant app (Aosu)' }), companionSteps(d.url || '')),
      h('details', { class: 'disclosure' }, h('summary', { text: 'Set up: Home Assistant with Nest, Eufy, Ring or another doorbell' }), haSteps()),
      h('details', { class: 'disclosure' }, h('summary', { text: 'iPhone?' }),
        h('p', { text: 'An iPhone doesn’t let any app read another app’s notifications, so it can’t pass Aosu’s alerts on. Use an Android phone left at home on the Wi-Fi with the Aosu app (an old one works), or Home Assistant with a doorbell it supports.' })),
    ]);
  }

  async function refresh() {
    try { render(await get('/doorbell')); } catch (e) { render({ available: false, error: e.message || 'Door alerts aren’t available.' }); }
  }

  const el = h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('home'), 'Doorbell and cameras')), body);
  refresh();
  const timer = setInterval(() => { if (!document.hidden && !el.contains(document.activeElement)) refresh(); }, 15000);
  return { el, destroy() { clearInterval(timer); } };
}
