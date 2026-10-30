// Assistant: type to LittleBot (the same brain as its voice), intercom, notes to its screen, which voice pipeline
// is on and why, and what its skills hold right now (lists, reminders, memories, games ...).

import { get, post } from '../api.js';
import { append, banner, button, clear, h, icon, pageHead, setText, switchRow, toast } from '../ui.js';

const SPEAK_KEY = 'littlebot.assistant.speak';
const KEY_HELP = {
  deepgram: 'DEEPGRAM_API_KEY: fast speech-to-text that knows when you have finished talking',
  cartesia: 'CARTESIA_API_KEY: the fastest natural voice',
  elevenlabs: 'ELEVENLABS_API_KEY: natural voice (used when Cartesia is not set)',
  openai: 'OPENAI_API_KEY: backup speech-to-text and voice',
  anthropic: 'ANTHROPIC_API_KEY: Claude, the brain',
};

function loadSpeak() {
  try { return localStorage.getItem(SPEAK_KEY) === '1'; } catch { return false; }
}
function saveSpeak(v) {
  try { localStorage.setItem(SPEAK_KEY, v ? '1' : '0'); } catch { /* private mode */ }
}

export default {
  mount(view, app) {
    let speak = loadSpeak();
    let busy = false;
    let timer = null;

    // ---------- chat
    const log = h('div', { class: 'chat-log', 'aria-live': 'polite' });
    const input = h('textarea', { class: 'input', rows: 2, maxlength: 2000, placeholder: 'Ask LittleBot anything… (Enter to send)' });
    const sendBtn = button('Send', { kind: 'primary', iconName: 'sparkles', onClick: () => send() });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    const speakRow = switchRow({ title: 'Say the answers out loud', text: 'LittleBot also speaks each answer.', checked: speak, onChange: (v) => { speak = v; saveSpeak(v); } });

    function bubble(who, text, meta) {
      const el = h('div', { class: `chat-msg ${who}` }, h('div', { class: 'chat-text', text }), meta ? h('div', { class: 'muted small', text: meta }) : null);
      log.append(el);
      log.scrollTop = log.scrollHeight;
      return el;
    }

    async function send() {
      const text = input.value.trim();
      if (!text || busy) return;
      busy = true;
      sendBtn.disabled = true;
      input.value = '';
      bubble('user', text);
      const pending = bubble('robot', '…');
      try {
        const res = await post('/assistant/chat', { text, speak });
        const meta = [res.model || res.provider, res.ms != null ? `${(res.ms / 1000).toFixed(1)} s` : '', (res.tools || []).length ? `tools: ${res.tools.join(', ')}` : ''].filter(Boolean).join(' · ');
        pending.remove();
        bubble('robot', res.silent ? '(no answer)' : (res.reply || '(no answer)'), meta);
      } catch (e) {
        pending.remove();
        toast(e.message || 'LittleBot could not answer.', 'error');
      } finally {
        busy = false;
        sendBtn.disabled = false;
        input.focus();
      }
    }

    // ---------- intercom + note
    const sayInput = h('input', { class: 'input', maxlength: 400, placeholder: 'e.g. Dinner is ready!' });
    const sayBtn = button('Say it', { iconName: 'speaker', onClick: async () => {
      const text = sayInput.value.trim();
      if (!text) return;
      if (await app.act(() => post('/assistant/say', { text }), 'LittleBot is saying it.')) sayInput.value = '';
    } });
    const noteBtn = button('Show on screen', { kind: 'soft', iconName: 'eye', onClick: async () => {
      const text = sayInput.value.trim();
      if (!text) return;
      if (await app.act(() => post('/assistant/note', { text, title: 'Message', say: false }), 'On LittleBot’s screen.')) sayInput.value = '';
    } });
    const glanceBtn = button('Show today at a glance', { kind: 'soft', iconName: 'sun', onClick: () => app.act(() => post('/assistant/glance'), 'Shown on LittleBot.') });

    // ---------- pipeline
    const pipeBody = h('div', { class: 'stack-sm' });
    const talkBtn = button('Start talking', { kind: 'primary', iconName: 'mic', onClick: () => app.act(() => post('/assistant/session/start'), 'LittleBot is listening.') });
    const stopBtn = button('Stop', { kind: 'soft', iconName: 'stop', onClick: () => app.act(() => post('/assistant/session/stop'), 'Stopped.') });

    // ---------- skills
    const skillsBody = h('div', { class: 'stack-sm' });

    // ---------- what it keeps: lists, reminders, memories
    const listsBody = h('div', { class: 'stack-sm' });
    const remindersBody = h('div', { class: 'stack-sm' });
    const memoriesBody = h('div', { class: 'stack-sm' });
    let dataKey = '';

    function row(text, actionLabel, onAction, sub) {
      return h('div', { class: 'row between skill-row' },
        h('div', {}, h('span', { text }), sub ? h('div', { class: 'muted small', text: sub }) : null),
        button(actionLabel, { kind: 'soft small', onClick: onAction }));
    }

    function renderData(d) {
      const key = JSON.stringify(d);
      if (key === dataKey) return;
      dataKey = key;
      clear(listsBody);
      if (!(d.lists || []).length) listsBody.append(h('p', { class: 'muted', text: 'No lists yet. Say “add milk to the shopping list”.' }));
      for (const l of d.lists || []) {
        listsBody.append(h('h3', { class: 'small-head', text: `${l.name} (${l.items.length})` }));
        for (const it of l.items) listsBody.append(row(it.item, 'Done', () => app.act(() => post('/assistant/lists/tick', { list: l.name, id: it.id })).then(refreshData)));
      }
      clear(remindersBody);
      if (!(d.reminders || []).length) remindersBody.append(h('p', { class: 'muted', text: 'Nothing coming up. Say “remind me at 6 to call mom”.' }));
      for (const r of d.reminders || []) {
        const when = new Date(r.at * 1000).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' });
        remindersBody.append(row(`${r.kind === 'alarm' ? '⏰ ' : ''}${r.label}`, 'Cancel', () => app.act(() => post('/assistant/reminders/cancel', { id: r.id })).then(refreshData),
          `${when}${r.repeat ? ` · ${r.repeat}` : ''}${r.state === 'ringing' ? ' · ringing now' : ''}`));
      }
      clear(memoriesBody);
      if (!(d.memories || []).length) memoriesBody.append(h('p', { class: 'muted', text: 'Nothing yet. Say “remember that…”.' }));
      for (const m of d.memories || []) memoriesBody.append(row(m.text, 'Forget', () => app.act(() => post('/assistant/memories/forget', { id: m.id })).then(refreshData)));
    }

    async function refreshData() {
      try { renderData(await get('/assistant/data')); } catch { /* shown by the poll */ }
    }

    view.append(
      pageHead('Assistant', 'Talk to LittleBot from here, send it messages, and see what it remembers.'),
      h('div', { class: 'stack' },
        h('section', { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('sparkles'), 'Ask LittleBot')),
          log, h('div', { class: 'row gap' }, input, sendBtn), speakRow,
          h('div', { class: 'btn-row mt-2' }, button('Forget this conversation', { kind: 'soft small', onClick: async () => {
            if (await app.act(() => post('/assistant/history/clear'), 'Forgotten.')) clear(log);
          } }))),
        h('section', { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('speaker'), 'Intercom')),
          h('p', { class: 'muted', text: 'LittleBot says your message out loud, or shows it big on its screen.' }),
          h('div', { class: 'row gap' }, sayInput), h('div', { class: 'btn-row mt-2' }, sayBtn, noteBtn, glanceBtn)),
        h('section', { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('mic'), 'Voice'), h('div', { class: 'btn-row' }, talkBtn, stopBtn)),
          pipeBody),
        h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('star'), 'Lists')), listsBody),
        h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('sun'), 'Reminders and alarms')), remindersBody),
        h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('sparkles'), 'What LittleBot remembers')), memoriesBody),
        h('section', { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('star'), 'Skills right now')),
          skillsBody)));

    function render(data) {
      const a = data.assistant || {};
      clear(pipeBody);
      const using = a.pipeline === 'assistant' ? 'The fast assistant voice (speech-to-text → Claude → natural voice).' : 'GPT-Live (ChatGPT’s voice), as before.';
      append(pipeBody, [
        h('p', {}, h('strong', { text: 'Wake word starts: ' }), using),
        h('p', { class: 'muted', text: a.pipeline_reason || '' }),
        a.active ? banner('info', 'mic', `Listening now: ${a.phase || ''}`, a.detail || '') : null,
        h('ul', { class: 'plain-list' }, Object.entries(a.keys || {}).map(([k, on]) =>
          h('li', {}, h('span', { class: on ? 'ok-dot' : 'off-dot', text: on ? '● ' : '○ ' }), KEY_HELP[k] || k, on ? ' (set)' : ' (not set)'))),
        a.last_call ? h('p', { class: 'muted small', text: `Last conversation: ${a.last_call.turns || 0} turn(s), ${a.last_call.seconds || 0} s, ended: ${a.last_call.ended_reason || '–'}${a.last_call.error ? ` (error: ${a.last_call.error})` : ''}` }) : null,
      ]);
      talkBtn.hidden = !!a.active;
      stopBtn.hidden = !a.active;
      clear(skillsBody);
      const skills = data.skills || [];
      if (!skills.length) skillsBody.append(h('p', { class: 'muted', text: 'No skills loaded.' }));
      for (const sk of skills) {
        skillsBody.append(h('div', { class: 'skill-row' },
          h('strong', { text: sk.name || '?' }), sk.available === false ? h('span', { class: 'muted', text: ' (not set up)' }) : null,
          sk.now ? h('pre', { class: 'skill-now', text: sk.now }) : null));
      }
    }

    async function refresh() {
      try { render(await get('/assistant')); } catch { /* the poll shows offline */ }
      refreshData();
    }
    refresh();
    timer = setInterval(refresh, 4000);
    return { destroy() { clearInterval(timer); } };
  },
};
