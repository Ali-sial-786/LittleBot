// Face: expressions, blinks, gaze pad, animations and a demo sequence.

import { get, post } from '../api.js';
import { button, clear, h, icon, miniFace, pageHead, setText, throttle, titleCase } from '../ui.js';

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

    const demoBtn = button('Play demo', { kind: 'primary', iconName: 'play', onClick: () => app.act(() => post('/face/demo'), 'Demo started - watch LittleBot!') });
    const demoStop = button('Stop demo', { kind: 'soft', iconName: 'square', onClick: () => app.act(() => post('/face/demo/stop')) });

    view.append(
      pageHead('Face', "Change LittleBot's expression, make it blink, or choose where it looks."),
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
          h('div', { class: 'mt-2' }, animRow))));

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
      unavailable = !info.available;
      if (unavailable) view.querySelector('.page-head')?.after(h('div', { class: 'banner warn mt-2' }, icon('alert'), h('div', {}, h('strong', { text: 'The face display is unavailable' }), h('span', { text: 'Buttons will not have any effect until it is running.' }))));
      if (app.status) update(app.status);
    }).catch(() => {});

    function update(s) {
      const fs = s.face || {};
      const expr = fs.expression || 'neutral';
      preview.update(expr, fs.gaze, !!fs.eyes_closed, `LittleBot looks ${expr}`);
      setText(exprLabel, fs.status === 'unavailable' ? 'Unavailable' : titleCase(expr));
      setText(faceState, fs.state ? `Mode: ${titleCase(fs.state)}${fs.fps ? ` · ${Math.round(fs.fps)} fps` : ''}` : '');
      for (const [name, b] of exprButtons) b.setAttribute('aria-pressed', String(name === expr));
    }
    return { update };
  },
};
