// Keys: add, change, check and remove the API keys and tokens LittleBot uses (Claude, the voices, the smart home ...).
// A key goes from this page to the robot only; the robot never sends one back (only its last four characters).

import { del, get, post, put } from '../api.js';
import { banner, button, clear, confirmDialog, h, icon, pageHead, setText } from '../ui.js';

const SOURCE = {
  dashboard: ['ok', 'Saved here'],
  env: ['', 'From the .env file'],
  file: ['', 'From an older key file'],
};

export default {
  mount(view, app) {
    const notices = h('div', { class: 'stack' });
    const list = h('div', { class: 'grid two' });
    const restartBox = h('div', { hidden: true });
    view.append(
      pageHead('Keys', 'The API keys and tokens LittleBot uses. A key saved here is used instead of the one in the .env file, straight away.'),
      notices, restartBox, list,
      h('p', { class: 'muted small mt-2' }, 'Keys are kept on the robot (data/keys, readable only by LittleBot) and are never shown here again: only their last four characters.'));

    function showRestart() {
      clear(restartBox).append(h('section', { class: 'card' },
        banner('warn', 'refresh', 'Restart LittleBot to use the new key', 'This key is only read when LittleBot starts.'),
        h('div', { class: 'btn-row mt-2' }, button('Restart now', { kind: 'soft', iconName: 'refresh', onClick: restart }))));
      restartBox.hidden = false;
    }

    async function restart() {
      const ok = await confirmDialog({ title: 'Restart LittleBot?', warn: true, confirm: 'Restart',
        body: 'LittleBot will stop and start again. The dashboard reconnects automatically if it runs as a service.' });
      if (ok) await app.act(() => post('/system/power', { action: 'restart', confirm: true }), (r) => r?.detail || 'Restarting.');
    }

    function row(k) {
      const id = `key-${k.name}`;
      const input = h('input', { type: 'password', id, autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off',
        maxlength: 300, placeholder: k.set ? 'Paste a new key to replace it' : 'Paste the key' });
      const result = h('p', { class: 'muted', 'aria-live': 'polite', hidden: true });
      const say = (text) => { result.hidden = !text; setText(result, text || ''); };
      const [cls, text] = k.set ? (SOURCE[k.source] || SOURCE.env) : ['warn', 'Not set'];
      const save = h('button', { type: 'submit', class: 'btn primary' }, icon('check'), h('span', { text: 'Save' }));
      const form = h('form', { class: 'stack-sm', autocomplete: 'off' },
        h('div', { class: 'field' }, h('label', { for: id, text: k.set ? 'New key' : 'Key' }), input),
        h('div', { class: 'btn-row' }, save,
          k.set && k.checkable ? button('Check', { kind: 'soft', iconName: 'refresh', onClick: check }) : null,
          k.saved_here ? button('Remove saved key', { kind: 'danger-outline', iconName: 'trash', onClick: remove }) : null,
          h('a', { class: 'btn soft', href: k.get_url, target: '_blank', rel: 'noopener noreferrer' }, icon('external'), h('span', { text: 'Get a key' }))));
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const value = input.value.trim();
        if (value.length < 8) { say('That is too short to be a key.'); input.focus(); return; }
        save.disabled = true;
        say(k.checkable ? 'Checking the key…' : 'Saving…');
        try {
          const r = await put(`/keys/${k.name}`, { value });
          input.value = '';
          if (r.restart_needed) showRestart();
          await load();
          app.refresh();
          sayAfter(k.name, r.detail);
        } catch (err) {
          say(err.message || 'Something went wrong.');
        } finally { save.disabled = false; }
      });

      async function check() {
        say('Checking…');
        try { say((await post(`/keys/${k.name}/check`)).detail); } catch (err) { say(err.message); }
      }

      async function remove() {
        const back = k.fallback === 'env' ? ' LittleBot goes back to the key in the .env file.' : k.fallback === 'file' ? ' LittleBot goes back to the older key file.' : ` LittleBot will have no ${k.label} key.`;
        if (!(await confirmDialog({ title: `Remove the ${k.label} key saved here?`, body: `The key saved on this page is deleted.${back}`, confirm: 'Remove', danger: true }))) return;
        try {
          const r = await del(`/keys/${k.name}`);
          if (r.restart_needed) showRestart();
          await load();
          app.refresh();
          sayAfter(k.name, r.detail);
        } catch (err) { say(err.message); }
      }

      const card = h('section', { class: 'card', id: `${id}-card` },
        h('div', { class: 'card-head' }, h('h2', {}, icon('key'), k.label), h('span', { class: `badge ${cls}`, text: text })),
        h('div', { class: 'stack-sm' },
          h('p', { class: 'muted', style: { margin: 0 }, text: k.what }),
          k.set ? h('p', { style: { margin: 0 } }, 'In use: ends in ', h('code', { text: `…${k.ends_with}` })) : null,
          !k.checkable ? h('p', { class: 'muted small', style: { margin: 0 }, text: "This key can't be checked from here: it is saved as it is." }) : null,
          form, result));
      card.say = say;
      return card;
    }

    // After a save / remove the list is drawn again: the message goes under the new row.
    function sayAfter(name, text) {
      const card = list.querySelector(`#key-${name}-card`);
      if (card?.say) card.say(text);
    }

    async function load() {
      let d;
      try { d = await get('/keys'); } catch (e) { clear(notices).append(banner('danger', 'alert', "Couldn't load the keys", e.message)); return; }
      clear(notices);
      if (d.offline_mode) notices.append(banner('info', 'cloud', 'Offline mode', 'LittleBot uses no keys in offline mode. Keys saved here are kept for when it isn’t.'));
      else if (!d.cloud_enabled) notices.append(banner('info', 'cloud', 'Cloud communication is off', 'Keys can be saved, but they are not checked (nothing is sent) until it is switched on in Privacy.'));
      clear(list).append(...(d.keys || []).map(row));
    }

    load();
    return {};
  },
};
