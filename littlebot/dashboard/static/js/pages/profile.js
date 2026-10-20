// Child profile: minimal, optional information. Stored only on the robot.

import { get, post, put } from '../api.js';
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
      h('div', {}, saveBtn));

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const body = {
        nickname: nickname.value.trim(),
        language: language.value || 'en',
        favourite_animals: animals.get(),
        favourite_songs: songs.get(),
        interests: interests.get(),
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
            h('li', { text: 'Kept to the minimum: a nickname, a birth month (not a date) and a few favourites.' }),
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
        h('section', { class: 'card span-2' }, form)));

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
      setText(ageBig, res.age_months != null ? fmtAge(res.age_months) : 'Age not set');
      setText(ageGroup, res.age_months != null ? `Age group: ${res.age_group_label}` : 'LittleBot uses its gentlest settings until you add an age.');
      clear(ageNotes).append(
        h('span', { class: `badge ${res.video_allowed ? 'ok' : ''}` }, icon('film'), res.video_allowed ? 'Videos OK by age' : 'Videos off (under 18 mo)'),
        h('span', { class: `badge ${res.cloud_ai_age_ok ? 'ok' : ''}` }, icon('sparkles'), res.cloud_ai_age_ok ? 'Cloud AI possible' : 'Scripted replies only'));
    }

    get('/profile').then(fill).catch((e) => view.append(banner('danger', 'alert', 'Could not load the profile', e.message)));
    return {};
  },
};
