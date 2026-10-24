// Face: expressions, blinks, gaze pad, animations, a demo sequence, and dozing / sleep settings.

import { get, post, put } from '../api.js';
import { button, clear, h, icon, miniFace, pageHead, setText, switchRow, throttle, titleCase, toast } from '../ui.js';

const FALLBACK_EXPRESSIONS = ['neutral', 'happy', 'excited', 'curious', 'surprised', 'sleepy', 'sad', 'thinking', 'listening', 'speaking', 'confused', 'playful', 'affectionate', 'celebrating'];
const FALLBACK_ANIMATIONS = ['bounce', 'wiggle', 'nod', 'shake', 'spin_eyes', 'sparkle', 'hearts', 'zzz', 'confetti', 'peek'];

export default {
  mount(view, app) {
    const preview = miniFace({ expression: 'neutral', size: 180, label: 'LittleBot face preview' });
    const exprLabel = h('div', { class: 'state-big' });
    const faceState = h('p', { class: 'muted' });
    const exprGrid = h('div', { class: 'expr-grid', role: 'group', 'aria-label': 'Expressions' });
    const animRow = h('div', { class: 'chips' });
    const exprButtons = new Map();
    let unavailable = null;

    const dot = h('div', { class: 'dot' });
    const pad = h('div', { class: 'pad', role: 'slider', tabindex: '0', 'aria-label': 'Where LittleBot looks. Use arrow keys or tap.', 'aria-valuetext': 'centre' },
      dot, h('span', { class: 'pad-label', text: 'Tap or drag to choose where LittleBot looks' }));
    let gaze = { x: 0.5, y: 0.5 };
    const sendGaze = throttle((x, y) => app.act(() => post('/face/gaze', { x, y, hold: 4 })), 120);
    const placeDot = (x, y) => {
      dot.style.left = `${x * 100}%`;
      dot.style.top = `${y * 100}%`;
      pad.setAttribute('aria-valuetext', `${Math.round(x * 100)}% across, ${Math.round(y * 100)}% down`);
    };
    placeDot(0.5, 0.5);
    const fromEvent = (e) => {
      const r = pad.getBoundingClientRect();
      return { x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) };
    };
    let dragging = false;
    pad.addEventListener('pointerdown', (e) => { dragging = true; pad.setPointerCapture(e.pointerId); gaze = fromEvent(e); placeDot(gaze.x, gaze.y); sendGaze(gaze.x, gaze.y); });
    pad.addEventListener('pointermove', (e) => { if (!dragging) return; gaze = fromEvent(e); placeDot(gaze.x, gaze.y); sendGaze(gaze.x, gaze.y); });
    const end = () => { dragging = false; };
    pad.addEventListener('pointerup', end);
    pad.addEventListener('pointercancel', end);
    pad.addEventListener('keydown', (e) => {
      const step = 0.1;
      const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
      if (!moves[e.key]) return;
      e.preventDefault();
      gaze = { x: Math.min(1, Math.max(0, gaze.x + moves[e.key][0])), y: Math.min(1, Math.max(0, gaze.y + moves[e.key][1])) };
      placeDot(gaze.x, gaze.y);
      sendGaze(gaze.x, gaze.y);
    });

    // Dozing (a look only: LittleBot still hears "Hey LittleBot", sees people and feels taps) and the deeper sleep.
    const doze = switchRow({ title: 'Doze when nothing is happening',
      text: 'When nobody is talking, playing or using the screen, the eyes slowly close and little Zs float up. Someone walking in, a tap or "Hey LittleBot" wakes it.',
      onChange: async (v) => (await app.act(() => put('/face/sleep', { doze_enabled: v }), v ? 'LittleBot dozes when nothing is happening.' : 'LittleBot stays wide awake.')) !== null });
    const dozeAfter = h('input', { type: 'number', id: 'doze-after', min: 5, max: 3600, inputmode: 'numeric' });
    const awakeFor = h('input', { type: 'number', id: 'doze-arrival', min: 0, max: 3600, inputmode: 'numeric' });
    const sleepAfter = h('input', { type: 'number', id: 'sleep-after', min: 0, max: 720, step: 1, inputmode: 'numeric' });
    const sleepNote = h('p', { class: 'muted' });
    const saveSleep = button('Save', {
      kind: 'primary', iconName: 'check',
      onClick: async () => {
        const after = parseInt(dozeAfter.value, 10), awake = parseInt(awakeFor.value, 10), mins = Number(sleepAfter.value);
        if (!(after >= 5 && after <= 3600)) { toast('Doze after must be between 5 and 3600 seconds.', 'error'); dozeAfter.focus(); return; }
        if (!(awake >= 0 && awake <= 3600)) { toast('Awake after someone walks in must be between 0 and 3600 seconds.', 'error'); awakeFor.focus(); return; }
        if (!(mins >= 0 && mins <= 720)) { toast('Go to sleep after must be between 0 and 720 minutes.', 'error'); sleepAfter.focus(); return; }
        const r = await app.act(() => put('/face/sleep', { doze_after_seconds: after, awake_on_arrival_seconds: awake, sleep_after_idle_minutes: mins }), 'Saved.');
        if (r?.sleep) showSleep(r.sleep);
      },
    });
    const sleepCard = h('section', { class: 'card span-2' },
      h('div', { class: 'card-head' }, h('h2', {}, icon('moon'), 'Dozing and sleep')),
      h('div', { class: 'stack' }, doze.el,
        h('div', { class: 'form-grid two' },
          h('div', { class: 'field' }, h('label', { for: 'doze-after', text: 'Doze after (seconds)' }), dozeAfter,
            h('span', { class: 'hint', text: 'How long nothing has to happen first. A voice chat, a game, a video or a card on the screen never dozes.' })),
          h('div', { class: 'field' }, h('label', { for: 'doze-arrival', text: 'Awake after someone walks in (seconds)' }), awakeFor,
            h('span', { class: 'hint', text: 'Someone sitting still may let it doze again after this; moving or talking wakes it.' })),
          h('div', { class: 'field' }, h('label', { for: 'sleep-after', text: 'Go to sleep after (minutes)' }), sleepAfter,
            h('span', { class: 'hint', text: 'Real sleep after a long quiet spell (the camera checks less often). 0: never.' }))),
        h('div', { class: 'btn-row' }, saveSleep), sleepNote));

    function showSleep(s) {
      if (!s) return;
      doze.set(s.doze_enabled);
      if (document.activeElement !== dozeAfter) dozeAfter.value = s.doze_after_seconds;
      if (document.activeElement !== awakeFor) awakeFor.value = s.awake_on_arrival_seconds;
      if (document.activeElement !== sleepAfter) sleepAfter.value = s.sleep_after_idle_minutes;
    }

    const demoBtn = button('Play demo', { kind: 'primary', iconName: 'play', onClick: () => app.act(() => post('/face/demo'), 'Demo started - watch LittleBot!') });
    const demoStop = button('Stop demo', { kind: 'soft', iconName: 'square', onClick: () => app.act(() => post('/face/demo/stop')) });

    view.append(
      pageHead('Face', "Change LittleBot's expression, make it blink, choose where it looks, and when it dozes."),
      h('div', { class: 'grid two' },
        h('section', { class: 'card' },
          h('div', { class: 'face-hero' }, preview, h('div', { class: 'who' }, h('div', { class: 'muted', text: 'Showing now' }), exprLabel, faceState)),
          h('div', { class: 'btn-row mt-2' },
            button('Blink', { kind: 'soft', iconName: 'eye', onClick: () => app.act(() => post('/face/blink', { double: false })) }),
            button('Double blink', { kind: 'soft', iconName: 'eye', onClick: () => app.act(() => post('/face/blink', { double: true })) }))),
        h('section', { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('sparkles'), 'Demo')),
          h('p', { class: 'muted', text: 'A short show of expressions and animations (about 15 seconds). Great for checking the display.' }),
          h('div', { class: 'btn-row mt-2' }, demoBtn, demoStop)),
        h('section', { class: 'card span-2' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('face'), 'Expressions')), exprGrid),
        h('section', { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('crosshair'), 'Look around')),
          pad,
          h('div', { class: 'btn-row mt-2' },
            button('Centre', { kind: 'soft', onClick: () => { gaze = { x: 0.5, y: 0.5 }; placeDot(0.5, 0.5); return app.act(() => post('/face/gaze', { x: 0.5, y: 0.5, hold: 4 })); } }),
            button('Let LittleBot look around', { kind: 'soft', iconName: 'refresh', onClick: () => app.act(() => post('/face/gaze/clear'), 'Back to following faces.') }))),
        h('section', { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('zap'), 'Animations')),
          h('p', { class: 'muted', text: 'Little flourishes played on top of the current expression.' }),
          h('div', { class: 'mt-2' }, animRow)),
        sleepCard));

    function build(expressions, animations) {
      clear(exprGrid);
      exprButtons.clear();
      for (const name of expressions) {
        const b = h('button', { type: 'button', class: 'expr', 'aria-pressed': 'false' }, miniFace({ expression: name, size: 64 }), h('span', { text: titleCase(name) }));
        b.addEventListener('click', () => app.act(() => post('/face/expression', { name, hold: null })));
        exprButtons.set(name, b);
        exprGrid.append(b);
      }
      clear(animRow);
      for (const name of animations) {
        animRow.append(button(titleCase(name), { kind: 'soft small', onClick: () => app.act(() => post('/face/animation', { name })) }));
      }
    }

    build(FALLBACK_EXPRESSIONS, FALLBACK_ANIMATIONS);
    get('/face').then((info) => {
      build(info.expressions || FALLBACK_EXPRESSIONS, info.animations || FALLBACK_ANIMATIONS);
      showSleep(info.sleep);
      unavailable = !info.available;
      if (unavailable) view.querySelector('.page-head')?.after(h('div', { class: 'banner warn mt-2' }, icon('alert'), h('div', {}, h('strong', { text: 'The face display is unavailable' }), h('span', { text: 'Buttons will not have any effect until it is running.' }))));
      if (app.status) update(app.status);
    }).catch(() => {});

    function update(s) {
      const fs = s.face || {};
      const expr = fs.expression || 'neutral';
      preview.update(expr, fs.gaze, !!fs.eyes_closed, `LittleBot looks ${expr}`);
      setText(exprLabel, fs.status === 'unavailable' ? 'Unavailable' : titleCase(expr));
      setText(faceState, fs.state ? `Mode: ${titleCase(fs.state)}${fs.dozing ? ' · dozing' : ''}${fs.fps ? ` · ${Math.round(fs.fps)} fps` : ''}` : '');
      setText(sleepNote, fs.dozing ? 'LittleBot is dozing right now (still listening).' : '');
      for (const [name, b] of exprButtons) b.setAttribute('aria-pressed', String(name === expr));
    }
    return { update };
  },
};
