// Voices and models (voice_setup_routes.py): what the wake word starts, what listens (and what listens alongside it,
// to compare), what thinks, what speaks, and the Cartesia voice (searched and heard on LittleBot before choosing).
// A choice is kept on the robot instead of config/local.yaml's until "Back to the config file". Keys stay on the robot.

import { get, post, put } from '../api.js';
import { append, banner, button, clear, h, icon, switchRow, toast } from '../ui.js';

const PIPELINES = [
  { value: 'auto', label: 'Automatic: the fast assistant when its keys are set, else GPT-Live' },
  { value: 'cascade', label: 'The fast assistant (speech-to-text → Claude → natural voice)' },
  { value: 'live', label: 'GPT-Live (ChatGPT’s own voice)' },
];
const LISTEN = [
  { value: 'deepgram', label: 'Deepgram Flux', key: 'deepgram', note: 'Fast, and knows when you have finished talking.' },
  { value: 'cartesia', label: 'Cartesia Ink 2', key: 'cartesia', note: 'Among the most accurate in public tests. Doesn’t say which words it was unsure of.' },
  { value: 'assemblyai', label: 'AssemblyAI Universal-3.6 Pro', key: 'assemblyai', note: 'Most accurate in its maker’s test. Words come only in pauses, so talking over LittleBot stops it a little later.' },
  { value: 'openai', label: 'OpenAI', key: 'openai', note: 'Writes it down after you pause: slower.' },
  { value: 'vosk', label: 'Vosk (on the robot)', key: null, note: 'No internet needed; the least accurate.' },
];
const COMPARE = LISTEN.slice(0, 3);
const BRAINS = [
  { value: 'claude-haiku-5-5', label: 'Claude Haiku 5.5', note: 'The fastest and cheapest.' },
  { value: 'claude-sonnet-5-5', label: 'Claude Sonnet 5.5', note: 'Smarter, but it always thinks first, so answers start later. About 20× Haiku 5.5’s price.' },
  { value: 'claude-opus-5-5', label: 'Claude Opus 5.5', note: 'The smartest and slowest to start. About 40× Haiku 5.5’s price.' },
  { value: 'claude-haiku-4-5', label: 'Claude Haiku 4.5', note: 'The older Haiku (LittleBot’s backup). About 10× Haiku 5.5’s price.' },
];
const DEEP = [
  { value: 'off', label: 'Off (the brain above answers everything)' },
  { value: 'claude-sonnet-5-5', label: 'Claude Sonnet 5.5' },
  { value: 'claude-opus-5-5', label: 'Claude Opus 5.5 (slower)' },
];
const SPEAK = [
  { value: 'cartesia', label: 'Cartesia Sonic', key: 'cartesia', note: 'The fastest natural voice. Pick its voice below.' },
  { value: 'elevenlabs', label: 'ElevenLabs', key: 'elevenlabs', note: 'Natural, a little slower to start.' },
  { value: 'openai', label: 'OpenAI', key: 'openai', note: 'Natural, slower to start.' },
  { value: 'local', label: 'On the robot', key: null, note: 'Robotic, but needs no internet.' },
];
const GENDERS = [['', 'Any voice'], ['feminine', 'Feminine'], ['masculine', 'Masculine'], ['gender_neutral', 'Neutral']];

export function voiceSetupPanel(app) {
  let data = null;
  const notices = h('div', { class: 'stack-sm' });
  const fields = {}; // name -> {sel, hint, mark}

  async function save(body, done) {
    const r = await app.act(() => put('/assistant/setup', body), done);
    if (r) fill(r);
    return r;
  }

  function choice(name, label, options, toBody) {
    const sel = h('select', { id: `setup-${name}` });
    const hint = h('span', { class: 'hint' });
    const mark = h('span', { class: 'badge brand', hidden: true }, 'changed here');
    sel.addEventListener('change', () => save(toBody(sel.value), 'Saved.'));
    fields[name] = { sel, hint, mark, options };
    return h('div', { class: 'field' }, h('label', { for: sel.id }, h('strong', { text: label }), ' ', mark), sel, hint);
  }

  function options(name, value, keys, none) {
    const f = fields[name];
    const opts = [...f.options];
    if (value == null && none) { opts.unshift(none); value = none.value; }
    if (value != null && !opts.some((o) => o.value === value)) opts.push({ value, label: `${value} (from the config file)` });
    const want = JSON.stringify(opts.map((o) => [o.value, keys && o.key ? keys[o.key] : null]));
    if (f.sel.dataset.opts !== want) {
      clear(f.sel).append(...opts.map((o) => h('option', { value: o.value, text: o.key && keys && !keys[o.key] ? `${o.label} (no key yet)` : o.label })));
      f.sel.dataset.opts = want;
    }
    if (document.activeElement !== f.sel) f.sel.value = value ?? 'off';
    const picked = opts.find((o) => o.value === (value ?? 'off'));
    f.hint.textContent = picked && picked.note ? picked.note : '';
  }

  const pipelineField = choice('pipeline', 'The wake word starts', PIPELINES, (v) => ({ pipeline: v }));
  const listenField = choice('stt', 'Listens with', LISTEN, (v) => ({ listen: v }));
  const brainField = choice('brain', 'Thinks with', BRAINS, (v) => ({ brain: v }));
  const deepField = choice('deep_model', 'Hard questions (“think harder”) go to', DEEP, (v) => ({ deep_model: v }));
  const speakField = choice('tts', 'Speaks with', SPEAK, (v) => ({ speak: v }));
  const order = { stt: h('p', { class: 'muted small' }), tts: h('p', { class: 'muted small' }) };

  // ---------- what listens alongside, to compare
  const compareBoxes = {};
  const compareRow = h('div', { class: 'stack-sm' });
  for (const o of COMPARE) {
    const input = h('input', { type: 'checkbox', id: `setup-compare-${o.value}` });
    const text = h('span', { text: o.label });
    input.addEventListener('change', async () => {
      const want = COMPARE.filter((c) => compareBoxes[c.value].input.checked).map((c) => c.value);
      const body = { compare: want };
      const logOff = !(data && data.choices.log_words.value);
      if (want.length && logOff) body.log_words = true; // nothing is compared without the words in the log
      const r = await save(body, want.length && logOff ? 'Saved. The words heard now go in the log too (needed to compare).' : 'Saved.');
      if (!r) input.checked = !input.checked;
    });
    compareBoxes[o.value] = { input, text };
    compareRow.append(h('label', { class: 'check', for: input.id }, input, text));
  }
  const compareMark = h('span', { class: 'badge brand', hidden: true }, 'changed here');
  const logWords = switchRow({
    title: 'Write what is heard and said in the log',
    text: 'Needed to compare. Stays on the robot (data/logs). For testing: turn it off when you are done.',
    onChange: async (v) => !!(await save({ log_words: v }, v ? 'The words go in the log.' : 'The words are no longer written down.')),
  });

  // ---------- the Cartesia voice
  const voiceNow = h('p', {});
  const query = h('input', { type: 'text', class: 'input', id: 'setup-voice-q', maxlength: 80, placeholder: 'Search: warm, British, narrator…', autocomplete: 'off', 'aria-label': 'Search Cartesia’s voices' });
  const gender = h('select', { id: 'setup-voice-gender', 'aria-label': 'Kind of voice', style: { width: 'auto', flex: '0 0 auto' } }, GENDERS.map(([value, text]) => h('option', { value, text })));
  const results = h('div', { class: 'stack-sm' });
  let next = null;
  const moreBtn = button('More voices', { kind: 'soft small', onClick: () => search(true) });
  moreBtn.hidden = true;
  query.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); search(false); } });

  async function search(more) {
    const params = new URLSearchParams();
    if (query.value.trim()) params.set('q', query.value.trim());
    if (gender.value) params.set('gender', gender.value);
    if (more && next) params.set('after', next);
    let res;
    try { res = await get(`/assistant/setup/voices?${params}`); } catch (e) { toast(e.message || 'Couldn’t ask Cartesia.', 'error'); return; }
    if (!more) clear(results);
    for (const v of res.voices || []) results.append(voiceRow(v));
    if (!more && !(res.voices || []).length) results.append(h('p', { class: 'muted', text: 'No voices found. Try other words.' }));
    next = res.next || null;
    moreBtn.hidden = !next;
  }

  function voiceRow(v) {
    const inUse = data && data.choices.cartesia_voice.value === v.id;
    const about = [v.tagline, v.accent, v.mine ? 'your own voice' : ''].filter(Boolean).join(' · ');
    return h('div', { class: 'row between skill-row', dataset: { voice: v.id } },
      h('div', {}, h('strong', { text: v.name || v.id }), about ? h('span', { class: 'muted', text: ` ${about}` }) : null,
        v.description ? h('div', { class: 'muted small', text: v.description }) : null),
      h('div', { class: 'btn-row' },
        button('Hear it', { kind: 'soft small', iconName: 'speaker', onClick: () => app.act(() => post('/assistant/setup/voices/try', { id: v.id }), 'LittleBot is saying it.') }),
        inUse ? h('span', { class: 'badge ok' }, icon('check'), 'In use')
          : button('Use', { kind: 'primary small', onClick: async () => { if (await save({ cartesia_voice: v.id }, `LittleBot now speaks as ${v.name || 'that voice'}.`)) { loadVoice(); search(false); } } })));
  }

  async function loadVoice() {
    try {
      const v = await get('/assistant/setup/voice');
      append(clear(voiceNow), [h('strong', { text: 'In use: ' }), v.name ? `${v.name}${v.tagline ? ` · ${v.tagline}` : ''}` : v.id,
        v.error ? h('span', { class: 'muted small', text: ` (${v.error})` }) : null]);
    } catch { /* shown by the poll */ }
  }

  const resetBtn = button('Back to the config file', { kind: 'soft small', iconName: 'refresh', onClick: async () => {
    const r = await app.act(() => post('/assistant/setup/reset'), 'Back to config/local.yaml’s choices.');
    if (r) { fill(r); loadVoice(); }
  } });

  const el = h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('h2', {}, icon('sliders'), 'Voices and models')),
    h('p', { class: 'muted', text: 'What LittleBot listens, thinks and speaks with. A change is used straight away, or when the conversation going on ends. It is kept on the robot instead of config/local.yaml’s choice until you press “Back to the config file”.' }),
    notices,
    h('div', { class: 'stack mt-1' },
      pipelineField,
      listenField, order.stt,
      h('div', { class: 'field' }, h('span', {}, h('strong', { text: 'Also listening, to compare' }), ' ', compareMark),
        h('span', { class: 'hint', text: 'They hear the same sound and change nothing; what each heard goes in the log side by side. On the robot, .venv/bin/python scripts/stt_compare.py lists where they differ. Each costs about as much as the one listening.' }),
        compareRow),
      logWords.el,
      brainField, deepField,
      speakField, order.tts,
      h('div', { class: 'field' }, h('strong', { text: 'Cartesia voice' }), voiceNow,
        query, h('div', { class: 'row' }, gender, button('Search', { kind: 'soft', onClick: () => search(false) })),
        h('span', { class: 'hint', text: '“Hear it” has LittleBot say a line in that voice on its speaker (a few Cartesia credits).' }),
        results, h('div', { class: 'btn-row' }, moreBtn)),
      h('div', { class: 'btn-row' }, resetBtn)));

  function fill(d) {
    data = d;
    const c = d.choices;
    options('pipeline', c.pipeline.value);
    options('stt', c.stt.value[0], d.keys);
    options('brain', c.brain.value, null, { value: '', label: 'ChatGPT (the config file sets no Claude model)', note: 'Pick a Claude model to use it instead.' });
    options('deep_model', c.deep_model.value);
    options('tts', c.tts.value[0], d.keys);
    for (const [name, key] of [['pipeline', 'pipeline'], ['stt', 'stt'], ['brain', 'brain'], ['deep_model', 'deep_model'], ['tts', 'tts']]) fields[name].mark.hidden = !c[key].changed;
    const live = d.live || {};
    const now = (x) => (x && x.first ? ` Now: ${x.first}${x.last && x.last !== x.first ? ` (last used: ${x.last})` : ''}.` : '');
    order.stt.textContent = `In order: ${c.stt.value.join(' → ')}. If one fails or has no key, the next one listens.${now(live.stt)}`;
    order.tts.textContent = `In order: ${c.tts.value.join(' → ')}. If one fails or has no key, the next one speaks.${now(live.tts)}`;
    const shadows = c.stt_shadow.value || [];
    for (const o of COMPARE) {
      const box = compareBoxes[o.value];
      const listening = c.stt.value[0] === o.value;
      if (document.activeElement !== box.input) box.input.checked = shadows.includes(o.value) && !listening;
      box.input.disabled = listening;
      box.text.textContent = `${o.label}${listening ? ' (listening already)' : ''}${d.keys[o.key] ? '' : ' (no key yet)'}`;
    }
    compareMark.hidden = !c.stt_shadow.changed;
    logWords.set(c.log_words.value);
    resetBtn.disabled = !Object.values(c).some((x) => x.changed);
    clear(notices);
    if (!d.running) notices.append(banner('warn', 'alert', 'The assistant isn’t running', 'The choices are kept and used when it starts.'));
    if (live.waiting) notices.append(banner('info', 'clock', 'Waiting for the conversation to end', 'The new choices are used as soon as it does.'));
    if (!d.cloud_ok) notices.append(banner('warn', 'cloud', 'Cloud communication is off', 'Only the robot’s own listening and voice work now (Privacy page).'));
    if (shadows.length && !c.log_words.value) notices.append(banner('warn', 'alert', 'Nothing is compared yet', 'Switch on “Write what is heard and said in the log”.'));
    const noKey = shadows.filter((n) => !d.keys[n]);
    if (noKey.length) notices.append(banner('warn', 'key', `No key for ${noKey.join(' and ')}`, 'Add it on the Keys page (More → Keys) to compare it.'));
  }

  const load = () => get('/assistant/setup').then(fill).catch(() => {});
  load();
  loadVoice();
  const timer = setInterval(() => { if (!document.hidden) load(); }, 15000);
  return { el, destroy() { clearInterval(timer); } };
}
