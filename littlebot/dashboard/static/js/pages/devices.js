// Audio & devices: speaker, speech, microphone and camera status, tests and settings.

import { get, post, put } from '../api.js';
import { banner, button, clear, deviceState, h, icon, pageHead, rangeField, setText, switchRow, titleCase } from '../ui.js';

function statusBadge(value) {
  const ds = deviceState(value);
  const cls = ds.cls === 'ok' ? 'ok' : ds.cls === 'bad' ? 'danger' : ds.cls === 'warn' ? 'warn' : '';
  return h('span', { class: `badge ${cls}`, text: ds.label });
}

function kv(dl, rows) {
  clear(dl);
  for (const [k, v] of rows) {
    if (v === undefined) continue;
    dl.append(h('dt', { text: k }), h('dd', { text: v === null || v === '' ? '–' : String(v) }));
  }
}

export default {
  mount(view, app) {
    const spkBadge = h('span');
    const micBadge = h('span');
    const camBadge = h('span');
    const spkKv = h('dl', { class: 'kv' });
    const micKv = h('dl', { class: 'kv' });
    const camKv = h('dl', { class: 'kv' });
    const micResult = h('div', { class: 'stack-sm' });
    const spkResult = h('div', { class: 'stack-sm' });
    const devList = h('div', { class: 'stack-sm' }, h('p', { class: 'muted', text: 'Tap “Scan devices” to list the speakers and microphones LittleBot can use.' }));
    const micNote = h('div', { hidden: true });
    const camNote = h('div', { hidden: true });
    const sounds = h('div', { class: 'chips' });
    const pct = (v) => `${Math.round(v * 100)}%`;
    const volNote = h('p', { class: 'muted', text: 'Volume is capped for little ears.' });

    const volume = rangeField({ label: 'Speaker volume', min: 0, max: 1, step: 0.05, value: 0.5, format: pct,
      onChange: async (v) => { const r = await app.act(() => put('/audio/settings', { volume: v })); if (r?.settings?.volume !== undefined) volume.set(r.settings.volume); } });
    const rate = rangeField({ label: 'Speaking speed', min: 0.5, max: 1.5, step: 0.05, value: 0.9, format: (v) => `${v.toFixed(2)}×`,
      onChange: (v) => app.act(() => put('/audio/settings', { tts_rate: v })) });
    const follow = switchRow({ title: 'Follow faces with the eyes', text: "LittleBot's eyes gently follow the nearest face the camera sees.",
      onChange: async (v) => (await app.act(() => put('/vision/settings', { follow_faces: v }))) !== null });
    const sayInput = h('input', { type: 'text', id: 'say-text', maxlength: 200, placeholder: 'Hello! Shall we play?', autocomplete: 'off' });
    const sayForm = h('form', { class: 'inline-form' },
      h('div', { class: 'field' }, h('label', { for: 'say-text', text: 'Make LittleBot say…' }), sayInput),
      h('button', { type: 'submit', class: 'btn primary' }, icon('send'), h('span', { text: 'Say it' })));
    sayForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const text = sayInput.value.trim();
      if (!text) { sayInput.focus(); return; }
      if (await app.act(() => post('/audio/say', { text }))) sayInput.value = '';
    });

    view.append(
      pageHead('Audio & devices', 'Check that the speaker, microphone and camera are working, and adjust how LittleBot sounds.'),
      h('div', { class: 'grid two' },
        h('section', { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('speaker'), 'Speaker'), spkBadge),
          h('div', { class: 'stack' }, spkKv, volume.el,
            volNote,
            h('div', { class: 'btn-row' },
              button('Test speaker', { kind: 'soft', iconName: 'speaker', onClick: async () => {
                const r = await app.act(() => post('/audio/test-speaker'));
                clear(spkResult);
                if (r?.result) spkResult.append(r.result.ok ? banner('ok', 'check', 'Speaker works', r.result.detail || '') : banner('danger', 'alert', 'Speaker problem', r.result.detail || ''));
              } }),
              button('Stop sound', { kind: 'ghost', iconName: 'square', onClick: () => app.act(() => post('/audio/stop')) })),
            spkResult,
            h('div', { class: 'stack-sm' }, h('span', { class: 'muted', text: 'Sound effects' }), sounds))),
        h('section', { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('music'), 'Voice')),
          h('div', { class: 'stack' }, rate.el, sayForm)),
        h('section', { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('mic'), 'Microphone'), micBadge),
          h('div', { class: 'stack' }, micNote, micKv,
            h('div', { class: 'btn-row' }, button('Test microphone (2 s)', { kind: 'soft', iconName: 'mic', onClick: testMic })),
            h('p', { class: 'muted', text: 'Say something during the test. The sound is measured and immediately discarded.' }),
            micResult)),
        h('section', { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('camera'), 'Camera'), camBadge),
          h('div', { class: 'stack' }, camNote, camKv, follow.el)),
        h('section', { class: 'card span-2' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('cpu'), 'Audio devices'),
            button('Scan devices', { kind: 'soft small', iconName: 'refresh', onClick: loadDevices })),
          devList)));

    async function testMic() {
      clear(micResult).append(h('p', { class: 'muted', text: 'Listening for 2 seconds…' }));
      const r = await app.act(() => post('/audio/test-microphone', { seconds: 2 }));
      clear(micResult);
      if (!r?.result) return;
      const res = r.result;
      const level = Math.max(0, Math.min(1, Number(res.peak) || 0));
      const bar = h('span');
      bar.style.width = `${Math.round(level * 100)}%`;
      micResult.append(
        res.ok ? banner('ok', 'check', 'Microphone works', res.detail || '') : banner('danger', 'alert', 'Microphone problem', res.detail || ''),
        h('div', { class: 'limit-line' }, h('div', { class: 'row' }, h('span', { text: 'Loudest sound' }), h('span', { class: 'tnum', text: `${Math.round(level * 100)}%` })), h('div', { class: 'progress' }, bar)));
    }

    async function loadDevices() {
      clear(devList).append(h('p', { class: 'muted', text: 'Scanning…' }));
      let d;
      try { d = await get('/audio/devices'); } catch (e) { clear(devList).append(h('p', { class: 'muted', text: e.message })); return; }
      clear(devList);
      for (const [label, items] of [['Outputs (speakers)', d.outputs], ['Inputs (microphones)', d.inputs]]) {
        devList.append(h('h3', { class: 'mt-1', text: label }));
        if (!items || !items.length) { devList.append(h('p', { class: 'muted', text: 'None found.' })); continue; }
        devList.append(h('ul', { class: 'stack-sm', style: { margin: 0, paddingLeft: '18px' } }, items.map((it) => h('li', {}, `${it.name ?? 'Device'} `, it.default ? h('span', { class: 'badge brand', text: 'default' }) : null))));
      }
    }

    function renderAudio(a, privacy) {
      clear(spkBadge).append(statusBadge(a.speaker?.status ?? a.status));
      const micOff = privacy?.microphone_enabled === false;
      clear(micBadge).append(statusBadge(micOff ? 'disabled' : a.microphone?.status ?? a.status));
      kv(spkKv, [['Backend', a.speaker?.backend], ['Device', a.speaker?.device ?? 'System default'], ['Voice', a.tts_provider ? titleCase(a.tts_provider) : undefined], ['Speaking now', a.speaking === undefined ? undefined : a.speaking ? 'Yes' : 'No']]);
      kv(micKv, [['Backend', a.microphone?.backend], ['Device', a.microphone?.device ?? 'System default'], ['Speech recognition', a.stt_provider ? titleCase(a.stt_provider) : undefined], ['Listening now', a.listening === undefined ? undefined : a.listening ? 'Yes' : 'No']]);
      micNote.replaceChildren(...(micOff ? [banner('info', 'info', 'Microphone is off', 'Turn it on in Privacy to use or test it.')] : []));
      micNote.hidden = !micOff;
    }

    function renderVision(v, privacy) {
      const camOff = privacy?.camera_enabled === false;
      clear(camBadge).append(statusBadge(camOff ? 'disabled' : v.camera ?? v.status));
      kv(camKv, [['Backend', v.backend], ['Face detector', v.detector], ['Frame rate', v.fps != null ? `${Number(v.fps).toFixed(1)} fps` : undefined], ['Faces seen', v.faces], ['Someone there', v.person_present === undefined ? undefined : v.person_present ? 'Yes' : 'No']]);
      camNote.replaceChildren(...(camOff ? [banner('info', 'info', 'Camera is off', 'Turn it on in Privacy to let LittleBot see faces.')] : []));
      camNote.hidden = !camOff;
    }

    get('/audio').then((d) => {
      if (d.max_volume) {
        volume.input.max = d.max_volume;
        setText(volNote, `Volume is capped at ${Math.round(d.max_volume * 100)}% for little ears.`);
      }
      volume.set(d.settings?.volume);
      rate.set(d.settings?.tts_rate);
      clear(sounds);
      for (const n of d.sounds || []) sounds.append(button(titleCase(n), { kind: 'soft small', onClick: () => app.act(() => post('/audio/sound', { name: n })) }));
      if (!d.available) spkResult.append(banner('warn', 'alert', 'Audio unavailable', 'LittleBot has no working audio system right now.'));
    }).catch(() => {});
    get('/vision').then((d) => follow.set(d.follow_faces)).catch(() => {});

    return {
      update(st) {
        renderAudio(st.audio || {}, st.privacy);
        renderVision(st.vision || {}, st.privacy);
        if (st.settings) {
          follow.set(st.settings.follow_faces);
          if (st.settings.audio_volume != null) volume.set(st.settings.audio_volume);
        }
      },
    };
  },
};
