// AI: optional cloud conversation. Keys are never shown - only whether one is configured.

import { get, post, put } from '../api.js';
import { banner, button, clear, h, icon, pageHead, setText, switchRow, titleCase, toast } from '../ui.js';

const PROVIDERS = [
  { value: 'mock', label: 'Built-in scripted replies (no internet)' },
  { value: 'anthropic', label: 'Anthropic Claude' },
  { value: 'openai', label: 'OpenAI' },
];

function humanize(key) {
  const t = String(key).replace(/_usd$/, ' (USD)').replace(/_ms$/, ' (ms)').replace(/_/g, ' ');
  return t.charAt(0).toUpperCase() + t.slice(1);
}

export default {
  mount(view, app) {
    let data = null;
    const notices = h('div', { class: 'stack-sm' });
    const enabled = switchRow({
      title: 'Use cloud AI for conversation',
      text: 'Lets LittleBot send short, filtered sentences to the provider below to get more varied answers. Off by default.',
      onChange: async (v) => {
        const r = await app.act(() => put('/ai/settings', { enabled: v }), v ? 'Cloud AI turned on.' : 'Cloud AI turned off.');
        if (r) fill(r);
        return r !== null;
      },
    });
    const shareFlag = (key, title, text) => switchRow({
      title, text,
      onChange: async (v) => {
        const r = await app.act(() => put('/ai/settings', { [key]: v }), v ? 'Will be shared with the cloud AI.' : 'No longer shared.');
        if (r) fill(r);
        return r !== null;
      },
    });
    const shareName = shareFlag('share_child_name', "Share your child's nickname",
      'Lets the cloud AI use the nickname from the child profile. Off: the name is replaced by “[name]” before anything is sent.');
    const shareInterests = shareFlag('share_interests', 'Share favourite animals & interests',
      'Sends the favourite animals and interests from the profile so answers can be more personal. Never sent when off.');
    const pttResult = h('div', { class: 'stack-sm' });
    const ptt = button('Push to talk', {
      kind: 'primary', iconName: 'mic',
      onClick: async () => {
        clear(pttResult).append(h('p', { class: 'muted', text: 'Listening… speak to LittleBot now.' }));
        const r = await app.act(() => post('/ai/push-to-talk', {}, { timeout: 60000 }));
        clear(pttResult);
        if (!r) return;
        if (r.error) { pttResult.append(banner('warn', 'mic', 'Nothing to answer', r.detail || r.error)); return; }
        pttResult.append(h('dl', { class: 'kv' },
          h('dt', { text: 'Heard' }), h('dd', { text: r.heard || '(nothing)' }),
          h('dt', { text: 'LittleBot said' }), h('dd', { text: r.reply || '(no reply)' }),
          h('dt', { text: 'Answered by' }), h('dd', { text: r.provider || '–' })));
      },
    });
    const provider = h('select', { id: 'ai-provider' }, PROVIDERS.map((p) => h('option', { value: p.value, text: p.label })));
    const keys = h('div', { class: 'chips' });
    const num = (id, min, max, step = 1) => h('input', { type: 'number', id, min, max, step, inputmode: step < 1 ? 'decimal' : 'numeric' });
    const maxWords = num('ai-words', 3, 60);
    const perHour = num('ai-hour', 0, 600);
    const perDay = num('ai-day', 0, 5000);
    const budget = num('ai-budget', 0, 20, 0.05);
    const testProvider = h('select', { id: 'ai-test-provider', 'aria-label': 'Provider to test' }, PROVIDERS.map((p) => h('option', { value: p.value, text: titleCase(p.value === 'mock' ? 'scripted' : p.value) })));
    const testResult = h('div', { class: 'stack-sm' });
    const usage = h('dl', { class: 'kv' });
    const brainState = h('dl', { class: 'kv' });

    const save = button('Save AI settings', {
      kind: 'primary',
      onClick: async () => {
        const body = {
          provider: provider.value,
          max_words: parseInt(maxWords.value, 10),
          max_requests_per_hour: parseInt(perHour.value, 10),
          max_requests_per_day: parseInt(perDay.value, 10),
          daily_budget_usd: parseFloat(budget.value),
        };
        for (const [k, v] of Object.entries(body)) if (typeof v === 'number' && Number.isNaN(v)) { toast(`Please fill in ${k.replace(/_/g, ' ')}.`, 'error'); return; }
        const r = await app.act(() => put('/ai/settings', body), 'AI settings saved.');
        if (r) fill(r);
      },
    });

    view.append(
      pageHead('AI conversation', 'LittleBot always works offline with friendly scripted replies. Cloud AI is optional.'),
      h('div', { class: 'stack' },
        h('section', { class: 'card info' },
          h('div', { class: 'stack-sm' },
            h('h2', {}, 'How cloud AI works'),
            h('p', { text: 'Cloud AI needs a separate, paid API key from Anthropic or OpenAI. A Claude.ai or ChatGPT subscription does not include API access.' }),
            h('p', {}, 'Put the key in the ', h('code', { text: '.env' }), ' file on the robot (', h('code', { text: 'ANTHROPIC_API_KEY' }), ' or ', h('code', { text: 'OPENAI_API_KEY' }), ') and restart LittleBot. Keys stay on the robot and are never shown here.'),
            h('p', { text: 'For babies and young toddlers (under 2 years) LittleBot only ever uses scripted, age-appropriate responses - even when cloud AI is on. Every AI reply is length-limited and safety-checked before it is spoken.' }))),
        notices,
        h('div', { class: 'grid two' },
          h('section', { class: 'card' },
            h('div', { class: 'card-head' }, h('h2', {}, icon('cloud'), 'Cloud AI')),
            enabled.el,
            h('div', { class: 'stack mt-1' },
              h('div', { class: 'field' }, h('label', { for: 'ai-provider', text: 'Provider' }), provider),
              h('div', { class: 'stack-sm' }, h('span', { class: 'muted', text: 'API keys on this robot' }), keys)),
            h('div', { class: 'divider mt-2' }),
            h('div', { class: 'mt-1' }, h('h3', { text: 'What the cloud AI may know' }), shareName.el, shareInterests.el)),
          h('section', { class: 'card' },
            h('div', { class: 'card-head' }, h('h2', {}, icon('sliders'), 'Limits')),
            h('div', { class: 'form-grid two' },
              h('div', { class: 'field' }, h('label', { for: 'ai-words', text: 'Max words per reply' }), maxWords, h('span', { class: 'hint', text: 'Younger children get even shorter replies automatically.' })),
              h('div', { class: 'field' }, h('label', { for: 'ai-budget', text: 'Daily budget (USD)' }), budget),
              h('div', { class: 'field' }, h('label', { for: 'ai-hour', text: 'Requests per hour' }), perHour),
              h('div', { class: 'field' }, h('label', { for: 'ai-day', text: 'Requests per day' }), perDay)),
            h('div', { class: 'mt-2' }, save)),
          h('section', { class: 'card' },
            h('div', { class: 'card-head' }, h('h2', {}, icon('wifi'), 'Connection test')),
            h('p', { class: 'muted', text: 'Sends one tiny test request (costs a fraction of a cent for cloud providers).' }),
            h('div', { class: 'inline-form mt-2' }, h('div', { class: 'field' }, testProvider),
              button('Test', { kind: 'soft', iconName: 'zap', onClick: runTest })),
            testResult),
          h('section', { class: 'card' },
            h('div', { class: 'card-head' }, h('h2', {}, icon('mic'), 'Talk to LittleBot')),
            h('p', { class: 'muted', text: 'LittleBot listens once through its microphone, then answers out loud - like a walkie-talkie button. Works with scripted replies too.' }),
            h('div', { class: 'btn-row mt-2' }, ptt), pttResult),
          h('section', { class: 'card' },
            h('div', { class: 'card-head' }, h('h2', {}, icon('history'), 'Usage today')),
            usage, h('div', { class: 'divider mt-2' }), h('div', { class: 'mt-2' }, brainState)))));

    async function runTest() {
      clear(testResult).append(h('p', { class: 'muted', text: 'Testing…' }));
      const r = await app.act(() => post('/ai/test', { provider: testProvider.value }));
      clear(testResult);
      if (!r) return;
      const res = r.result || {};
      testResult.append(res.ok
        ? banner('ok', 'check', 'Connection works', res.latency_ms != null ? `Answered in ${Math.round(res.latency_ms)} ms.` : '')
        : banner('danger', 'alert', 'Connection failed', res.error || 'No details available.'));
    }

    function kvRows(dl, obj) {
      clear(dl);
      const entries = Object.entries(obj || {}).filter(([, v]) => v !== null && typeof v !== 'object');
      if (!entries.length) { dl.append(h('dt', { text: 'No data' }), h('dd', { text: '–' })); return; }
      for (const [k, v] of entries) dl.append(h('dt', { text: humanize(k) }), h('dd', { text: typeof v === 'boolean' ? (v ? 'Yes' : 'No') : String(v) }));
    }

    function fill(d) {
      data = d;
      const st = d.settings || {};
      enabled.set(st.enabled);
      shareName.set(st.share_child_name);
      shareInterests.set(st.share_interests);
      if (document.activeElement !== provider) provider.value = st.provider || 'mock';
      if (document.activeElement !== maxWords) maxWords.value = st.max_words ?? '';
      if (document.activeElement !== perHour) perHour.value = st.max_requests_per_hour ?? '';
      if (document.activeElement !== perDay) perDay.value = st.max_requests_per_day ?? '';
      if (document.activeElement !== budget) budget.value = st.daily_budget_usd ?? '';
      if (!testProvider.dataset.touched) testProvider.value = st.provider || 'mock';
      clear(keys).append(
        h('span', { class: `badge ${d.keys?.has_anthropic_key ? 'ok' : ''}` }, icon(d.keys?.has_anthropic_key ? 'check' : 'key'), d.keys?.has_anthropic_key ? 'Anthropic key configured' : 'No Anthropic key'),
        h('span', { class: `badge ${d.keys?.has_openai_key ? 'ok' : ''}` }, icon(d.keys?.has_openai_key ? 'check' : 'key'), d.keys?.has_openai_key ? 'OpenAI key configured' : 'No OpenAI key'));
      clear(notices);
      if (!d.available) notices.append(banner('warn', 'alert', 'Conversation system unavailable', 'LittleBot cannot hold conversations right now.'));
      if (d.privacy?.offline_mode) notices.append(banner('info', 'info', 'Offline mode', 'LittleBot is running in offline mode, so cloud AI is not available.'));
      else if (st.enabled && !d.privacy?.cloud_enabled) {
        const link = h('a', { href: '#/privacy', text: 'Privacy' });
        notices.append(h('div', { class: 'banner warn' }, icon('alert'), h('div', {}, h('strong', { text: 'Cloud communication is off' }), h('span', {}, 'Cloud AI is on, but nothing will be sent until you also allow cloud communication in ', link, '.'))));
      }
      if (st.enabled && st.provider === 'anthropic' && !d.keys?.has_anthropic_key) notices.append(banner('warn', 'key', 'No Anthropic key', 'Add ANTHROPIC_API_KEY to the .env file on the robot, then restart LittleBot.'));
      if (st.enabled && st.provider === 'openai' && !d.keys?.has_openai_key) notices.append(banner('warn', 'key', 'No OpenAI key', 'Add OPENAI_API_KEY to the .env file on the robot, then restart LittleBot.'));
      if (!d.policy?.cloud_ai_age_ok) notices.append(banner('info', 'shield', 'Scripted replies only for now', `Your child is under ${Math.round((d.policy?.min_age_months_for_cloud ?? 24) / 12)} years (or no age is set). LittleBot answers with gentle scripted responses of up to ${d.policy?.max_reply_words ?? 8} words.`));
      kvRows(usage, d.brain?.usage);
      kvRows(brainState, { active_provider: d.brain?.active_provider, cloud_allowed: d.brain?.cloud_allowed, reason: d.brain?.reason, last_error: d.brain?.last_error });
    }
    testProvider.addEventListener('change', () => { testProvider.dataset.touched = '1'; });

    get('/ai').then(fill).catch((e) => notices.append(banner('danger', 'alert', 'Could not load AI settings', e.message)));
    const timer = setInterval(() => { if (!document.hidden) get('/ai').then((d) => { data = d; kvRows(usage, d.brain?.usage); }).catch(() => {}); }, 15000);
    return { destroy() { clearInterval(timer); } };
  },
};
