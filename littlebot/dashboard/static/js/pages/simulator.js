// Simulator: pretend the child spoke, place a pretend face in the camera view, answer prompts.

import { post } from '../api.js';
import { button, clear, h, icon, pageHead, setText, titleCase } from '../ui.js';

const PTT_ERRORS = { nothing_heard: "LittleBot didn't hear anything. Try again and speak close to it.", microphone_disabled: 'The microphone is turned off in Privacy.', no_audio: 'No speaker or microphone is available.' };

const PHRASES = ['hello', 'ball', 'more', 'dog', 'again', 'what is that?', 'bye-bye'];

export default {
  mount(view, app) {
    const chat = h('div', { class: 'chat', 'aria-live': 'polite' }, h('p', { class: 'empty', text: 'Type something a child might say and see how LittleBot answers.' }));
    const speech = h('input', { type: 'text', id: 'sim-text', maxlength: 200, autocomplete: 'off', placeholder: 'e.g. doggy!', enterkeyhint: 'send' });
    const route = h('select', { id: 'sim-route', 'aria-label': 'Where the speech goes' },
      h('option', { value: 'auto', text: 'Automatic' }),
      h('option', { value: 'brain', text: 'Conversation' }),
      h('option', { value: 'microphone', text: 'Microphone (for activities)' }));
    const form = h('form', { class: 'stack-sm' },
      h('label', { for: 'sim-text', class: 'field' }, 'Pretend your child says…'),
      h('div', { class: 'row nowrap' }, h('div', { class: 'grow' }, speech), h('button', { type: 'submit', class: 'btn primary icon', 'aria-label': 'Send' }, icon('send'))),
      h('div', { class: 'row' }, h('span', { class: 'muted', text: 'Send to' }), h('div', { class: 'grow' }, route)));
    const quick = h('div', { class: 'chips' }, PHRASES.map((p) => h('button', { type: 'button', class: 'chip', text: p, onclick: () => say(p) })));
    let first = true;

    function bubble(who, text, cls) {
      if (first) { clear(chat); first = false; }
      chat.append(h('div', { class: ['bubble', cls] }, h('span', { class: 'who', text: who }), text));
      chat.scrollTop = chat.scrollHeight;
      while (chat.children.length > 40) chat.firstChild.remove();
    }

    async function say(text) {
      text = (text || '').trim();
      if (!text) { speech.focus(); return; }
      bubble('Child', text, 'child');
      speech.value = '';
      const r = await app.act(() => post('/sim/speech', { text, route: route.value }, { timeout: 40000 }));
      if (!r) return;
      if (r.route === 'microphone') bubble('LittleBot', '(heard through the microphone)', 'bot');
      else bubble(`LittleBot${r.provider ? ` · ${r.provider}` : ''}`, r.reply || (r.error ? `(${r.error})` : '(no reply)'), 'bot');
    }
    form.addEventListener('submit', (e) => { e.preventDefault(); say(speech.value); });
    const ptt = button('Push to talk (real microphone)', {
      kind: 'soft', iconName: 'mic',
      onClick: async () => {
        bubble('LittleBot', '(listening…)', 'bot');
        const r = await app.act(() => post('/ai/push-to-talk', {}, { timeout: 60000 }));
        if (!r) return;
        if (r.error) { bubble('LittleBot', `(${r.detail || PTT_ERRORS[r.error] || r.error})`, 'bot'); return; }
        if (r.heard) bubble('Child', r.heard, 'child');
        bubble(`LittleBot${r.provider ? ` · ${r.provider}` : ''}`, r.reply || '(no reply)', 'bot');
      },
    });

    // camera view
    const mark = h('div', { class: 'face-mark', hidden: true });
    const cam = h('div', { class: 'pad camera', role: 'button', tabindex: '0', 'aria-label': 'Tap where the child is, as seen facing LittleBot' },
      mark, h('span', { class: 'pad-label', text: 'Tap where the child is, as seen facing LittleBot - its eyes look that way' }));
    const place = async (x, y) => {
      mark.hidden = false;
      mark.style.left = `${x * 100}%`;
      mark.style.top = `${y * 100}%`;
      await app.act(() => post('/sim/camera', { event: 'face', x, y, size: 0.25 }));
    };
    cam.addEventListener('click', (e) => {
      const r = cam.getBoundingClientRect();
      place(Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)));
    });
    cam.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); place(0.5, 0.5); } });

    // battery (only with power.monitor: simulated)
    const batText = h('p', { class: 'muted' });
    const sim = (body, msg) => app.act(() => post('/power/simulate', body), msg);
    const batCard = h('section', { class: 'card span-2', hidden: true }, h('div', { class: 'card-head' }, h('h2', {}, icon('battery'), 'Battery (simulated)')),
      h('div', { class: 'stack-sm' }, batText,
        h('div', { class: 'btn-row' }, [80, 25, 18, 5].map((v) => button(`${v}%`, { kind: 'soft small', onClick: () => sim({ percent: v }) }))),
        h('div', { class: 'btn-row' },
          button('Plug the charger in', { kind: 'soft small', iconName: 'batteryCharging', onClick: () => sim({ charging: true }) }),
          button('Unplug', { kind: 'soft small', iconName: 'battery', onClick: () => sim({ charging: false }) })),
        h('div', { class: 'btn-row' }, [[24, 'Normal'], [55, 'Hot (55 °C)'], [62, 'Too hot (62 °C)'], [-3, 'Cold (−3 °C)']].map(([t, label]) => button(label, { kind: 'soft small', iconName: 'thermo', onClick: () => sim({ temperature_c: t }) }))),
        h('p', { class: 'muted', text: 'LittleBot reacts like on the real battery: a gentle “please charge me” at the low level, and at the empty level a goodbye, then (on the robot only) a clean shutdown.' })));

    // caregiver
    const promptText = h('p', { class: 'muted', text: 'No question waiting right now.' });
    const promptBtns = h('div', { class: 'btn-row' });
    let pk = null;

    view.append(
      pageHead('Simulator', 'Try LittleBot without a child or devices: pretend speech, faces and caregiver answers.'),
      h('div', { class: 'grid two' },
        h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('mic'), 'Speech')), h('div', { class: 'stack' }, chat, form, quick, h('div', {}, ptt))),
        h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('camera'), 'Camera')),
          h('div', { class: 'stack' }, cam,
            h('div', { class: 'btn-row' },
              button('No face', { kind: 'soft', iconName: 'eyeOff', onClick: async () => { mark.hidden = true; await app.act(() => post('/sim/camera', { event: 'no_face' })); } }),
              button('Person left', { kind: 'soft', iconName: 'logout', onClick: async () => { mark.hidden = true; await app.act(() => post('/sim/camera', { event: 'person_left' }), 'Face removed - LittleBot notices the person has left after about 3 seconds.'); } })),
            h('p', { class: 'muted', text: '“Person left” removes the pretend face; like with a real camera, LittleBot waits about 3 seconds before deciding nobody is there.' }))),
        h('section', { class: 'card span-2' }, h('div', { class: 'card-head' }, h('h2', {}, icon('person'), 'Caregiver answers')),
          h('div', { class: 'stack-sm' }, promptText, promptBtns)),
        batCard));

    return {
      update(s) {
        const pw = s.power || {};
        batCard.hidden = pw.kind !== 'simulated';
        if (!batCard.hidden) setText(batText, `${pw.percent ?? '–'}%${pw.charging ? ', charging' : pw.charged ? ', charged' : ''}${pw.temperature_c != null ? `, ${pw.temperature_c} °C` : ''}`);
        const p = s.activities?.prompt;
        const key = p ? `${p.prompt_id}|${p.text}` : '';
        if (key === pk) return;
        pk = key;
        setText(promptText, p ? `LittleBot asks: “${p.text}”` : (s.activities?.running ? 'The activity is running - no question waiting.' : 'Start an activity to get caregiver questions.'));
        clear(promptBtns);
        const opts = p ? (p.options && p.options.length ? p.options : ['done', 'skip']) : [];
        for (const o of opts.slice(0, 6)) promptBtns.append(button(titleCase(o), { kind: 'soft', onClick: () => app.act(() => post('/sim/caregiver', { response: o, prompt_id: p.prompt_id ?? null })) }));
      },
    };
  },
};
