// Videos: approved YouTube list, playback, volume, screen-time limits, under-18-months authorisation.

import { del, get, patch, post, put } from '../api.js';
import {
  banner, button, clear, confirmDialog, fmtDuration, h, heldText, icon, pageHead, promptDialog, rangeField, setText, switchRow,
  titleCase, toast,
} from '../ui.js';

const YT_RE = /^(https?:\/\/)?((www|m|music)\.)?(youtube\.com\/(watch\?(.*&)?v=[\w-]{11}|shorts\/[\w-]{11}|live\/[\w-]{11}|embed\/[\w-]{11}|playlist\?(.*&)?list=[\w-]+)|youtu\.be\/[\w-]{11}|youtube-nocookie\.com\/embed\/[\w-]{11})/i;
const ID_RE = /^[\w-]{11}$/;
const HAS_VIDEO_AND_LIST = (u) => /[?&]v=[\w-]{11}/.test(u) && /[?&]list=[\w-]+/.test(u);

/** Player URLs from the robot may say 127.0.0.1; on a phone use the address this page came from. */
function playerHref(url) {
  if (!url || typeof url !== 'string') return null;
  try {
    const u = new URL(url, location.origin);
    if (!/^https?:$/.test(u.protocol)) return null;
    if (['127.0.0.1', 'localhost', '0.0.0.0', '::1', '[::1]'].includes(u.hostname) && u.hostname !== location.hostname) {
      return `${location.origin}${u.pathname}${u.search}${u.hash}`;
    }
    return u.href;
  } catch { return null; }
}

function thumb(item) {
  const box = h('div', { class: 'thumb' });
  if (item.kind !== 'playlist' && ID_RE.test(item.youtube_id || '')) {
    const img = h('img', { src: `https://i.ytimg.com/vi/${item.youtube_id}/mqdefault.jpg`, alt: '', loading: 'lazy', referrerpolicy: 'no-referrer' });
    img.addEventListener('error', () => { clear(box).append(icon('film')); });
    box.append(img);
  } else {
    box.append(icon(item.kind === 'playlist' ? 'music' : 'film'));
  }
  return box;
}

export default {
  mount(view, app) {
    let data = null;

    // ---- policy + now playing
    const policyBox = h('div');
    const npTitle = h('div', { class: 'title' });
    const npHeld = h('p', { class: 'muted', hidden: true });
    const npState = h('span', { class: 'badge' });
    const npSession = h('span', { class: 'tnum' });
    const npDaily = h('span', { class: 'tnum' });
    const npSessionBar = h('span');
    const npDailyBar = h('span');
    const npControls = h('div', { class: 'btn-row' });
    const playerLink = h('a', { class: 'btn soft', target: '_blank', rel: 'noopener', hidden: true }, icon('external'), 'Open player on this device');
    const volume = rangeField({
      label: 'Video volume', min: 0, max: 1, step: 0.05, value: 0.5, format: (v) => `${Math.round(v * 100)}%`,
      onChange: (v) => app.act(() => post('/media/volume', { volume: v })),
    });
    const nowCard = h('section', { class: 'card now-playing' },
      h('div', { class: 'card-head' }, h('h2', {}, icon('play'), 'Now playing'), npState),
      h('div', { class: 'stack' }, npTitle, npHeld, npControls,
        h('div', { class: 'limit-line' }, h('div', { class: 'row' }, h('span', { text: 'This session' }), npSession), h('div', { class: 'progress' }, npSessionBar)),
        h('div', { class: 'limit-line' }, h('div', { class: 'row' }, h('span', { text: 'Today' }), npDaily), h('div', { class: 'progress' }, npDailyBar)),
        volume.el, h('div', { class: 'btn-row' }, playerLink)));

    // ---- add form
    const urlInput = h('input', { type: 'url', id: 'media-url', inputmode: 'url', autocomplete: 'off', spellcheck: 'false', maxlength: 500, placeholder: 'https://www.youtube.com/watch?v=…', required: true });
    const titleInput = h('input', { type: 'text', id: 'media-title', maxlength: 120, placeholder: 'e.g. Twinkle Twinkle (optional)' });
    const urlHint = h('span', { class: 'hint', text: 'Paste a YouTube video or playlist link.' });
    let addKind = 'video';
    const kindVideo = h('button', { type: 'button', 'aria-pressed': 'true', text: 'Just this video' });
    const kindList = h('button', { type: 'button', 'aria-pressed': 'false', text: 'Whole playlist' });
    const setKind = (k) => { addKind = k; kindVideo.setAttribute('aria-pressed', String(k === 'video')); kindList.setAttribute('aria-pressed', String(k === 'playlist')); };
    kindVideo.addEventListener('click', () => setKind('video'));
    kindList.addEventListener('click', () => setKind('playlist'));
    const kindBox = h('div', { class: 'stack-sm', hidden: true },
      h('span', { class: 'muted', text: 'This link is a video inside a playlist. Approve:' }),
      h('div', { class: 'segmented', role: 'group', 'aria-label': 'What to approve' }, kindVideo, kindList));
    const addBtn = h('button', { type: 'submit', class: 'btn primary' }, icon('plus'), h('span', { text: 'Add to approved list' }));
    const addForm = h('form', { class: 'stack', novalidate: true },
      h('div', { class: 'form-grid' },
        h('div', { class: 'field' }, h('label', { for: 'media-url', text: 'YouTube link' }), urlInput, urlHint),
        kindBox,
        h('div', { class: 'field' }, h('label', { for: 'media-title', text: 'Name (optional)' }), titleInput)),
      h('div', {}, addBtn));
    urlInput.addEventListener('input', () => {
      const v = urlInput.value.trim();
      urlInput.removeAttribute('aria-invalid');
      kindBox.hidden = !HAS_VIDEO_AND_LIST(v);
      if (kindBox.hidden) setKind('video');
      if (!v) { urlHint.className = 'hint'; urlHint.textContent = 'Paste a YouTube video or playlist link.'; }
      else if (YT_RE.test(v)) { urlHint.className = 'hint good'; urlHint.textContent = 'Looks like a YouTube link.'; }
      else { urlHint.className = 'hint error'; urlHint.textContent = "That doesn't look like a YouTube link yet."; }
    });
    addForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const url = urlInput.value.trim();
      if (!url) { urlInput.focus(); return; }
      addBtn.disabled = true;
      try {
        const body = { url, title: titleInput.value.trim() || null };
        if (HAS_VIDEO_AND_LIST(url)) body.kind = addKind;
        const res = await post('/media/items', body);
        urlInput.value = ''; titleInput.value = '';
        kindBox.hidden = true; setKind('video');
        urlHint.className = 'hint good';
        urlHint.textContent = `Added “${res.item?.title || 'video'}”.`;
        await reload();
      } catch (ex) {
        urlInput.setAttribute('aria-invalid', 'true');
        urlHint.className = 'hint error';
        urlHint.textContent = ex.message;
        urlInput.focus();
      } finally {
        addBtn.disabled = false;
      }
    });

    // ---- list
    const listBox = h('div', { class: 'media-list' });
    const listCount = h('span', { class: 'muted' });

    // ---- limits
    const enabled = switchRow({
      title: 'Videos allowed', text: 'Turn off to stop all video playback on LittleBot.',
      onChange: async (v) => {
        const r = await app.act(() => put('/media/settings', { enabled: v }), v ? 'Videos allowed.' : 'Videos turned off.');
        await reload(); // the "Videos are turned off" banner depends on it
        return r !== null;
      },
    });
    const sessionInput = h('input', { type: 'number', id: 'lim-session', min: 1, max: 180, inputmode: 'numeric' });
    const dailyInput = h('input', { type: 'number', id: 'lim-daily', min: 0, max: 600, inputmode: 'numeric' });
    const saveLimits = button('Save limits', {
      kind: 'primary',
      onClick: async () => {
        const session = parseInt(sessionInput.value, 10), daily = parseInt(dailyInput.value, 10);
        if (!(session >= 1 && session <= 180)) { toast('Minutes per session must be between 1 and 180.', 'error'); sessionInput.focus(); return; }
        if (!(daily >= 0 && daily <= 600)) { toast('Minutes per day must be between 0 and 600.', 'error'); dailyInput.focus(); return; }
        await app.act(() => put('/media/settings', { session_limit_minutes: session, daily_limit_minutes: daily }), 'Limits saved.');
        await reload();
      },
    });
    const underAge = switchRow({
      title: 'Allow videos before 18 months',
      text: 'Off by default. Health experts recommend avoiding screen media (other than video calls) for children under 18 months.',
      onChange: async (v) => {
        if (v) {
          const ok = await confirmDialog({
            title: 'Allow videos for a child under 18 months?', warn: true,
            body: [
              'Paediatric guidance (for example the American Academy of Pediatrics and the WHO) recommends no screen media for children under 18 months, apart from video calls.',
              'If you allow it, keep videos short, watch together, and talk about what you see. Your time limits still apply.',
            ],
            check: 'I understand and want to allow videos anyway', confirm: 'Allow videos',
          });
          if (!ok) return false;
        }
        const r = await app.act(() => put('/media/settings', { allow_under_min_age: v, confirm_under_min_age: v }), v ? 'Videos authorised.' : 'Back to the safe default.');
        await reload();
        return r !== null;
      },
    });
    const underAgeBox = h('div');

    view.append(
      pageHead('Videos', 'Only videos on your approved list can ever play on LittleBot, with time limits you choose.'),
      h('div', { class: 'stack' },
        policyBox,
        h('p', { class: 'muted', text: 'Videos stream from YouTube and are controlled only here (the “Videos allowed” switch), not by the Privacy page\'s cloud switch. LittleBot sends YouTube nothing about your child - it only plays the videos you approved.' }),
        h('div', { class: 'grid two' },
          nowCard,
          h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('plus'), 'Add a video')), addForm)),
        h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('check'), 'Approved videos'), listCount), listBox),
        h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('clock'), 'Screen-time limits')),
          enabled.el,
          h('div', { class: 'form-grid two mt-1' },
            h('div', { class: 'field' }, h('label', { for: 'lim-session', text: 'Minutes per session' }), sessionInput, h('span', { class: 'hint', text: 'Video stops gently after this long.' })),
            h('div', { class: 'field' }, h('label', { for: 'lim-daily', text: 'Minutes per day' }), dailyInput, h('span', { class: 'hint', text: '0 means no videos today.' }))),
          h('div', { class: 'mt-2' }, saveLimits),
          h('div', { class: 'divider mt-3' }),
          underAgeBox)));

    function renderList() {
      clear(listBox);
      const items = data?.items || [];
      setText(listCount, items.length ? `${items.length} approved` : '');
      if (!data?.available) { listBox.append(h('p', { class: 'empty', text: 'The video player is not available right now.' })); return; }
      if (!items.length) { listBox.append(h('div', { class: 'empty' }, icon('film'), 'No videos yet. Add a favourite song or story above.')); return; }
      const playingId = app.status?.media?.item?.id;
      const move = async (idx, delta) => {
        const ids = items.map((x) => x.id);
        const j = idx + delta;
        if (j < 0 || j >= ids.length) return;
        [ids[idx], ids[j]] = [ids[j], ids[idx]];
        await app.act(() => post('/media/reorder', { ids }));
        await reload();
      };
      items.forEach((it, idx) => {
        const off = it.enabled === false;
        const allow = h('input', { type: 'checkbox', checked: !off });
        allow.addEventListener('change', async () => {
          const r = await app.act(() => patch(`/media/items/${it.id}`, { enabled: allow.checked }), allow.checked ? 'Allowed again.' : 'Paused - it stays on the list but won\'t play.');
          if (!r) allow.checked = !allow.checked;
          await reload();
        });
        const meta = [titleCase(it.kind || 'video'), it.last_error ? `Problem: ${it.last_error}` : null].filter(Boolean).join(' · ');
        listBox.append(h('div', { class: ['media-item', it.id === playingId ? 'playing' : '', off ? 'off' : ''] },
          thumb(it),
          h('div', { class: 'info' }, h('div', { class: 'title', text: it.title || it.url }), h('div', { class: 'muted', text: meta }),
            data.supports_update ? h('label', { class: 'mini-toggle mt-1' }, allow, 'Allowed') : null),
          h('div', { class: 'actions' },
            data.supports_reorder && idx > 0 ? button('', { kind: 'ghost icon small', iconName: 'chevron', ariaLabel: `Move ${it.title} up`, title: 'Move up', onClick: () => move(idx, -1) }) : null,
            button('', { kind: 'primary icon', iconName: 'play', ariaLabel: `Play ${it.title}`, title: 'Play on LittleBot', onClick: () => app.act(() => post('/media/play', { item_id: it.id }), `Playing “${it.title}”`) }),
            button('', { kind: 'soft icon', iconName: 'edit', ariaLabel: `Rename ${it.title}`, title: 'Rename', onClick: async () => {
              const t = await promptDialog({ title: 'Rename video', label: 'Name', value: it.title || '' });
              if (t) { await app.act(() => patch(`/media/items/${it.id}`, { title: t }), 'Renamed.'); await reload(); }
            } }),
            button('', { kind: 'soft icon', iconName: 'trash', ariaLabel: `Remove ${it.title}`, title: 'Remove', onClick: async () => {
              if (await confirmDialog({ title: 'Remove this video?', body: `“${it.title}” will no longer be allowed to play.`, confirm: 'Remove', danger: true })) {
                await app.act(() => del(`/media/items/${it.id}`), 'Removed.');
                await reload();
              }
            } }))));
      });
      for (const b of listBox.querySelectorAll('[title="Move up"] svg')) b.classList.add('rot-up');
    }

    function renderSettings() {
      const st = data?.settings || {};
      enabled.set(st.enabled);
      if (document.activeElement !== sessionInput) sessionInput.value = st.session_limit_minutes ?? '';
      if (document.activeElement !== dailyInput) dailyInput.value = st.daily_limit_minutes ?? '';
      volume.set(st.volume);
      underAge.set(st.allow_under_min_age);
      const pol = data?.policy || {};
      clear(underAgeBox).append(underAge.el);
      clear(policyBox);
      if (pol.under_min_age && !pol.parent_authorised) {
        policyBox.append(banner('info', 'info', 'Videos are off for now',
          `Your child is under ${pol.min_age_months} months (or no age is set), so LittleBot won't play videos. You can still manage the list, and authorise videos below if you choose.`));
      } else if (pol.under_min_age && pol.parent_authorised) {
        policyBox.append(banner('warn', 'alert', 'Videos authorised under 18 months', 'You chose to allow videos for a young child. Keep sessions short and watch together.'));
      }
      if (st.enabled === false) policyBox.append(banner('warn', 'alert', 'Videos are turned off', 'Turn on “Videos allowed” below to play videos.'));
      if (data?.status?.offline) policyBox.append(banner('info', 'wifi', 'Offline mode', 'Videos stream from YouTube, so they need the internet. LittleBot is running in offline mode.'));
    }

    let npKey = '';
    function renderNow(s) {
      const m = s.media || {};
      const unavailable = m.status === 'unavailable' || m.state === 'unavailable';
      const state = unavailable ? 'unavailable' : m.state || 'idle';
      const key = `${state}|${m.item?.id}`;
      setText(npState, titleCase(state));
      npState.className = `badge ${state === 'playing' ? 'ok' : state === 'paused' ? 'warn' : state === 'error' ? 'danger' : ''}`;
      setText(npTitle, unavailable ? 'The video player is not available.' : m.item?.title || 'Nothing playing');
      npHeld.hidden = !m.held;
      setText(npHeld, heldText(m.held));
      const sess = m.session_seconds || 0, sessLim = m.session_limit_seconds || 0;
      const day = m.daily_used_seconds || 0, dayLim = m.daily_limit_seconds || 0;
      setText(npSession, sessLim ? `${fmtDuration(sess)} of ${fmtDuration(sessLim)}` : fmtDuration(sess));
      setText(npDaily, dayLim ? `${fmtDuration(day)} of ${fmtDuration(dayLim)}` : fmtDuration(day));
      npSessionBar.style.width = sessLim ? `${Math.min(100, (sess / sessLim) * 100)}%` : '0%';
      npDailyBar.style.width = dayLim ? `${Math.min(100, (day / dayLim) * 100)}%` : '0%';
      const pl = m.player && typeof m.player === 'object' ? m.player : {};
      const href = typeof pl.path === 'string' && pl.path.startsWith('/') ? `${location.origin}${pl.path}` : playerHref(pl.url);
      playerLink.hidden = !href;
      if (href && playerLink.getAttribute('href') !== href) playerLink.setAttribute('href', href);
      if (key === npKey) return;
      npKey = key;
      clear(npControls);
      if (state === 'playing' || state === 'paused' || state === 'loading') {
        npControls.append(
          state === 'paused'
            ? button('Resume', { kind: 'primary', iconName: 'play', onClick: () => app.act(() => post('/media/resume')) })
            : button('Pause', { kind: 'soft', iconName: 'pause', onClick: () => app.act(() => post('/media/pause')) }),
          button('Skip', { kind: 'soft', iconName: 'skip', onClick: () => app.act(() => post('/media/skip')) }),
          button('Stop', { kind: 'danger-outline', iconName: 'square', onClick: () => app.act(() => post('/media/stop'), 'Video stopped.') }));
      } else {
        npControls.append(h('p', { class: 'muted', text: 'Tap play on an approved video below to start.' }));
      }
      if (data) renderList();
    }

    async function reload() {
      try {
        data = await get('/media');
      } catch (e) {
        app.act(() => Promise.reject(e));
        return;
      }
      renderSettings();
      renderList();
    }

    reload();
    return { update: renderNow };
  },
};
