// Motion: live robot drawing, gestures, joints, emergency stop and guarded physical arming.

import { get, post } from '../api.js';
import { banner, button, clear, confirmDialog, h, icon, pageHead, rangeField, s, setText, titleCase } from '../ui.js';

function robotSvg() {
  const head = s('g', { class: 'moving' },
    s('rect', { class: 'head', x: 90, y: 34, width: 120, height: 82, rx: 24 }),
    s('rect', { class: 'eye', x: 122, y: 58, width: 16, height: 30, rx: 8 }),
    s('rect', { class: 'eye', x: 162, y: 58, width: 16, height: 30, rx: 8 }));
  const armL = s('g', { class: 'moving' }, s('rect', { class: 'arm', x: -10, y: 0, width: 20, height: 74, rx: 10 }));
  const armR = s('g', { class: 'moving' }, s('rect', { class: 'arm', x: -10, y: 0, width: 20, height: 74, rx: 10 }));
  const tHead = s('text', { x: 150, y: 20, 'text-anchor': 'middle' });
  const tL = s('text', { x: 62, y: 250, 'text-anchor': 'middle' });
  const tR = s('text', { x: 238, y: 250, 'text-anchor': 'middle' });
  const root = s('svg', { class: 'robot-svg', viewBox: '0 0 300 260', role: 'img', 'aria-label': 'Drawing of LittleBot showing head and arm positions' },
    armL, armR,
    s('rect', { class: 'body', x: 95, y: 122, width: 110, height: 104, rx: 26 }),
    s('circle', { class: 'joint', cx: 95, cy: 142, r: 9 }),
    s('circle', { class: 'joint', cx: 205, cy: 142, r: 9 }),
    s('rect', { class: 'joint', x: 138, y: 112, width: 24, height: 14, rx: 6 }),
    head, tHead, tL, tR);
  const place = (g, x, y, deg) => { g.style.transform = `translate(${x}px, ${y}px) rotate(${deg}deg)`; };
  place(armL, 95, 142, 20);
  place(armR, 205, 142, -20);
  // Joint "position" is normalised by the motion module: -1..1, 0 = neutral,
  // +1 = arm raised / head turned to the robot's left (the viewer's right).
  const position = (joints, servos, n) => {
    const j = joints?.[n];
    if (j && typeof j.position === 'number') return Math.max(-1, Math.min(1, j.position));
    const sv = servos?.[n];
    if (!j || !sv || typeof j.angle !== 'number') return 0;
    let p = j.angle >= sv.neutral ? (j.angle - sv.neutral) / Math.max(1, sv.max - sv.neutral) : (j.angle - sv.neutral) / Math.max(1, sv.neutral - sv.min);
    if (sv.inverted) p = -p;
    return Math.max(-1, Math.min(1, p));
  };
  const armDeg = (p) => (p >= 0 ? 15 + 150 * p : 15 + 15 * p); // hanging ~15° out .. raised ~165°
  root.update = (joints, servos, estop) => {
    const ang = (n, d) => (joints?.[n]?.angle ?? servos?.[n]?.neutral ?? d);
    const hP = position(joints, servos, 'head');
    head.style.transformOrigin = '150px 118px';
    head.style.transform = `translateX(${(hP * 10).toFixed(1)}px) rotate(${(hP * 10).toFixed(1)}deg)`;
    const hA = ang('head', 90);
    // Robot's left arm is drawn on the viewer's right (the robot faces you).
    place(armR, 205, 142, -armDeg(position(joints, servos, 'left_arm')));
    place(armL, 95, 142, armDeg(position(joints, servos, 'right_arm')));
    tHead.textContent = `Head ${Math.round(hA)}°`;
    tL.textContent = `Right arm ${Math.round(ang('right_arm', 160))}°`;
    tR.textContent = `Left arm ${Math.round(ang('left_arm', 20))}°`;
    root.classList.toggle('estop', !!estop);
  };
  return root;
}

export default {
  mount(view, app) {
    let info = null;
    const robot = robotSvg();
    const modeBox = h('div', { class: 'stack-sm' });
    const estopBox = h('div', { class: 'stack-sm' });
    const gestures = h('div', { class: 'btn-row' });
    const joints = h('div', { class: 'stack' });
    const sliders = {};
    const armBox = h('div', { class: 'stack-sm' });
    const statusLine = h('p', { class: 'muted center' });

    const estopBtn = h('button', { type: 'button', class: 'estop-big' }, icon('stop'), 'EMERGENCY STOP');
    estopBtn.addEventListener('click', () => app.act(() => post('/motion/estop'), 'Emergency stop: motors off.').then((r) => { if (r && r.status) fill(r); }));

    view.append(
      pageHead('Motion', 'Head and arm movements. Physical motors stay disabled until you deliberately enable them.'),
      h('div', { class: 'stack' },
        modeBox,
        h('div', { class: 'grid two' },
          h('section', { class: 'card' }, h('div', { class: 'robot-wrap' }, robot), statusLine),
          h('div', { class: 'stack' },
            h('section', { class: 'card' }, estopBtn, h('div', { class: 'mt-2' }, estopBox)),
            h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('wave'), 'Gestures')), gestures,
              h('div', { class: 'btn-row mt-2' },
                button('Neutral pose', { kind: 'soft', iconName: 'refresh', onClick: () => app.act(() => post('/motion/neutral')) }),
                button('Cancel movement', { kind: 'soft', iconName: 'x', onClick: () => app.act(() => post('/motion/cancel')) }))))),
        h('div', { class: 'grid two' },
          h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('sliders'), 'Joints')), joints),
          h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('lock'), 'Physical motors')), armBox))));

    function fill(d) {
      info = d;
      // mode banner
      clear(modeBox);
      if (!d.available) modeBox.append(banner('warn', 'alert', 'Motion system unavailable', 'Gestures and joints are disabled until it is running.'));
      else if (d.simulation_only) modeBox.append(banner('info', 'info', 'Simulation only', `${d.reason || 'Physical motion is disabled.'} No servo will move - the drawing shows what LittleBot would do.`));
      else if (d.armed) modeBox.append(banner('danger', 'alert', 'Physical motors are ARMED', 'LittleBot\'s servos will really move. Keep fingers and faces clear and supervise closely.'));
      else modeBox.append(banner('warn', 'lock', 'Physical motors disarmed', d.reason || 'Motion is simulated until you arm the motors below.'));
      // gestures
      clear(gestures);
      for (const g of d.gestures || []) gestures.append(button(titleCase(g), { kind: 'soft', onClick: () => app.act(() => post('/motion/gesture', { name: g })) }));
      if (!(d.gestures || []).length) gestures.append(h('p', { class: 'muted', text: 'No gestures available.' }));
      // joints
      const names = Object.keys(d.servos || {});
      if (!Object.keys(sliders).length || Object.keys(sliders).join() !== names.join()) {
        clear(joints);
        for (const n of names) {
          const sv = d.servos[n];
          const lim = d.status?.joints?.[n] || {};
          const r = rangeField({
            label: titleCase(n), min: lim.min ?? sv.min, max: lim.max ?? sv.max, step: 1, value: lim.angle ?? sv.neutral,
            format: (v) => `${Math.round(v)}°`,
            onChange: (v) => app.act(() => post('/motion/joint', { joint: n, angle: v })),
          });
          sliders[n] = r;
          joints.append(h('div', { class: 'joint-row' }, r.el, h('span', { class: 'muted', text: `Range ${lim.min ?? sv.min}°-${lim.max ?? sv.max}° · neutral ${sv.neutral}°` })));
        }
      }
      // arming
      clear(armBox);
      if (!d.physical_enabled) {
        armBox.append(h('p', { class: 'muted', text: 'Disabled in the configuration. To use real servos, wire and power them separately, calibrate them, then set motion.physical_enabled: true in config/local.yaml and restart. See the motion guide.' }));
      } else if (d.armed) {
        armBox.append(h('p', { text: 'Motors are armed.' }), button('Disarm motors', { kind: 'primary', iconName: 'lock', onClick: () => app.act(() => post('/motion/arm', { armed: false }), 'Motors disarmed.').then((r) => r && fill(r)) }));
      } else {
        armBox.append(h('p', { class: 'muted', text: 'Arming lets the servos really move. Make sure LittleBot is on a stable surface and nothing is in the way.' }),
          button('Arm motors…', {
            kind: 'danger-outline', iconName: 'zap',
            onClick: async () => {
              const typed = await confirmDialog({
                title: 'Arm the physical motors?', danger: true,
                body: ['LittleBot\'s head and arms will really move.', 'Only arm the motors while an adult is watching. The emergency stop disarms them instantly.'],
                phrase: d.arm_phrase || 'ARM MOTORS', confirm: 'Arm motors',
              });
              if (!typed) return;
              const r = await app.act(() => post('/motion/arm', { armed: true, confirmation: typed }), 'Motors armed.');
              if (r) fill(r);
            },
          }));
      }
      delete estopBox.dataset.key;
      renderStatus(d.status || {});
    }

    function renderStatus(ms) {
      robot.update(ms.joints, info?.servos, ms.estop);
      const ek = String(!!ms.estop);
      if (estopBox.dataset.key !== ek) {
        estopBox.dataset.key = ek;
        renderEstop(ms);
      }
      const bits = [ms.backend ? `Backend: ${ms.backend}` : null, ms.moving ? 'Moving' : 'Still', ms.gesture ? `Gesture: ${titleCase(ms.gesture)}` : null].filter(Boolean);
      setText(statusLine, bits.join(' · '));
      for (const [n, r] of Object.entries(sliders)) { const a = ms.joints?.[n]?.angle; if (a !== undefined) r.set(Math.round(a)); }
    }

    function renderEstop(ms) {
      clear(estopBox);
      if (ms.estop) {
        estopBox.append(banner('danger', 'alert', 'Emergency stop is active', 'All motion is halted and the servos are de-energised.'),
          button('Reset emergency stop', { kind: 'primary', iconName: 'refresh', onClick: () => app.act(() => post('/motion/estop/reset'), 'Emergency stop reset.').then((r) => r && fill(r)) }));
      } else {
        estopBox.append(h('p', { class: 'muted center', text: 'Stops all motion immediately and de-energises the servos.' }));
      }
    }

    let lastKey = '';
    get('/motion').then(fill).catch((e) => modeBox.append(banner('danger', 'alert', 'Could not load motion', e.message)));
    return {
      update(st) {
        const ms = st.motion || {};
        if (!info) return;
        const key = `${ms.estop}|${ms.armed}|${ms.simulation_only}|${ms.reason}`;
        if (key !== lastKey && lastKey) { lastKey = key; get('/motion').then(fill).catch(() => {}); return; }
        lastKey = key;
        renderStatus(ms);
      },
    };
  },
};
