// Home: live status, STOP, sleep/wake, caregiver prompt, current activity and video, devices, system.

import { get, post } from '../api.js';
import {
  button, clear, deviceState, fmtAge, fmtDuration, h, icon, miniFace, setText, stateLabel, titleCase,
} from '../ui.js';

function stat(label, iconName) {
  const value = h('div', { class: 'value', text: '–' });
  return { el: h('div', { class: 'stat' }, h('div', { class: 'label' }, icon(iconName), label), value), value };
}

function device(name, iconName) {
  const state = h('div', { class: 'state', text: '…' });
  const el = h('div', { class: 'device off' }, h('div', { class: 'ico' }, icon(iconName)), h('div', { class: 'meta' }, h('div', { class: 'name', text: name }), state));
  return { el, set(cls, label) { el.className = `device ${cls}`; setText(state, label); } };
}

export default {
  mount(view, app) {
    // ---------- prompt
    const promptText = h('p', { class: 'prompt-text' });
    const promptOpts = h('div', { class: 'btn-row fill' });
    const promptCard = h('section', { class: 'card prompt span-2', hidden: true, 'aria-live': 'assertive' },
      h('div', { class: 'prompt-label' }, icon('person'), 'LittleBot is asking you'), promptText, promptOpts);
    let promptKey = '';

    // ---------- hero
    const bigFace = miniFace({ expression: 'neutral', size: 120, label: 'LittleBot face' });
    const nameEl = h('h1');
    const stateEl = h('div', { class: 'state-big' });
    const sinceEl = h('p', { class: 'muted' });
    const sleepBtn = h('button', { type: 'button', class: 'btn soft' });
    sleepBtn.addEventListener('click', async () => {
      sleepBtn.disabled = true;
      const asleep = app.status?.state?.state === 'SLEEPING';
      await app.act(() => post(asleep ? '/wake' : '/sleep'), asleep ? 'Good morning, LittleBot!' : 'Night night, LittleBot.');
      sleepBtn.disabled = false;
    });
    const hero = h('section', { class: 'card' },
      h('div', { class: 'face-hero' }, bigFace, h('div', { class: 'who' }, nameEl, stateEl, sinceEl)),
      h('div', { class: 'btn-row mt-2' }, sleepBtn));

    // ---------- stop
    const stopCard = h('section', { class: 'card' },
      h('button', { type: 'button', class: 'stop-big', onclick: () => app.stopEverything() },
        icon('stop'), h('span', {}, 'STOP', h('small', { text: 'Activity, video, speech and motion' }))),
      h('p', { class: 'muted mt-2 center', text: 'The red STOP button at the top is always there too.' }));

    // ---------- activity
    const actBody = h('div', { class: 'running' });
    const actCard = h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('shapes'), 'Activity')), actBody);
    let suggestions = null;
    let actKey = '';

    // ---------- media
    const mediaBody = h('div', { class: 'stack-sm' });
    const mediaCard = h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('film'), 'Video')), mediaBody);
    let mediaKey = '';

    // ---------- devices
    const dev = {
      camera: device('Camera', 'camera'), mic: device('Microphone', 'mic'), speaker: device('Speaker', 'speaker'),
      cloud: device('Cloud AI', 'cloud'), motion: device('Motion', 'robot'), person: device('Presence', 'person'),
    };
    const devCard = h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('shield'), 'Devices & privacy'),
      h('a', { href: '#/privacy', class: 'btn ghost small' }, 'Privacy', icon('chevron'))),
    h('div', { class: 'devices' }, Object.values(dev).map((d) => d.el)));

    // ---------- system
    const st = {
      cpu: stat('CPU', 'cpu'), mem: stat('Memory', 'memory'), temp: stat('Temperature', 'thermo'),
      up: stat('Running for', 'clock'), mode: stat('Mode', 'zap'), age: stat('Child age', 'user'),
    };
    const sysCard = h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('cpu'), 'System')),
      h('div', { class: 'stats' }, Object.values(st).map((x) => x.el)));

    view.append(h('div', { class: 'grid two' }, promptCard, hero, stopCard, actCard, mediaCard, devCard, sysCard));

    get(`/activities`).then((res) => { suggestions = res; actKey = ''; if (app.status) renderActivity(app.status); }).catch(() => { suggestions = { items: [] }; });

    function renderPrompt(s) {
      const p = s.activities?.prompt;
      const key = p ? `${p.prompt_id}|${p.text}|${(p.options || []).join(',')}` : '';
      if (key === promptKey) return;
      promptKey = key;
      promptCard.hidden = !p;
      if (!p) return;
      setText(promptText, p.text || 'Please respond');
      clear(promptOpts);
      const opts = (p.options && p.options.length ? p.options : ['done', 'skip']).slice(0, 6);
      opts.forEach((opt, i) => {
        promptOpts.append(button(titleCase(opt), {
          kind: i === 0 ? 'primary big' : 'soft big',
          onClick: () => app.act(() => post('/activities/respond', { response: opt, prompt_id: p.prompt_id ?? null }), 'Thanks! LittleBot will carry on.'),
        }));
      });
    }

    function renderActivity(s) {
      const a = s.activities || {};
      const unavailable = a.status === 'unavailable';
      const key = unavailable ? 'na' : a.running ? `run|${a.id}|${a.paused}|${a.step_index}|${a.step_text}` : `idle|${suggestions ? 1 : 0}`;
      if (key === actKey) return;
      actKey = key;
      clear(actBody);
      if (unavailable) {
        actBody.append(h('p', { class: 'muted', text: 'The activities engine is not available right now.' }));
        return;
      }
      if (a.running) {
        const total = Number(a.total_steps) || 0;
        const idx = Number(a.step_index) || 0;
        const bar = h('span');
        bar.style.width = total ? `${Math.min(100, ((idx + 1) / total) * 100)}%` : '0%';
        actBody.append(
          h('div', { class: 'row between' }, h('h3', { text: a.name || 'Activity' }), a.paused ? h('span', { class: 'badge warn', text: 'Paused' }) : h('span', { class: 'badge ok', text: 'Playing' })),
          total ? h('div', { class: 'stack-sm' }, h('div', { class: 'progress' }, bar), h('span', { class: 'muted', text: `Step ${Math.min(idx + 1, total)} of ${total}` })) : null,
          a.step_text ? h('p', { class: 'step', text: a.step_text }) : null,
          h('div', { class: 'btn-row' },
            a.paused
              ? button('Resume', { kind: 'primary', iconName: 'play', onClick: () => app.act(() => post('/activities/resume')) })
              : button('Pause', { kind: 'soft', iconName: 'pause', onClick: () => app.act(() => post('/activities/pause')) }),
            button('Stop activity', { kind: 'danger-outline', iconName: 'square', onClick: () => app.act(() => post('/activities/stop'), 'Activity stopped.') })));
        return;
      }
      actBody.append(h('p', { class: 'muted', text: 'Nothing running. Pick something to play together:' }));
      const items = (suggestions?.items || []).filter((i) => i.enabled !== false);
      const picks = [...items.filter((i) => i.favourite), ...items.filter((i) => !i.favourite && i.suitable_now)].slice(0, 3);
      if (picks.length) {
        actBody.append(h('div', { class: 'stack-sm' }, picks.map((i) => h('div', { class: 'row between nowrap' },
          h('div', { class: 'grow' }, h('strong', { text: i.name }), h('div', { class: 'muted', text: [i.age_label, titleCase(i.category)].filter(Boolean).join(' · ') })),
          button('Start', { kind: 'primary small', iconName: 'play', onClick: () => app.act(() => post(`/activities/${encodeURIComponent(i.id)}/start`), `Starting ${i.name}`) })))));
      }
      actBody.append(h('a', { href: '#/activities', class: 'btn soft' }, 'Browse all activities', icon('chevron')));
    }

    function renderMedia(s) {
      const m = s.media || {};
      const unavailable = m.status === 'unavailable' || m.state === 'unavailable';
      const used = m.daily_used_seconds, limit = m.daily_limit_seconds;
      const key = unavailable ? 'na' : `${m.state}|${m.item?.id}|${Math.floor((used || 0) / 30)}|${limit}`;
      if (key === mediaKey) return;
      mediaKey = key;
      clear(mediaBody);
      if (unavailable) {
        mediaBody.append(h('p', { class: 'muted', text: 'The video player is not available right now.' }));
        return;
      }
      const active = m.state === 'playing' || m.state === 'paused' || m.state === 'loading';
      if (active) {
        mediaBody.append(
          h('div', { class: 'row between' }, h('strong', { text: m.item?.title || 'Video' }), h('span', { class: `badge ${m.state === 'paused' ? 'warn' : 'ok'}`, text: titleCase(m.state) })),
          h('div', { class: 'btn-row' },
            m.state === 'paused'
              ? button('Resume', { kind: 'primary', iconName: 'play', onClick: () => app.act(() => post('/media/resume')) })
              : button('Pause', { kind: 'soft', iconName: 'pause', onClick: () => app.act(() => post('/media/pause')) }),
            button('Stop video', { kind: 'danger-outline', iconName: 'square', onClick: () => app.act(() => post('/media/stop'), 'Video stopped.') })));
      } else {
        mediaBody.append(h('p', { class: 'muted', text: m.state === 'error' ? 'The last video had a problem.' : 'No video playing.' }));
      }
      if (limit) {
        const bar = h('span');
        const pct = Math.min(100, ((used || 0) / limit) * 100);
        bar.style.width = `${pct}%`;
        mediaBody.append(h('div', { class: 'limit-line' },
          h('div', { class: 'row' }, h('span', { text: 'Screen time today' }), h('span', { class: 'tnum', text: `${fmtDuration(used || 0)} of ${fmtDuration(limit)}` })),
          h('div', { class: `progress ${pct > 85 ? 'warn' : ''}` }, bar)));
      }
      mediaBody.append(h('a', { href: '#/media', class: 'btn soft' }, 'Videos & limits', icon('chevron')));
    }

    function renderDevices(s) {
      const priv = s.privacy || {};
      const cam = s.vision || {};
      const audio = s.audio || {};
      dev.camera.set(...Object.values(priv.camera_enabled === false ? deviceState('disabled') : deviceState(cam.camera ?? cam.status)));
      dev.mic.set(...Object.values(priv.microphone_enabled === false ? deviceState('disabled') : deviceState(audio.microphone?.status ?? audio.status)));
      dev.speaker.set(...Object.values(deviceState(audio.speaker?.status ?? audio.status)));
      const cloudOn = priv.cloud_enabled && s.settings?.ai_enabled;
      dev.cloud.set(cloudOn ? 'ok' : 'off', priv.offline_mode ? 'Offline' : cloudOn ? 'On' : 'Off');
      const mo = s.motion || {};
      if (mo.status === 'unavailable') dev.motion.set('off', 'Not available');
      else if (mo.estop) dev.motion.set('bad', 'Emergency stop');
      else if (mo.physical_enabled && mo.armed) dev.motion.set('warn', 'Motors armed');
      else dev.motion.set('ok', 'Simulated');
      if (cam.status === 'unavailable' || priv.camera_enabled === false) dev.person.set('off', '–');
      else dev.person.set(cam.person_present ? 'ok' : 'off', cam.person_present ? 'Someone is here' : 'Nobody seen');
    }

    function renderSystem(s) {
      const sys = s.system || {};
      setText(st.cpu.value, sys.cpu_percent != null ? `${Math.round(sys.cpu_percent)}%` : '–');
      setText(st.mem.value, sys.memory_mb != null ? `${Math.round(sys.memory_mb)} MB` : '–');
      setText(st.temp.value, sys.temperature_c != null ? `${Math.round(sys.temperature_c)}°C` : '–');
      setText(st.up.value, fmtDuration(s.uptime_seconds));
      setText(st.mode.value, titleCase(s.mode || '–'));
      setText(st.age.value, s.age_months != null ? fmtAge(s.age_months) : 'Not set');
    }

    return {
      update(s) {
        const fs = s.face || {};
        const asleep = s.state?.state === 'SLEEPING';
        bigFace.update(asleep ? 'sleepy' : fs.expression || 'neutral', fs.gaze, !!fs.eyes_closed || asleep, `LittleBot looks ${fs.expression || 'neutral'}`);
        setText(nameEl, s.name || 'LittleBot');
        setText(stateEl, stateLabel(s));
        const since = s.state?.since_seconds;
        setText(sinceEl, s.state?.state === 'ERROR' && s.state?.last_error ? `Problem: ${s.state.last_error}` : since != null ? `for ${fmtDuration(since)}` : '');
        const label = asleep ? 'Wake up' : 'Go to sleep';
        if (sleepBtn.dataset.label !== label) {
          sleepBtn.dataset.label = label;
          clear(sleepBtn).append(icon(asleep ? 'sun' : 'moon'), h('span', { text: label }));
        }
        renderPrompt(s);
        renderActivity(s);
        renderMedia(s);
        renderDevices(s);
        renderSystem(s);
      },
    };
  },
};
