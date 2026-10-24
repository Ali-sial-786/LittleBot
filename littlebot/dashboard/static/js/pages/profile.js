// Child profile: minimal, optional information. Stored only on the robot.

import { del, get, post, put } from '../api.js';
import { banner, button, clear, confirmDialog, fmtAge, h, icon, pageHead, setText, tagInput, toast } from '../ui.js';

export default {
  mount(view, app) {
    const ageBig = h('div', { class: 'state-big' });
    const ageGroup = h('p', { class: 'muted' });
    const ageNotes = h('div', { class: 'chips mt-2' });

    const nickname = h('input', { type: 'text', id: 'pf-nick', maxlength: 30, autocomplete: 'off', placeholder: 'Optional - e.g. Sami' });
    const modeBirth = h('button', { type: 'button', 'aria-pressed': 'true', text: 'Birth month' });
    const modeAge = h('button', { type: 'button', 'aria-pressed': 'false', text: 'Age in months' });
    const now = new Date();
    const maxMonth = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
    const minDate = new Date(now.getFullYear() - 6, now.getMonth(), 1);
    const minMonth = `${minDate.getFullYear()}-${String(minDate.getMonth() + 1).padStart(2, '0')}`;
    const birth = h('input', { type: 'month', id: 'pf-birth', max: maxMonth, min: minMonth, placeholder: 'YYYY-MM' });
    const ageMonths = h('input', { type: 'number', id: 'pf-age', min: 0, max: 72, inputmode: 'numeric', placeholder: 'e.g. 10' });
    const birthField = h('div', { class: 'field' }, h('label', { for: 'pf-birth', text: 'Birth month' }), birth, h('span', { class: 'hint', text: 'Only month and year - LittleBot never needs the exact date.' }));
    const ageField = h('div', { class: 'field', hidden: true }, h('label', { for: 'pf-age', text: 'Age in months' }), ageMonths, h('span', { class: 'hint', text: 'Use this if you prefer not to give a birth month.' }));
    let mode = 'birth';
    const setMode = (m) => {
      mode = m;
      modeBirth.setAttribute('aria-pressed', String(m === 'birth'));
      modeAge.setAttribute('aria-pressed', String(m === 'age'));
      birthField.hidden = m !== 'birth';
      ageField.hidden = m !== 'age';
    };
    modeBirth.addEventListener('click', () => setMode('birth'));
    modeAge.addEventListener('click', () => setMode('age'));
    const language = h('select', { id: 'pf-lang' });
    const animals = tagInput({ label: 'Favourite animals', placeholder: 'e.g. duck, cat' });
    const songs = tagInput({ label: 'Favourite songs', placeholder: 'e.g. Twinkle Twinkle' });
    const interests = tagInput({ label: 'Interests', placeholder: 'e.g. balls, bubbles' });
    const remembered = tagInput({ label: 'Things LittleBot remembers', max: 20, maxLen: 100, placeholder: "e.g. Ali is Amara's dad" });
    const who = whosWho(app);

    const saveBtn = h('button', { type: 'submit', class: 'btn primary big' }, icon('check'), h('span', { text: 'Save profile' }));
    const form = h('form', { class: 'stack', novalidate: true },
      h('div', { class: 'form-grid two' },
        h('div', { class: 'field' }, h('label', { for: 'pf-nick', text: 'Nickname' }), nickname, h('span', { class: 'hint', text: 'What LittleBot calls your child. A nickname is fine.' })),
        h('div', { class: 'field' }, h('label', { for: 'pf-lang', text: 'Language' }), language)),
      h('fieldset', { class: 'stack-sm' }, h('legend', { text: 'Age' }), h('div', { class: 'segmented' }, modeBirth, modeAge), birthField, ageField),
      h('div', { class: 'form-grid two' },
        h('div', { class: 'field' }, h('span', { text: 'Favourite animals' }), animals.el),
        h('div', { class: 'field' }, h('span', { text: 'Favourite songs' }), songs.el)),
      h('div', { class: 'field' }, h('span', { text: 'Interests' }), interests.el, h('span', { class: 'hint', text: 'Press Enter after each one. LittleBot uses these to personalise activities.' })),
      h('div', { class: 'field' }, h('span', { text: 'Things LittleBot remembers' }), remembered.el, h('span', { class: 'hint', text: 'Say "remember that ..." in a voice chat, or type them here. Every voice chat starts with these notes. Remove one with its x and save.' })),
      h('div', {}, saveBtn));

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const body = {
        nickname: nickname.value.trim(),
        language: language.value || 'en',
        favourite_animals: animals.get(),
        favourite_songs: songs.get(),
        interests: interests.get(),
        remembered: remembered.get(),
      };
      if (mode === 'birth') {
        if (birth.value && !/^\d{4}-\d{2}$/.test(birth.value)) { toast('Birth month must look like 2025-03.', 'error'); birth.focus(); return; }
        body.birth_month = birth.value || '';
        body.age_months = null;
      } else {
        const v = ageMonths.value.trim();
        if (v && !(Number(v) >= 0 && Number(v) <= 72)) { toast('Age must be between 0 and 72 months.', 'error'); ageMonths.focus(); return; }
        body.age_months = v === '' ? null : parseInt(v, 10);
        body.birth_month = '';
      }
      saveBtn.disabled = true;
      const res = await app.act(() => put('/profile', body), 'Profile saved.');
      saveBtn.disabled = false;
      if (res) fill(res);
    });

    view.append(
      pageHead('Child profile', 'Only what LittleBot needs to choose age-appropriate games. Everything is optional and stays on the robot.'),
      h('div', { class: 'grid two' },
        h('section', { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('user'), 'Age used for safety')),
          ageBig, ageGroup, ageNotes),
        h('section', { class: 'card soft' },
          h('div', { class: 'card-head' }, h('h2', {}, icon('lock'), 'Your data')),
          h('ul', { class: 'stack-sm muted', style: { paddingLeft: '18px', margin: 0 } },
            h('li', { text: 'Stored only in LittleBot\'s settings file on the robot.' }),
            h('li', { text: 'Kept to the minimum: a nickname, a birth month (not a date), a few favourites and the notes you ask LittleBot to remember.' }),
            h('li', { text: 'An unknown age is treated as the youngest group - the gentlest settings.' })),
          h('div', { class: 'mt-2' }, button('Forget profile', {
            kind: 'danger-outline', iconName: 'trash',
            onClick: async () => {
              if (await confirmDialog({ title: 'Forget the child profile?', body: 'This clears the nickname, age, favourites and activity preferences.', confirm: 'Forget', danger: true })) {
                const res = await app.act(() => post('/profile/clear'), 'Profile cleared.');
                if (res) fill(res);
              }
            },
          }))),
        h('section', { class: 'card span-2' }, form),
        who.el));

    let languagesFilled = false;
    function fill(res) {
      const p = res.profile || {};
      if (!languagesFilled) {
        for (const [code, name] of Object.entries(res.languages || { en: 'English' })) language.append(h('option', { value: code, text: name }));
        languagesFilled = true;
      }
      nickname.value = p.nickname || '';
      language.value = p.language || 'en';
      birth.value = p.birth_month || '';
      ageMonths.value = p.age_months ?? '';
      setMode(p.age_months !== null && p.age_months !== undefined && p.age_months !== '' ? 'age' : 'birth');
      animals.set(p.favourite_animals || []);
      songs.set(p.favourite_songs || []);
      interests.set(p.interests || []);
      remembered.set(p.remembered || []);
      setText(ageBig, res.age_months != null ? fmtAge(res.age_months) : 'Age not set');
      setText(ageGroup, res.age_months != null ? `Age group: ${res.age_group_label}` : 'LittleBot uses its gentlest settings until you add an age.');
      clear(ageNotes).append(
        h('span', { class: `badge ${res.video_allowed ? 'ok' : ''}` }, icon('film'), res.video_allowed ? 'Videos OK by age' : 'Videos off (under 18 mo)'),
        h('span', { class: `badge ${res.cloud_ai_age_ok ? 'ok' : ''}` }, icon('sparkles'), res.cloud_ai_age_ok ? 'Cloud AI possible' : 'Scripted replies only'));
    }

    get('/profile').then(fill).catch((e) => view.append(banner('danger', 'alert', 'Could not load the profile', e.message)));
    who.load();
    return {};
  },
};

// Who's who: the people LittleBot may recognise on its camera (littlebot/vision/faces.py, opt-in in Privacy)
// and by voice (littlebot/voices/, opt-in in Privacy). Only names, roles and sample counts come from the
// robot. Taking a sample or checking who is in view shows one small preview of what the camera saw; it is
// not kept anywhere. "Record voice" records ~20 s with LittleBot's own microphone; only voiceprints are kept.
function whosWho(app) {
  const status = h('div');
  const list = h('div', { class: 'media-list' });
  const shot = h('img', { alt: 'What the camera saw (not kept)', style: { maxWidth: '320px', width: '100%', borderRadius: '8px', display: 'block' } });
  const shotText = h('p', { class: 'muted' });
  const shotBox = h('div', { class: 'stack-sm mt-2' }, shot, shotText);
  shotBox.hidden = true;
  const name = h('input', { type: 'text', id: 'ww-name', maxlength: 30, autocomplete: 'off', placeholder: 'e.g. Ali' });
  const role = h('input', { type: 'text', id: 'ww-role', maxlength: 30, autocomplete: 'off', placeholder: 'e.g. dad, the baby' });
  const addBtn = h('button', { type: 'submit', class: 'btn primary' }, icon('plus'), h('span', { text: 'Add person' }));
  const addForm = h('form', { class: 'form-grid two mt-2', novalidate: true },
    h('div', { class: 'field' }, h('label', { for: 'ww-name', text: 'Name' }), name),
    h('div', { class: 'field' }, h('label', { for: 'ww-role', text: 'Who they are' }), role, h('span', { class: 'hint', text: 'A few words, for example "dad", "the baby" or "Amara\'s grandma".' })),
    h('div', {}, addBtn));
  let state = { people: [], suggested_samples: 5, retake_days: 30 };
  const voiceBox = h('div', { class: 'card info mt-2', hidden: true });
  let recording = false;

  const voiceText = (p) => {
    if (!p.voice_samples) return 'Voice: not set up.';
    const learned = p.voice_learned ? `, ${p.voice_learned} learned in chats` : '';
    return `Voice: set up (${p.voice_samples} sample${p.voice_samples === 1 ? '' : 's'}${learned}).`;
  };

  async function recordVoice(p) {
    const v = state.voices || {};
    const secs = Math.round(v.record_seconds || 20);
    const left = h('strong', { text: `${secs} s` });
    clear(voiceBox).append(
      h('h3', { text: `Recording ${p.name}'s voice` }),
      h('p', { class: 'muted', text: `${p.name}: read these three sentences out loud, in your normal voice, about an arm's length from LittleBot. Only you should talk.` }),
      h('ol', { class: 'stack-sm' }, (v.sentences || []).map((t) => h('li', { text: t }))),
      h('p', { class: 'mt-1' }, 'Listening… ', left, ' left'));
    voiceBox.hidden = false;
    recording = true;
    const started = Date.now();
    const timer = setInterval(() => setText(left, `${Math.max(0, secs - Math.round((Date.now() - started) / 1000))} s`), 500);
    let res = null;
    try {
      res = await app.act(() => post(`/people/${p.id}/voice`, { seconds: secs }, { timeout: (secs + 25) * 1000 }));
    } finally {
      clearInterval(timer);
      recording = false;
    }
    if (!res) { voiceBox.hidden = true; return; }
    fill(res);
    if (res.ok) {
      toast(`${p.name}'s voice is set up (${res.samples} sample${res.samples === 1 ? '' : 's'}).`, 'ok');
      clear(voiceBox).append(h('p', { text: `Done. LittleBot kept ${p.name}'s voiceprint (numbers only); the recording itself was not kept.` }));
    } else {
      toast(res.detail || 'That recording was not kept.', 'error');
      clear(voiceBox).append(h('p', { text: res.detail || 'That recording was not kept. Try again.' }));
    }
  }

  const showShot = (res, text) => {
    shotBox.hidden = !res?.preview;
    if (res?.preview) shot.src = res.preview; // a data: URL made for this answer only
    setText(shotText, text || '');
  };

  addForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!name.value.trim()) { toast('Add a name first.', 'error'); name.focus(); return; }
    addBtn.disabled = true;
    const res = await app.act(() => post('/people', { name: name.value.trim(), role: role.value.trim() }), (r) => `${r.person?.name || 'They'} can now have face samples taken.`);
    addBtn.disabled = false;
    if (res) { name.value = ''; role.value = ''; fill(res); }
  });

  const label = (p) => (p.role ? `${p.name} (${p.role})` : p.name);
  const samplesText = (p) => {
    if (!p.samples) return `No samples yet: take about ${state.suggested_samples}.`;
    const when = p.last_sample_days == null ? '' : p.last_sample_days < 1 ? ', newest today' : `, newest ${p.last_sample_days} day${p.last_sample_days === 1 ? '' : 's'} ago`;
    const more = p.samples < state.suggested_samples ? ` A few more help (about ${state.suggested_samples}).` : '';
    return `${p.samples} sample${p.samples === 1 ? '' : 's'}${when}.${more}`;
  };

  function row(p) {
    const take = button('Take a sample', {
      kind: 'primary', small: true, iconName: 'camera',
      onClick: async () => {
        const res = await app.act(() => post(`/people/${p.id}/samples`));
        if (!res) return;
        fill(res);
        if (res.ok) {
          toast(`Sample kept for ${p.name} (${res.samples}).`, 'ok');
          showShot(res, `The face boxed in green was kept as numbers for ${p.name}; this photo isn't kept.`);
        } else {
          toast(res.detail || 'That sample was not kept.', 'error');
          showShot(res, res.detail || '');
        }
      },
    });
    const again = button('', {
      kind: 'ghost', small: true, iconName: 'refresh', title: 'Start again', ariaLabel: `Delete ${p.name}'s samples and start again`,
      onClick: async () => {
        if (await confirmDialog({ title: `Start ${p.name}'s samples again?`, body: 'This deletes their samples; take new ones afterwards.', confirm: 'Delete samples', danger: true })) {
          const res = await app.act(() => del(`/people/${p.id}/samples`), 'Samples deleted.');
          if (res) fill(res);
        }
      },
    });
    const remove = button('', {
      kind: 'danger-outline', small: true, iconName: 'trash', title: 'Remove', ariaLabel: `Remove ${p.name}`,
      onClick: async () => {
        if (await confirmDialog({ title: `Remove ${p.name}?`, body: 'LittleBot forgets their name, face samples and voiceprints.', confirm: 'Remove', danger: true })) {
          const res = await app.act(() => del(`/people/${p.id}`), `${p.name} removed.`);
          if (res) fill(res);
        }
      },
    });
    const record = button('Record voice', {
      kind: 'soft', small: true, iconName: 'mic', ariaLabel: `Record ${p.name}'s voice`,
      onClick: () => (recording ? null : recordVoice(p)),
    });
    const forgetVoice = button('', {
      kind: 'ghost', small: true, iconName: 'x', title: 'Forget voice', ariaLabel: `Forget ${p.name}'s voice`,
      onClick: async () => {
        if (await confirmDialog({ title: `Forget ${p.name}'s voice?`, body: 'This deletes their voiceprints. LittleBot no longer recognises their voice until you record it again (or they say who they are in a voice chat).', confirm: 'Forget voice', danger: true })) {
          const res = await app.act(() => del(`/people/${p.id}/voice`), 'Voice forgotten.');
          if (res) fill(res);
        }
      },
    });
    forgetVoice.hidden = !p.voice_samples;
    return h('div', { class: 'media-item' },
      h('div', { class: 'thumb' }, icon('person')),
      h('div', { class: 'info' },
        h('div', { class: 'title', text: label(p) }),
        h('div', { class: 'muted', text: samplesText(p) }),
        h('div', { class: 'muted', text: voiceText(p) }),
        p.needs_new_samples ? h('span', { class: 'badge warn mt-1' }, icon('clock'), 'Time for new samples') : null),
      h('div', { class: 'actions' }, take, again, record, forgetVoice, remove));
  }

  function fill(res) {
    state = res;
    const banners = [];
    if (res.blocked === 'off') banners.push(banner('info', 'info', 'Recognising family members is off', 'Turn on "Recognise family members" on the Privacy page to take samples and let LittleBot use these names.'));
    else if (res.blocked) banners.push(banner('warn', 'alert', 'LittleBot can\'t recognise faces right now', res.blocked_detail || ''));
    const v = res.voices;
    if (v && v.blocked === 'off') banners.push(banner('info', 'info', 'Recognising family voices is off', 'Turn on "Recognise family voices" on the Privacy page to record voices and let the voice chat know who is talking.'));
    else if (v && v.blocked) banners.push(banner('warn', 'alert', 'LittleBot can\'t recognise voices right now', v.blocked_detail || ''));
    clear(status).append(...banners);
    clear(list).append(...(res.people || []).map(row));
    if (!(res.people || []).length) list.append(h('p', { class: 'muted', text: 'Nobody added yet.' }));
    addForm.hidden = (res.people || []).length >= (res.max_people || 12);
  }

  const check = button('Who\'s in view?', {
    kind: 'soft', iconName: 'eye',
    onClick: async () => {
      const res = await app.act(() => post('/people/check'));
      if (!res) return;
      fill(res);
      const names = (res.faces || []).map((f) => `${f.n}: ${f.name ? `${f.name}${f.role ? ` (${f.role})` : ''}` : 'someone LittleBot doesn\'t know'} (match ${f.score.toFixed(2)}, needs ${res.threshold.toFixed(2)})`);
      showShot(res, names.length ? names.join('; ') : 'LittleBot can\'t see a clear face right now.');
    },
  });
  const forget = button('Forget all faces', {
    kind: 'danger-outline', iconName: 'trash',
    onClick: async () => {
      if (await confirmDialog({ title: 'Forget every face and voice?', body: 'This deletes everyone on this list, all their face samples and all their voiceprints. It cannot be undone.', confirm: 'Forget all faces', danger: true })) {
        const res = await app.act(() => post('/people/forget-all'), 'All faces forgotten.');
        if (res) { fill(res); showShot(null); }
      }
    },
  });

  const el = h('section', { class: 'card span-2' },
    h('div', { class: 'card-head' }, h('h2', {}, icon('person'), 'Who\'s who')),
    h('p', { class: 'muted', text: 'LittleBot can learn to recognise the family on its camera, so the voice chat knows who is in front of it and uses their names. Add each person, then take about 5 samples with only them in front of the camera, about an arm\'s length away, facing it, in good light (turn the head a little between samples). Babies\' faces change fast: take new samples every month or so.' }),
    h('p', { class: 'muted mt-1', text: 'Kept on the robot only: the names and, for each sample, 128 numbers that describe the face. Never the photos. The voice chat is told only the names of the people in view.' }),
    h('p', { class: 'muted mt-1', text: 'Voices: press Record voice and read three short sentences to LittleBot (about 20 seconds), so the voice chat knows which grown-up is talking, from "Hey LittleBot" on. LittleBot also learns a voice when someone says who they are in a voice chat ("I\'m Ali"), and gets better each time it is sure. Only voiceprints (192 numbers per sample) are kept on the robot, never recordings.' }),
    status, addForm, h('div', { class: 'mt-2' }, list), voiceBox, shotBox,
    h('div', { class: 'btn-row mt-2' }, check, forget));

  return {
    el,
    load: () => get('/people').then(fill).catch((e) => clear(status).append(banner('danger', 'alert', 'Could not load Who\'s who', e.message))),
  };
}
