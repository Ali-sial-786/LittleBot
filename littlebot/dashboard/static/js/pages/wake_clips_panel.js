// The family's own "hey little bot" clips for the next wake word training (littlebot/audio/wake_clips.py): a
// recording made on a phone or PC, uploaded here, is cut into one clip per "hey little bot" on the robot. Only the
// clips are kept; the page shows how many each person has and can delete them.

import { del, get, upload } from '../api.js';
import { append, button, clear, h, icon, toast } from '../ui.js';

const ENOUGH = 100; // clips a person needs for the training to learn their voice well

export function wakeClipsPanel(app) {
  const body = h('div', { class: 'stack-sm' });
  const names = h('datalist', { id: 'wake-clip-names' });
  const name = h('input', { type: 'text', id: 'wake-clip-name', maxlength: 40, autocomplete: 'off', list: 'wake-clip-names', placeholder: 'Ali' });
  const file = h('input', { type: 'file', id: 'wake-clip-file', accept: 'audio/*,video/mp4,.m4a,.mp3,.wav,.ogg,.webm,.aac,.amr,.3gp' });
  const status = h('p', { class: 'muted small', text: '' });
  let data = null;
  let busy = false;

  const send = button('Upload', { kind: 'primary', iconName: 'mic', onClick: async () => {
    const who = name.value.trim();
    const f = file.files && file.files[0];
    if (!who) { toast('Type who is speaking first.', 'info'); name.focus(); return; }
    if (!f) { toast('Choose the recording first.', 'info'); return; }
    if (data && f.size > data.max_mb * 1024 * 1024) { toast(`That file is bigger than ${data.max_mb} MB: record a shorter one.`, 'error'); return; }
    busy = true;
    send.disabled = true;
    status.textContent = 'Uploading and cutting it into clips… (up to a minute for a few minutes of talking)';
    const r = await app.act(() => upload(`/wake/clips/upload?name=${encodeURIComponent(who)}`, f));
    busy = false;
    send.disabled = false;
    status.textContent = '';
    if (!r) return;
    data = r;
    file.value = '';
    const a = r.added || {};
    toast(`${a.found} clips from that recording (${a.clips} for ${a.name} in all).`, 'ok');
    render();
  } });

  async function remove(who) {
    if (!window.confirm(`Delete all of ${who}’s clips?`)) return;
    const r = await app.act(() => del(`/wake/clips/${encodeURIComponent(who)}`), 'Deleted.');
    if (r) { data = r; render(); }
  }

  function render() {
    clear(body);
    if (!data || data.error) {
      body.append(h('p', { class: 'muted', text: (data && data.error) || 'Loading…' }));
      return;
    }
    clear(names);
    for (const n of data.names || []) names.append(h('option', { value: n }));
    const people = data.people || [];
    append(body, [
      h('p', { text: 'Help LittleBot learn your family’s voices. On your phone or computer, record yourself saying “hey little bot” over and over for 2–3 minutes (breaks or no breaks), then upload the recording here. LittleBot cuts it into one clip per “hey little bot” for its next wake word training.' }),
      h('ul', { class: 'muted small' },
        h('li', { text: 'Mix it up now and then: louder and softer, nearer and further from the phone, faster and slower, from another room.' }),
        h('li', { text: 'A natural pace works best; a tiny breath between them is fine.' }),
        h('li', { text: `Any phone recording works (m4a, mp3, wav …), up to ${data.max_minutes} minutes or ${data.max_mb} MB.` }),
        h('li', { text: 'Only the clips stay on LittleBot; the recording isn’t kept, and nothing leaves the robot.' })),
      h('div', { class: 'field' }, h('label', { for: 'wake-clip-name', text: 'Who is speaking' }), name, names),
      h('div', { class: 'field' }, h('label', { for: 'wake-clip-file', text: 'The recording' }), file),
      h('div', { class: 'btn-row' }, send),
      status,
      people.length ? h('h3', { class: 'small-head', text: 'Clips so far' }) : h('p', { class: 'muted small', text: 'No clips yet.' }),
      people.map((p) => h('div', { class: 'row between skill-row' },
        h('div', {}, h('span', { text: `${p.name}: ${p.clips} clips` }),
          h('div', { class: 'muted small', text: `${p.recordings} recording${p.recordings === 1 ? '' : 's'}${p.clips >= ENOUGH ? ' · enough' : ` · about ${ENOUGH - p.clips} more would help`}` })),
        button('Delete', { kind: 'soft small', iconName: 'trash', onClick: () => remove(p.name) }))),
    ]);
    send.disabled = busy;
  }

  async function refresh() {
    try { data = await get('/wake/clips'); } catch (e) { data = { error: e.message || 'The clips aren’t available.' }; }
    render();
  }

  const el = h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('mic'), 'Wake word: your voices')), body);
  refresh();
  return { el, destroy() {} };
}
