// Activities: filter by age group / category, start / pause / stop, favourites, caregiver prompts.

import { get, post, put } from '../api.js';
import { button, clear, fmtAge, fmtDuration, h, icon, pageHead, setText, titleCase } from '../ui.js';

const STEP_KIND = { note: 'Tip for you', song: 'Sing along', sing: 'Sing along', say: 'LittleBot says', prompt: 'Your turn', wait: 'Waiting' };

export default {
  mount(view, app) {
    let meta = null;
    let filter = { mode: null, group: null, category: '', favourites: false };
    let loadSeq = 0;

    const running = h('section', { class: 'card info', hidden: true });
    const groupChips = h('div', { class: 'chips scroll', role: 'group', 'aria-label': 'Age group' });
    const catSelect = h('select', { 'aria-label': 'Category' }, h('option', { value: '', text: 'All categories' }));
    const favChip = h('button', { type: 'button', class: 'chip', 'aria-pressed': 'false' }, icon('star'), 'Favourites');
    const list = h('div', { class: 'act-grid', 'aria-live': 'polite' });
    const countEl = h('p', { class: 'muted' });

    view.append(
      pageHead('Activities', 'Short, gentle games. LittleBot leads with sounds, eyes and movement - you join in and help.'),
      h('div', { class: 'stack' },
        running,
        h('div', { class: 'stack-sm' }, groupChips, h('div', { class: 'row' }, h('div', { class: 'grow' }, catSelect), favChip)),
        countEl,
        list));

    catSelect.addEventListener('change', () => { filter.category = catSelect.value; load(); });
    favChip.addEventListener('click', () => {
      filter.favourites = !filter.favourites;
      favChip.setAttribute('aria-pressed', String(filter.favourites));
      load();
    });

    function renderChips() {
      clear(groupChips);
      const chips = [];
      if (meta.child_age_months != null) chips.push({ mode: 'child', label: `For your child · ${fmtAge(meta.child_age_months)}` });
      chips.push({ mode: 'all', label: 'All ages' });
      for (const g of meta.age_groups) chips.push({ mode: 'group', group: g.key, label: g.label });
      for (const c of chips) {
        const pressed = c.mode === filter.mode && (c.mode !== 'group' || c.group === filter.group);
        const b = h('button', { type: 'button', class: 'chip', 'aria-pressed': String(pressed), text: c.label });
        b.addEventListener('click', () => { filter.mode = c.mode; filter.group = c.group || null; renderChips(); load(); });
        groupChips.append(b);
      }
    }

    async function load() {
      const seq = ++loadSeq;
      const q = new URLSearchParams();
      if (filter.mode === 'child' && meta?.child_age_months != null) q.set('age_months', meta.child_age_months);
      if (filter.mode === 'group' && filter.group) q.set('age_group', filter.group);
      if (filter.category) q.set('category', filter.category);
      if (filter.favourites) q.set('favourites', 'true');
      let res;
      try {
        res = await get(`/activities?${q}`);
      } catch (e) {
        if (seq === loadSeq) { clear(list).append(h('p', { class: 'empty', text: e.message })); }
        return;
      }
      if (seq !== loadSeq) return;
      if (!meta) {
        meta = res;
        filter.mode = res.child_age_months != null ? 'child' : 'all';
        for (const c of res.categories) catSelect.append(h('option', { value: c, text: titleCase(c) }));
        renderChips();
        if (filter.mode === 'child') { load(); return; }
      }
      renderList(res);
    }

    function renderList(res) {
      clear(list);
      if (!res.available) {
        list.append(h('div', { class: 'empty card flat' }, icon('shapes'), 'The activities engine is not available right now.'));
        setText(countEl, '');
        return;
      }
      setText(countEl, `${res.items.length} ${res.items.length === 1 ? 'activity' : 'activities'}`);
      if (!res.items.length) {
        list.append(h('div', { class: 'empty card flat' }, icon('shapes'), filter.favourites ? 'No favourites yet - tap the star on an activity.' : 'No activities match these filters.'));
        return;
      }
      for (const a of res.items) list.append(activityCard(a));
    }

    function activityCard(a) {
      const star = h('button', { type: 'button', class: 'star', 'aria-pressed': String(!!a.favourite), 'aria-label': `Favourite ${a.name}` }, icon(a.favourite ? 'starFill' : 'star'));
      star.addEventListener('click', async () => {
        const want = star.getAttribute('aria-pressed') !== 'true';
        const r = await app.act(() => put(`/activities/${encodeURIComponent(a.id)}/preferences`, { favourite: want }));
        if (r) { star.setAttribute('aria-pressed', String(want)); clear(star).append(icon(want ? 'starFill' : 'star')); }
      });
      const allow = h('input', { type: 'checkbox', checked: a.enabled !== false });
      const article = h('article', { class: ['card', 'act', a.enabled === false ? 'disabled' : ''] });
      allow.addEventListener('change', async () => {
        const r = await app.act(() => put(`/activities/${encodeURIComponent(a.id)}/preferences`, { enabled: allow.checked }));
        if (!r) allow.checked = !allow.checked;
        article.classList.toggle('disabled', !allow.checked);
      });
      const badges = [
        a.age_label ? h('span', { class: 'badge brand', text: a.age_label }) : null,
        a.category ? h('span', { class: 'badge', text: titleCase(a.category) }) : null,
        a.duration_seconds ? h('span', { class: 'badge' }, icon('clock'), `~${fmtDuration(a.duration_seconds)}`) : null,
        a.suitable_now ? h('span', { class: 'badge ok', text: 'Good age match' }) : null,
      ];
      const details = (a.parent_instructions || a.safety_notes)
        ? h('details', {},
          h('summary', {}, icon('chevron', 'chev'), 'For grown-ups'),
          h('div', { class: 'body' },
            a.parent_instructions ? h('p', { text: a.parent_instructions }) : null,
            a.safety_notes ? h('div', { class: 'safety' }, icon('shield'), h('span', { text: a.safety_notes })) : null))
        : null;
      article.append(
        h('div', { class: 'act-title' }, h('h3', { text: a.name }), star),
        h('div', { class: 'meta' }, badges),
        a.description ? h('p', { class: 'desc', text: a.description }) : null,
        a.participation ? h('p', { class: 'participation' }, h('b', { text: 'Your child: ' }), a.participation) : null,
        details,
        h('div', { class: 'act-foot' },
          h('label', { class: 'mini-toggle', title: 'When off, LittleBot will not suggest this activity' }, allow, 'Allowed'),
          button('Start', { kind: 'primary', iconName: 'play', onClick: () => app.act(() => post(`/activities/${encodeURIComponent(a.id)}/start`), `Starting “${a.name}”`) })));
      return article;
    }

    let runKey = '';
    function renderRunning(s) {
      const a = s.activities || {};
      const p = a.prompt;
      const key = a.running ? `${a.id}|${a.paused}|${a.step_index}|${a.step_kind}|${a.step_text}|${p?.prompt_id}|${p?.text}` : '';
      if (key === runKey) return;
      runKey = key;
      running.hidden = !a.running;
      clear(running);
      if (!a.running) return;
      const total = Number(a.total_steps) || 0;
      running.append(
        h('div', { class: 'row between' },
          h('div', {}, h('div', { class: 'muted', text: a.paused ? 'Paused' : 'Now playing' }), h('h2', { text: a.name || 'Activity' })),
          h('div', { class: 'btn-row' },
            a.paused
              ? button('Resume', { kind: 'primary small', iconName: 'play', onClick: () => app.act(() => post('/activities/resume')) })
              : button('Pause', { kind: 'soft small', iconName: 'pause', onClick: () => app.act(() => post('/activities/pause')) }),
            button('Stop', { kind: 'danger-outline small', iconName: 'square', onClick: () => app.act(() => post('/activities/stop'), 'Activity stopped.') }))),
        a.step_text ? h('p', { class: 'mt-1' }, h('span', { class: 'muted', text: `${total ? `Step ${Math.min((a.step_index || 0) + 1, total)} of ${total}` : ''}${STEP_KIND[a.step_kind] ? ` · ${STEP_KIND[a.step_kind]}` : ''}` }), h('br'), a.step_text) : null);
      if (p && p.text) {
        const opts = (p.options && p.options.length ? p.options : ['done', 'skip']).slice(0, 6);
        running.append(h('div', { class: 'card prompt mt-2' },
          h('div', { class: 'prompt-label' }, icon('person'), 'LittleBot is asking you'),
          h('p', { class: 'prompt-text', text: p.text }),
          h('div', { class: 'btn-row fill' }, opts.map((o, i) => button(titleCase(o), {
            kind: i === 0 ? 'primary' : 'soft',
            onClick: () => app.act(() => post('/activities/respond', { response: o, prompt_id: p.prompt_id ?? null })),
          })))));
      }
    }

    load();
    return { update: renderRunning };
  },
};
