// Privacy: camera / microphone / cloud switches with plain-language explanations.

import { get, post, put } from '../api.js';
import { banner, button, confirmDialog, h, icon, pageHead, switchRow } from '../ui.js';

export default {
  mount(view, app) {
    const flag = (key, title, text, okText) => switchRow({
      title, text,
      onChange: async (v) => {
        const r = await app.act(() => put('/privacy', { [key]: v }), okText ? okText(v) : `${title} ${v ? 'on' : 'off'}.`);
        if (r) fill(r);
        return r !== null;
      },
    });
    const camera = flag('camera_enabled', 'Camera',
      'Lets LittleBot notice a face and look at your child. Pictures are analysed on the robot, in memory, and thrown away straight after - never saved, and never sent (except to your own dashboard while you watch the Camera page, and to the AI if you let it look, below). Turning it off also stops the Camera page.');
    const mic = flag('microphone_enabled', 'Microphone',
      'Lets LittleBot hear simple words during games and conversations. Sound is never recorded or stored. LittleBot does not listen while it is talking.');
    const cloud = flag('cloud_enabled', 'Cloud communication',
      'Allows online services that receive information about your child - cloud AI conversation and cloud speech - to be used. Off by default: while it is off, nothing about your child leaves the robot. (YouTube videos are controlled separately on the Videos page; they only stream the videos you approved.)');
    const aiCamera = flag('ai_camera_enabled', 'Let the AI look through the camera when asked',
      'When someone asks ChatGPT about what LittleBot sees - for example “how many fingers am I holding up?”, during live voice or with the button on the AI page - LittleBot takes one photo and sends it with the question to ChatGPT. Only when asked, one photo at a time; it is never stored on the robot, but OpenAI receives it. Also needs Camera and Cloud communication on and a ChatGPT sign-in. Off by default.',
      (v) => (v ? 'The AI may now look through the camera when asked.' : 'The AI can no longer look through the camera.'));
    const faces = flag('face_recognition_enabled', 'Recognise family members',
      'LittleBot learns the faces of the people you add under Who\'s who on the Profile page, so the voice chat knows who is in front of the camera and can use their names. It all happens on the robot: only the names and 128 numbers per face sample are kept there, never photos. During a voice chat the names of the people in view are told to the voice (OpenAI); pictures and face data never leave the robot. Also needs Camera on. Switching it off stops recognising; delete the face samples on the Profile page. Off by default.',
      (v) => (v ? 'LittleBot may now recognise the family members you added.' : 'LittleBot no longer recognises faces.'));
    const voices = flag('voice_recognition_enabled', 'Recognise family voices',
      'LittleBot learns the voices of the people you add under Who\'s who on the Profile page, so the voice chat knows which grown-up is talking, from "Hey LittleBot" on. It all happens on the robot: only a voiceprint (192 numbers per sample) is kept there, never a recording. You can record a voice on the Profile page, and LittleBot also learns when someone says who they are in a voice chat ("I\'m Ali"). During a voice chat the name of who is talking is told to the voice (OpenAI); sound recordings and voiceprints never leave the robot. Also needs Microphone on. Switching it off stops all of it; delete voiceprints on the Profile page. Off by default.',
      (v) => (v ? 'LittleBot may now recognise the voices of the family members you added.' : 'LittleBot no longer recognises voices.'));
    const history = flag('record_history', 'Remember activity history',
      'Keeps a simple list of which activities were played (names and times only), stored on the robot.');
    const offlineNote = h('div');
    // Info cards on LittleBot's screen (littlebot/screens): their own switches, kept as settings.
    const screenFlag = (key, title, text, okText) => switchRow({
      title, text,
      onChange: async (v) => {
        const r = await app.act(() => put('/screens/settings', { [key]: v }), okText(v));
        if (r) fillScreens(r);
        return r !== null;
      },
    });
    const cards = screenFlag('enabled', 'Info cards on the screen during voice chats',
      'While LittleBot talks with you it can show what you asked on its screen - the weather, a word\'s meaning, the steps of a recipe, a timer - with its own little eyes in the corner. A card goes 30 seconds after the chat ends (a running timer stays until it rings), or straight away when you tap the little eyes or say “close it”. On by default.',
      (v) => (v ? 'Info cards are on.' : 'Info cards are off.'));
    const pictures = screenFlag('web_pictures', 'Show pictures from the internet on LittleBot\'s screen',
      'Lets a card show a picture of what you asked about (an animal, a place, a dish), found on the internet with strict safe search. Pictures are only shown, never kept: nothing is saved on the robot, and nothing about your child is sent - only the word being looked up. Off: cards show no pictures. On by default.',
      (v) => (v ? 'Cards may show pictures from the internet.' : 'Cards no longer show pictures from the internet.'));
    const showing = h('p', { class: 'muted' });
    const closeBtn = button('Close the card', {
      kind: 'soft', iconName: 'x',
      onClick: async () => { const r = await app.act(() => post('/screens/close'), 'Card closed.'); if (r) fillScreens(r); },
    });
    // The browser view (littlebot/browser): Google on the screen when a grown-up explicitly asks for it.
    const browser = switchRow({
      title: 'Allow the browser view (Google with SafeSearch, grown-ups only)',
      text: 'When a grown-up says “google it”, “look it up online” or “search that in the browser”, LittleBot shows Google on its screen - with SafeSearch always on, no address bar, and its own little eyes and a big × in the corner. Ordinary questions still get info cards. Only the search words go to Google; nothing is kept (each time is a fresh browser that is deleted afterwards), unless you let it remember Google below. Needs Cloud communication on. It closes on the ×, “close it”, a video or game, or two minutes after the chat with nobody touching it. On by default.',
      onChange: async (v) => {
        const r = await app.act(() => put('/browser/settings', { enabled: v }), v ? 'The browser view is on.' : 'The browser view is off.');
        if (r) fillBrowser(r);
        return r !== null;
      },
    });
    const keepSignins = switchRow({
      title: 'Let the browser remember Google (fewer “I\'m not a robot” checks)',
      text: 'Google sometimes asks “are you a robot?” before showing its results; LittleBot then shows that check big on its screen for a grown-up to tick (or Bing\'s results if nobody does). On: the browser keeps its cookies, history and cache on the robot between searches, so after one check Google usually stops asking. Nothing is sent anywhere else. Off: a fresh browser every time, deleted afterwards, and switching it off deletes what was kept. Off by default.',
      onChange: async (v) => {
        const r = await app.act(() => put('/browser/settings', { keep_signins: v }), v ? 'The browser will remember Google.' : 'The browser no longer keeps anything; what it kept is deleted.');
        if (r) fillBrowser(r);
        return r !== null;
      },
    });
    const browserForget = button('Forget what the browser kept', {
      kind: 'danger-outline', iconName: 'trash',
      onClick: async () => { const r = await app.act(() => post('/browser/forget'), (d) => (d?.forgot ? 'The browser\'s cookies, history and cache are deleted.' : 'The browser had nothing kept.')); if (r) fillBrowser(r); },
    });
    const browserShowing = h('p', { class: 'muted' });
    const browserClose = button('Close the browser', {
      kind: 'soft', iconName: 'x',
      onClick: async () => { const r = await app.act(() => post('/browser/close'), 'Browser closed.'); if (r) fillBrowser(r); },
    });

    view.append(
      pageHead('Privacy', 'You decide what LittleBot can see, hear and share.'),
      h('div', { class: 'stack' },
        h('section', { class: 'card info' },
          h('div', { class: 'hero-note' }, icon('shield'),
            h('div', {},
              h('h2', { text: 'LittleBot never stores audio or video' }),
              h('p', { class: 'mt-1', text: 'Camera frames and microphone sound are processed on the robot and discarded immediately. Nothing is uploaded unless you turn on cloud communication, and camera images are never sent anywhere - except to your own dashboard while you watch the Camera page (LittleBot shows a camera sign meanwhile), and, if you also let the AI look through the camera, one photo each time someone asks.' })))),
        offlineNote,
        h('div', { class: 'grid two' },
          h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('eye'), 'Sensors & sharing')), camera.el, mic.el, cloud.el, aiCamera.el, faces.el, voices.el, history.el),
          h('div', { class: 'stack' },
            h('section', { class: 'card' },
              h('div', { class: 'card-head' }, h('h2', {}, icon('face'), 'LittleBot\'s screen')),
              cards.el, pictures.el, showing, h('div', { class: 'mt-2' }, closeBtn),
              browser.el, browserShowing, h('div', { class: 'mt-2' }, browserClose),
              keepSignins.el, h('div', { class: 'mt-2' }, browserForget)),
            h('section', { class: 'card' },
              h('div', { class: 'card-head' }, h('h2', {}, icon('history'), 'Clear history')),
              h('p', { class: 'muted', text: 'Removes any stored activity history and conversation memory from the robot.' }),
              h('div', { class: 'mt-2' }, button('Clear history', {
                kind: 'danger-outline', iconName: 'trash',
                onClick: async () => {
                  if (await confirmDialog({ title: 'Clear all history?', body: 'This cannot be undone.', confirm: 'Clear history', danger: true })) {
                    await app.act(() => post('/privacy/clear-history'), (r) => r?.detail || 'History cleared.');
                  }
                },
              }))),
            h('section', { class: 'card' },
              h('div', { class: 'card-head' }, h('h2', {}, icon('stop'), 'Stop interacting')),
              h('p', { class: 'muted', text: 'Immediately ends any activity, video, speech, listening and movement.' }),
              h('div', { class: 'mt-2' }, button('Stop all interactions', { kind: 'danger', iconName: 'square', onClick: () => app.act(() => post('/privacy/stop-interactions'), 'Everything stopped.') })))))));

    function fill(d) {
      camera.set(d.camera_enabled);
      mic.set(d.microphone_enabled);
      cloud.set(d.cloud_enabled);
      history.set(d.record_history);
      aiCamera.set(!!d.ai_camera_enabled);
      faces.set(!!d.face_recognition_enabled);
      voices.set(!!d.voice_recognition_enabled);
      cloud.setDisabled(!!d.offline_mode);
      aiCamera.setDisabled(!!d.offline_mode && !d.ai_camera_enabled);
      offlineNote.replaceChildren(...(d.offline_mode ? [banner('info', 'info', 'Offline mode', 'LittleBot is running in offline mode: cloud communication is switched off and cannot be turned on.')] : []));
    }

    function fillScreens(d) {
      cards.set(d.enabled !== false);
      pictures.set(d.web_pictures !== false);
      showing.textContent = d.active ? `On the screen now: ${d.showing || d.card?.title || 'a card'}.` : 'No card on the screen right now.';
      closeBtn.disabled = !d.active;
    }

    function fillBrowser(d) {
      browser.set(d.enabled !== false);
      if (d.active) browserShowing.textContent = `The browser shows: ${d.showing || 'a web page'}.`;
      else if (d.available === false) browserShowing.textContent = `The browser view isn't available here${d.detail ? ` (${d.detail})` : ''}.`;
      else browserShowing.textContent = 'The browser is closed.';
      browserClose.disabled = !d.active;
      keepSignins.set(!!d.keep_signins);
      browserForget.disabled = !d.saved;
    }

    get('/screens').then(fillScreens).catch(() => { showing.textContent = 'Info card status is unavailable.'; });
    get('/browser').then(fillBrowser).catch(() => { browserShowing.textContent = 'Browser status is unavailable.'; });
    get('/privacy').then(fill).catch((e) => offlineNote.replaceChildren(banner('danger', 'alert', 'Could not load privacy settings', e.message)));
    return {
      update(s) {
        if (s.privacy) fill(s.privacy);
        if (s.browser && s.browser.status !== 'unavailable') fillBrowser(s.browser);
      },
    };
  },
};
