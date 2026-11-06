// Photos: the pictures and videos LittleBot took when asked (littlebot/capture, docs/PHOTOS.md). They stay on the
// robot. Take a photo or record from here, browse them by day, open one full screen to save it, send it to a phone,
// share a link (with its QR code), show it on LittleBot or delete it; and the parent's switches.

import { del, get, post, put } from '../api.js';
import { append, banner, button, clear, confirmDialog, h, icon, pageHead, s, setText, switchRow, toast } from '../ui.js';

const POLL_MS = 3000;
const POLL_REC_MS = 1000;
const VOICE_HINT = 'Or say “Hey LittleBot, take a picture”, “record this”, “stop recording”, “show my photos” or “send it to my phone”.';

// ---------------------------------------------------------------- formatting
function clock(sec) {
  const t = Math.max(0, Math.floor(Number(sec) || 0));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`;
}

function size(bytes) {
  const b = Number(bytes) || 0;
  const mb = b / (1024 * 1024);
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
  if (mb >= 10) return `${Math.round(mb)} MB`;
  if (mb >= 1) return `${mb.toFixed(1)} MB`;
  return `${Math.max(1, Math.round(b / 1024))} KB`;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

function hoursText(hours) { // 24 -> "24 hours", 0.5 -> "30 minutes"
  const hr = Number(hours) || 0;
  if (hr >= 1) return plural(Math.round(hr * 10) / 10, 'hour');
  return plural(Math.max(1, Math.round(hr * 60)), 'minute');
}

function minutesText(min) { // 10 -> "10 minutes", 0.5 -> "30 seconds"
  const m = Number(min) || 0;
  if (m >= 1) return plural(Math.round(m * 10) / 10, 'minute');
  return plural(Math.max(1, Math.round(m * 60)), 'second');
}

function dayStart(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); }

/** "Today", "Yesterday", "Tomorrow" or "Tuesday 4 November" (with the year when it isn't this year). */
function dayLabel(ts) {
  const d = new Date(ts * 1000);
  const now = new Date();
  const days = Math.round((dayStart(now) - dayStart(d)) / 86400000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days === -1) return 'Tomorrow';
  const opts = { weekday: 'long', day: 'numeric', month: 'long' };
  if (d.getFullYear() !== now.getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString(undefined, opts);
}

const timeLabel = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
const whenLabel = (ts) => `${dayLabel(ts)} at ${timeLabel(ts)}`;
const inSentence = (text) => text.replace(/^(Today|Yesterday|Tomorrow)\b/, (w) => w.toLowerCase());

function saveIcon() { // (no download icon in ui.js)
  return s('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false' },
    s('path', { d: 'M12 4v11 M7.5 10.5 12 15l4.5-4.5 M5 19.5h14' }));
}

function thumbButton(it, onOpen) {
  const what = it.kind === 'video' ? `Video, ${clock(it.seconds)}` : 'Photo';
  const img = h('img', { loading: 'lazy', decoding: 'async', alt: '', src: it.thumb });
  const btn = h('button', { type: 'button', class: 'pg-thumb', dataset: { id: it.id }, 'aria-label': `${what}, ${inSentence(whenLabel(it.taken_at))}` },
    img, it.kind === 'video' ? h('span', { class: 'pg-badge', 'aria-hidden': 'true' }, icon('play'), clock(it.seconds)) : null);
  img.addEventListener('error', () => { img.remove(); btn.prepend(h('span', { class: 'pg-noimg' }, icon(it.kind === 'video' ? 'film' : 'camera'))); }, { once: true });
  btn.addEventListener('click', () => onOpen(it.id));
  return btn;
}

export default {
  mount(view, app) {
    let data = null;
    let loadError = '';
    let polledAt = 0;
    let timer = null;
    let ticker = null;
    let loading = false;
    let alive = true;
    let busy = ''; // 'photo' | 'start' | 'stop' while a request runs
    let gridKey = null;
    let bannerKey = null;

    const items = () => data?.items || [];
    const itemUrl = (it, rest = '') => `/capture/items/${encodeURIComponent(it.id)}${rest}`;

    // ---- take one now
    const photoLabel = h('span', { text: 'Take a photo' });
    const photoBtn = h('button', { type: 'button', class: 'btn primary', disabled: true }, icon('camera'), photoLabel);
    photoBtn.addEventListener('click', takePhoto);
    const recLabel = h('span', { class: 'tnum', text: 'Start recording' });
    const recBtn = h('button', { type: 'button', class: 'btn soft', disabled: true }, h('span', { class: 'pg-rec-dot', 'aria-hidden': 'true' }), recLabel);
    recBtn.addEventListener('click', toggleRecording);
    const whyEl = h('p', { class: 'muted', hidden: true });
    const banners = h('div', { class: 'stack-sm' });
    const takeCard = h('section', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, icon('camera'), 'Take one now')),
      h('div', { class: 'stack-sm' }, h('div', { class: 'btn-row pg-take' }, photoBtn, recBtn), whyEl, h('p', { class: 'muted', text: VOICE_HINT })));

    // ---- the gallery
    const countEl = h('span', { class: 'muted' });
    const gridBox = h('div', { class: 'pg-days' }, h('p', { class: 'muted', text: 'Loading…' }));
    const galleryCard = h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('grid'), 'Gallery'), countEl), gridBox);

    // ---- settings
    const enabledSw = switchRow({
      title: 'Photos and videos by voice',
      text: 'LittleBot takes a photo or records a video when someone asks. Off: no new ones at all, not even from this page.',
      onChange: (v) => saveSetting({ enabled: v }, v ? 'Photos and videos switched on.' : 'Photos and videos switched off.'),
    });
    const soundSw = switchRow({
      title: 'Sound in videos',
      text: 'Record the room’s sound with the picture (starts with the next video).',
      onChange: (v) => saveSetting({ video_sound: v }, v ? 'Videos will have sound.' : 'Videos will be silent.'),
    });
    const micNote = h('p', { class: 'muted', hidden: true, text: 'The microphone is off in Privacy, so videos are silent for now whatever this switch says.' });
    const linksEl = h('p', { class: 'muted' });
    const revokeBtn = button('Stop every share link', {
      kind: 'danger-outline', iconName: 'link', onClick: async () => {
        const n = data?.share?.links || 0;
        const ok = await confirmDialog({
          title: 'Stop every share link?', danger: true, confirm: 'Stop them',
          body: `${n === 1 ? 'The link you shared' : `The ${n} links you shared`} (and the QR codes) won’t open any more. The photos and videos stay on LittleBot.`,
        });
        if (ok) {
          const r = await app.act(() => del('/capture/shares'), (res) => `Stopped ${plural(res.revoked || 0, 'share link')}.`);
          if (r?.status) { data = r.status; polledAt = Date.now(); }
        }
        setTimeout(renderAll, 0); // after button() re-enables itself
      },
    });
    const aboutEl = h('p', { class: 'muted' });
    const settingsCard = h('section', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, icon('sliders'), 'Settings')),
      enabledSw.el, soundSw.el, micNote,
      h('div', { class: 'divider mt-2' }),
      h('div', { class: 'stack-sm mt-2' }, linksEl, h('div', { class: 'btn-row' }, revokeBtn), aboutEl));

    view.append(
      pageHead('Photos', 'The pictures and videos LittleBot took. They stay on LittleBot.'),
      h('div', { class: 'stack' }, banners, takeCard, galleryCard, settingsCard));

    // ---------------------------------------------------------------- rendering
    function reasons() {
      const d = data || {};
      if (!data) return { photo: loadError || '', video: loadError || '' };
      let both = null;
      if (d.available === false) both = 'Photos and videos aren’t available on LittleBot right now.';
      else if (d.enabled === false) both = 'Photos and videos are switched off (Settings, at the bottom of this page).';
      else if (d.camera && d.camera.ok === false) both = d.camera.detail || 'The camera isn’t working right now.';
      else if (d.free_mb != null && d.min_free_mb != null && d.free_mb < d.min_free_mb) both = 'LittleBot’s storage is full.';
      let video = both;
      if (!video && d.ffmpeg === false) video = 'Recording needs ffmpeg on LittleBot (sudo apt install ffmpeg). Photos still work.';
      return { photo: both, video };
    }

    function recSeconds() {
      const rec = data?.recording;
      if (!rec) return 0;
      return (Number(rec.seconds) || 0) + Math.max(0, Date.now() - polledAt) / 1000;
    }

    function renderTop() {
      const rec = data?.recording || null;
      const why = reasons();
      photoBtn.disabled = !!busy || why.photo !== null;
      setText(photoLabel, busy === 'photo' ? 'Taking a photo…' : 'Take a photo');
      recBtn.className = rec ? 'btn danger' : 'btn soft';
      recBtn.disabled = !!busy || (!rec && why.video !== null) || rec?.state === 'saving';
      if (busy === 'stop' || rec?.state === 'saving') setText(recLabel, 'Saving the video…');
      else if (busy === 'start') setText(recLabel, 'Starting…');
      else if (rec) setText(recLabel, `Stop recording (${clock(recSeconds())})`);
      else setText(recLabel, 'Start recording');
      let line = '';
      if (rec) {
        line = `Recording${rec.sound ? ' with sound' : ' without sound'}. It stops by itself after ${minutesText((Number(rec.max_seconds) || 0) / 60 || data.max_video_minutes)}.`;
      } else if (why.photo) line = why.photo;
      else if (why.video) line = why.video;
      whyEl.hidden = !line;
      setText(whyEl, line);
      if (rec && !ticker) ticker = setInterval(() => { if (data?.recording && !busy && data.recording.state !== 'saving') setText(recLabel, `Stop recording (${clock(recSeconds())})`); }, 500);
      if (!rec && ticker) { clearInterval(ticker); ticker = null; }
    }

    function renderBanners() {
      const d = data || {};
      const share = d.share || {};
      const full = d.free_mb != null && d.min_free_mb != null && d.free_mb < d.min_free_mb;
      const key = JSON.stringify([!!data, d.available, d.detail, share.reachable, share.lan_help, full, full && d.free_mb]);
      if (key === bannerKey) return;
      bannerKey = key;
      clear(banners);
      if (!data) return;
      append(banners, [
        d.available === false ? banner('info', 'info', 'Not available on LittleBot', `Photos and videos aren’t running${d.detail ? ` (${d.detail})` : ''}. The photos already taken are safe.`) : null,
        d.available !== false && share.reachable === false
          ? banner('warn', 'wifi', 'Phones can’t open share links yet', share.lan_help || 'The dashboard only listens on LittleBot itself, so a phone can’t reach a share link.') : null,
        full ? banner('warn', 'alert', 'LittleBot’s storage is nearly full', `Only ${d.free_mb} MB is free, so new photos and videos can’t be saved. Delete a few you don’t need.`) : null,
      ]);
    }

    function renderGrid() {
      const list = items();
      let photos = data?.photos, videos = data?.videos;
      if (photos == null) photos = list.filter((it) => it.kind === 'photo').length;
      if (videos == null) videos = list.filter((it) => it.kind === 'video').length;
      const parts = [photos ? plural(photos, 'photo') : null, videos ? plural(videos, 'video') : null].filter(Boolean).join(', ');
      setText(countEl, parts ? `${parts}${data?.bytes ? ` · ${size(data.bytes)}` : ''}` : '');
      // only when the list changed (or the day did: "Today" becomes "Yesterday"), so the pictures aren't reloaded
      const key = `${!!data}|${loadError}|${new Date().toDateString()}|${list.map((it) => `${it.id}:${it.bytes}`).join(',')}`;
      if (key === gridKey) return;
      gridKey = key;
      clear(gridBox);
      if (!data) { gridBox.append(h('p', { class: 'muted', text: loadError || 'Loading…' })); return; }
      if (!list.length) {
        gridBox.append(h('div', { class: 'empty' }, icon('camera'), 'No photos yet. Say “Hey LittleBot, take a picture”, or tap Take a photo above.'));
        return;
      }
      let day = null, grid = null;
      for (const it of list) {
        const label = dayLabel(it.taken_at);
        if (label !== day) {
          day = label;
          grid = h('div', { class: 'pg-grid' });
          gridBox.append(h('h3', { class: 'pg-day', text: label }), grid);
        }
        grid.append(thumbButton(it, openViewer));
      }
    }

    function renderSettings() {
      const d = data || {};
      enabledSw.set(d.enabled !== false);
      soundSw.set(d.video_sound !== false);
      enabledSw.setDisabled(!data);
      soundSw.setDisabled(!data);
      micNote.hidden = d.microphone !== false;
      const n = d.share?.links || 0;
      setText(linksEl, n ? `${n === 1 ? '1 share link is' : `${n} share links are`} working right now.` : 'No share links are working right now.');
      revokeBtn.disabled = !n || d.available === false;
      const hours = d.share?.hours ? hoursText(d.share.hours) : 'a day';
      const max = d.max_video_minutes ? minutesText(d.max_video_minutes) : 'a few minutes';
      setText(aboutEl, `Everything stays on LittleBot: nothing is uploaded. A share link opens only on your home Wi-Fi and stops working after ${hours}. Recordings stop by themselves after ${max}.`);
    }

    function renderAll() {
      if (!alive) return;
      renderBanners();
      renderTop();
      renderGrid();
      renderSettings();
      if (vId !== null) {
        if (!current()) { closeViewer(); toast('That one was deleted.', 'info'); }
        else vSend.hidden = !(data?.phones || []).length;
      }
    }

    // ---------------------------------------------------------------- actions (page)
    function took(r) {
      if (r?.status) { data = r.status; polledAt = Date.now(); loadError = ''; }
    }

    async function takePhoto() {
      if (busy) return;
      busy = 'photo';
      renderTop();
      try {
        const r = await app.act(() => post('/capture/photo', {}, { timeout: 30000 }));
        took(r);
        if (r?.ok) toast('Photo taken. It’s in the gallery below.', 'ok');
        else if (r) toast(r.detail || 'No photo this time.', 'error');
      } finally {
        busy = '';
        renderAll();
      }
    }

    async function toggleRecording() {
      if (busy) return;
      const stopping = !!data?.recording;
      busy = stopping ? 'stop' : 'start';
      renderTop();
      try {
        if (stopping) {
          const r = await app.act(() => post('/capture/video/stop', {}, { timeout: 60000 }));
          took(r);
          if (r?.ok) toast(`Video saved (${clock(r.seconds)}).`, 'ok');
          else if (r) toast(r.detail || 'The video wasn’t saved.', 'error');
        } else {
          const r = await app.act(() => post('/capture/video/start'));
          took(r);
          if (r?.ok) toast(r.sound ? 'Recording, with sound.' : 'Recording, without sound.', 'ok');
          else if (r) toast(r.detail || 'The recording didn’t start.', 'error');
        }
      } finally {
        busy = '';
        renderAll();
        schedule();
      }
    }

    async function saveSetting(body, okMessage) {
      const r = await app.act(() => put('/capture/settings', body), okMessage);
      if (r) {
        const { ok, ...rest } = r;
        data = rest;
        polledAt = Date.now();
        renderAll();
      }
      return r !== null;
    }

    // ---------------------------------------------------------------- the full-screen viewer
    let vId = null; // the item shown, while open
    let vOpener = null; // what had the focus before it opened
    let vPanel = null; // 'share' | 'phones' | null
    let vHistory = false; // a history entry was pushed (the phone's Back button closes the viewer)
    let vBackPending = false; // closing went Back over that entry and its popstate hasn't come yet
    const vWhen = h('div', { class: 'pv-when' });
    const vWhat = h('div', { class: 'pv-what' });
    const vClose = h('button', { type: 'button', class: 'pv-icon', 'aria-label': 'Close', title: 'Close (Esc)' }, icon('x'));
    const vPrev = h('button', { type: 'button', class: 'pv-nav prev', 'aria-label': 'Previous (newer)', title: 'Newer' }, icon('chevron'));
    const vNext = h('button', { type: 'button', class: 'pv-nav next', 'aria-label': 'Next (older)', title: 'Older' }, icon('chevron'));
    const vStage = h('div', { class: 'pv-stage' });
    const vNote = h('p', { class: 'pv-note', role: 'status', 'aria-live': 'polite' });
    const vPanelBox = h('div', { class: 'pv-panel', hidden: true });
    const vSave = h('a', { class: 'pv-act', href: '#', download: '' }, saveIcon(), h('span', { text: 'Save' }));
    const vSend = actButton('send', 'Send to phone', sendToPhone);
    const vShare = actButton('link', 'Share link', () => (vPanel === 'share' ? closePanel() : shareLink(false)));
    const vShow = actButton('robot', 'Show on LittleBot', showOnRobot);
    const vDelete = actButton('trash', 'Delete', deleteItem, 'danger');
    const overlay = h('div', { class: 'pv', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Photo', tabindex: '-1', hidden: true },
      h('div', { class: 'pv-top' }, h('div', { class: 'pv-title' }, vWhen, vWhat), vClose),
      h('div', { class: 'pv-body' }, vStage, vPrev, vNext),
      h('div', { class: 'pv-bottom' }, vNote, vPanelBox, h('div', { class: 'pv-actions' }, vSave, vSend, vShare, vShow, vDelete)));
    document.body.append(overlay);

    vClose.addEventListener('click', () => closeViewer());
    vPrev.addEventListener('click', () => step(-1));
    vNext.addEventListener('click', () => step(1));

    function actButton(iconName, label, fn, cls) {
      const b = h('button', { type: 'button', class: ['pv-act', cls] }, icon(iconName), h('span', { text: label }));
      b.addEventListener('click', async () => {
        if (b.disabled) return;
        b.disabled = true;
        try { await fn(); } finally { b.disabled = false; }
      });
      return b;
    }

    const current = () => (vId === null ? null : items().find((it) => it.id === vId) || null);

    function note(text, error = false) {
      setText(vNote, text || '');
      vNote.classList.toggle('error', !!error);
    }

    /** An API call from the viewer: errors go in its own status line (toasts would cover its buttons). */
    async function call(fn) {
      try { return await fn(); } catch (e) {
        if (e?.status !== 401) note(e?.message || 'Something went wrong.', true);
        return null;
      }
    }

    function stopMedia() {
      const v = vStage.querySelector('video');
      if (v) { v.pause(); v.removeAttribute('src'); v.load(); }
    }

    function closePanel() {
      vPanel = null;
      clear(vPanelBox);
      vPanelBox.hidden = true;
    }

    function openViewer(id) {
      vOpener = document.activeElement;
      overlay.hidden = false;
      document.documentElement.classList.add('pv-open');
      const appRoot = document.getElementById('app');
      if (appRoot) appRoot.inert = true;
      document.addEventListener('keydown', onKey);
      if (!vBackPending) pushHistory(); // (else onPop pushes it once the last close's Back has happened)
      showItem(id);
      vClose.focus();
    }

    function closeViewer({ fromHistory = false } = {}) {
      if (overlay.hidden) return;
      stopMedia();
      closePanel();
      overlay.hidden = true;
      document.documentElement.classList.remove('pv-open');
      const appRoot = document.getElementById('app');
      if (appRoot) appRoot.inert = false;
      document.removeEventListener('keydown', onKey);
      if (vHistory && !fromHistory && history.state?.photosViewer) { vBackPending = true; history.back(); }
      vHistory = false;
      const back = (vId !== null && gridBox.querySelector(`.pg-thumb[data-id="${CSS.escape(vId)}"]`))
        || (vOpener?.isConnected ? vOpener : null) || gridBox.querySelector('.pg-thumb');
      vId = null;
      clear(vStage);
      if (back) back.focus();
    }

    function pushHistory() {
      try { history.pushState({ photosViewer: true }, ''); vHistory = true; } catch { vHistory = false; }
    }

    function onPop() {
      if (vBackPending) { // our own Back from closing (asynchronous): the viewer may have been opened again since
        vBackPending = false;
        if (!overlay.hidden) pushHistory();
        return;
      }
      closeViewer({ fromHistory: true });
    }
    window.addEventListener('popstate', onPop);

    function showItem(id) {
      const list = items();
      const idx = list.findIndex((it) => it.id === id);
      if (idx < 0) { closeViewer(); return; }
      const it = list[idx];
      stopMedia();
      closePanel();
      note('');
      vId = it.id;
      const video = it.kind === 'video';
      overlay.setAttribute('aria-label', video ? 'Video' : 'Photo');
      vPrev.disabled = idx <= 0;
      vNext.disabled = idx >= list.length - 1;
      setText(vWhen, whenLabel(it.taken_at));
      const meta = [video ? `Video, ${clock(it.seconds)}` : 'Photo', it.who ? `asked by ${it.who}` : null, it.bytes ? size(it.bytes) : null, `${idx + 1} of ${list.length}`];
      setText(vWhat, meta.filter(Boolean).join(' · '));
      clear(vStage);
      if (video) {
        vStage.append(h('video', { class: 'pv-media', controls: true, playsinline: true, preload: 'metadata', poster: it.thumb, src: it.file }));
      } else {
        // the small preview at once, then the full photo when it has arrived
        const img = h('img', { class: 'pv-media', alt: `Photo taken ${inSentence(whenLabel(it.taken_at))}`, src: it.thumb });
        const full = new Image();
        full.addEventListener('load', () => { if (vId === it.id) img.src = it.file; });
        full.src = it.file;
        vStage.append(img);
      }
      vSave.setAttribute('href', `${it.file}?download=1`);
      vSave.setAttribute('download', it.download_name || '');
      vSave.setAttribute('aria-label', `Save this ${video ? 'video' : 'photo'} to this device`);
      vSend.hidden = !(data?.phones || []).length;
    }

    function step(delta) {
      const list = items();
      const idx = list.findIndex((it) => it.id === vId);
      const next = idx < 0 ? null : list[idx + delta];
      if (next) showItem(next.id);
    }

    function onKey(e) {
      if (overlay.hidden || document.getElementById('dialog')?.open) return; // a confirm dialog handles its own keys
      if (e.key === 'Escape') { e.preventDefault(); closeViewer(); return; }
      const tag = e.target?.tagName;
      if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && tag !== 'VIDEO' && tag !== 'INPUT') {
        e.preventDefault();
        step(e.key === 'ArrowLeft' ? -1 : 1);
        return;
      }
      if (e.key === 'Tab') trapFocus(e);
    }

    function trapFocus(e) {
      const all = [...overlay.querySelectorAll('button, a[href], video[controls], input')].filter((el) => !el.disabled && el.offsetParent !== null);
      if (!all.length) return;
      const first = all[0], last = all[all.length - 1];
      const inside = overlay.contains(document.activeElement) && document.activeElement !== overlay;
      if (e.shiftKey && (!inside || document.activeElement === first)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (!inside || document.activeElement === last)) { e.preventDefault(); first.focus(); }
    }

    // swipe left / right on a phone (not on a video's control bar)
    let touch = null;
    vStage.addEventListener('touchstart', (e) => {
      const t = e.touches[0];
      touch = null;
      if (e.touches.length !== 1) return;
      if (e.target?.tagName === 'VIDEO' && t.clientY > e.target.getBoundingClientRect().bottom - 64) return;
      touch = { x: t.clientX, y: t.clientY };
    }, { passive: true });
    vStage.addEventListener('touchend', (e) => {
      if (!touch) return;
      const t = e.changedTouches[0];
      const dx = t.clientX - touch.x, dy = t.clientY - touch.y;
      touch = null;
      if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) step(dx < 0 ? 1 : -1);
    }, { passive: true });

    // ---- the viewer's actions
    async function sendToPhone() {
      const phones = data?.phones || [];
      if (phones.length > 1) {
        if (vPanel === 'phones') { closePanel(); return; }
        closePanel();
        vPanel = 'phones';
        vPanelBox.hidden = false;
        append(vPanelBox, [
          h('strong', { text: 'Send it to…' }),
          h('div', { class: 'btn-row' }, phones.map((p) => button(p.name, { kind: 'soft small', iconName: 'send', onClick: () => sendTo(p) }))),
        ]);
        return;
      }
      if (phones.length === 1) await sendTo(phones[0]);
    }

    async function sendTo(phone) {
      const it = current();
      if (!it) return;
      note('Sending…');
      const r = await call(() => post(itemUrl(it, '/share'), { to: phone.id ?? null, push: true, screen: false }));
      if (!r || vId !== it.id) return;
      if (!r.ok) note(r.detail || 'It didn’t send.', true);
      else if (r.pushed) {
        closePanel();
        note(`Sent to ${phone.id == null ? 'the family’s phone' : phone.name}. Tap the notification to open it and save it.`);
      } else note(r.push_error || 'The phone notification didn’t go.', true);
      schedule(150); // the share link count
    }

    async function shareLink(screen) {
      const it = current();
      if (!it) return;
      const r = await call(() => post(itemUrl(it, '/share'), { push: false, screen: !!screen }));
      if (!r || vId !== it.id) return;
      if (!r.ok) { note(r.detail || 'There’s no share link right now.', true); return; }
      if (vPanel !== 'share') showSharePanel(r);
      if (screen) {
        if (r.on_screen) note('The QR code is on LittleBot’s screen. Point a phone’s camera at it.');
        else note('LittleBot’s screen couldn’t show it right now.', true);
      } else note('');
      schedule(150);
    }

    function showSharePanel(r) {
      closePanel();
      vPanel = 'share';
      vPanelBox.hidden = false;
      const urlBox = h('input', { type: 'text', class: 'pv-url', readonly: true, value: r.url, 'aria-label': 'Share link', spellcheck: 'false' });
      urlBox.addEventListener('focus', () => urlBox.select());
      append(vPanelBox, [
        h('div', { class: 'pv-share' },
          h('img', { class: 'pv-qr', src: r.qr, alt: 'QR code of the share link', width: 168, height: 168 }),
          h('div', { class: 'stack-sm pv-share-text' },
            h('strong', { text: 'Scan it with a phone’s camera, or copy the link' }),
            h('div', { class: 'pv-url-row' }, urlBox, button('Copy', { kind: 'soft small', onClick: () => copy(r.url, urlBox) })),
            h('span', { class: 'muted', text: `It opens on your home Wi-Fi until ${inSentence(whenLabel(r.expires_at))}.` }))),
        r.reachable === false ? banner('warn', 'wifi', 'Phones can’t open it yet', r.lan_help || '') : null,
        h('div', { class: 'btn-row' }, button('Show the QR on LittleBot’s screen', { kind: 'soft small', iconName: 'robot', onClick: () => shareLink(true) })),
        // no phone set up for notifications: say how, so "Send to phone" can appear
        !(data?.phones || []).length && data?.phone_help
          ? h('span', { class: 'muted', text: `To get a notification on your phone instead: ${data.phone_help}` }) : null,
      ]);
    }

    async function copy(text, input) {
      try {
        await navigator.clipboard.writeText(text);
        note('Link copied.');
        return;
      } catch { /* no clipboard API over plain http: select it instead */ }
      input.focus();
      input.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch { ok = false; }
      note(ok ? 'Link copied.' : 'Select the link and copy it.');
    }

    async function showOnRobot() {
      const it = current();
      if (!it) return;
      const video = it.kind === 'video';
      const r = await call(() => post(itemUrl(it, '/show'), { play: video }));
      if (!r || vId !== it.id) return;
      if (r.ok) note(video ? 'Playing on LittleBot’s screen.' : 'It’s on LittleBot’s screen now.');
      else note(r.detail || 'LittleBot’s screen couldn’t show it right now.', true);
    }

    async function deleteItem() {
      const it = current();
      if (!it) return;
      const what = it.kind === 'video' ? 'video' : 'photo';
      const ok = await confirmDialog({ title: `Delete this ${what}?`, body: 'It can’t be undone.', confirm: 'Delete', danger: true });
      if (!ok || vId !== it.id) return;
      const idx = items().findIndex((x) => x.id === it.id);
      const r = await call(() => del(itemUrl(it)));
      if (!r) return;
      if (r.status) { data = r.status; polledAt = Date.now(); }
      else data = { ...data, items: items().filter((x) => x.id !== it.id) };
      const left = items();
      const next = left[Math.min(idx, left.length - 1)];
      vId = next ? next.id : null; // (renderAll would otherwise close the viewer for the deleted one)
      renderAll();
      if (next) { showItem(next.id); note(`The ${what} was deleted.`); vClose.focus(); }
      else { vId = it.id; closeViewer(); toast(`The ${what} was deleted.`, 'ok'); }
    }

    // ---------------------------------------------------------------- polling
    async function refresh() {
      if (!alive) return;
      if (loading) { schedule(200); return; }
      if (!document.hidden) {
        loading = true;
        try {
          data = await get('/capture', { timeout: 8000 });
          polledAt = Date.now();
          loadError = '';
        } catch (e) {
          if (!data) loadError = e?.message || 'Photos aren’t available right now.';
        } finally {
          loading = false;
        }
        renderAll();
      }
      schedule();
    }

    function schedule(ms) {
      clearTimeout(timer);
      timer = null;
      if (!alive) return;
      timer = setTimeout(refresh, ms ?? (data?.recording ? POLL_REC_MS : POLL_MS));
    }

    const onVisible = () => { if (!document.hidden) schedule(0); };
    document.addEventListener('visibilitychange', onVisible);
    renderAll();
    refresh();

    return {
      destroy() {
        alive = false;
        clearTimeout(timer);
        if (ticker) clearInterval(ticker);
        document.removeEventListener('visibilitychange', onVisible);
        window.removeEventListener('popstate', onPop);
        closeViewer({ fromHistory: true });
        overlay.remove();
      },
    };
  },
};
