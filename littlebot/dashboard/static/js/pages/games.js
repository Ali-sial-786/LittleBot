// Games: start / stop the on-screen games (Bubble Pop, Memory Lights ...) and see what's playing.

import { get, post } from '../api.js';
import { append, button, clear, fmtAge, h, icon, pageHead, setText } from '../ui.js';

const MINUTES_KEY = 'littlebot.games.minutes';
const LENGTHS = [
  { value: '', label: 'Game default' },
  { value: '2', label: '2 min' },
  { value: '3', label: '3 min' },
  { value: '5', label: '5 min' },
  { value: '10', label: '10 min' },
];
const GROUPS = [
  { audience: 'little', title: 'For little ones', note: 'No reading needed: tap and something happens. They also play by themselves, so watching is fun too.' },
  { audience: 'big', title: 'For big kids & grown-ups', note: 'Quick games to play against LittleBot.' },
];

function loadMinutes() {
  try { return localStorage.getItem(MINUTES_KEY) || ''; } catch { return ''; }
}
function saveMinutes(v) {
  try { localStorage.setItem(MINUTES_KEY, v); } catch { /* private mode: not remembered */ }
}
function fmtClock(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}

export default {
  mount(view, app) {
    let items = [];
    let status = null;
    let statusAt = 0;
    let minutes = loadMinutes();
    let lastFetch = 0;

    const running = h('section', { class: 'card info game-running', hidden: true, 'aria-live': 'polite' });
    const unavailable = h('div', { hidden: true });
    const lastLine = h('p', { class: 'muted', hidden: true });
    const lengthChips = h('div', { class: 'chips', role: 'group', 'aria-label': 'How long a game lasts' });
    const groups = GROUPS.map((g) => ({ ...g, grid: h('div', { class: 'act-grid game-grid' }), noteEl: h('p', { class: 'muted', text: g.note }) }));
    const cards = new Map();

    view.append(
      pageHead('Games', "Fun games on LittleBot's screen. Little ones tap and watch; big kids and grown-ups can play against LittleBot."),
      h('div', { class: 'stack' },
        unavailable,
        running,
        lastLine,
        h('section', { class: 'card soft' },
          h('div', { class: 'row between' },
            h('div', {}, h('strong', { text: 'Game length' }), h('div', { class: 'muted', text: 'Each game ends by itself with a little celebration, then the face comes back.' })),
            lengthChips)),
        ...groups.map((g) => h('section', { class: 'stack-sm', 'aria-label': g.title },
          h('h2', { class: 'game-group-title', text: g.title }), g.noteEl, g.grid)),
        h('div', { class: 'banner info' }, icon('info'), h('div', {},
          h('strong', { text: 'How to play' }),
          h('span', { text: "Tap LittleBot's screen (on the desktop simulator: click; keys 1-9 and space work too). "
            + 'The big STOP button or Esc on a keyboard ends a game. Touch needs the screen\'s touch layer set up (docs/DISPLAY_SETUP.md); '
            + "without it the little-ones games still play by themselves." })))));

    function renderLengths() {
      clear(lengthChips);
      for (const l of LENGTHS) {
        const b = h('button', { type: 'button', class: 'chip', 'aria-pressed': String(l.value === minutes), text: l.label });
        b.addEventListener('click', () => { minutes = l.value; saveMinutes(minutes); renderLengths(); });
        lengthChips.append(b);
      }
    }

    function gameCard(g) {
      const playing = h('span', { class: 'badge ok', hidden: true }, icon('play'), 'Playing');
      const startBtn = button('Play', { kind: 'primary', iconName: 'play', ariaLabel: `Play ${g.name}`, onClick: () => start(g) });
      const stopBtn = button('Stop', { kind: 'danger-outline', iconName: 'square', onClick: stop });
      stopBtn.hidden = true;
      const article = h('article', { class: 'card act game-card' },
        h('div', { class: 'game-title' },
          h('span', { class: ['game-emoji', `aud-${g.audience}`], 'aria-hidden': 'true', text: g.emoji || '🎲' }),
          h('div', { class: 'grow' }, h('h3', { text: g.name }),
            h('div', { class: 'meta' }, h('span', { class: 'badge brand', text: g.ages }), playing))),
        h('p', { class: 'desc', text: g.description }),
        g.how_to ? h('details', {}, h('summary', {}, icon('chevron', 'chev'), 'How to play'), h('div', { class: 'body' }, h('p', { text: g.how_to }))) : null,
        h('div', { class: 'act-foot' }, h('span', { class: 'muted', text: `About ${g.minutes} min` }), h('div', { class: 'btn-row' }, stopBtn, startBtn)));
      cards.set(g.id, { article, playing, startBtn, stopBtn });
      return article;
    }

    function renderList() {
      cards.clear();
      for (const g of groups) {
        clear(g.grid);
        for (const it of items.filter((i) => i.audience === g.audience)) g.grid.append(gameCard(it));
      }
      renderStatus();
    }

    async function start(g) {
      const body = minutes ? { minutes: Number(minutes) } : {};
      const r = await app.act(() => post(`/games/${encodeURIComponent(g.id)}/start`, body), `${g.name} is on LittleBot's screen!`);
      if (r?.status) setStatus(r.status);
    }

    async function stop() {
      const r = await app.act(() => post('/games/stop'), 'Game stopped.');
      if (r?.status) setStatus(r.status);
    }

    function setStatus(st) {
      status = st || null;
      statusAt = Date.now();
      renderStatus();
    }

    function secondsLeft() {
      if (!status?.running) return 0;
      return Math.max(0, (status.seconds_left || 0) - (Date.now() - statusAt) / 1000);
    }

    let runKey = '';
    function renderStatus() {
      const st = status || {};
      for (const [id, c] of cards) {
        const on = !!st.running && st.id === id;
        c.playing.hidden = !on;
        c.stopBtn.hidden = !on;
        c.startBtn.querySelector('span').textContent = on ? 'Restart' : 'Play';
        c.article.classList.toggle('playing', on);
      }
      const last = st.last;
      lastLine.hidden = !!st.running || !last;
      if (last && !st.running) {
        const why = last.completed ? 'finished' : (last.reason || 'stopped');
        setText(lastLine, `Last game: ${last.name} (${why}${last.summary ? `, ${last.summary}` : ''}).`);
      }
      const key = st.running ? `${st.id}|${st.game?.summary || ''}|${st.shared}` : '';
      running.hidden = !st.running;
      if (key !== runKey) {
        runKey = key;
        clear(running);
        if (st.running) {
          const timeEl = h('span', { class: 'tnum game-clock' });
          append(running, [
            h('div', { class: 'row between' },
              h('div', { class: 'row nowrap' },
                h('span', { class: 'game-emoji big', 'aria-hidden': 'true', text: st.emoji || '🎲' }),
                h('div', {}, h('div', { class: 'muted', text: 'Now playing on LittleBot' }), h('h2', { text: st.name || 'Game' }),
                  h('div', { class: 'muted' }, st.game?.summary ? `${st.game.summary} · ` : '', timeEl, ' left'))),
              button('Stop game', { kind: 'danger-outline', iconName: 'square', onClick: stop })),
            st.shared ? h('p', { class: 'muted mt-1', text: 'LittleBot is talking at the same time, so the game shares its speaker for now.' }) : null]);
        }
      }
      const clock = running.querySelector('.game-clock');
      if (clock) setText(clock, fmtClock(secondsLeft()));
    }

    const ticker = setInterval(() => { if (status?.running) renderStatus(); }, 1000);

    async function load() {
      let res;
      try {
        res = await get('/games');
      } catch (e) {
        unavailable.hidden = false;
        clear(unavailable).append(h('div', { class: 'banner warn' }, icon('alert'), h('div', {}, h('strong', { text: "Couldn't load the games" }), h('span', { text: e.message }))));
        return;
      }
      if (!res.available) {
        unavailable.hidden = false;
        clear(unavailable).append(h('div', { class: 'banner warn' }, icon('alert'), h('div', {}, h('strong', { text: 'Games are unavailable' }), h('span', { text: 'The games module is not running on LittleBot.' }))));
      }
      items = res.items || [];
      if (res.child_age_months != null) setText(groups[0].noteEl, `${groups[0].note} Your child is ${fmtAge(res.child_age_months)}.`);
      setStatus(res.status);
      renderList();
    }

    function update(st) {
      if (st.games && typeof st.games === 'object' && 'running' in st.games) {
        setStatus(st.games);
      } else if (Date.now() - lastFetch > 3000) { // no robot facade: ask the games API directly
        lastFetch = Date.now();
        get('/games/status').then(setStatus).catch(() => {});
      }
    }

    renderLengths();
    load();
    return { update, destroy() { clearInterval(ticker); } };
  },
};
