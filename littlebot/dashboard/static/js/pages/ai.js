// AI: optional cloud conversation. Keys and ChatGPT sign-in tokens are never shown - only whether one is set up.

import { get, post, put } from '../api.js';
import { banner, button, clear, fmtDuration, h, icon, pageHead, setText, switchRow, titleCase, toast } from '../ui.js';

const PROVIDERS = [
  { value: 'mock', label: 'Built-in scripted replies (no internet)' },
  { value: 'anthropic', label: 'Anthropic Claude' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'chatgpt', label: 'ChatGPT (sign in with your plan)' },
];

const LIVE_PHASES = { connecting: 'Connecting…', listening: 'Listening', speaking: 'Talking', thinking: 'Looking something up…', ended: 'Ended' };

function fmtClock(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

const PTT_ERRORS = { nothing_heard: "LittleBot didn't hear anything. Try again and speak close to it.", microphone_disabled: 'The microphone is turned off in Privacy.', no_audio: 'No speaker or microphone is available.' };

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
        if (r.error) { pttResult.append(banner('warn', 'mic', 'Nothing to answer', r.detail || PTT_ERRORS[r.error] || r.error)); return; }
        pttResult.append(h('dl', { class: 'kv' },
          h('dt', { text: 'Heard' }), h('dd', { text: r.heard || '(nothing)' }),
          h('dt', { text: 'LittleBot said' }), h('dd', { text: r.reply || '(no reply)' }),
          h('dt', { text: 'Answered by' }), h('dd', { text: r.provider || '–' })));
      },
    });
    const provider = h('select', { id: 'ai-provider' }, PROVIDERS.map((p) => h('option', { value: p.value, text: p.label })));
    const keys = h('div', { class: 'chips' });
    const num = (id, min, max, step = 1) => h('input', { type: 'number', id, min, max, step, inputmode: step < 1 ? 'decimal' : 'numeric' });
    const maxWords = num('ai-words', 3, 150);
    const perHour = num('ai-hour', 0, 600);
    const perDay = num('ai-day', 0, 5000);
    const budget = num('ai-budget', 0, 20, 0.05);
    const testProvider = h('select', { id: 'ai-test-provider', 'aria-label': 'Provider to test' }, PROVIDERS.map((p) => h('option', { value: p.value, text: titleCase(p.value === 'mock' ? 'scripted' : p.value) })));
    const testResult = h('div', { class: 'stack-sm' });
    const usage = h('dl', { class: 'kv' });
    const brainState = h('dl', { class: 'kv' });
    const account = h('div', { class: 'stack-sm' }, h('p', { class: 'muted', text: 'Loading…' }));
    const live = h('div', { class: 'stack-sm' });
    const present = h('input', { type: 'checkbox', id: 'live-present' });
    const wakeSwitch = switchRow({
      title: 'Wake word: say “Hey LittleBot” to start live voice',
      text: 'LittleBot listens for “Hey LittleBot” or “Hey Bot” on the robot itself, offline - nothing is sent anywhere until it hears one. Then it plays a short “listening” sound and starts live voice, as if you had pressed Start.',
      onChange: async (v) => {
        const r = await app.act(() => put('/chatgpt/wake', { enabled: v }), v ? 'Wake word on.' : 'Wake word off.');
        if (r) { fillChatGPT(r); schedule(1500); }  // the listener needs a moment to start / stop
        return r !== null;
      },
    });
    const wakeState = h('div', { class: 'stack-sm', 'aria-live': 'polite' });
    const lookQuestion = h('input', { type: 'text', id: 'look-question', maxlength: 300, autocomplete: 'off', placeholder: 'How many fingers am I holding up?' });
    const lookSpeak = h('input', { type: 'checkbox', id: 'look-speak' });
    const lookState = h('div', { class: 'stack-sm' });
    const lookResult = h('div', { class: 'stack-sm', 'aria-live': 'polite' });
    const lookButton = button('Look and answer', { kind: 'primary', iconName: 'camera', onClick: runLook });
    lookQuestion.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); lookButton.click(); } });

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
            h('p', { text: 'Easiest: sign in with your ChatGPT account below and pick “ChatGPT” as the provider. It uses your own ChatGPT plan, no API key. It relies on OpenAI endpoints that are not officially documented, so it may stop working if OpenAI changes them.' }),
            h('p', {}, 'Or use a separate, paid API key from Anthropic or OpenAI: put it in the ', h('code', { text: '.env' }), ' file on the robot (', h('code', { text: 'ANTHROPIC_API_KEY' }), ' or ', h('code', { text: 'OPENAI_API_KEY' }), ') and restart LittleBot. Keys and sign-ins stay on the robot and are never shown here.'),
            h('p', { text: 'For babies and young toddlers (under 2 years) LittleBot only ever uses scripted, age-appropriate responses - even when cloud AI is on. Every AI reply is length-limited and safety-checked before it is spoken.' }))),
        notices,
        h('div', { class: 'grid two' },
          h('section', { class: 'card' },
            h('div', { class: 'card-head' }, h('h2', {}, icon('key'), 'ChatGPT account')),
            account),
          h('section', { class: 'card' },
            h('div', { class: 'card-head' }, h('h2', {}, icon('wave'), 'Live voice (GPT-Live)')),
            h('p', { class: 'muted', text: 'A real-time spoken conversation with ChatGPT’s voice, on your plan. You start and stop it here (or say “Hey LittleBot”); it ends by itself after the time limit or when nobody talks. Everything it says is checked against LittleBot’s safety rules while it talks: a rule break cuts the voice, ends the call and alerts you. Some words may already be heard by then, so stay with your child. Your voices go to OpenAI; nothing is recorded on LittleBot.' }),
            live,
            h('div', { class: 'divider mt-2' }),
            h('div', { class: 'mt-1' }, wakeSwitch.el, wakeState))),
        h('section', { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('camera'), 'Ask what LittleBot sees')),
          h('p', { class: 'muted', text: 'LittleBot takes one photo with its camera and sends it with your question to ChatGPT - only when you press the button, or when someone asks during live voice (for example “how many fingers am I holding up?”). The photo is kept in memory just long enough to send, never stored on LittleBot and never shown here; you only see the answer. Needs “Let the AI look through the camera when asked” on in Privacy (off by default).' }),
          h('div', { class: 'stack-sm mt-1' },
            h('div', { class: 'field' }, h('label', { for: 'look-question', text: 'Your question' }), lookQuestion),
            h('label', { class: 'check-row' }, lookSpeak, h('span', { text: 'Say the answer out loud' })),
            h('div', { class: 'btn-row' }, lookButton),
            lookResult,
            lookState)),
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
              h('div', { class: 'field' }, h('label', { for: 'ai-words', text: 'Max words per reply' }), maxWords, h('span', { class: 'hint', text: 'Grown-ups get full answers up to this length; LittleBot keeps it short and simple with your child.' })),
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

    function renderAccount(d) {
      const a = d.account || {};
      clear(account);
      if (a.signed_in) {
        account.append(
          h('p', {}, 'Signed in as ', h('strong', { text: a.email || 'your ChatGPT account' }), a.plan ? ` (${titleCase(a.plan)} plan)` : ''),
          h('p', { class: 'muted', text: 'The sign-in is stored only on LittleBot and is never sent to this page.' }),
          h('div', { class: 'btn-row' }, button('Sign out', { kind: 'soft', iconName: 'logout', onClick: () => act(() => post('/chatgpt/sign-out', {}), 'Signed out of ChatGPT.') })));
      } else if (a.pending) {
        const url = a.pending.verification_url;
        account.append(
          h('p', { text: 'On your phone or computer, open this page, log in to ChatGPT and enter the code:' }),
          h('p', {}, h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', text: url.replace(/^https:\/\//, '') })),
          h('p', { class: 'sign-in-code', text: a.pending.user_code }),
          h('p', { class: 'muted', text: `Waiting for your approval… the code expires in ${fmtDuration(a.pending.expires_in)}.` }),
          h('div', { class: 'btn-row' }, button('Cancel', { kind: 'soft', iconName: 'x', onClick: () => act(() => post('/chatgpt/sign-in/cancel', {})) })));
      } else {
        account.append(
          h('p', { class: 'muted', text: 'Uses your own ChatGPT plan instead of a paid API key. LittleBot shows a code; you approve it on OpenAI’s website with your ChatGPT login.' }),
          h('div', { class: 'btn-row' }, button('Sign in with ChatGPT', { kind: 'primary', iconName: 'key', onClick: () => act(() => post('/chatgpt/sign-in', {})) })));
        if (d.privacy && !d.privacy.cloud_enabled) account.append(banner('info', 'lock', 'Cloud communication is off', 'Turn it on in Privacy before signing in.'));
      }
      if (a.error) account.append(banner('warn', 'alert', 'Sign-in did not finish', a.error));
    }

    function renderLive(d) {
      const l = d.live || {};
      clear(live);
      if (l.active) {
        live.append(
          h('dl', { class: 'kv' },
            h('dt', { text: 'Now' }), h('dd', { text: LIVE_PHASES[l.phase] || l.phase || '–' }),
            h('dt', { text: 'Time left' }), h('dd', { text: fmtClock(l.remaining_s) }),
            l.voice_in_use ? [h('dt', { text: 'Voice' }), h('dd', { text: titleCase(l.voice_in_use) })] : null,
            l.photos != null ? [h('dt', { text: 'Photos this call' }), h('dd', { text: String(l.photos) })] : null),
          l.captions?.length
            ? h('div', { class: 'captions', 'aria-live': 'polite' }, l.captions.map((c) => (c.who === 'note'
              ? h('p', { class: 'caption-note', text: c.text })
              : h('p', {}, h('strong', { text: c.who === 'heard' ? 'Heard: ' : 'LittleBot: ' }), c.text))))
            : null,
          h('div', { class: 'btn-row' }, button('Stop live voice', { kind: 'danger', iconName: 'stop', onClick: () => act(() => post('/chatgpt/live/stop', {}), 'Live voice stopped.') })));
        return;
      }
      if (!l.available) live.append(banner('info', 'info', 'Not installed on this robot yet', 'Live voice needs the “live” extra (aiortc). See docs/CHATGPT.md.'));
      live.append(
        h('label', { class: 'check-row' }, present, h('span', { text: 'I am with my child and will stay for the whole conversation.' })),
        h('div', { class: 'btn-row' }, button('Start live voice', {
          kind: 'primary', iconName: 'mic',
          onClick: () => {
            if (!present.checked) { toast('Please confirm you are with your child.', 'error'); return null; }
            return act(() => post('/chatgpt/live/start', { parent_present: true }, { timeout: 30000 }), 'Live voice starting…');
          },
        })));
      if (l.blocked && !['parent_not_present', 'already_running'].includes(l.blocked) && l.blocked_detail) live.append(h('p', { class: 'muted', text: l.blocked_detail }));
      const last = l.last_call;
      if (last) {
        if (last.safety_stop) live.append(banner('danger', 'shield', 'Last call was stopped by the safety rules', `It broke the “${last.safety_stop}” rule, so LittleBot cut it off.`));
        else if (last.error) live.append(banner('warn', 'alert', 'Last call', last.error));
        else {
          const extra = [last.voice ? `voice: ${titleCase(last.voice)}` : '', last.photos ? `${last.photos} photo${last.photos === 1 ? '' : 's'}` : ''].filter(Boolean).join(', ');
          live.append(h('p', { class: 'muted', text: `Last call: ${last.ended_reason} after ${fmtDuration(last.duration_s)}${extra ? ` (${extra})` : ''}.` }));
        }
      }
    }

    async function act(fn, ok) {
      const r = await app.act(fn, ok);
      if (r && (r.account || r.live)) fillChatGPT(r);
      schedule(300);
      return r;
    }

    const LOOK_FIX = { ai_camera_off: true, camera_off: true, cloud_off: true };
    function renderLook(st) {
      clear(lookState);
      if (!st) return;
      if (st.blocked) {
        const why = st.blocked_detail || 'LittleBot can’t look right now.';
        lookState.append(LOOK_FIX[st.blocked]
          ? h('div', { class: 'banner info' }, icon('lock'), h('div', {}, h('strong', { text: 'Can’t look right now' }), h('span', {}, `${why} `, h('a', { href: '#/privacy', text: 'Open Privacy' }))))
          : banner('info', 'info', 'Can’t look right now', why));
      }
      lookState.append(h('p', { class: 'muted look-count', text: `Photos sent today: ${Number(st.photos_today) || 0}` }));
    }

    async function runLook() {
      const question = lookQuestion.value.trim();
      clear(lookResult).append(h('p', { class: 'muted', text: 'Taking one photo and asking ChatGPT…' }));
      let r;
      try {
        r = await post('/chatgpt/look', { ...(question ? { question } : {}), speak: lookSpeak.checked }, { timeout: 90000 });
      } catch (e) {
        clear(lookResult);
        if (e.status !== 401) lookResult.append(banner('warn', 'alert', 'No answer', e.message || 'Something went wrong.'));
        schedule(300);
        return;
      }
      const res = r.result || {};
      clear(lookResult).append(h('div', { class: 'look-answer' },
        h('span', { class: 'muted', text: question ? `“${question}”` : 'What LittleBot sees' }),
        h('p', { text: res.answer || '(no answer)' }),
        res.latency_ms != null ? h('span', { class: 'muted', text: `Answered in ${(res.latency_ms / 1000).toFixed(1)} s${r.spoken ? ', and said out loud' : ''}.` }) : null));
      if (r.look) renderLook(r.look);
      schedule(300);
    }

    const WAKE_RESULTS = { starting: 'starting live voice…', started: 'live voice started', connected: 'live voice started', blocked: 'live voice could not start', failed: 'live voice did not start' };
    function renderWake(w) {
      clear(wakeState);
      if (!w) return;
      wakeSwitch.set(w.enabled);
      if (w.enabled && w.state === 'unavailable') {
        wakeState.append(banner('info', 'info', w.model_missing ? 'The wake-word model is missing' : 'The wake word isn’t available', w.detail || ''));
      } else if (w.enabled) {
        wakeState.append(h('p', { class: `muted wake-state ${w.listening ? 'listening' : ''}` }, w.listening ? [icon('mic'), ' '] : null, w.detail || ''));
      } else if (w.unavailable_detail) {
        wakeState.append(h('p', { class: 'muted', text: `Not ready on this robot yet: ${w.unavailable_detail}` }));
      }
      const last = w.last_heard;
      if (last && last.phrase) {
        const ago = last.at ? ` ${fmtDuration(Date.now() / 1000 - last.at)} ago` : '';
        const why = ['blocked', 'failed'].includes(last.result) && last.detail ? ` (${last.detail})` : '';
        wakeState.append(h('p', { class: 'muted', text: `Last heard “${last.phrase}”${ago}: ${WAKE_RESULTS[last.result] || last.result}${why}.` }));
      }
    }

    function fillChatGPT(d) {
      const fast = Boolean(d.account?.pending || d.live?.active);
      renderAccount(d);
      renderLive(d);
      renderLook(d.look);
      renderWake(d.wake);
      if (fast !== wasFast) { wasFast = fast; schedule(fast ? 1000 : 10000); }
    }

    let pollTimer = null;
    let wasFast = false;
    function schedule(ms) {
      clearTimeout(pollTimer);
      pollTimer = setTimeout(async () => {
        if (!document.hidden) {
          try { fillChatGPT(await get('/chatgpt')); } catch { /* shown on the next successful poll */ }
        }
        schedule(wasFast ? 1000 : 10000);
      }, ms);
    }

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
      if (st.enabled && st.provider === 'chatgpt' && d.brain?.provider_status?.chatgpt === 'not_configured') notices.append(banner('warn', 'key', 'Not signed in with ChatGPT', 'Sign in with ChatGPT below to use your plan.'));
      if (st.enabled && st.provider === 'openai' && !d.keys?.has_openai_key) notices.append(banner('warn', 'key', 'No OpenAI key', 'Add OPENAI_API_KEY to the .env file on the robot, then restart LittleBot.'));
      if (!d.policy?.cloud_ai_age_ok) notices.append(banner('info', 'shield', 'Scripted replies only for now', `Your child is under the minimum age set for cloud AI (${d.policy?.min_age_months_for_cloud ?? 0} months, ai.min_age_months_for_cloud), or no age is set. LittleBot answers with gentle scripted responses.`));
      kvRows(usage, d.brain?.usage);
      kvRows(brainState, { active_provider: d.brain?.active_provider, cloud_allowed: d.brain?.cloud_allowed, reason: d.brain?.reason, last_error: d.brain?.last_error });
    }
    testProvider.addEventListener('change', () => { testProvider.dataset.touched = '1'; });

    get('/ai').then(fill).catch((e) => notices.append(banner('danger', 'alert', 'Could not load AI settings', e.message)));
    const timer = setInterval(() => { if (!document.hidden) get('/ai').then((d) => { data = d; kvRows(usage, d.brain?.usage); }).catch(() => {}); }, 15000);
    get('/chatgpt').then(fillChatGPT).catch((e) => { clear(account).append(banner('danger', 'alert', 'Could not load the ChatGPT account', e.message)); });
    schedule(10000);
    return { destroy() { clearInterval(timer); clearTimeout(pollTimer); } };
  },
};
