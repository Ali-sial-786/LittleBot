// Calendars (littlebot/info/calendar.py): each person's Google calendars (Work, Personal, ...) that LittleBot may
// read, added with their secret iCal link. The link goes to the robot once and never comes back to this page.

import { del, get, post } from '../api.js';
import { banner, button, clear, confirmDialog, h, icon, setText } from '../ui.js';

const LABELS = ['Work', 'Personal', 'Family', 'School', 'Kids', 'Sports'];

function ago(iso) {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

export function calendarsPanel(app) {
  const notices = h('div', { class: 'stack-sm' });
  const list = h('div', { class: 'stack' });
  const today = h('p', { class: 'muted', 'aria-live': 'polite', hidden: true, style: { whiteSpace: 'pre-line', margin: 0 } });
  const peopleList = h('datalist', { id: 'calendar-people' });
  const labelList = h('datalist', { id: 'calendar-labels' }, LABELS.map((l) => h('option', { value: l })));

  const person = h('input', { type: 'text', id: 'calendar-person', maxlength: 40, autocomplete: 'off', list: 'calendar-people', placeholder: 'Ali' });
  const label = h('input', { type: 'text', id: 'calendar-label', maxlength: 40, autocomplete: 'off', list: 'calendar-labels', placeholder: 'Work' });
  const link = h('input', {
    type: 'url', id: 'calendar-link', maxlength: 2050, autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off',
    inputmode: 'url', placeholder: 'https://calendar.google.com/calendar/ical/…/basic.ics',
  });
  const addBtn = h('button', { type: 'submit', class: 'btn primary' }, icon('plus'), h('span', { text: 'Add calendar' }));
  const formNote = h('p', { class: 'muted', 'aria-live': 'polite', hidden: true, style: { margin: 0 } });
  const form = h('form', { class: 'stack', hidden: true },
    h('div', { class: 'form-grid two' },
      h('div', { class: 'field' }, h('label', { for: 'calendar-person', text: 'Whose calendar' }), person,
        h('span', { class: 'hint', text: 'Their name, the way you say it to LittleBot (“what’s on Ali’s calendar?”).' })),
      h('div', { class: 'field' }, h('label', { for: 'calendar-label', text: 'Which calendar' }), label,
        h('span', { class: 'hint', text: 'Work, Personal… so you can ask for one (“Ali’s work calendar”). Empty: the calendar’s own name.' }))),
    h('div', { class: 'field' }, h('label', { for: 'calendar-link', text: 'Secret address in iCal format' }), link,
      h('span', { class: 'hint', text: 'From Google Calendar’s settings (steps below). LittleBot reads it now to check it, keeps it on the robot only, and never shows it again.' })),
    formNote,
    h('div', { class: 'btn-row' }, addBtn,
      button('Done', { kind: 'soft', onClick: () => { form.hidden = true; addOpen.hidden = false; formNote.hidden = true; } })),
    peopleList, labelList);
  const addOpen = button('Add a calendar', { kind: 'primary', iconName: 'plus', onClick: () => openForm() });

  const steps = h('details', { class: 'disclosure' }, h('summary', { text: 'How to find a calendar’s secret address (2 minutes)' }),
    h('div', { class: 'stack-sm mt-1' },
      h('ol', { class: 'stack-sm', style: { margin: 0, paddingLeft: '22px' } },
        h('li', {}, 'On a computer, open ', h('strong', { text: 'calendar.google.com' }), ', signed in as the calendar’s owner. (On a phone: open it in the browser and choose “Desktop site” in the browser’s menu; the Calendar app doesn’t show it.)'),
        h('li', {}, 'On the left, under ', h('strong', { text: 'My calendars' }), ', point at the calendar, click its ', h('strong', { text: '⋮' }), ' menu, then ', h('strong', { text: 'Settings and sharing' }), '.'),
        h('li', {}, 'Scroll down to ', h('strong', { text: 'Integrate calendar' }), ' and copy the ', h('strong', { text: 'Secret address in iCal format' }), ' (the copy button next to it). It ends in ', h('code', { text: 'basic.ics' }), '.'),
        h('li', {}, 'Paste it above, with the person’s name and the calendar’s name, and press ', h('strong', { text: 'Add calendar' }), '.'),
        h('li', {}, 'Same person, another calendar (Work and Personal)? The form stays open with their name: paste the next one.')),
      h('p', { class: 'muted', style: { margin: 0 } }, h('strong', { text: 'Work calendar with no secret address? ' }),
        'Some workplaces switch it off for their Google accounts. Then ask your IT team, or add only your personal calendar. Calendars from other apps (Outlook, iCloud) work too: use their “publish” or “share” link that ends in .ics.'),
      h('p', { class: 'muted', style: { margin: 0 } }, h('strong', { text: 'Keep it secret. ' }),
        'Anyone with the link can see that calendar. LittleBot only reads it (it never changes your calendar) and sends only the events you ask about to the AI: titles, times and places, never notes or guests. To stop a link working, choose “Reset” next to the secret address in Google Calendar, and remove it here.')));

  const el = h('section', { class: 'card span-2' },
    h('div', { class: 'card-head' }, h('h2', {}, icon('calendar'), 'Calendars')),
    h('div', { class: 'stack' },
      h('p', { class: 'muted', style: { margin: 0 }, text: 'LittleBot can tell you what’s on: “What’s on today?”, “Is Sam busy on Friday?”, “What’s on my work calendar this week?”, “When is the dentist?”. Add each Google calendar once. Each person can have as many as they like, for example Work and Personal.' }),
      notices, list, h('div', { class: 'btn-row' }, addOpen), form, steps,
      h('div', { class: 'btn-row' }, button('What’s on today?', { kind: 'soft', iconName: 'calendar', onClick: showToday })), today));

  function openForm(name) {
    form.hidden = false;
    addOpen.hidden = true;
    if (name) person.value = name;
    (person.value ? (label.value ? link : label) : person).focus();
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!person.value.trim()) { person.focus(); return; }
    if (!link.value.trim()) { link.focus(); return; }
    addBtn.disabled = true;
    formNote.hidden = false;
    setText(formNote, 'Checking the calendar…');
    const who = person.value.trim();
    const r = await app.act(() => post('/calendars', { person: who, label: label.value.trim(), link: link.value.trim() }, { timeout: 30000 }));
    addBtn.disabled = false;
    if (!r) { formNote.hidden = true; return; }
    const c = r.calendar || {};
    const check = r.check || {};
    setText(formNote, check.ok
      ? `Added ${c.person}’s ${c.label} calendar: ${check.events_soon ?? 0} events in the next two weeks${check.next ? `, next: ${check.next}` : ''}. Another calendar for ${c.person}? Paste its link.`
      : `Added ${c.person}’s ${c.label} calendar, but LittleBot couldn’t read it yet: ${check.detail || check.problem}`);
    link.value = '';
    label.value = '';
    person.value = who;
    label.focus();
    render(r);
  });

  function row(c) {
    const state = c.ok === true
      ? h('span', { class: 'badge ok' }, icon('check'), `${c.events_soon ?? 0} events in the next two weeks`)
      : c.ok === false ? h('span', { class: 'badge danger' }, icon('alert'), 'Not working') : h('span', { class: 'badge' }, 'Not read yet');
    const detail = h('span', { class: 'muted', style: { fontSize: '.85rem' } },
      [c.link, c.name && c.name !== c.label ? `“${c.name}”` : '', c.checked_at ? `read ${ago(c.checked_at)}` : ''].filter(Boolean).join(' · '));
    const problem = c.ok === false && c.problem_detail ? h('span', { class: 'hint error', style: { fontSize: '.85rem' }, text: c.problem_detail }) : null;
    return h('div', { class: 'media-item', style: { alignItems: 'flex-start' } },
      h('div', { class: 'grow stack-sm', style: { gap: '4px' } },
        h('div', { class: 'row' }, h('strong', { text: c.label }), state), detail, problem),
      h('div', { class: 'btn-row', style: { flex: 'none' } },
        button('Check', { kind: 'soft small', iconName: 'refresh', ariaLabel: `Check ${c.person}’s ${c.label} calendar`, onClick: () => check(c) }),
        button('', { kind: 'danger-outline small', iconName: 'trash', ariaLabel: `Remove ${c.person}’s ${c.label} calendar`, onClick: () => remove(c) })));
  }

  function render(d) {
    clear(notices);
    if (!d.installed) notices.append(banner('warn', 'alert', 'The calendar reader isn’t installed', 'Run scripts/install_pi.sh again on the robot (or .venv/bin/python -m pip install -e .), then restart LittleBot.'));
    if (d.blocked) notices.append(banner('warn', 'cloud', 'LittleBot can’t read calendars right now', d.blocked === 'cloud_off' ? 'Cloud communication is off in Privacy. Calendars you add are kept and read once it is on.' : 'LittleBot is in offline mode.'));
    clear(list);
    const byPerson = new Map();
    for (const c of d.calendars || []) {
      if (!byPerson.has(c.person)) byPerson.set(c.person, []);
      byPerson.get(c.person).push(c);
    }
    if (!byPerson.size) list.append(h('p', { class: 'empty', style: { padding: '8px' }, text: 'No calendars yet. Add the first one below.' }));
    for (const [name, cals] of byPerson) {
      list.append(h('div', { class: 'stack-sm' },
        h('div', { class: 'row', style: { justifyContent: 'space-between' } },
          h('strong', {}, icon('person'), ` ${name}`),
          button(`Add another for ${name}`, { kind: 'soft small', iconName: 'plus', onClick: () => { label.value = ''; openForm(name); } })),
        h('div', { class: 'media-list' }, cals.map(row))));
    }
    addOpen.disabled = (d.count || 0) >= (d.max || 20);
    clear(peopleList).append(...(d.people || []).map((p) => h('option', { value: p })));
    suggestPeople();
  }

  let suggested = false;
  async function suggestPeople() {
    if (suggested) return;  // the family members from Who's who, once (it may not be set up: then nothing)
    suggested = true;
    try {
      const d = await get('/people');
      const have = new Set([...peopleList.children].map((o) => o.value.toLowerCase()));
      for (const p of d.people || []) if (p.name && !have.has(p.name.toLowerCase())) peopleList.append(h('option', { value: p.name }));
    } catch { /* fine without */ }
  }

  async function check(c) {
    const r = await app.act(() => post(`/calendars/${c.id}/check`, {}, { timeout: 30000 }),
      (res) => (res.ok ? `${c.person}’s ${c.label}: ${res.events_soon ?? 0} events in the next two weeks${res.next ? `; next: ${res.next}` : ''}.` : `${c.person}’s ${c.label}: ${res.detail || res.problem}`));
    if (r) render(r);
  }

  async function remove(c) {
    const ok = await confirmDialog({
      title: `Remove ${c.person}’s ${c.label} calendar?`, danger: true, confirm: 'Remove',
      body: 'LittleBot forgets its link and stops reading it. Your Google calendar itself doesn’t change.',
    });
    if (!ok) return;
    const r = await app.act(() => del(`/calendars/${c.id}`), 'Calendar removed.');
    if (r) render(r);
  }

  async function showToday() {
    today.hidden = false;
    setText(today, 'Reading the calendars…');
    try { setText(today, (await get('/calendars/agenda', { timeout: 30000 })).summary); } catch (e) { setText(today, e.message); }
  }

  get('/calendars').then(render).catch((e) => notices.append(banner('danger', 'alert', 'Could not load the calendars', e.message)));
  return { el };
}
