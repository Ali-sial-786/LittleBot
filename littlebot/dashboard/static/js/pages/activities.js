// Activities: filter by age group / category, start / pause / stop, favourites, caregiver prompts.

import { get, post, put } from '../api.js';
import { append, banner, button, clear, fmtAge, fmtDuration, h, icon, pageHead, setText, titleCase } from '../ui.js';

const STEP_KIND = { note: 'Tip for you', song: 'Sing along', sing: 'Sing along', say: 'LittleBot says', prompt: 'Your turn', wait: 'Waiting' };

export default {
  mount(view, app) {
    let meta = null;
    let filter = { mode: null, group: null, category: '', favourites: false };
    let loadSeq = 0;

    const running = h('section', { class: 'card info act-running', hidden: true });
    const suggestTitle = h('h2');
    const suggestNote = h('p', { class: 'muted' });
    const suggestList = h('div', { class: 'suggest-grid' });
    const suggest = h('section', { class: 'card', hidden: true, 'aria-label': 'Suggested activities' },
      h('div', { class: 'card-head' }, suggestTitle), suggestNote, suggestList);
    const groupChips = h('div', { class: 'chips scroll', role: 'group', 'aria-label': 'Age group' });
    const catSelect = h('select', { 'aria-label': 'Category' }, h('option', { value: '', text: 'All categories' }));
    const favChip = h('button', { type: 'button', class: 'chip', 'aria-pressed': 'false' }, icon('star'), 'Favourites');
    const list = h('div', { class: 'act-grid', 'aria-live': 'polite' });
    const countEl = h('p', { class: 'muted' });
    // "Record activity voices in the ChatGPT voice" (littlebot/dashboard/routes/voice_routes.py).
    const voices = h('section', { class: 'card', hidden: true, 'aria-label': 'Activity voices', 'aria-live': 'polite' });
    let voiceTimer = null;
    let withName = null;

    view.append(
      pageHead('Activities', 'Short, gentle games. LittleBot leads with sounds, eyes and movement - you join in and help.'),
      h('div', { class: 'stack' },
        running,
        suggest,
        voices,
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

    async function loadSuggested() {
      let res;
      try { res = await get('/activities/recommended?limit=6'); } catch { suggest.hidden = true; return; }
      clear(suggestList);
      suggest.hidden = !res.available || !res.items.length;
      if (suggest.hidden) return;
      setText(suggestTitle, `Suggested for ${res.nickname || 'your child'}`);
      setText(suggestNote, res.child_age_months != null
        ? `Picked for ${fmtAge(res.child_age_months)} - favourites first.`
        : 'Add your child\'s age in the Child profile for suggestions that fit.');
      for (const a of res.items) {
        suggestList.append(h('div', { class: 'suggest-item' },
          h('div', { class: 'grow' }, h('strong', { text: a.name }), h('div', { class: 'muted', text: [a.age_label, titleCase(a.category)].filter(Boolean).join(' · ') })),
          button('Start', { kind: 'primary small', iconName: 'play', ariaLabel: `Start ${a.name}`, onClick: () => startActivity(a) })));
      }
    }

    // On a phone the running card (with the caregiver's answer buttons) is far above the card
    // that was tapped: show it straight away and bring it into view.
    async function startActivity(a) {
      const r = await app.act(() => post(`/activities/${encodeURIComponent(a.id)}/start`), `Starting “${a.name}”`);
      if (r?.status?.running) {
        renderRunning({ activities: r.status });
        running.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }

    function activityCard(a) {
      const star = h('button', { type: 'button', class: 'star', 'aria-pressed': String(!!a.favourite), 'aria-label': `Favourite ${a.name}` }, icon(a.favourite ? 'starFill' : 'star'));
      star.addEventListener('click', async () => {
        const want = star.getAttribute('aria-pressed') !== 'true';
        const r = await app.act(() => put(`/activities/${encodeURIComponent(a.id)}/preferences`, { favourite: want }));
        if (r) { star.setAttribute('aria-pressed', String(want)); clear(star).append(icon(want ? 'starFill' : 'star')); loadSuggested(); }
      });
      const allow = h('input', { type: 'checkbox', checked: a.enabled !== false });
      const article = h('article', { class: ['card', 'act', a.enabled === false ? 'disabled' : ''] });
      allow.addEventListener('change', async () => {
        const r = await app.act(() => put(`/activities/${encodeURIComponent(a.id)}/preferences`, { enabled: allow.checked }));
        if (!r) allow.checked = !allow.checked;
        article.classList.toggle('disabled', !allow.checked);
        if (r) loadSuggested();
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
      append(article, [
        h('div', { class: 'act-title' }, h('h3', { text: a.name }), star),
        h('div', { class: 'meta' }, badges),
        a.description ? h('p', { class: 'desc', text: a.description }) : null,
        a.participation ? h('p', { class: 'participation' }, h('b', { text: 'Your child: ' }), a.participation) : null,
        details,
        h('div', { class: 'act-foot' },
          h('label', { class: 'mini-toggle', title: 'When off, LittleBot will not suggest this activity' }, allow, 'Allowed'),
          button('Start', { kind: 'primary', iconName: 'play', onClick: () => startActivity(a) }))]);
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
      append(running, [
        h('div', { class: 'row between' },
          h('div', {}, h('div', { class: 'muted', text: a.paused ? 'Paused' : 'Now playing' }), h('h2', { text: a.name || 'Activity' })),
          h('div', { class: 'btn-row' },
            a.paused
              ? button('Resume', { kind: 'primary small', iconName: 'play', onClick: () => app.act(() => post('/activities/resume')) })
              : button('Pause', { kind: 'soft small', iconName: 'pause', onClick: () => app.act(() => post('/activities/pause')) }),
            button('Stop', { kind: 'danger-outline small', iconName: 'square', onClick: () => app.act(() => post('/activities/stop'), 'Activity stopped.') }))),
        a.step_text ? h('p', { class: 'mt-1' }, h('span', { class: 'muted', text: `${total ? `Step ${Math.min((a.step_index || 0) + 1, total)} of ${total}` : ''}${STEP_KIND[a.step_kind] ? ` · ${STEP_KIND[a.step_kind]}` : ''}` }), h('br'), a.step_text) : null]);
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

    async function loadVoices() {
      clearTimeout(voiceTimer);
      let v;
      try { v = await get('/voice-lines'); } catch { voices.hidden = true; return; }
      renderVoices(v);
    }

    function plural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }

    function renderVoices(v) {
      clearTimeout(voiceTimer);
      clear(voices);
      voices.hidden = !v || !v.total;
      if (voices.hidden) return;
      const job = v.job;
      const who = v.nickname || 'your child';
      const all = v.recorded >= v.total;
      const summary = all
        ? `All ${v.total} lines LittleBot says in activities, games and chats are recorded in its ChatGPT voice.`
        : `${v.recorded} of ${v.total} lines LittleBot says in activities, games and chats are recorded in its ChatGPT voice. The others use the robot voice.`;
      append(voices, [
        h('div', { class: 'card-head' }, h('h2', {}, icon('speaker'), 'Activity voices')),
        h('p', { class: 'muted', text: summary })]);
      if (job && job.running) {
        const bar = h('span');
        bar.style.width = `${job.total ? Math.round((100 * job.done) / job.total) : 0}%`;
        const stopIt = async () => {
          const r = await app.act(() => post('/voice-lines/stop'), 'Stopping after this line. What is recorded is kept.');
          if (r) loadVoices();
        };
        voices.append(h('div', { class: 'stack-sm' },
          h('div', { class: 'row between' },
            h('strong', { text: job.phase === 'connecting' ? 'Calling the ChatGPT voice…' : `Recording ${job.done} of ${job.total}` }),
            h('span', { class: 'muted tnum', text: `${job.saved} saved${job.failed_count ? ` · ${job.failed_count} to try again` : ''}` })),
          h('div', { class: 'progress' }, bar),
          job.current ? h('p', { class: 'muted', text: `Now: “${job.current}”` }) : null,
          h('p', { class: 'muted', text: 'LittleBot can keep playing meanwhile: the recording does not use its speaker. New recordings are used straight away.' }),
          h('div', { class: 'btn-row' }, button('Stop recording', { kind: 'danger-outline small', iconName: 'square', onClick: stopIt }))));
        voiceTimer = setTimeout(loadVoices, 3000);
        return;
      }
      if (job) {
        const failed = job.phase === 'failed';
        const done = job.error || `${job.saved} of ${job.total} lines recorded${job.phase === 'stopped' ? ' before you stopped it' : ''}.`;
        const retry = job.failed_count ? ` ${plural(job.failed_count, 'line')} didn't come out right and keep the robot voice: press the button again to try them once more.` : '';
        voices.append(banner(failed ? 'warn' : 'ok', failed ? 'alert' : 'check', 'Last recording', done + retry));
      }
      if (v.blocked) {
        voices.append(banner('info', 'info', 'Recording needs ChatGPT', v.blocked_detail));
        return;
      }
      if (all) return;
      if (withName === null) withName = !!v.share_name;
      const nameBox = h('input', { type: 'checkbox', checked: withName });
      nameBox.addEventListener('change', () => { withName = nameBox.checked; renderVoices(v); });
      const missing = withName ? v.missing : v.missing - v.named_missing;
      const minutes = Math.max(1, Math.round(withName ? v.minutes : v.minutes_without_name));
      const record = async () => {
        const r = await app.act(() => post('/voice-lines/record', { with_name: withName }), 'Recording started.');
        if (r) renderVoices(r);
      };
      append(voices, [
        v.named_missing
          ? h('label', { class: 'check-row' }, nameBox,
            h('span', { text: `Also record the ${plural(v.named_missing, 'line')} with ${who}'s name (this sends the name to ChatGPT)` }))
          : null,
        h('p', { text: `${plural(missing, 'line')} to record: about ${plural(minutes, 'minute')}. The activities for ${who}'s age come first. You can stop any time and carry on later.` }),
        h('div', { class: 'btn-row' }, button('Record activity voices in the ChatGPT voice', { kind: 'primary', iconName: 'mic', onClick: record }))]);
    }

    load();
    loadSuggested();
    loadVoices();
    return { update: renderRunning, destroy: () => clearTimeout(voiceTimer) };
  },
};
