// Calendars and phones, per person (littlebot/skills/agenda.py "Whose calendar", push.py, docs/CALENDAR.md): the
// household's and each grown-up's own Google Calendar or iCal address, whose each calendar is, and each phone for the
// heads-ups. LittleBot uses the calendar of whoever is talking (voice and face recognition).

import { get, post, put } from '../api.js';
import { append, banner, button, clear, confirmDialog, h, icon, toast } from '../ui.js';

const field = (id, label, input) => h('div', { class: 'field' }, h('label', { for: id, text: label }), input);
const secretInput = (id, placeholder) => h('input', { class: 'input', id, type: 'password', autocomplete: 'off', spellcheck: 'false', placeholder });
const textInput = (id, placeholder) => h('input', { class: 'input', id, type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder });

export function calendarPanel(app) {
  const body = h('div', { class: 'stack-sm' });
  let data = null;
  let shownKey = '';
  let open = null; // what is being set up: {pid, what, result}

  const whoLabel = (pid) => (pid ? (data.people.find((p) => p.id === pid) || {}).name || 'someone' : 'the family');

  async function act(fn, done) {
    const r = await app.act(fn, done);
    if (r && r.people) { data = r; render(true); }
    return r;
  }

  function googleForm(pid) {
    const cid = textInput(`cal-cid-${pid || 0}`, '1234-abc.apps.googleusercontent.com');
    const sec = secretInput(`cal-sec-${pid || 0}`, 'GOCSPX-…');
    const back = textInput(`cal-back-${pid || 0}`, 'http://127.0.0.1:8889/?state=…&code=…');
    const again = h('p', { class: 'muted small' });
    const finishRow = h('div', { class: 'stack-sm', hidden: true }, again,
      h('p', { class: 'small', text: 'After you allow it, the tab goes to a page that doesn’t load (127.0.0.1). That is expected: copy that whole address from the address bar and paste it here.' }),
      field(back.id, 'The address you landed on', back),
      h('div', { class: 'btn-row' }, button('Finish', { kind: 'primary', onClick: async () => {
        const r = await act(() => post('/calendar/google/finish', { person_id: pid, url: back.value }), `Google Calendar connected for ${whoLabel(pid)}.`);
        if (r) { open = null; render(true); }
      } })));
    const start = button('Open Google’s sign-in', { kind: 'primary', iconName: 'link', onClick: async () => {
      const bodyJson = { person_id: pid };
      if (!data.client_known || cid.value) Object.assign(bodyJson, { client_id: cid.value, client_secret: sec.value });
      const r = await app.act(() => post('/calendar/google/begin', bodyJson));
      if (!r) return;
      window.open(r.url, '_blank', 'noopener');
      clear(again);
      again.append('No new tab? ', h('a', { href: r.url, target: '_blank', rel: 'noopener', text: 'Open Google’s sign-in' }), '.');
      finishRow.hidden = false;
      sec.value = '';
    } });
    return h('div', { class: 'stack-sm' },
      data.client_known
        ? h('p', { class: 'muted small', text: `Signs in with the same Google Cloud app as before. Sign in as ${whoLabel(pid)} on Google’s page (use “switch account” if the phone is signed in as someone else).` })
        : h('div', { class: 'stack-sm' },
          h('p', { class: 'muted small', text: 'Once: make a free “Desktop app” OAuth client in Google Cloud (docs/CALENDAR.md, step 1) and paste its two values here. Everyone after this uses the same one.' }),
          field(cid.id, 'Client ID', cid), field(sec.id, 'Client secret', sec)),
      h('div', { class: 'btn-row' }, start, button('Cancel', { kind: 'soft', onClick: () => { open = null; render(true); } })),
      finishRow);
  }

  function icsForm(pid) {
    const url = secretInput(`cal-ics-${pid || 0}`, 'https://calendar.google.com/calendar/ical/…/basic.ics');
    const name = h('input', { class: 'input', id: `cal-ics-name-${pid || 0}`, type: 'text', autocomplete: 'off', maxlength: 40, list: 'cal-ics-names', placeholder: 'Work' });
    const names = h('datalist', { id: 'cal-ics-names' }, ['Work', 'Personal', 'Family', 'School', 'Kids', 'Sports'].map((v) => h('option', { value: v })));
    return h('div', { class: 'stack-sm' },
      h('p', { class: 'muted small', text: 'Read-only: Google Calendar on a computer → ⋮ next to the calendar → Settings and sharing → Integrate calendar → “Secret address in iCal format”; Outlook → Shared calendars → Publish → ICS; iCloud → share calendar → Public. One calendar at a time: add each one (Work, Personal…) the same way.' }),
      field(name.id, 'Which calendar (so you can ask for it: “Ali’s work calendar”)', name), names,
      field(url.id, 'iCal address (kept secret, never shown again)', url),
      h('div', { class: 'btn-row' },
        button('Check and save', { kind: 'primary', onClick: async () => {
          const r = await act(() => put('/calendar/ics', { person_id: pid, url: url.value, name: name.value }, { timeout: 30000 }));
          if (r) { toast(`Saved ${whoLabel(pid)}’s ${r.calendar.name} calendar: ${r.events} event(s) in the next days.`, 'ok'); open = null; render(true); }
        } }),
        button('Cancel', { kind: 'soft', onClick: () => { open = null; render(true); } })));
  }

  function phoneResult(r, pid) {
    const sub = `${r.server.replace(/\/$/, '')}/${r.topic}`;
    return h('div', { class: 'stack-sm' },
      h('p', {}, `On ${pid ? `${whoLabel(pid)}’s` : 'the'} phone: install the free `, h('strong', { text: 'ntfy' }), ' app, tap + and subscribe to this topic (keep it secret: anyone with it can read the notifications):'),
      h('pre', { class: 'json', text: r.topic }),
      h('p', { class: 'muted small' }, 'Or open ', h('a', { href: sub, target: '_blank', rel: 'noopener', text: sub }), ' on the phone.'),
      h('p', { class: 'muted small', text: r.test_sent ? 'A test message was sent: it shows up once the phone has subscribed.' : `The test didn’t go (${r.test_problem || 'unknown'}).` }),
      button('Done', { kind: 'soft', onClick: () => { open = null; render(true); } }));
  }

  function personRow(pid, name, role, st) {
    const google = st.google
      ? h('span', {}, h('span', { class: 'ok-dot', text: '● ' }), 'Google Calendar', ' ',
        button('Disconnect', { kind: 'soft', small: true, onClick: async () => {
          if (await confirmDialog({ title: `Disconnect ${name}’s Google Calendar?`, confirm: 'Disconnect', body: 'LittleBot stops reading it and can’t add to it. You can connect it again any time.' })) {
            act(() => post('/calendar/google/disconnect', { person_id: pid }), 'Disconnected.');
          }
        } }))
      : button('Connect Google Calendar', { kind: 'soft', small: true, iconName: 'link', onClick: () => { open = { pid, what: 'google' }; render(true); } });
    const icsList = (st.ics || []).map((c) => h('span', {}, h('span', { class: 'ok-dot', text: '● ' }), `${c.name} (iCal)`, ' ',
      button('Remove', { kind: 'soft', small: true, ariaLabel: `Remove ${name}’s ${c.name} calendar`, onClick: async () => {
        if (await confirmDialog({ title: `Remove ${name}’s ${c.name} calendar?`, confirm: 'Remove', body: 'LittleBot forgets its address and stops reading it. The calendar itself doesn’t change.' })) {
          act(() => post('/calendar/ics/remove', { person_id: pid, id: c.id }), 'Removed.');
        }
      } })));
    const ics = h('span', { class: 'btn-row' }, icsList,
      (st.ics || []).length < 8 ? button((st.ics || []).length ? 'Add another iCal address' : 'Add an iCal address', { kind: 'soft', small: true, onClick: () => { open = { pid, what: 'ics' }; render(true); } }) : null);
    const phone = st.phone
      ? h('span', {}, h('span', { class: 'ok-dot', text: '● ' }), 'Phone', ' ',
        button('Test', { kind: 'soft', small: true, onClick: async () => { const r = await app.act(() => post('/calendar/phone/test', { person_id: pid })); if (r) toast(r.detail, r.ok ? 'ok' : 'error'); } }),
        button('Remove', { kind: 'soft', small: true, onClick: () => act(() => post('/calendar/phone/remove', { person_id: pid }), 'Removed.') }))
      : button('Set up a phone', { kind: 'soft', small: true, iconName: 'send', onClick: async () => {
        const r = await app.act(() => post('/calendar/phone', { person_id: pid }));
        if (r) { data = r; open = { pid, what: 'phone', result: r }; render(true); }
      } });
    const row = h('div', { class: 'skill-row stack-sm' },
      h('div', {}, h('strong', { text: name }), role ? h('span', { class: 'muted', text: ` (${role})` }) : null),
      h('div', { class: 'btn-row' }, google, ics, phone));
    if (open && open.pid === pid) {
      row.append(open.what === 'google' ? googleForm(pid) : open.what === 'ics' ? icsForm(pid) : phoneResult(open.result, pid));
    }
    return row;
  }

  function calendarsList() {
    const cals = data.calendars || [];
    if (!cals.length) return null;
    const options = [['0', 'The family']].concat(data.people.map((p) => [String(p.id), p.name]));
    return h('div', { class: 'stack-sm' },
      h('h3', { class: 'small-head', text: 'Whose calendar' }),
      h('p', { class: 'muted small', text: 'LittleBot guesses: a person’s own sign-in is theirs, the family’s is everyone’s, one named after someone is theirs, one called Family, Home or Holidays is everyone’s. Change it here.' }),
      h('ul', { class: 'plain-list' }, cals.map((c) => {
        const sel = h('select', { 'aria-label': `Whose ${c.name} is`, style: { width: 'auto', maxWidth: '55%' } },
          options.map(([v, label]) => h('option', { value: v, text: label, selected: String(c.owner || 0) === v })));
        sel.addEventListener('change', () => act(() => put('/calendar/owner', { key: `${c.account}:${c.id}`, person_id: Number(sel.value) || null }), 'Saved.'));
        return h('li', { class: 'row between' }, h('span', { text: `${c.name}${c.can_edit ? '' : ' (read-only)'}` }), sel);
      })));
  }

  function render(force) {
    const key = JSON.stringify([data, open && { pid: open.pid, what: open.what }]);
    if (key === shownKey && !force) return;
    shownKey = key;
    clear(body);
    if (!data || data.error) {
      body.append(h('p', { class: 'muted', text: (data && data.error) || 'Loading…' }));
      return;
    }
    append(body, [
      h('p', { class: 'muted', text: 'Each grown-up on Who’s who can have their own calendar and phone. When LittleBot knows who is talking (voice or face recognition), “what’s on today?” is their own events and the family’s, and “add the dentist on Tuesday” goes on their calendar. Heads-ups say whose event it is and go to that person’s phone.' }),
      data.problem ? banner('warn', 'alert', 'The calendar has a problem', data.problem) : null,
      personRow(null, 'The family', 'shared, for everyone', data.home),
      ...data.people.map((p) => personRow(p.id, p.name, p.role, p)),
      data.people.length ? null : h('p', { class: 'muted small', text: 'Add the grown-ups under Profile → Who’s who (and their voice or face) to give each one their own calendar.' }),
      calendarsList(),
    ]);
  }

  async function refresh() {
    try { data = await get('/calendar'); } catch (e) { data = { error: e.message || 'The calendar isn’t available.' }; }
    render();
  }

  const el = h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('clock'), 'Calendars and phones')), body);
  refresh();
  const timer = setInterval(() => { if (!document.hidden && !open && !el.contains(document.activeElement)) refresh(); }, 20000);
  return { el, destroy() { clearInterval(timer); } };
}
