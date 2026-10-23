// Settings: password, sessions, dashboard access (LAN opt-in), weather & time, system info, events, config, power.

import { get, post, put } from '../api.js';
import { banner, button, clear, confirmDialog, fmtDuration, h, icon, pageHead, setText, titleCase, toast } from '../ui.js';

export default {
  mount(view, app) {
    const cur = h('input', { type: 'password', id: 'pw-cur', autocomplete: 'current-password', maxlength: 256 });
    const nw = h('input', { type: 'password', id: 'pw-new', autocomplete: 'new-password', maxlength: 256 });
    const nw2 = h('input', { type: 'password', id: 'pw-new2', autocomplete: 'new-password', maxlength: 256 });
    const pwBtn = h('button', { type: 'submit', class: 'btn primary' }, icon('key'), h('span', { text: 'Change password' }));
    const pwForm = h('form', { class: 'stack' },
      h('div', { class: 'field' }, h('label', { for: 'pw-cur', text: 'Current password' }), cur),
      h('div', { class: 'form-grid two' },
        h('div', { class: 'field' }, h('label', { for: 'pw-new', text: 'New password' }), nw, h('span', { class: 'hint', text: 'At least 8 characters.' })),
        h('div', { class: 'field' }, h('label', { for: 'pw-new2', text: 'New password again' }), nw2)),
      h('div', {}, pwBtn));
    pwForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!cur.value) { cur.focus(); return; }
      if (nw.value.length < 8) { toast('The new password needs at least 8 characters.', 'error'); nw.focus(); return; }
      if (nw.value !== nw2.value) { toast("The new passwords don't match.", 'error'); nw2.focus(); return; }
      pwBtn.disabled = true;
      const r = await app.act(() => post('/auth/password', { current_password: cur.value, new_password: nw.value }), (res) => res?.detail || 'Password changed.');
      pwBtn.disabled = false;
      if (r) { cur.value = ''; nw.value = ''; nw2.value = ''; loadSessions(); }
    });

    const sessionsEl = h('p', { class: 'muted' });
    const accessBox = h('div', { class: 'stack' });
    const sysKv = h('dl', { class: 'kv' });
    const services = h('div', { class: 'chips' });
    const events = h('div', { class: 'log', 'aria-live': 'off' });
    const configPre = h('pre', { class: 'json', text: 'Loading…' });
    const eventsDetails = h('details', { class: 'disclosure' }, h('summary', { text: 'Recent events (newest first)' }),
      h('div', { class: 'stack-sm mt-1' }, h('div', {}, button('Refresh', { kind: 'soft small', iconName: 'refresh', onClick: loadEvents })), events));
    const configDetails = h('details', { class: 'disclosure' }, h('summary', { text: 'Configuration (secrets are never shown)' }), h('div', { class: 'mt-1' }, configPre));
    eventsDetails.addEventListener('toggle', () => { if (eventsDetails.open) loadEvents(); });
    configDetails.addEventListener('toggle', () => { if (configDetails.open) loadConfig(); });

    // Weather & time: where home is for the weather (and where that came from), the units, the time here.
    const wxWhere = h('p', { style: { margin: 0 }, text: 'Weather location: finding where LittleBot is…' });
    const wxBanner = h('div');
    const wxKv = h('dl', { class: 'kv' });
    const wxResult = h('p', { class: 'muted', 'aria-live': 'polite', hidden: true });
    const wxInput = h('input', { type: 'text', id: 'weather-location', maxlength: 120, autocomplete: 'off', placeholder: 'Toronto, or 43.65, -79.38' });
    const wxUnits = h('select', { id: 'weather-units' },
      [['auto', 'Automatic'], ['celsius', '°C (Celsius)'], ['fahrenheit', '°F (Fahrenheit)']].map(([value, text]) => h('option', { value, text })));
    const wxSave = h('button', { type: 'submit', class: 'btn primary' }, icon('check'), h('span', { text: 'Save' }));
    const wxForm = h('form', { class: 'stack', hidden: true },
      h('div', { class: 'form-grid two' },
        h('div', { class: 'field' }, h('label', { for: 'weather-location', text: 'Place' }), wxInput,
          h('span', { class: 'hint', text: 'A city or town (add the country if there are several), or latitude, longitude. Empty: use your internet connection.' })),
        h('div', { class: 'field' }, h('label', { for: 'weather-units', text: 'Units' }), wxUnits)),
      h('div', { class: 'btn-row' }, wxSave,
        button('Use my internet connection', { kind: 'soft', iconName: 'wifi', onClick: () => saveWeather('') }),
        button('Cancel', { kind: 'soft', onClick: () => { wxForm.hidden = true; wxChange.hidden = false; } })));
    const wxChange = button('Change', { kind: 'soft small', iconName: 'edit', onClick: () => {
      wxForm.hidden = false; wxChange.hidden = true; wxInput.focus();
    } });
    wxForm.addEventListener('submit', (e) => { e.preventDefault(); saveWeather(wxInput.value.trim()); });
    const weatherCard = h('section', { class: 'card span-2' }, h('div', { class: 'card-head' }, h('h2', {}, icon('sun'), 'Weather & time')),
      h('div', { class: 'stack' },
        h('p', { class: 'muted', text: 'LittleBot can tell you the weather and the time when you ask. ChatGPT gets the weather for this place unless you ask about somewhere else.' }),
        wxBanner, h('div', { class: 'btn-row', style: { alignItems: 'center' } }, wxWhere, wxChange), wxForm, wxKv,
        h('div', { class: 'btn-row' }, button('Check the weather now', { kind: 'soft', iconName: 'cloud', onClick: checkWeather })), wxResult));

    view.append(
      pageHead('Settings', 'Your parent account, how to reach the dashboard, and system details.'),
      h('div', { class: 'grid two' },
        h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('lock'), 'Parent password')), pwForm),
        h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('logout'), 'Sessions')),
          h('div', { class: 'stack' }, sessionsEl,
            h('div', { class: 'btn-row' },
              button('Log out', { kind: 'soft', iconName: 'logout', onClick: async () => { if (await app.act(() => post('/logout'))) location.reload(); } }),
              button('Log out everywhere', { kind: 'danger-outline', iconName: 'logout', onClick: async () => {
                if (await confirmDialog({ title: 'Log out on every device?', body: 'Every phone and computer (including this one) will need the password again.', confirm: 'Log out everywhere', danger: true })) {
                  if (await app.act(() => post('/auth/logout-all'))) location.reload();
                }
              } })))),
        h('section', { class: 'card span-2' }, h('div', { class: 'card-head' }, h('h2', {}, icon('wifi'), 'Dashboard access')), accessBox),
        weatherCard,
        h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('cpu'), 'System')), h('div', { class: 'stack' }, sysKv, services)),
        h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('power'), 'Restart or shut down')),
          h('p', { class: 'muted', text: 'Stops LittleBot safely (motors off first). On the Raspberry Pi the system service starts it again after a restart.' }),
          h('div', { class: 'btn-row mt-2' },
            button('Restart', { kind: 'soft', iconName: 'refresh', onClick: () => power('restart') }),
            button('Shut down', { kind: 'danger-outline', iconName: 'power', onClick: () => power('shutdown') }))),
        h('section', { class: 'card span-2' }, h('div', { class: 'stack' }, eventsDetails, configDetails))));

    async function power(action) {
      const ok = await confirmDialog({
        title: action === 'restart' ? 'Restart LittleBot?' : 'Shut LittleBot down?', danger: action === 'shutdown', warn: true,
        body: action === 'restart' ? 'LittleBot will stop and start again. The dashboard reconnects automatically if it runs as a service.' : 'LittleBot will stop. You will need to start it again on the robot.',
        confirm: action === 'restart' ? 'Restart' : 'Shut down',
      });
      if (!ok) return;
      await app.act(() => post('/system/power', { action, confirm: true }), (r) => r?.detail || 'Done.');
    }

    async function loadSessions() {
      try {
        const s = await get('/auth/sessions');
        setText(sessionsEl, `${s.active_sessions} active ${s.active_sessions === 1 ? 'session' : 'sessions'}. Sessions last ${Math.round(s.session_hours)} hours; this one ends ${new Date(s.current.expires * 1000).toLocaleString()}.`);
      } catch { /* toasted elsewhere */ }
    }

    async function loadSystem() {
      let d;
      try { d = await get('/system'); } catch (e) { accessBox.append(banner('danger', 'alert', 'Could not load system info', e.message)); return; }
      const dash = d.dashboard || {};
      clear(accessBox);
      const urls = (dash.urls || []).filter(Boolean);
      if (!urls.length) urls.push(`${location.origin}/`);
      if (dash.lan_access) {
        accessBox.append(banner('ok', 'wifi', 'Phones on your home Wi-Fi can connect', 'LAN access is on. Anyone on your network can see the login page, so keep your password private and never forward this port on your router.'));
      } else {
        accessBox.append(banner('info', 'lock', 'This computer only', 'The dashboard only accepts connections from the robot itself (localhost). This is the safest default.'));
      }
      if (urls.length) {
        accessBox.append(h('div', { class: 'stack-sm' }, h('strong', { text: 'Dashboard address' }),
          h('ul', { class: 'stack-sm', style: { margin: 0, paddingLeft: '18px' } }, urls.map((u) => h('li', {}, h('code', { text: u }))))));
      }
      accessBox.append(h('details', { class: 'disclosure' }, h('summary', { text: dash.lan_access ? 'How to turn LAN access off' : 'How to use the dashboard from your phone' }),
        h('div', { class: 'stack-sm mt-1 muted' },
          dash.lan_access
            ? h('p', {}, 'Remove ', h('code', { text: 'lan_access: true' }), ' from ', h('code', { text: 'config/local.yaml' }), ' (or stop passing ', h('code', { text: '--lan' }), ') and restart LittleBot.')
            : h('p', {}, 'LAN access is opt-in. Start LittleBot with ', h('code', { text: 'python -m littlebot --lan' }), ', or add the following to ', h('code', { text: 'config/local.yaml' }), ' and restart:'),
          dash.lan_access ? null : h('pre', { class: 'json', text: 'dashboard:\n  lan_access: true' }),
          h('p', { text: 'Then open the address LittleBot prints (for example http://192.168.1.20:8765/) on a phone connected to the same Wi-Fi. Only use this on a home network you trust.' }))));
      clear(sysKv);
      for (const [k, v] of [['Name', d.name], ['Version', d.version], ['Mode', titleCase(d.mode)], ['Platform', d.monitor?.platform?.system || (d.monitor?.is_raspberry_pi ? 'Raspberry Pi' : '–')],
        ['Running for', fmtDuration(app.status?.uptime_seconds ?? d.monitor?.uptime_seconds)], ['Login attempts', `${dash.login_attempts_per_minute ?? '–'} per minute`]]) {
        sysKv.append(h('dt', { text: k }), h('dd', { text: v ?? '–' }));
      }
      clear(services);
      services.hidden = !Object.keys(d.services || {}).length;
      for (const [name, svc] of Object.entries(d.services || {})) {
        const st = String(svc?.status || 'unknown');
        const cls = st === 'running' ? 'ok' : st === 'failed' ? 'danger' : st === 'degraded' ? 'warn' : '';
        services.append(h('span', { class: `badge ${cls}`, title: svc?.detail || '' }, `${titleCase(name)}: ${st}`));
      }
    }

    async function loadEvents() {
      try {
        const d = await get('/system/events?n=150');
        clear(events);
        if (!d.events.length) { events.append(h('p', { class: 'muted', text: 'No events yet.' })); return; }
        for (const e of d.events) {
          const data = Object.keys(e.data || {}).length ? JSON.stringify(e.data) : '';
          events.append(h('div', { class: 'log-line' }, h('span', { class: 't', text: `-${fmtDuration(e.age_seconds)}` }), h('span', { class: 'topic', text: e.topic }), h('span', { class: 'd', text: data })));
        }
      } catch (e) { clear(events).append(h('p', { class: 'muted', text: e.message })); }
    }

    async function loadConfig() {
      try { configPre.textContent = JSON.stringify(await get('/system/config'), null, 2); } catch (e) { configPre.textContent = e.message; }
    }

    function renderWeather(d) {
      const w = d.weather || {};
      const t = d.time || {};
      clear(wxWhere).append('Weather location: ', h('strong', { text: w.location || 'not known yet' }), ` (${w.source_detail || 'not set'})`);
      clear(wxBanner);
      if (w.blocked) wxBanner.append(banner('warn', 'cloud', "LittleBot can't get the weather right now", w.blocked === 'cloud_off' ? 'Cloud communication is off in Privacy.' : 'LittleBot is in offline mode.'));
      else if (!w.found && w.source === 'internet') wxBanner.append(banner('info', 'info', 'Home not found yet', "LittleBot couldn't tell where it is from your internet connection. Press Change and type your town."));
      const deg = (u) => (u === 'fahrenheit' ? '°F' : '°C');
      clear(wxKv);
      for (const [k, v] of [
        ['Units', w.units === 'auto' ? `Automatic (${deg(w.units_in_use)})` : deg(w.units)],
        ['Time here', t.day ? `${t.day} ${t.time_24h} (${t.timezone})${t.timezone_problem ? `: ${t.timezone_problem}` : ''}` : '–'],
        ['Web search', d.web_search ? 'On: ChatGPT can find news, scores, opening hours and other things that change' : 'Off (chatgpt.web_search)'],
      ]) wxKv.append(h('dt', { text: k }), h('dd', { text: v }));
      wxInput.value = w.source === 'dashboard' ? (w.query || w.location || '') : '';
      wxUnits.value = w.units || 'auto';
    }

    async function loadWeather() {
      try { renderWeather(await get('/info?lookup=true')); } catch (e) { setText(wxWhere, `Weather location: ${e.message}`); }
    }

    async function saveWeather(location) {
      wxSave.disabled = true;
      const r = await app.act(() => put('/info', { location, units: wxUnits.value }), (res) => (location ? `Weather location: ${res?.weather?.location || location}.` : 'The weather now uses your internet connection.'));
      wxSave.disabled = false;
      if (r) { renderWeather(r); wxForm.hidden = true; wxChange.hidden = false; wxResult.hidden = true; }
    }

    async function checkWeather() {
      wxResult.hidden = false;
      setText(wxResult, 'Asking the weather service…');
      try { setText(wxResult, (await get('/info/weather')).summary); } catch (e) { setText(wxResult, e.message); }
    }

    loadSessions();
    loadSystem();
    loadWeather();
    return {};
  },
};
