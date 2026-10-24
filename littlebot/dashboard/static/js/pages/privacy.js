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
    const history = flag('record_history', 'Remember activity history',
      'Keeps a simple list of which activities were played (names and times only), stored on the robot.');
    const offlineNote = h('div');

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
          h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, icon('eye'), 'Sensors & sharing')), camera.el, mic.el, cloud.el, aiCamera.el, faces.el, history.el),
          h('div', { class: 'stack' },
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
      cloud.setDisabled(!!d.offline_mode);
      aiCamera.setDisabled(!!d.offline_mode && !d.ai_camera_enabled);
      offlineNote.replaceChildren(...(d.offline_mode ? [banner('info', 'info', 'Offline mode', 'LittleBot is running in offline mode: cloud communication is switched off and cannot be turned on.')] : []));
    }

    get('/privacy').then(fill).catch((e) => offlineNote.replaceChildren(banner('danger', 'alert', 'Could not load privacy settings', e.message)));
    return {
      update(s) {
        if (s.privacy) fill(s.privacy);
      },
    };
  },
};
