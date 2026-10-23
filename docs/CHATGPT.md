# Sign in with ChatGPT and live voice

LittleBot can use **your own ChatGPT plan** instead of a paid API key, in two ways:

* **ChatGPT answers** for the typed / push-to-talk conversation: the same short, checked
  replies as the other cloud providers, with the same rules about who gets them.
* **Live voice (GPT-Live):** a real-time spoken conversation with ChatGPT's voice through
  LittleBot's microphone and speaker. **A grown-up starts it from the dashboard and stays
  for the whole call.**
* **Letting the AI see (camera), optional and off by default:** ask "how many fingers am I
  holding up?" and LittleBot takes **one photo** and sends it with the question to ChatGPT
  ([section 4](#4-letting-the-ai-see-camera)).

> **Unofficial and untested against OpenAI.** This uses OpenAI's public Codex sign-in (the one
> the Codex CLI uses) and **undocumented** Codex endpoints on chatgpt.com, the same approach as the
> Codex CLI and WatchGPT. It is not a public OpenAI API: it **may change or stop working without
> notice**. Usage counts against your **ChatGPT plan's limits**. There is no API key and no
> per-token bill. So far it has been tested only against mocked OpenAI responses and a local
> stand-in, **not against OpenAI's real service and not on the robot's real microphone and
> speaker** (see [Testing status](#testing-status)).

| | ChatGPT answers (typed / push-to-talk) | Live voice |
|---|---|---|
| Who starts it | LittleBot, when your child talks (or you press **Push to talk**) | Only you, from the dashboard, after ticking "I am with my child…" |
| Child's age | 24 months or older (younger: scripted replies) | Any age: you are there |
| Goes to OpenAI | Short text with personal info removed | The microphone **audio** of the call. Audio can't be cleaned of personal info |
| Checked by LittleBot | Every reply, before it is spoken | Every caption of what the voice says, **while it is speaking** |
| Limits | LittleBot's request limits + your plan's limits | 10-minute calls, hangs up after 90 s of quiet, your plan's limits |
| Needs | Sign-in, cloud AI on, Privacy cloud on | Sign-in, Privacy cloud + microphone on, the `live` extra |

---

## 1. Sign in

1. In the dashboard open **Privacy** and turn on **Cloud communication**. The sign-in button
   refuses to start without it, and nothing works in offline mode.
2. Open **AI** → **ChatGPT account** → **Sign in with ChatGPT**. LittleBot shows a short code.
3. On your phone or computer open **<https://auth.openai.com/codex/device>**, log in to ChatGPT
   and type the code. The code expires after 15 minutes. **Cancel** stops waiting.
4. Within a few seconds the card shows **Signed in as a\*\*\*@example.com (Plus plan)**.

If OpenAI says code sign-in is off, open ChatGPT → **Settings** → **Security**, turn on
**device code login**, and press **Sign in with ChatGPT** again.

Which ChatGPT plans and countries can use this is up to OpenAI. If yours is not included you
will see "not available for this account" (see [Troubleshooting](#6-troubleshooting)).

**Where the sign-in is kept.** The tokens stay on the robot in `data/chatgpt_auth.json`, which
only LittleBot's user account can read (mode 0600). They are never shown in the dashboard, never
sent to your browser and never logged. The dashboard only gets the masked e-mail and the plan
name. The tokens go only to `auth.openai.com` and `chatgpt.com`. LittleBot renews the sign-in by
itself, shortly before it runs out. Anyone who can log in to the Pi as that user could copy the
file, so keep the Pi's own password safe.

**Sign out.** **AI** → **ChatGPT account** → **Sign out** ends any live call, asks OpenAI to
cancel the sign-in (best effort: if the robot is offline the request can't be sent) and deletes
the file. If you lose the robot, or signed out while it was offline, log out of all devices
from ChatGPT's security settings.

## 2. ChatGPT for typed and push-to-talk conversation

1. Sign in (section 1).
2. **AI** → **Cloud AI** → **Provider**: choose **ChatGPT (sign in with your plan)**, then
   **Save AI settings**.
3. Switch on **Use cloud AI for conversation**.
4. Optional: in **Connection test** pick ChatGPT (listed as "Chatgpt") and press **Test**.

The usual gates still apply on every turn ([AI_SAFETY.md §1](AI_SAFETY.md#1-who-gets-which-kind-of-conversation)):
cloud AI on, Privacy cloud on, **your child's age set and at least 24 months** for open-ended
cloud conversation, and the hourly and daily request limits. Until your child turns 2,
LittleBot keeps answering with its scripted replies, whatever the provider. Each reply is
length-limited and safety-checked before it is spoken. Personal info is removed from what your
child said before it is sent. The nickname and interests are shared only if you switch that on.

ChatGPT requests count as **$0** in "Usage today", so the daily budget never stops them. The
request limits do, and so do your plan's own limits.

## 3. Live voice (GPT-Live)

A real-time spoken conversation with ChatGPT's voice. While it runs, LittleBot's face shows
listening, talking and thinking, and the mouth moves with the voice. The voice is told it is a
small toy robot talking with *a baby about N months old and a grown-up who is right there*. For
a baby it babbles back, copies sounds and makes animal noises, counts, says colours, plays
peekaboo or says a few lines of a nursery rhyme, and it helps the grown-up play.

### Start and stop

1. Sign in (section 1). In **Privacy**, turn on **Cloud communication** and the **Microphone**.
2. **AI** → **Live voice (GPT-Live)**: tick **I am with my child and will stay for the whole
   conversation**.
3. Press **Start live voice**. The card shows "Connecting…", then **Listening**. Just talk.
   It also shows **Talking**, **Looking something up…**, the time left, the voice in use, the
   photos taken this call and live captions ("Heard:" / "LittleBot:", plus grey notes such as
   "LittleBot took a photo to answer").

**Asking what LittleBot sees.** If you turned on **Let the AI look through the camera when
asked** in Privacy, you can ask during the call "how many fingers am I holding up?" or "what
colour is my cup?": LittleBot takes **one photo**, ChatGPT answers, and the voice says the answer.
With the switch off, no photo is taken and the voice says it can't see right now. See
[section 4](#4-letting-the-ai-see-camera).

The provider choice, the "Use cloud AI" switch and your child's age don't matter here. You
start every call yourself, and you are there for all of it.

**It stops when:**

* you press **Stop live voice**, or the red **STOP** on any page;
* you turn off cloud communication or the microphone in **Privacy**, or sign out;
* the **time limit** is reached (`chatgpt.live_session_minutes`, default **10 minutes**). The
  voice is asked to say a short goodbye, then LittleBot hangs up (at most 10 seconds later);
* nobody has talked for **90 seconds** (`chatgpt.live_idle_hangup_seconds`);
* OpenAI ends the call, the connection drops, or something more important (a system task)
  needs the speaker;
* a safety rule is broken (below).

Afterwards the card shows how the last call ended (for example "Last call: time limit reached
after 10 min"), a safety stop, or an error.

### Talking to it: one at a time

The Pi has no echo cancellation. If the microphone stayed open, ChatGPT would hear its own
voice and answer itself. So the conversation is **half-duplex**: while LittleBot's voice is
playing, and for 0.45 s after it stops, the microphone sends silence. **You can't talk over it
or interrupt it.** Wait until it stops, then speak. Stand close to LittleBot. The volume is the
speaker volume from **Audio & devices**, capped for little ears.

### What LittleBot checks

* **What the voice says.** Every caption of what the voice is saying is checked **while it
  speaks**, with the same rules as typed replies: no secrets, no "best friend" or guilt, no
  pretending to be a person, no personal questions, nothing unsafe, scary or rude, no medical
  advice, no ads, no internet. If a rule is broken, LittleBot **silences the speaker at once,
  ends the call** and shows a **"Live voice stopped"** alert with the rule's category only.
  **Some words may already have been heard by then**, because the captions trail the voice a
  little.
* **What it hears.** Worrying words such as "I'm hurt", "help" or "I ate a battery" show the usual
  **"Your child may need you"** alert (category only, never the words). Unlike typed
  conversation, the call carries on, and the voice is told to say "Let's get a grown-up right
  now".
* **Look-ups.** When the voice needs a fact, it hands the question to LittleBot. LittleBot asks
  the ChatGPT text model with its own child-safe prompt (at most 40 words) and checks the answer
  with the same rules before the voice gets it. If the answer is unsafe or the look-up fails, the
  voice is told to say it isn't sure and to suggest asking a grown-up.
* **The voice's instructions** also ask for short, gentle sentences: it says it is a robot, never
  a person or family member, keeps no secrets, asks no personal questions, gives no medical
  advice, mentions no shopping, apps or websites, and suggests nothing risky. Instructions are
  only guidance. The checks above are what LittleBot enforces.

**Limits of the checks.** The rules read OpenAI's captions. If the captions don't match the
audio, or the problem is in the tone rather than the words, the rules can't see it. Word rules
don't understand meaning, either ([AI_SAFETY.md §3](AI_SAFETY.md#3-live-voice-gpt-live)). That is
why live voice only runs while you are there.

### Privacy

* During a call, **the robot's microphone audio goes to OpenAI** (silence while LittleBot is
  talking), and the voice comes back. Unlike typed text, audio **can't be cleaned of personal
  info**. Anything said near the robot reaches OpenAI, including names, places and other people
  talking in the room.
* Each call also sends the robot's name, your child's approximate age ("a baby about 10 months
  old"), and the day and time. It does not send the nickname or interests.
* Look-ups send the question, with personal info removed, plus the last few caption lines to the
  ChatGPT text model.
* **Photos** are sent only if you turned on **Let the AI look through the camera when asked**,
  and only one photo each time someone asks what LittleBot sees (section 4).
* **Nothing is recorded on LittleBot:** no audio and no transcripts. The captions on the dashboard
  (the last 8 lines) exist only in memory during the call and are cleared when it ends. Logs hold
  only timings, end reasons and categories.
* What OpenAI keeps is governed by OpenAI's terms and your ChatGPT account settings, not by
  LittleBot.

### Usage

Live voice uses your plan's voice allowance, and LittleBot adds no cost of its own. Calls and
their look-ups are **not** counted in the AI page's "Usage today" or its request limits. The time
limit, the idle hang-up and your plan's limits are what keep it bounded.

### Installing live voice

Live voice needs the `live` extra (the aiortc WebRTC library and its prebuilt wheels, about 40 MB
to download, nothing to compile). `scripts/install_pi.sh` installs it by default (`--no-live`
skips it). Without it, the card says **Not installed on this robot yet**, and everything else,
including ChatGPT text answers, still works.

## 4. Letting the AI see (camera)

ChatGPT can look through LittleBot's camera to answer a question about what is in front of it:
"how many fingers am I holding up?", "what colour is this ball?", "what animal is on my
shirt?". It is **off by default**.

### Turn it on

1. Sign in with ChatGPT (section 1).
2. In **Privacy**, turn on **Camera**, **Cloud communication** and **Let the AI look through the
   camera when asked**. That last switch is off by default and can't be turned on in offline
   mode.

### Ask

* **From the dashboard:** **AI** → **Ask what LittleBot sees**. Type a question (or leave it
  empty for "What do you see?"), press **Look and answer**, and the answer appears below the
  button. Tick **Say the answer out loud** to have LittleBot say it too. The card also shows
  **Photos sent today** and, while it can't look, why (for example "turn it on in Privacy").
* **During live voice:** just ask out loud. When the voice hands LittleBot a question about what
  it sees, LittleBot takes one photo for it, and the captions show the grey note "LittleBot took a
  photo to answer (sent to ChatGPT, not kept)". The live card counts **Photos this call**.

### What happens

* LittleBot grabs **one still frame** from the camera at the moment of the question (a small
  JPEG, at most 768 pixels wide) and sends it with the question to the ChatGPT text model on your
  plan. **Nothing is filmed or streamed**, and no photo is ever taken on a timer or in the
  background. LittleBot's face does a little "peek" when it takes the photo, so everyone can see
  it happened.
* The answer comes back as text (at most 40 words) and is checked with LittleBot's safety rules
  before it is shown or spoken. If it fails the check, LittleBot says "I'm not sure what I see.
  Let's ask a grown-up." instead. Only one photo is taken at a time.
* The photo exists **only in memory** while the question is being answered. It is **never saved**
  on LittleBot, never put in logs, and never sent to the dashboard: your browser only ever gets
  the answer text and a count of photos sent today.

### Privacy

* **OpenAI receives the photo.** Whatever is in front of the camera goes with it: your child,
  you, other people and the room. What OpenAI keeps is governed by OpenAI's terms and your
  ChatGPT account settings, not by LittleBot.
* A photo is taken **only when someone asks**, **only if you turned the switch on**, and only
  while the **Camera** and **Cloud communication** switches are on (and not in offline mode).
  Turning any of them off stops further photos at once.
* The everyday typed / push-to-talk conversation doesn't take photos. Only the **Look and
  answer** button and live voice do.

### Limits

* Like everything here it uses undocumented endpoints and your plan's limits, and it is **not
  yet tested against OpenAI's real service or with the Pi's real camera** (only with mocked
  responses and a simulated camera). How well it counts fingers or reads small things depends on
  the light, the distance and the model.
* In live voice it depends on the voice handing the question to LittleBot. If it answers on its
  own instead, it has not seen anything (it is told never to pretend), so ask again: "look and
  tell me how many fingers I'm holding up".

## 5. Settings you may want to change

In `config/local.yaml` (or with `LITTLEBOT__CHATGPT__<KEY>=value` environment variables).
Restart LittleBot afterwards.

```yaml
chatgpt:
  live_voice: bubbie              # asked for first; any name is passed to OpenAI as is
  live_voice_fallbacks: [sol]     # tried in order if OpenAI refuses the voice (HTTP 400/422)
  live_session_minutes: 10        # hard limit per call
  live_idle_hangup_seconds: 90    # hang up after this long with nobody talking (0 = never)
  live_echo_tail_ms: 450          # microphone stays shut this long after LittleBot's voice stops
  live_volume: null               # null = the speaker volume setting; 0..1 (always capped at max_volume)
```

All keys and defaults are listed in the [technical section](#configuration).

<a id="5-troubleshooting"></a>

## 6. Troubleshooting

| What you see | What it means | What to do |
|---|---|---|
| "Turn on cloud communication in Privacy first." | Cloud is off | **Privacy** → Cloud communication on |
| "Code sign-in is off for your ChatGPT account…" | Device code login is disabled in ChatGPT | ChatGPT → **Settings** → **Security** → turn on **device code login**, then sign in again |
| "The sign-in code expired before it was used." | The 15 minutes ran out | Press **Sign in with ChatGPT** again for a new code |
| "The ChatGPT sign-in expired. Sign in again…", **Not signed in with ChatGPT**, the AI page's reason says "ChatGPT sign-in rejected (sign in again)", or Test shows `expired` | OpenAI no longer accepts the sign-in (for example after "log out of all devices" or a password change) | Sign in again (section 1) |
| Live: "Live voice is not available for this ChatGPT account or region…"; text: Test shows `forbidden` (HTTP 403) | OpenAI doesn't offer this endpoint to your account, plan or country. Having a subscription doesn't guarantee access | Nothing LittleBot can fix. Use scripted replies or an API key provider |
| Live: "ChatGPT voice usage limit reached…"; text: Test shows `usage_limit` (HTTP 429) | Your plan's allowance is used up | Wait until it resets. LittleBot uses scripted replies meanwhile. `plan_not_included` means your plan doesn't include it at all |
| Live: "The undocumented ChatGPT voice endpoint is unavailable or changed."; text: Test shows `endpoint_changed` (HTTP 404) | OpenAI changed or removed the undocumented endpoint | Update LittleBot (`git pull`, then `scripts/install_pi.sh --yes`). If no fix exists yet, it has stopped working |
| **Ask what LittleBot sees** says "Can't look right now" | A switch is off, you're not signed in, or there is no camera. The card says which | Turn on **Camera**, **Cloud communication** and **Let the AI look through the camera when asked** in **Privacy**, and sign in (section 4) |
| Test shows `model_unavailable` | None of the models in `chatgpt.models` is offered to your account | Put a model your plan offers in `chatgpt.models` |
| **Not installed on this robot yet** (Live voice card) | The `live` extra (aiortc) is missing | On the robot: `cd ~/LittleBot && .venv/bin/python -m pip install --prefer-binary -e '.[live]'` (or re-run `scripts/install_pi.sh --yes`), then restart LittleBot: `systemctl --user restart littlebot` |
| "Could not reach OpenAI…" / "Could not connect to ChatGPT live voice…" / "The connection to ChatGPT was lost." | Internet problem (or OpenAI is down) | Check the robot's Wi-Fi. A network that blocks outgoing UDP can stop the call's audio |
| "ChatGPT ended the call." | OpenAI closed the call | Start a new one |
| "LittleBot is busy with something else. Stop it first." | A video or another parent action holds the speaker | Stop it, then start live voice |
| "The speaker or microphone could not be opened." | The USB devices are missing or in use by another program | **Audio & devices**: check both devices. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#no-sound--sound-from-the-wrong-device) |
| It hears itself, echoes or answers itself | Its own voice reaches the microphone after the gate opens | Lower the volume, move the microphone away from the speaker, raise `chatgpt.live_echo_tail_ms` (for example to 700). Keep `live_half_duplex: true` |
| It doesn't react when you talk | It was still talking (the microphone is shut then), or your voice is too quiet | Wait until it stops, speak closer. Raise `audio.microphone.input_gain` (for example 1.5) |
| Too quiet or too loud | The speaker volume | **Audio & devices** → volume (capped at `audio.speaker.max_volume`). Or set `chatgpt.live_volume` for live voice only |
| "hung up after 90 s of quiet" | Nobody talked | Normal. Change `chatgpt.live_idle_hangup_seconds` if it's too short |

The app log (`data/logs/littlebot.log`) has the HTTP status and end reason of each call. It never
contains what was said.

---

## 7. Technical details

### How it works

**Sign-in** (`littlebot/chatgpt/auth.py`): the OAuth device-code flow against
`https://auth.openai.com` with OpenAI's public Codex client id:

1. `POST /api/accounts/deviceauth/usercode {client_id}` → `device_auth_id`, `user_code` (a 404
   here means device code login is off);
2. the parent enters the code at `/codex/device`;
3. LittleBot polls `POST /api/accounts/deviceauth/token` (server interval, 15-minute lifetime)
   → `authorization_code` + `code_verifier`;
4. `POST /oauth/token` (`authorization_code`, PKCE verifier, redirect `/deviceauth/callback`) →
   access, refresh and id tokens. The account id, plan and e-mail come from the JWT claims.

Tokens are refreshed 5 minutes before they expire, or once after a 401 (one refresh at a time).
`invalid_grant`, `refresh_token_expired`/`_reused`/`_invalidated` or a 401 on refresh signs out.
Sign out calls `POST /oauth/revoke`. Values were copied from WatchGPT and checked against
openai/codex on 24.09.2026.

**Text** (`responses.py`, provider `chatgpt`): `POST https://chatgpt.com/backend-api/codex/responses`,
server-sent events. Headers: `Authorization: Bearer …`, `ChatGPT-Account-ID`, `originator`,
`session-id`. Body: `model`, `instructions` (LittleBot's system prompt), `input`, `store: false`,
`stream: true`, `reasoning.effort`, `text.verbosity: low`, `prompt_cache_key`. The endpoint rejects
`temperature`, `max_output_tokens` and `previous_response_id`, so the length cap is applied
afterwards by `validate_reply` (as for every provider), and the short history is resent each
turn. If a model is unavailable (HTTP 400), the next one in `chatgpt.models` is tried.

| HTTP / stream error | Provider error `kind` |
|---|---|
| 401 (after one refresh) | `expired` |
| 403 | `forbidden` |
| 429 `usage_limit_reached` / `usage_not_included` / other | `usage_limit` / `plan_not_included` / rate limited |
| 503 `server_is_overloaded` | `overloaded` |
| 404 / 405 / 410 | `endpoint_changed` |
| `response.failed` with a policy code | `refused` |

**Live voice** (`live.py`, `live_audio.py`, `wire.py`):
`POST https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas`
with `OpenAI-Alpha: quicksilver=v2` and
`{"sdp": <offer>, "session": {model, instructions, audio.output.voice, delegation: {type: client}}}`
returns the SDP answer. Audio (Opus, 48 kHz mono) then flows directly between the robot and
OpenAI over DTLS-SRTP. No STUN/TURN servers are configured, and the offer is sent once the
robot's candidates are gathered. The `oai-events` data channel carries `input_transcript.added`,
`output_transcript.added`, `turn.done`, `delegation.created`, `error` and `session.closed`.
LittleBot answers a delegation with `delegation.context.append` frames (≤ 1800 characters, split
into ≤ 500-byte frames) and sends the goodbye request with `session.context.append`.

During a call the live service holds the face, speaker and microphone at parent priority. The
audio service lends it the devices (`AudioService.suspend`). `LiveAudio` opens its own PortAudio
streams at 48 kHz in 20 ms frames, resampling with PyAV when a device needs another rate. It
queues at most 1 s of voice and applies the half-duplex gate: a speaker block above
`live_voice_level` RMS counts as speaking, and the microphone stays shut for `live_echo_tail_ms`
after. While a reply is in progress, its latest ~320 characters are re-checked as caption text
arrives (at most every 0.1 s, or at once at the end of a sentence). The whole turn is checked
again when it is done.

Dashboard API (parent session + CSRF, like every route): `GET /api/chatgpt`,
`POST /api/chatgpt/sign-in`, `/sign-in/cancel`, `/sign-out`,
`/live/start {"parent_present": true}`, `/live/stop`. Responses contain only the masked e-mail, the
plan, the one-time code and the live status, never tokens.

### Configuration

`chatgpt:` section (`littlebot/core/config.py`, `ChatGPTConfig`). Select the text provider with
`ai.provider: chatgpt` (or on the AI page).

| Key | Default | Meaning |
|---|---|---|
| `originator` | `littlebot` | how requests identify themselves to OpenAI |
| `token_file` | `chatgpt_auth.json` | under `data_dir` (`data/`), mode 0600 |
| `models` | `[gpt-6-luna, gpt-6-sol, gpt-5.6-luna]` | text model for replies and look-ups: the first one that works |
| `reasoning_effort` | `low` | `minimal` / `low` / `medium` / `high` |
| `live_model` | `gpt-live-1-codex` | the voice model |
| `live_voice` | `bubbie` | the voice asked for first. Any name is passed to OpenAI as is (no list is enforced). Known voices: `cove`, `arbor`, `breeze`, `ember`, `juniper`, `maple`, `sol`, `spruce`, `vale` |
| `live_voice_fallbacks` | `[sol]` | if OpenAI refuses a voice (HTTP 400/422), the next one in this list is tried. The card shows the voice in use |
| `live_session_minutes` | `10` | hard limit per call, counted from connection. Then a goodbye and hang-up |
| `live_idle_hangup_seconds` | `90` | hang up after this long with nobody talking. `0` = never. Until the first words are heard, at least 30 s |
| `live_connect_timeout_seconds` | `20` | waiting for OpenAI's SDP answer |
| `live_half_duplex` | `true` | shut the microphone while LittleBot's voice plays. Turn off only with a headset or hardware echo cancellation |
| `live_echo_tail_ms` | `450` | how long the microphone stays shut after the voice stops |
| `live_voice_level` | `0.012` | speaker RMS (0..1) above which the voice counts as speaking |
| `live_volume` | `null` | `null` = the speaker volume setting. Always capped at `audio.speaker.max_volume` |

### Files

| Path | Contents |
|---|---|
| `littlebot/chatgpt/auth.py` | device-code sign-in, token file, refresh, sign-out |
| `littlebot/chatgpt/responses.py` | the `chatgpt` conversation provider |
| `littlebot/chatgpt/wire.py` | GPT-Live data-channel events |
| `littlebot/chatgpt/live.py` | `LiveVoiceService` (`ctx.live`): gates, safety checks, limits, delegations, clean-up |
| `littlebot/chatgpt/live_audio.py` | microphone / speaker bridge with the half-duplex gate |
| `littlebot/chatgpt/look.py` | `CameraLook`: one photo + question to ChatGPT, with the privacy gates (section 4) |
| `littlebot/dashboard/routes/chatgpt_routes.py`, `static/js/pages/ai.js` | the "ChatGPT account", "Live voice (GPT-Live)" and "Ask what LittleBot sees" cards (`POST /api/chatgpt/look` returns the answer text only, never the photo) |
| `data/chatgpt_auth.json` | the sign-in (secret; not in the backups listed in PI_SETUP.md: just sign in again) |

### Testing status

* **Automated, no network** (`tests/chatgpt/`, `tests/dashboard/test_dashboard_chatgpt.py`):
  sign-in, refresh, sign-out and the token file against mocked OpenAI responses; the text provider
  and its error mapping against mocked event streams; the data-channel wire format; the dashboard
  routes, including that tokens never reach the browser.
* **Live call end to end against a local WebRTC peer standing in for OpenAI**: a real aiortc
  call on one machine with tone audio, delegations over the data channel, the safety cut, the
  worrying-words alert, STOP ALL, Privacy off, time limit with goodbye, idle hang-up, server close,
  friendly 403/429/404 messages and the start gates. These tests are skipped when aiortc is not
  installed. A stand-in replaces `LiveAudio` in them; the real microphone/speaker bridge is
  tested separately (`test_live_audio.py`) against a fake PortAudio: device and rate choice,
  resampling to 48 kHz, the half-duplex gate and echo tail, mute on a safety cut, and the
  playback backlog cap.
* **Letting the AI see:** the dashboard route and card are tested with a stand-in for the camera
  look (answer text only reaches the browser, blocked → a plain reason, "say it out loud", the
  Privacy switch off by default) and in a phone-sized browser.
* **Not yet tested:** OpenAI's real service (sign-in, text, live voice or photos), the Pi's real
  microphone, speaker and camera, echo in a real room, latency, and real plan limits.
