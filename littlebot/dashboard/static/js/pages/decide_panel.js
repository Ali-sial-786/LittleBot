// Fast decisions (littlebot/decide): TypeSafe's Jev, OpenAI's Decisions API or Claude for LittleBot's small multiple-choice
// calls. The keys stay in .env on the robot; this page only learns whether one is set.

import { get, put } from '../api.js';
import { banner, clear, h, icon, switchRow } from '../ui.js';

const PROVIDERS = [
  { value: 'off', label: 'Off (nothing is sent)' },
  { value: 'jev', label: 'TypeSafe Jev (TYPESAFE_API_KEY)' },
  { value: 'openai', label: 'OpenAI Decisions API (OPENAI_API_KEY)' },
  { value: 'claude', label: 'Claude (ANTHROPIC_API_KEY)' },
];

const MODES = [
  { value: 'off', label: 'Off' },
  { value: 'shadow', label: 'Shadow (only compare)' },
  { value: 'on', label: 'On (use when sure)' },
];

const FEATURES = {
  cards: ['Screen cards', 'Whether a card fits what LittleBot answered, and which kind (only real kinds).'],
  routing: ['Voice hand-offs', 'Whether a hand-off is a face, a camera look, “who is here”, or a question for ChatGPT.'],
  answered: ['Answered yet?', 'Whether the voice has really answered, or is still saying “hold on” (the card waits).'],
  browser: ['Browser follow-ups', 'Whether what was said is still about the web page (so it doesn’t close too soon).'],
  naming: ['Grown-up, not baby', 'Whether the voice talked to you as if you were the baby, to put it right.'],
  videos: ['Which video', 'Which YouTube result fits what was asked for and your child best.'],
  names: ['Which activity or game', 'Which one was meant when the name matches none closely.'],
  concern: ['Danger check', 'Could someone be in danger? Only ever adds an alert to LittleBot’s own safety check.'],
  addressed: ['Talk in the room', 'When someone talks over LittleBot: was it to LittleBot, or to someone else (then it carries on)?'],
};

export function decidePanel(app) {
  const notices = h('div', { class: 'stack-sm' });
  const provider = h('select', { id: 'decide-provider' }, PROVIDERS.map((p) => h('option', { value: p.value, text: p.label })));
  const keys = h('div', { class: 'chips' });
  const rows = h('div', { class: 'stack-sm' });
  const selects = {};

  provider.addEventListener('change', async () => {
    const r = await app.act(() => put('/decide/settings', { provider: provider.value }), 'Decision model saved.');
    if (r) fill(r);
  });

  const context = switchRow({
    title: 'Log what each decision was about',
    text: 'Each decision in LittleBot’s log also says what it heard and said, the card on the screen, and who face and voice recognition found, so you can judge which way was right. Stays on the robot (data/logs).',
    onChange: async (v) => {
      const r = await app.act(() => put('/decide/settings', { log_context: v }), v ? 'Decisions are logged with what they were about.' : 'Decisions are logged as labels only.');
      if (r) fill(r);
      return r !== null;
    },
  });

  for (const [key, [title, text]] of Object.entries(FEATURES)) {
    const sel = h('select', { id: `decide-${key}`, 'aria-label': `${title} mode` }, MODES.map((m) => h('option', { value: m.value, text: m.label })));
    sel.addEventListener('change', async () => {
      const r = await app.act(() => put('/decide/settings', { modes: { [key]: sel.value } }), `${title}: ${sel.value}.`);
      if (r) fill(r);
    });
    const stats = h('span', { class: 'muted' });
    selects[key] = { sel, stats };
    rows.append(h('div', { class: 'field' },
      h('label', { for: `decide-${key}` }, h('strong', { text: title }), ' ', h('span', { class: 'muted', text: text })),
      sel, stats));
  }

  const el = h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('h2', {}, icon('zap'), 'Fast decisions')),
    h('p', { class: 'muted', text: 'A decision model picks from fixed answers in about 0.2 s, where LittleBot otherwise guesses from word lists or asks ChatGPT for a whole answer. It sends only the few words each decision needs (personal details removed), and only with Cloud communication on. Start with Shadow: it decides alongside LittleBot’s own way and both are logged, nothing changes. Then turn on what agrees well. Paid per use (fractions of a cent a day).' }),
    notices,
    h('div', { class: 'stack mt-1' },
      h('div', { class: 'field' }, h('label', { for: 'decide-provider', text: 'Decision model' }), provider),
      h('div', { class: 'stack-sm' }, h('span', { class: 'muted', text: 'Keys on this robot' }), keys),
      rows,
      context.el));

  function fill(d) {
    if (document.activeElement !== provider) provider.value = d.provider || 'off';
    context.set(d.log_context);
    clear(keys).append(
      h('span', { class: `badge ${d.has_typesafe_key ? 'ok' : ''}` }, icon(d.has_typesafe_key ? 'check' : 'key'), d.has_typesafe_key ? 'TypeSafe key configured' : 'No TypeSafe key'),
      h('span', { class: `badge ${d.has_openai_key ? 'ok' : ''}` }, icon(d.has_openai_key ? 'check' : 'key'), d.has_openai_key ? 'OpenAI key configured' : 'No OpenAI key'),
      h('span', { class: `badge ${d.has_anthropic_key ? 'ok' : ''}` }, icon(d.has_anthropic_key ? 'check' : 'key'), d.has_anthropic_key ? 'Anthropic key configured' : 'No Anthropic key'));
    clear(notices);
    if (d.provider !== 'off' && d.why_off) notices.append(banner('warn', 'alert', 'Not deciding right now', d.why_off === "TYPESAFE_API_KEY isn't set" || d.why_off === "OPENAI_API_KEY isn't set" ? `${d.why_off}: add it to the .env file on the robot, then restart LittleBot.` : `${d.why_off.charAt(0).toUpperCase()}${d.why_off.slice(1)}.`));
    for (const [key, f] of Object.entries(d.features || {})) {
      const s = selects[key];
      if (!s) continue;
      if (document.activeElement !== s.sel) s.sel.value = f.chosen || 'off';
      const parts = [];
      if (f.asked) parts.push(`${f.asked} asked since start`);
      if (f.avg_ms != null) parts.push(`${f.avg_ms} ms`);
      if (f.agreement != null) parts.push(`agrees ${Math.round(f.agreement * 100)}% of ${f.agreed + f.differed}`);
      if (f.used) parts.push(`used ${f.used}×`);
      if (f.failed) parts.push(`${f.failed} failed`);
      s.stats.textContent = parts.join(' · ');
    }
  }

  const load = () => get('/decide').then(fill).catch(() => {});
  load();
  const timer = setInterval(() => { if (!document.hidden) load(); }, 15000);
  return { el, destroy() { clearInterval(timer); } };
}
