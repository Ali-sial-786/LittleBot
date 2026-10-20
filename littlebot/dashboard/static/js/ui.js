// Small DOM toolkit for the dashboard: element builder, icons, face drawing,
// toasts, dialogs and form controls. Everything is built with DOM APIs and
// textContent (never by parsing HTML strings) so robot data cannot inject markup.

const SVG_NS = 'http://www.w3.org/2000/svg';

/** h('div', {class: 'x', onclick: fn, attrs...}, ...children) */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  applyProps(el, props);
  append(el, children);
  return el;
}

export function s(tag, props, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  applyProps(el, props, true);
  append(el, children);
  return el;
}

function applyProps(el, props, isSvg = false) {
  if (!props) return;
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.setAttribute('class', Array.isArray(v) ? v.filter(Boolean).join(' ') : v);
    else if (k === 'text') el.textContent = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (!isSvg && (k === 'value' || k === 'checked' || k === 'disabled' || k === 'selected')) el[k] = v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false || c === '') continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
export function setText(el, text) { if (el.textContent !== String(text ?? '')) el.textContent = text ?? ''; }

// ---------------------------------------------------------------- icons
// 24x24 line icons (stroke = currentColor).
const ICONS = {
  home: [['path', { d: 'M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z' }]],
  shapes: [['path', { d: 'M7 3.5 11 10.5H3z' }], ['circle', { cx: 17.5, cy: 7, r: 3.5 }], ['rect', { x: 3.5, y: 14, width: 7, height: 7, rx: 1.5 }], ['path', { d: 'M17.5 13.5l1.2 2.5 2.8.4-2 2 .5 2.8-2.5-1.3-2.5 1.3.5-2.8-2-2 2.8-.4z' }]],
  film: [['rect', { x: 3, y: 5, width: 18, height: 14, rx: 2.5 }], ['path', { d: 'M10 9.2v5.6l4.6-2.8z' }]],
  face: [['rect', { x: 3, y: 4.5, width: 18, height: 15, rx: 5 }], ['path', { d: 'M9 9.5v4 M15 9.5v4' }]],
  user: [['circle', { cx: 12, cy: 8, r: 4 }], ['path', { d: 'M4.5 20.5a7.5 7.5 0 0 1 15 0' }]],
  sparkles: [['path', { d: 'M11 3.5l1.7 4.3 4.3 1.7-4.3 1.7L11 15.5l-1.7-4.3L5 9.5l4.3-1.7z' }], ['path', { d: 'M18.5 14.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z' }]],
  shield: [['path', { d: 'M12 3 4.5 6v5.5c0 4.6 3.2 8.4 7.5 9.5 4.3-1.1 7.5-4.9 7.5-9.5V6z' }], ['path', { d: 'M8.8 12.2l2.2 2.2 4.4-4.4' }]],
  robot: [['rect', { x: 5, y: 8, width: 14, height: 10, rx: 3 }], ['path', { d: 'M12 4.5V8 M9.5 12.5v1.5 M14.5 12.5v1.5 M2.5 12v3 M21.5 12v3 M8.5 21h7' }], ['circle', { cx: 12, cy: 3.5, r: 1 }]],
  speaker: [['path', { d: 'M11 5 6.5 9H3.5v6h3L11 19z' }], ['path', { d: 'M15.5 9a4.5 4.5 0 0 1 0 6 M18.5 6a8.5 8.5 0 0 1 0 12' }]],
  gamepad: [['path', { d: 'M7 8h10a4.5 4.5 0 0 1 4.5 4.5v.5a3.5 3.5 0 0 1-6.3 2.1L14.5 14h-5l-.7 1.1A3.5 3.5 0 0 1 2.5 13v-.5A4.5 4.5 0 0 1 7 8z' }], ['path', { d: 'M7.5 10.5v3 M6 12h3 M15.5 11h.01 M17.5 13h.01' }]],
  sliders: [['path', { d: 'M4 6h9 M17 6h3 M15 4v4 M4 12h3 M11 12h9 M9 10v4 M4 18h11 M19 18h1 M17 16v4' }]],
  grid: [['rect', { x: 4, y: 4, width: 6.5, height: 6.5, rx: 1.5 }], ['rect', { x: 13.5, y: 4, width: 6.5, height: 6.5, rx: 1.5 }], ['rect', { x: 4, y: 13.5, width: 6.5, height: 6.5, rx: 1.5 }], ['rect', { x: 13.5, y: 13.5, width: 6.5, height: 6.5, rx: 1.5 }]],
  stop: [['path', { d: 'M8.2 3h7.6L21 8.2v7.6L15.8 21H8.2L3 15.8V8.2z' }], ['rect', { x: 9, y: 9, width: 6, height: 6, rx: 1, fill: 'currentColor' }]],
  square: [['rect', { x: 6, y: 6, width: 12, height: 12, rx: 2, fill: 'currentColor' }]],
  play: [['path', { d: 'M8 5.5v13l10.5-6.5z', fill: 'currentColor' }]],
  pause: [['rect', { x: 6.5, y: 5, width: 4, height: 14, rx: 1, fill: 'currentColor' }], ['rect', { x: 13.5, y: 5, width: 4, height: 14, rx: 1, fill: 'currentColor' }]],
  skip: [['path', { d: 'M5 5.5v13l9.5-6.5z', fill: 'currentColor' }], ['path', { d: 'M18 5.5v13' }]],
  moon: [['path', { d: 'M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z' }]],
  sun: [['circle', { cx: 12, cy: 12, r: 4 }], ['path', { d: 'M12 2.5v2 M12 19.5v2 M4.6 4.6l1.4 1.4 M18 18l1.4 1.4 M2.5 12h2 M19.5 12h2 M4.6 19.4 6 18 M18 6l1.4-1.4' }]],
  trash: [['path', { d: 'M4 7h16 M10 11v6 M14 11v6 M6 7l1 13h10l1-13 M9 7V4h6v3' }]],
  plus: [['path', { d: 'M12 5v14 M5 12h14' }]],
  star: [['path', { d: 'M12 3.2l2.7 5.5 6 .9-4.4 4.2 1 6-5.3-2.8-5.4 2.8 1-6L3.3 9.6l6-.9z' }]],
  starFill: [['path', { d: 'M12 3.2l2.7 5.5 6 .9-4.4 4.2 1 6-5.3-2.8-5.4 2.8 1-6L3.3 9.6l6-.9z', fill: 'currentColor' }]],
  check: [['path', { d: 'M5 12.5l4.5 4.5L19 7.5' }]],
  x: [['path', { d: 'M6 6l12 12 M18 6 6 18' }]],
  eye: [['path', { d: 'M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z' }], ['circle', { cx: 12, cy: 12, r: 3 }]],
  eyeOff: [['path', { d: 'M3 3l18 18 M10.6 5.1A10.6 10.6 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4 M6.6 6.6C3.8 8.4 2 12 2 12s3.6 7 10 7a9.9 9.9 0 0 0 4.4-1 M9.9 9.9a3 3 0 0 0 4.2 4.2' }]],
  mic: [['rect', { x: 9, y: 3, width: 6, height: 11, rx: 3 }], ['path', { d: 'M5.5 11a6.5 6.5 0 0 0 13 0 M12 17.5V21' }]],
  camera: [['path', { d: 'M4 7.5h3l2-3h6l2 3h3a1 1 0 0 1 1 1V19a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8.5a1 1 0 0 1 1-1z' }], ['circle', { cx: 12, cy: 13.5, r: 3.8 }]],
  cloud: [['path', { d: 'M7 18.5a4.5 4.5 0 0 1-.7-8.95A6 6 0 0 1 17.8 9.5 4.5 4.5 0 0 1 17.5 18.5z' }]],
  wifi: [['path', { d: 'M2.5 9a14 14 0 0 1 19 0 M5.5 12.5a9.5 9.5 0 0 1 13 0 M8.8 16a4.8 4.8 0 0 1 6.4 0' }], ['circle', { cx: 12, cy: 19.3, r: 1, fill: 'currentColor' }]],
  logout: [['path', { d: 'M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4 M10 16.5 14.5 12 10 7.5 M14.5 12H3.5' }]],
  key: [['circle', { cx: 15.5, cy: 8.5, r: 4.5 }], ['path', { d: 'M12.3 11.7 3.5 20.5 M6.5 17.5l2.5 2.5 M9 15l2 2' }]],
  refresh: [['path', { d: 'M20 11.5A8 8 0 1 0 17.7 17 M20 4.5v7h-7' }]],
  external: [['path', { d: 'M14 4h6v6 M20 4l-9 9 M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5' }]],
  chevron: [['path', { d: 'M9 6l6 6-6 6' }]],
  info: [['circle', { cx: 12, cy: 12, r: 9 }], ['path', { d: 'M12 11v5.5 M12 7.6v.2' }]],
  alert: [['path', { d: 'M12 3.5 2.5 20h19z' }], ['path', { d: 'M12 10v4.5 M12 17.3v.2' }]],
  cpu: [['rect', { x: 6, y: 6, width: 12, height: 12, rx: 2 }], ['rect', { x: 9.5, y: 9.5, width: 5, height: 5, rx: 1 }], ['path', { d: 'M9 3v3 M15 3v3 M9 18v3 M15 18v3 M3 9h3 M3 15h3 M18 9h3 M18 15h3' }]],
  memory: [['rect', { x: 3, y: 7, width: 18, height: 10, rx: 2 }], ['path', { d: 'M7 10.5v3 M11 10.5v3 M15 10.5v3 M6 17v2.5 M18 17v2.5' }]],
  thermo: [['path', { d: 'M14 14.6V5a2 2 0 0 0-4 0v9.6a4 4 0 1 0 4 0z' }], ['path', { d: 'M12 9v7' }]],
  clock: [['circle', { cx: 12, cy: 12, r: 9 }], ['path', { d: 'M12 7v5l3.5 2' }]],
  heart: [['path', { d: 'M12 20s-7.5-4.6-7.5-10.2A4.3 4.3 0 0 1 12 7a4.3 4.3 0 0 1 7.5 2.8C19.5 15.4 12 20 12 20z' }]],
  lock: [['rect', { x: 5, y: 10.5, width: 14, height: 10, rx: 2.5 }], ['path', { d: 'M8 10.5V8a4 4 0 0 1 8 0v2.5' }]],
  send: [['path', { d: 'M4 12 20.5 4l-5.5 16.5-3.5-7z' }]],
  edit: [['path', { d: 'M4 20h4L19.5 8.5l-4-4L4 16z M13.5 6.5l4 4' }]],
  power: [['path', { d: 'M12 3v8.5 M6.3 6.5a8 8 0 1 0 11.4 0' }]],
  person: [['circle', { cx: 12, cy: 7, r: 3 }], ['path', { d: 'M6.5 21v-5a5.5 5.5 0 0 1 11 0v5' }]],
  wave: [['path', { d: 'M7.5 11.5V6a1.5 1.5 0 0 1 3 0v4.5 M10.5 10V4.5a1.5 1.5 0 0 1 3 0V10 M13.5 10V5.5a1.5 1.5 0 0 1 3 0v6 M16.5 11.5V8.5a1.5 1.5 0 0 1 3 0V14a7 7 0 0 1-7 7h-.7a6.5 6.5 0 0 1-5.2-2.6L3.8 15a1.6 1.6 0 0 1 2.5-2l1.2 1.3' }]],
  music: [['path', { d: 'M9 18V5.5l11-2V16' }], ['circle', { cx: 6.5, cy: 18, r: 2.5 }], ['circle', { cx: 17.5, cy: 16, r: 2.5 }]],
  history: [['path', { d: 'M3.5 12a8.5 8.5 0 1 0 2.5-6 M3 4v4.5h4.5 M12 8v4.5l3 1.8' }]],
  link: [['path', { d: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1 M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1' }]],
  crosshair: [['circle', { cx: 12, cy: 12, r: 8 }], ['path', { d: 'M12 2v4 M12 18v4 M2 12h4 M18 12h4' }]],
  zap: [['path', { d: 'M13 2.5 4.5 13.5H12l-1 8 8.5-11H12z' }]],
};

export function icon(name, cls) {
  const defs = ICONS[name] || ICONS.info;
  const el = s('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false', class: cls });
  for (const [tag, attrs] of defs) el.append(s(tag, attrs));
  return el;
}

// ---------------------------------------------------------------- face drawing
const EYE_L = 40, EYE_R = 80, EYE_Y = 45;

function roundEye(cx, cy, w, hgt) {
  return s('rect', { class: 'mf-eye', x: cx - w / 2, y: cy - hgt / 2, width: w, height: hgt, rx: Math.min(w, hgt) / 2 });
}
function arcEye(cx, cy, up = true) {
  const d = up ? `M${cx - 11} ${cy + 5} Q${cx} ${cy - 15} ${cx + 11} ${cy + 5}` : `M${cx - 11} ${cy - 2} Q${cx} ${cy + 10} ${cx + 11} ${cy - 2}`;
  return s('path', { class: 'mf-line', d, 'stroke-width': 8, 'stroke-linecap': 'round', fill: 'none' });
}
function heartEye(cx, cy) {
  const k = 1.15;
  const d = `M${cx} ${cy + 12 * k} C${cx - 18 * k} ${cy} ${cx - 12 * k} ${cy - 14 * k} ${cx} ${cy - 5 * k} C${cx + 12 * k} ${cy - 14 * k} ${cx + 18 * k} ${cy} ${cx} ${cy + 12 * k}z`;
  return s('path', { class: 'mf-heart', d });
}
function sparkle(x, y, r) {
  return s('path', { class: 'mf-eye', d: `M${x} ${y - r} L${x + r * .3} ${y - r * .3} L${x + r} ${y} L${x + r * .3} ${y + r * .3} L${x} ${y + r} L${x - r * .3} ${y + r * .3} L${x - r} ${y} L${x - r * .3} ${y - r * .3}z` });
}
function lid(cx, cy, w, hgt, side) {
  // Covers the outer-top corner of an eye (sad look).
  const top = cy - hgt / 2 - 2, l = cx - w / 2 - 3, r = cx + w / 2 + 3;
  const pts = side === 'left' ? `${l},${top} ${r},${top} ${l},${cy + 2}` : `${l},${top} ${r},${top} ${r},${cy + 2}`;
  return s('polygon', { class: 'mf-bg', points: pts });
}

function eyesFor(expr, closed) {
  if (closed || expr === 'sleeping') {
    return [arcEye(EYE_L, EYE_Y + 4, false), arcEye(EYE_R, EYE_Y + 4, false)];
  }
  switch (expr) {
    case 'happy': return [arcEye(EYE_L, EYE_Y), arcEye(EYE_R, EYE_Y)];
    case 'celebrating': return [arcEye(EYE_L, EYE_Y), arcEye(EYE_R, EYE_Y), sparkle(18, 20, 6), sparkle(103, 24, 5), sparkle(98, 70, 4)];
    case 'excited': return [roundEye(EYE_L, EYE_Y, 22, 40), roundEye(EYE_R, EYE_Y, 22, 40), sparkle(103, 20, 5), sparkle(17, 22, 4)];
    case 'surprised': return [s('circle', { class: 'mf-eye', cx: EYE_L, cy: EYE_Y, r: 14 }), s('circle', { class: 'mf-eye', cx: EYE_R, cy: EYE_Y, r: 14 })];
    case 'curious': return [roundEye(EYE_L, EYE_Y + 3, 18, 28), roundEye(EYE_R, EYE_Y - 2, 22, 40)];
    case 'confused': return [roundEye(EYE_L, EYE_Y + 4, 16, 20), roundEye(EYE_R, EYE_Y - 3, 20, 36)];
    case 'sleepy': return [roundEye(EYE_L, EYE_Y + 8, 22, 10), roundEye(EYE_R, EYE_Y + 8, 22, 10)];
    case 'sad': return [roundEye(EYE_L, EYE_Y + 3, 18, 30), lid(EYE_L, EYE_Y + 3, 18, 30, 'left'), roundEye(EYE_R, EYE_Y + 3, 18, 30), lid(EYE_R, EYE_Y + 3, 18, 30, 'right')];
    case 'thinking': return [roundEye(EYE_L + 5, EYE_Y - 6, 18, 22), roundEye(EYE_R + 5, EYE_Y - 8, 18, 30)];
    case 'listening': return [roundEye(EYE_L, EYE_Y, 20, 38), roundEye(EYE_R, EYE_Y, 20, 38)];
    case 'speaking': return [roundEye(EYE_L, EYE_Y, 18, 30), roundEye(EYE_R, EYE_Y, 18, 30)];
    case 'playful': return [arcEye(EYE_L, EYE_Y), roundEye(EYE_R, EYE_Y, 19, 34)];
    case 'affectionate': return [heartEye(EYE_L, EYE_Y), heartEye(EYE_R, EYE_Y)];
    default: return [roundEye(EYE_L, EYE_Y, 18, 34), roundEye(EYE_R, EYE_Y, 18, 34)];
  }
}

/** Draws LittleBot's eyes for an expression. Returns an <svg>; call .update() to redraw. */
export function miniFace({ expression = 'neutral', size = 64, gaze = null, closed = false, label } = {}) {
  const root = s('svg', { class: 'mini-face', viewBox: '0 0 120 90', width: size, height: Math.round(size * 0.75), role: label ? 'img' : null, 'aria-hidden': label ? null : 'true' });
  const title = label ? s('title', { text: label }) : null;
  if (title) root.append(title);
  root.append(s('rect', { class: 'mf-screen', x: 0, y: 0, width: 120, height: 90, rx: 26 }));
  const g = s('g', { class: 'mf-eyes' });
  root.append(g);
  let last = '';
  root.update = (expr, gz, isClosed, lbl) => {
    const key = `${expr}|${isClosed}`;
    if (key !== last) {
      clear(g);
      append(g, eyesFor(expr, isClosed));
      last = key;
    }
    const gx = gz && typeof gz.x === 'number' ? gz.x : 0.5;
    const gy = gz && typeof gz.y === 'number' ? gz.y : 0.5;
    g.style.transform = `translate(${((gx - 0.5) * 18).toFixed(1)}px, ${((gy - 0.5) * 12).toFixed(1)}px)`;
    if (title && lbl) title.textContent = lbl;
  };
  root.update(expression, gaze, closed, label);
  return root;
}

// ---------------------------------------------------------------- toasts
export function toast(message, kind = 'info', timeout) {
  const box = document.getElementById('toasts');
  if (!box) return;
  const ico = kind === 'error' ? 'alert' : kind === 'ok' ? 'check' : 'info';
  const el = h('div', { class: ['toast', kind], role: kind === 'error' ? 'alert' : 'status' },
    icon(ico), h('div', { class: 'msg', text: message }),
    h('button', { type: 'button', 'aria-label': 'Dismiss', text: '×', onclick: () => dismiss() }));
  const dismiss = () => {
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 220);
  };
  box.append(el);
  while (box.children.length > 3) box.firstChild.remove();
  setTimeout(dismiss, timeout ?? (kind === 'error' ? 6000 : 3200));
}

// ---------------------------------------------------------------- dialogs
/**
 * Promise-based confirm dialog.
 * opts: {title, body (string | Node | Node[]), confirm, cancel, danger, check (label), phrase (text to type)}
 */
export function confirmDialog(opts) {
  const dlg = document.getElementById('dialog');
  return new Promise((resolve) => {
    clear(dlg);
    const confirmBtn = h('button', { type: 'submit', class: ['btn', opts.danger ? 'danger' : 'primary'], value: 'ok', text: opts.confirm || 'Confirm' });
    const parts = [];
    let checkbox = null, phraseInput = null;
    if (opts.check) {
      checkbox = h('input', { type: 'checkbox' });
      parts.push(h('label', { class: 'check' }, checkbox, h('span', { text: opts.check })));
    }
    if (opts.phrase) {
      phraseInput = h('input', { type: 'text', autocomplete: 'off', autocapitalize: 'characters', spellcheck: 'false', placeholder: opts.phrase, 'aria-label': `Type ${opts.phrase}` });
      parts.push(h('label', { class: 'field' }, h('span', {}, 'Type ', h('code', { text: opts.phrase }), ' to confirm'), phraseInput));
    }
    const sync = () => {
      const ok = (!checkbox || checkbox.checked) && (!phraseInput || phraseInput.value.trim().toUpperCase() === opts.phrase.toUpperCase());
      confirmBtn.disabled = !ok;
    };
    checkbox?.addEventListener('change', sync);
    phraseInput?.addEventListener('input', sync);
    const body = Array.isArray(opts.body) ? opts.body : [opts.body];
    const form = h('form', { method: 'dialog' },
      h('h2', { id: 'dialog-title' }, opts.danger || opts.warn ? icon('alert') : null, opts.title || 'Are you sure?'),
      h('div', { class: 'body' }, body.map((b) => (typeof b === 'string' ? h('p', { text: b }) : b))),
      parts,
      h('div', { class: 'actions' },
        h('button', { type: 'button', class: 'btn ghost', text: opts.cancel || 'Cancel', onclick: () => dlg.close('cancel') }),
        confirmBtn));
    dlg.append(form);
    sync();
    const onClose = () => {
      dlg.removeEventListener('close', onClose);
      resolve(dlg.returnValue === 'ok' ? (phraseInput ? phraseInput.value.trim() : true) : false);
    };
    dlg.returnValue = '';
    dlg.addEventListener('close', onClose);
    dlg.showModal();
    (phraseInput || checkbox || confirmBtn).focus();
  });
}

/** Ask for one line of text. Resolves to the string, or null if cancelled. */
export function promptDialog({ title, label, value = '', maxLength = 120, confirm = 'Save' }) {
  const dlg = document.getElementById('dialog');
  return new Promise((resolve) => {
    clear(dlg);
    const id = nextId('pd');
    const input = h('input', { type: 'text', id, value, maxlength: maxLength, autocomplete: 'off' });
    const form = h('form', { method: 'dialog' },
      h('h2', { id: 'dialog-title', text: title }),
      h('div', { class: 'field' }, h('label', { for: id, text: label }), input),
      h('div', { class: 'actions' },
        h('button', { type: 'button', class: 'btn ghost', text: 'Cancel', onclick: () => dlg.close('cancel') }),
        h('button', { type: 'submit', class: 'btn primary', value: 'ok', text: confirm })));
    dlg.append(form);
    const onClose = () => {
      dlg.removeEventListener('close', onClose);
      resolve(dlg.returnValue === 'ok' ? input.value.trim() : null);
    };
    dlg.returnValue = '';
    dlg.addEventListener('close', onClose);
    dlg.showModal();
    input.select();
  });
}

// ---------------------------------------------------------------- controls
let uid = 0;
export const nextId = (p = 'f') => `${p}-${++uid}`;

/** Toggle switch row with title + explanation. Returns {el, input, set(checked), setDisabled(bool)} */
export function switchRow({ title, text, checked = false, disabled = false, onChange }) {
  const id = nextId('sw');
  const input = h('input', { type: 'checkbox', role: 'switch', id, checked, disabled });
  const el = h('div', { class: 'switch-row' },
    h('div', { class: 'text' }, h('strong', {}, h('label', { for: id, text: title })), text ? h('span', { text }) : null),
    h('div', { class: 'switch' }, input, h('span', { class: 'track' }), h('span', { class: 'thumb' })));
  input.addEventListener('change', async () => {
    const want = input.checked;
    if (!onChange) return;
    input.disabled = true;
    try {
      const res = await onChange(want);
      if (res === false) input.checked = !want;
    } catch (e) {
      input.checked = !want;
    } finally {
      input.disabled = disabled;
    }
  });
  return {
    el, input,
    set(v) { if (document.activeElement !== input) input.checked = !!v; },
    setDisabled(v) { disabled = !!v; input.disabled = disabled; },
  };
}

/** Slider with live label; onChange fires on release. */
export function rangeField({ label, min = 0, max = 1, step = 0.05, value = 0, format = (v) => v, onInput, onChange }) {
  const id = nextId('rg');
  const input = h('input', { type: 'range', id, min, max, step, value });
  const out = h('output', { for: id, text: format(Number(value)) });
  let dragging = false;
  input.addEventListener('input', () => { dragging = true; out.textContent = format(Number(input.value)); onInput?.(Number(input.value)); });
  input.addEventListener('change', () => { dragging = false; onChange?.(Number(input.value)); });
  const el = h('div', { class: 'range' }, h('div', { class: 'range-head' }, h('label', { for: id, text: label }), out), input);
  return {
    el, input,
    set(v) { if (!dragging && document.activeElement !== input && v !== undefined && v !== null) { input.value = v; out.textContent = format(Number(v)); } },
  };
}

/** Chips-style list editor. */
export function tagInput({ values = [], max = 12, maxLen = 40, placeholder = 'Type and press Enter', label }) {
  let items = [...values];
  const input = h('input', { type: 'text', placeholder, maxlength: maxLen, 'aria-label': label || placeholder, enterkeyhint: 'done' });
  const box = h('div', { class: 'tags', onclick: (e) => { if (e.target === box) input.focus(); } });
  const render = () => {
    clear(box);
    items.forEach((v, i) => box.append(h('span', { class: 'tag' }, v,
      h('button', { type: 'button', 'aria-label': `Remove ${v}`, onclick: () => { items.splice(i, 1); render(); input.focus(); } }, icon('x')))));
    box.append(input);
    input.disabled = items.length >= max;
    input.placeholder = items.length >= max ? `Up to ${max}` : (items.length ? 'Add another…' : placeholder);
  };
  const commit = () => {
    const parts = input.value.split(',').map((p) => p.trim()).filter(Boolean);
    for (const p of parts) {
      if (items.length >= max) break;
      if (!items.some((x) => x.toLowerCase() === p.toLowerCase())) items.push(p.slice(0, maxLen));
    }
    input.value = '';
    render();
    input.focus();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); commit(); }
    else if (e.key === 'Backspace' && !input.value && items.length) { items.pop(); render(); input.focus(); }
  });
  input.addEventListener('blur', () => { if (input.value.trim()) commit(); });
  render();
  return { el: box, get: () => { if (input.value.trim()) commit(); return [...items]; }, set: (v) => { items = [...(v || [])]; render(); } };
}

export function card(title, iconName, ...children) {
  return h('section', { class: 'card' },
    title ? h('div', { class: 'card-head' }, h('h2', {}, iconName ? icon(iconName) : null, title)) : null,
    children);
}

export function banner(kind, iconName, title, text) {
  return h('div', { class: ['banner', kind], role: kind === 'danger' ? 'alert' : null },
    icon(iconName), h('div', {}, title ? h('strong', { text: title }) : null, text ? h('span', { text }) : null));
}

export function pageHead(title, text, ...actions) {
  return h('div', { class: 'page-head' },
    h('div', {}, h('h1', { text: title }), text ? h('p', { text }) : null),
    actions.length ? h('div', { class: 'btn-row' }, actions) : null);
}

/** Button that disables itself while its async handler runs. */
export function button(label, { kind = '', iconName, onClick, title, small = false, type = 'button', ariaLabel } = {}) {
  const b = h('button', { type, class: ['btn', kind, small ? 'small' : ''], title, 'aria-label': ariaLabel },
    iconName ? icon(iconName) : null, label ? h('span', { text: label }) : null);
  if (onClick) {
    b.addEventListener('click', async (e) => {
      if (b.disabled) return;
      b.disabled = true;
      try { await onClick(e); } catch (err) { /* errors are toasted by the caller / api layer */ }
      finally { b.disabled = false; }
    });
  }
  return b;
}

// ---------------------------------------------------------------- formatting
export function fmtDuration(sec) {
  if (sec === null || sec === undefined || Number.isNaN(Number(sec))) return '–';
  sec = Math.max(0, Math.round(Number(sec)));
  if (sec < 60) return `${sec} s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m} min`;
  const hrs = Math.floor(m / 60);
  if (hrs < 48) return `${hrs} h ${m % 60} min`;
  return `${Math.floor(hrs / 24)} days`;
}

export function fmtAge(months) {
  if (months === null || months === undefined) return 'Age not set';
  if (months < 24) return `${months} month${months === 1 ? '' : 's'}`;
  const y = Math.floor(months / 12), m = months % 12;
  return m ? `${y} years ${m} month${m === 1 ? '' : 's'}` : `${y} years`;
}

export function titleCase(sv) {
  return String(sv || '').replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

export const STATE_LABELS = {
  IDLE: 'Ready to play',
  LISTENING: 'Listening',
  THINKING: 'Thinking',
  SPEAKING: 'Talking',
  PLAYING_ACTIVITY: 'Playing an activity',
  PLAYING_MEDIA: 'Playing a video',
  SLEEPING: 'Sleeping',
  ERROR: 'Needs attention',
};

export function stateLabel(st) {
  const s0 = st?.state?.state;
  return STATE_LABELS[s0] || (s0 ? titleCase(s0.toLowerCase()) : 'Unknown');
}

/** Turn a device status string into {cls, label} for the device chips. */
export function deviceState(value) {
  const v = String(value ?? 'unavailable').toLowerCase();
  if (['ok', 'running', 'ready', 'connected', 'playing'].includes(v)) return { cls: 'ok', label: 'Working' };
  if (v === 'disabled' || v === 'off') return { cls: 'off', label: 'Turned off' };
  if (v === 'mock' || v === 'simulated' || v === 'fake') return { cls: 'warn', label: 'Simulated' };
  if (v === 'disconnected' || v === 'error' || v === 'failed') return { cls: 'bad', label: v === 'disconnected' ? 'Disconnected' : 'Problem' };
  if (v === 'unavailable' || v === 'none') return { cls: 'off', label: 'Not available' };
  return { cls: 'warn', label: titleCase(v) };
}

/** Camera status for deviceState(): off by privacy, or "simulated" for the mock camera / a pretend face. */
export function cameraState(vision, privacy) {
  const v = vision || {};
  if (privacy?.camera_enabled === false) return 'disabled';
  if (v.camera === 'ok' && (v.simulated || v.backend === 'mock')) return 'simulated';
  return v.camera ?? v.status;
}

export function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

export function throttle(fn, ms) {
  let last = 0, timer = null, pending = null;
  return (...args) => {
    const now = Date.now();
    pending = args;
    if (now - last >= ms) {
      last = now;
      fn(...pending);
      pending = null;
    } else if (!timer) {
      timer = setTimeout(() => { timer = null; last = Date.now(); if (pending) { fn(...pending); pending = null; } }, ms - (now - last));
    }
  };
}
