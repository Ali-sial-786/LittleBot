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
* **Asking LittleBot to do things:** "turn the volume up", "play baby shark", "start the peekaboo
  activity", "play a game", "go to sleep", "how hot are you?", "what can you do?". ChatGPT controls
  LittleBot and its app, and can find and play **YouTube videos** when asked
  ([section 5](#5-asking-littlebot-to-do-things)).

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
| Child's age | Any age once you switch it on (optional minimum: `ai.min_age_months_for_cloud`) | Any age: you are there |
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
will see "not available for this account" (see [Troubleshooting](#7-troubleshooting)).

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
cloud AI on, Privacy cloud on, and the hourly and daily request limits. There is no age gate
by default (set `ai.min_age_months_for_cloud`, e.g. to 24, to keep a young child on scripted
replies). LittleBot answers like a home assistant for you (the weather, the news, facts,
recipes, parenting and health questions, in two to four sentences) and keeps it short and
playful with your child. Each reply is length-limited (`ai.max_words`, default 60) and checked
before it is spoken, but the check only stops what could harm a small child
([AI_SAFETY.md §2](AI_SAFETY.md#2-enforced-in-code-vs-asked-in-the-prompt)). Personal info is
removed from what was said before it is sent. The nickname and interests are shared only if
you switch that on.

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
start every call yourself (here, or by saying "Hey LittleBot": [wake word](#wake-word-hey-littlebot)),
and you are there for all of it.

**It stops when:**

* you press **Stop live voice**, or the red **STOP** on any page;
* you turn off cloud communication or the microphone in **Privacy**, or sign out;
* the **time limit** is reached (`chatgpt.live_session_minutes`, default **10 minutes**). The
  voice is asked to say a short goodbye, then LittleBot hangs up (at most 10 seconds later);
* nobody has talked for **90 seconds** (`chatgpt.live_idle_hangup_seconds`);
* you say only **"bye"**, "hang up", "end the call", "that's all", "stop listening" or "never mind"
  (at once). **"Stop"**, "be quiet", "shh" or "that's enough" make the voice go quiet straight away and
  wait for you: say anything and it answers again; nothing for **30 seconds** and the call ends. As
  the first words of a call (a "Hey LittleBot" heard by mistake) "stop" ends it at once too;
* OpenAI ends the call, the connection drops, or something more important (a system task)
  needs the speaker;
* a safety rule is broken (below).

Afterwards the card shows how the last call ended (for example "Last call: time limit reached
after 10 min"), a safety stop, or an error.

<a id="talking-to-it-one-at-a-time"></a>

### Talking over it (barge-in)

**With echo cancelling** (set up once with `scripts/setup_echo_cancel.sh`, see
[PI_SETUP.md](PI_SETUP.md#echo-cancelling)), PipeWire takes LittleBot's own voice out of what the
microphone hears, so a grown-up can **interrupt it**: just start talking while it speaks. After
about 0.3 s of your speech its voice fades out, it listens, and ChatGPT hears you from your first
word. The face goes from speaking to listening at once.

* **Only grown-ups' voices interrupt it.** It listens for voiced speech at a grown-up's pitch
  (about 80-260 Hz) that is clearly louder than what is left of its own voice. The baby's babbling
  (300-600 Hz), a cough, a short "mm" or a TV quieter than you don't. When voice recognition knows
  who is talking (`ctx.voices`, if set up), a known grown-up interrupts a little sooner (0.25 s) and
  a child never. Once it knows which grown-up it is talking with (their last turn's voice), someone
  else it recognises talking over it doesn't stop it either: they are talking to each other, and the
  voice carries on with its answer ("false alarm (voice recognition says it was Ira, not who it is
  talking with)" in the journal).
* **Didn't mean to?** Say only "oops, go ahead", "sorry, carry on", "never mind, keep going" or
  similar, and it carries on from where it stopped, without starting over.
* **Just a noise?** If no words of yours reach ChatGPT within 1.2 s (for example someone laughed),
  it was a false alarm: its voice plays on from where it faded out (it waits up to 1.5 s).
* Stand within about a metre and speak normally; a loud TV or someone else talking nearby can
  still interrupt it.

**Without echo cancelling** (until you run the script, or with `chatgpt.live_barge_in: off`) it
is one at a time, as before: if the microphone stayed open, ChatGPT would hear its own voice and
answer itself. So the conversation is **half-duplex**: while LittleBot's voice is playing, and for
0.45 s after it stops, the microphone sends silence. **You can't talk over it or interrupt it.**
Wait until it stops, then speak. Stand close to LittleBot.

The journal says which it is when a call starts: `journalctl --user-unit littlebot | grep "live audio:"`
ends in "echo cancelling on (PipeWire): a grown-up can talk over the voice (barge-in on)" or
"one at a time (half-duplex; ...)". The volume is the speaker volume from **Audio & devices**,
capped for little ears.

**Not tested on a real Pi yet.** The echo cancelling script, PipeWire's echo canceller with the
robot's USB microphone and speaker, and barge-in with real voices in a real room have only been
tested with stand-ins and synthetic sounds (see [Testing status](#testing-status)).

### What LittleBot checks

* **What the voice says.** Every caption of what the voice is saying is checked **while it
  speaks**, with the same rules as typed replies, which only stop what could harm a small
  child: secrets from you, turning the child away from you, inviting the child to do something
  dangerous or to meet someone, sexual content, gore, violence, self-harm and slurs. If one of
  those is broken, LittleBot **silences the speaker at once, ends the call** and shows a
  **"Live voice stopped"** alert with the rule's category only. **Some words may already have
  been heard by then**, because the captions trail the voice a little. If the voice asks your
  child for personal details (name, address, school) or gives a child's medicine dose, the call
  goes on and the voice is told not to. The weather, news, YouTube, websites, prices and health
  information never stop a call.
* **What it hears.** Worrying words such as "I'm hurt", "help" or "I ate a battery" show the usual
  **"Your child may need you"** alert (category only, never the words). Unlike typed
  conversation, the call carries on, and the voice is told to say "Let's get a grown-up right
  now".
* **Look-ups.** When the voice needs a fact or anything live (the weather, the news, the time),
  it hands the question to LittleBot. LittleBot asks the ChatGPT text model with its own prompt
  (at most 60 words; with ChatGPT tools on it uses LittleBot's tools for live information) and
  checks the answer with the same rules before the voice gets it. If the answer breaks a rule,
  the voice is told to say it isn't sure; if the look-up fails, to say it didn't work this time.
* **The voice's instructions** ask for short, gentle sentences with your child and full, plain
  answers for you; it says it is a robot, gives helpful general health and development
  information (no diagnosis, no medicine doses for a child), asks your child no personal
  questions, keeps no secrets and suggests nothing risky. Instructions are only guidance. The
  checks above are what LittleBot enforces.

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
* **Who is talking:** with **Recognise family members** or **Recognise family voices** on (both off
  by default), the names and roles of the family LittleBot recognises, on the robot, by face or by
  voice, go to the voice too, never photos, sound or voiceprints ([VOICES.md](VOICES.md),
  [AI_SAFETY.md §4](AI_SAFETY.md#4-privacy)).
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

### Activities in the ChatGPT voice

The GPT-Live voice only speaks inside a call: there is no "read this text" service for it. So
LittleBot **records its fixed lines once** in that voice and plays the recordings: the
activities' lines, the screen games' animal noises, the scripted chat replies and greetings,
and the fallback / safety lines. Everything else (the AI's answers, what the child said) keeps
the built-in text-to-speech voice.

**Activities** page → **Activity voices** → **Record activity voices in the ChatGPT voice**.
It uses this robot's ChatGPT sign-in and needs **Cloud communication** on. The card shows how
many lines are recorded, a progress bar and a **Stop** button. The first time it is about
1,000 lines and takes roughly 1½ hours; the activities that suit your child's age come first
(about half an hour for a baby). You can stop and carry on later: each line is saved, and used,
as soon as it is done. LittleBot keeps playing meanwhile (the recording doesn't use its speaker
or microphone); not during a voice chat, though.

Or on the robot's terminal (the same recording, with progress printed line by line):

```bash
cd ~/LittleBot
.venv/bin/python scripts/record_voice_lines.py --dry-run       # the lines and how long; records nothing
.venv/bin/python scripts/record_voice_lines.py --missing       # record what isn't recorded yet
.venv/bin/python scripts/record_voice_lines.py --only peekaboo --only "Again! Again!"
.venv/bin/python scripts/record_voice_lines.py --for-age       # only the activities for your child's age now
.venv/bin/python scripts/record_voice_lines.py --with-name     # also the lines with the child's nickname
```

Without `--missing` every line is recorded again (replacing the old recording). `--retries N`
(default 2) and `--lines-per-call N` (default 40) tune the recording; `--all-ages` adds the
scripted replies meant for older children.

* **The child's nickname** ("Hi Amara! Let's play peekaboo!") is sent to ChatGPT only with your
  OK: the card's *Also record the lines with …'s name* box, `--with-name`, or the AI page's switch
  to share the child's name. Otherwise those few lines keep the robot voice.
* **Checked line by line.** Each take's transcript must match the line (ignoring case,
  punctuation, spelling of numbers and "Moooo" vs "Moo"). A mismatch is asked again twice; a
  line that never matched is listed (the card, or the end of the command's output) and isn't
  saved. Press the button again (or use `--missing`) to try those once more.
* **Played instead of text-to-speech** whenever LittleBot says exactly that line (case,
  punctuation and spacing don't matter), with the same volume, mouth movement, STOP and pause.
  The journal says once per line which voice was used:
  `journalctl --user-unit littlebot | grep "speech in"` ("the recorded ChatGPT voice (bubbie)" or
  "text-to-speech (espeak; not recorded)"). The speech-rate setting doesn't change a recording.
* **Re-record** after changing the nickname or favourites in the profile, or adding your own
  activity: the new lines are simply missing, the button records them.
* Files: `data/voice_lines/<hash>.wav` and `data/voice_lines/index.json` (each line's text, hash,
  voice and date). Updates never touch `data/`. Delete the folder to go back to the robot voice.

### Info cards on the screen

With the screen's info cards on (`screens.enabled`), LittleBot shows a card while the voice answers a
grown-up's question that has something worth seeing: the weather ("and tomorrow?" changes it), a
word's meaning, a recipe one step at a time ("next step", "go back"), a timer ("stop the timer"), a sum,
a fact, the games menu ("what games do you have?"). "Close it" or "show me your face" takes it away.
Most turns (chit-chat, songs, the baby's babble) show nothing. How it decides
(`littlebot/screens/director.py`): common phrases are matched on the robot at once; other questions
get one small request to the ChatGPT text model (the camera looks' fast model and effort, `chatgpt.look_models`,
`look_effort`) with the last few caption lines (personal details removed) and the cards it can show.
Every word of a card passes the same safety rules as the voice's words. A turn voice recognition says was
the child's, or one of fewer than three real words from someone it doesn't know, gets no card. The voice is
told what is on the screen and never reads it out.

**The card follows the answer.** In a live voice call the director waits for LittleBot's own answer before
it picks a card (up to 25 s): the answer a hand-off brings back, or else what the voice says, once it isn't
just "hold on" / "let me think" / "one moment". The card's numbers, names and facts are taken from that
answer, never from the director's own knowledge, so the screen agrees with what is said; when the voice
hasn't really answered, no card shows. A card the answer itself put up (`show_on_screen`) isn't followed by
a second one. So a card now comes up as the answer is being said, not before it. A card kind the model
names that doesn't exist is logged by name (`unknown card 'movie'`). Bus topics: `live.call_started` `{}` and
`live.call_ended` `{reason}` around each call; a card's `screen.news` `{text}` ("the egg timer is done")
is passed to the voice as a note.

Explicit "google it" / "look it up online" / "search that in the browser" requests from a grown-up show
Google (SafeSearch on, no address bar) on the screen instead of a card, through the director's rules or the
`search_in_browser` / `browser` tools; "scroll down", "open the first one", "what does it say?" and "close
it" steer it. See [BROWSER.md](BROWSER.md).

Optionally, a fast decision model (TypeSafe's Jev or OpenAI's Decisions API) can make the director's choice
(no card, close, or which kind), say whether the voice has really answered yet, route hand-offs and more,
first only in shadow (compared and logged). See [DECISIONS.md](DECISIONS.md).

### Wake word: "Hey LittleBot"

Say **"Hey LittleBot"** (or **"Hey Bot"**) near the robot: its face starts listening, it plays a
short "listening" sound (two soft rising tones, like a smart speaker) and live voice starts, just
as if you had pressed **Start live voice**.

* **Say it all in one go.** "Hey LittleBot, what's the weather today?": what you say after the
  wake phrase goes into the call, so there is no need to wait for the sound. When you carry on
  talking it plays no sound (it would hide your words) and starts the call at once; the voice
  hears your question a moment after it connects, then catches up by shortening the pauses. The
  log shows how much was carried over and how long each step took (`wake word: kept ...`,
  `live voice: microphone on ... ms after the wake phrase`, `live voice: caught up ...`).
* **Heard on the robot, offline.** LittleBot recognises the phrase itself, with
  [Vosk](https://alphacephei.com/vosk/) and a small English model (40 MB). Microphone audio is
  processed in memory; only the sentence the recogniser is still deciding on is kept (at most
  8 seconds) and it is forgotten as soon as that sentence turns out not to hold the phrase.
  **Nothing is sent anywhere until it hears the phrase** (or a follow-up, below); then the rest of that sentence goes
  into the call. It is never written to disk, and it is dropped if live voice can't start or
  connect. From then on it is a normal live voice call (privacy above).
* **"Hey Bot" is stricter.** The short phrase is what starts calls by mistake most, so it needs a
  surer hearing (`wake.short_min_confidence`, 0.7) unless someone's face was in front of the camera in
  the last 10 s. The longer phrases work as before.
* **Follow-up without the wake word.** For 8 s after a chat ended by itself (its time limit, a quiet
  hang-up, a lost connection; not after "goodbye", "stop" or a video starting), a few words said by
  someone facing the camera start the next chat, and those words are its first turn. This is the one
  exception to "nothing is sent until it hears the phrase". `wake.follow_up_seconds: 0` turns it off.
* **The switch:** **AI** → **Live voice** → **Wake word: say "Hey LittleBot" to start live voice**
  (on by default). The line under it says "Listening for …", why it is paused or what is missing,
  and what happened the last time it heard you. Saying the phrase counts as a grown-up starting
  the call; turn the switch off if your child could start calls when you are not there.
* **It pauses** while live voice is on, while LittleBot listens for push to talk or tests the
  microphone, and while the microphone is off in **Privacy**. It ignores what it hears while
  LittleBot itself is making a sound.
* **During a video** it keeps listening, but only for the whole **"Hey LittleBot"** ("Hey Bot"
  alone is what videos say by mistake most), and a little more strictly, because the video's
  sound reaches the microphone too. Saying it pauses the video (where it was), brings the face
  back, then plays the listening sound and starts live voice; the voice is told which video is
  paused. **"Keep watching"**, "resume the video", "play it again" or "go back to the video"
  carries on with it after a short goodbye; **"stop the video"**, "turn it off" or "no more
  videos" stops it and the chat goes on. If nobody says anything (it probably heard the video) or
  the chat can't start, the video carries on by itself; otherwise it waits, paused with the face
  showing, and stops 2 minutes after the chat (the dashboard shows it, with **Resume**). Over a
  loud video it hears you less well: say it louder or closer, or turn the video down. Details,
  settings and measurements: [YOUTUBE_ON_PI.md section 10](YOUTUBE_ON_PI.md#10-talking-to-littlebot-during-a-video).
* **If live voice can't start** (not signed in, cloud communication off, no connection …) you hear
  "uh-oh", the status line says why and the log has `wake word: live voice could not start (...)`.
  It listens again after 4 seconds (`wake.cooldown_seconds`); it never retries by itself.
* **False starts.** "Hey Bot" is short and sounds like "hey but", "hey bud" or "hey boss";
  "Hey LittleBot" is much safer. "Hey Lil Bot" works too: with 8 synthetic voices at 2 speeds it was heard
  15 times of 16 (13 before it was a phrase, as "hey little bot" or "hey bot"), and of 20 similar phrases
  ("hey lil buddy", "hey kill bot" …) it added 4 false starts in 320 tries. With synthetic voices and 69 deliberately similar phrases
  ("hey there little guy", "little bottle", "hey bob" …) "Hey LittleBot" never started by mistake,
  "Hey Bot" did for a handful. If it happens to you, keep only the long phrase:
  `wake: {phrases: ["hey little bot"]}` in `config/local.yaml`. Real children's voices, accents and
  distance have not been tested yet.
* **Cost:** about 140 MB of memory while it is switched on (freed when it is off, or during a
  video with `wake.during_videos: false`) and a few percent of one CPU core (about 4 % of one desktop core measured; roughly 10 % of
  one Pi 5 core expected). The model is shared with `audio.stt.vosk_model_path` if that points to
  the same folder.
* **Installing:** `scripts/install_pi.sh` installs it by default (`--no-wake-word` skips it): the
  `local-stt` extra (vosk: one prebuilt aarch64 wheel) and the model. By hand:

  ```bash
  cd ~/LittleBot
  .venv/bin/python -m pip install --prefer-binary -e '.[local-stt]'
  .venv/bin/python scripts/download_models.py vosk-en      # -> models/vosk-model-small-en-us-0.15/
  systemctl --user restart littlebot
  ```

```yaml
wake:                                # config/local.yaml
  enabled: true                      # default of the dashboard switch
  phrases: ["hey little bot", "hey littlebot", "hey lil bot", "hey bot"]   # "littlebot" is heard as "little bot"
  model_path: models/vosk-model-small-en-us-0.15
  min_confidence: 0.5                # per word, 0..1: higher = fewer false starts, more misses
  short_min_confidence: 0.7          # "hey bot" (two words) needs this, or a face in front of the camera
  short_face_seconds: 10             # ...seen this recently
  follow_up_seconds: 8               # after a chat ended by itself: talking while facing it starts a new one (0 = off)
  follow_up_words: 2                 # ...with at least this many words
  distractors: true                  # common words the recogniser may pick instead: fewer false starts
  cooldown_seconds: 4
  during_videos: true                # listen while a video plays (with the stricter rules below)
  video_phrases: ["hey little bot", "hey lil bot"]  # only these count during a video ([] = all phrases)
  video_min_confidence: 0.7
  video_duck: false                  # turn the video down for a moment after "hey ..." (off: videos say "hey" a lot)
  video_no_words_seconds: 15         # a call that paused a video and nobody talks: back to the video
```

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
  JPEG, 640 pixels wide, or 512 for simple questions) and sends it with the question to the ChatGPT
  text model on your plan. **Nothing is filmed or streamed**, and no photo is ever taken on a timer or in the
  background. LittleBot's face does a little "peek" when it takes the photo, so everyone can see
  it happened. During a live call the camera keeps its latest frame in memory (replaced with every
  new one, never sent or saved), so the photo for a question doesn't wait for the camera.
* "How many fingers am I holding up?" is counted on LittleBot itself when the hand models are
  installed (`scripts/download_models.py hands`). When it is sure within a moment, the photo is not
  sent at all; when it isn't sure, ChatGPT answers. If counting takes longer than that moment (a busy
  robot), the photo goes to ChatGPT too and the first sure answer is used.
* The answer comes back as text (at most 40 words) and is checked with LittleBot's safety rules
  before it is shown or spoken. If it fails the check, LittleBot says "I'm not sure what I see.
  Can you show me again?" instead. Only one photo is taken at a time.
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

### LittleBot's face

ChatGPT knows LittleBot has an animated face and which faces it can make: happy, surprised, sleepy,
curious, excited, sad, playful, celebrating, affectionate, confused, thinking, neutral, wink,
spin_eyes, peekaboo (eyes closed, then "peek!"), nod, shake, bounce, wiggle, sparkle, hearts and
confetti.

* **Live voice:** ask "make a happy face", "wink" or "play peekaboo". The voice hands it to
  LittleBot (like a look-up), the face plays it straight away, and the voice carries on talking.
* **Typed / push-to-talk:** ChatGPT may end a reply with a hidden tag such as `[face: happy]`.
  LittleBot removes it before speaking (it is never spoken, stored or shown) and plays that face.

## 5. Asking LittleBot to do things

With **Sign in with ChatGPT**, you (or your child) can ask LittleBot to control itself and its app:
in typed chat, with **Push to talk**, or during a **live voice** call. ChatGPT picks one of LittleBot's
*tools*, LittleBot does it, and ChatGPT says in a few words what happened. Switch it off with
`chatgpt.tools_enabled: false` (ChatGPT then only answers).

| Ask | What LittleBot does |
|---|---|
| "Turn the volume up", "quieter", "set the volume to 30" | changes the speaker volume (and the video volume), never above the safe maximum (`audio.speaker.max_volume`, `media.max_volume`) |
| "Play baby shark", "put on a video of diggers" | finds the video on YouTube and plays it on its screen ([YouTube videos](#youtube-videos)) |
| "Start the peekaboo activity", "let's do Old MacDonald" | starts one of its activities (by name, or close to it) |
| "Play a game", "stop the game" | starts or stops an on-screen game (when the games are installed) |
| "Make a surprised face", "wink" | shows that face |
| "Go to sleep", "wake up" | goes to sleep or wakes up |
| "Stop everything" | stops any video, activity or game. The conversation carries on (the red **STOP** still stops everything, the conversation too) |
| "Keep watching", "stop the video" (a video paused for this chat) | carries on with the video where it stopped after the answer (in live voice: after a short goodbye, then the call ends), or stops it ([during a video](YOUTUBE_ON_PI.md#10-talking-to-littlebot-during-a-video)) |
| "How hot are you?", "are you on Wi-Fi?", "what's connected?", "which ChatGPT plan?" | answers from its status: CPU temperature and load, memory, how long it has been running, Wi-Fi signal, camera / microphone / speaker / screen / motors, volume, the signed-in plan, live voice on or off, what is playing, the Privacy switches |
| "What can you do?", "tell me about yourself", "who made you?" | a short summary of what it can do; it knows who built it (`ai.creator`, `ai.creator_relation`: Ali, the little one's dad), its version and what it runs on |
| "What went wrong?", "why didn't the video play?", "check your debug log" | reads its own debug log for the last minutes (`read_debug_log`: what it did, warnings and errors, never what anyone said) and explains the likely cause and what might fix it |
| Typed chat only: "what am I holding?" | one camera photo, if you allowed it in Privacy ([section 4](#4-letting-the-ai-see-camera)). In live voice the call's own camera look is used |
| "What's the weather today?", "will it rain tomorrow?", "do we need coats?", "weather in Paris this weekend" | gets the real weather: now, today, tomorrow or up to 7 days, at home or anywhere ([Weather, time and finding things out](#weather-time-and-finding-things-out)) |
| "What time is it?", "what day is it?", "what's the date?", "what time is it in Tokyo?" | reads its own clock (or that place's time zone) |
| "Any news today?", "who won the hockey game?", "is the zoo open on Sundays?" | ChatGPT looks it up with its own web search |

**When things start.** A video, an activity or going to sleep needs the screen and the speaker that
the conversation is using, so it starts **after** LittleBot has said its answer. In a **live voice
call** that also ends the call: the voice says a short goodbye, LittleBot hangs up (at most 15 s
later), then the video or activity starts. The call's microphone would otherwise hear the video.
It starts once LittleBot's own speaker plays again (it checks, for up to 3 s): the call's sound
stream on the same USB speaker has only just closed, and a sound started at that moment could be
cut off after a moment, so the activity's first line or sound is heard in full.
The last call then shows "handed over". The red **STOP** before that cancels it. Games share the
speaker with a conversation, so they start at once and the call carries on.

**Waiting for an answer (live voice).** The voice waits in silence while LittleBot works on what
it was handed, so LittleBot answers as early as it can:

* **The weather, the time, the volume and timers need no text model.** "What's the weather like?",
  "will it rain this weekend?", "what time is it?", "louder", "set the volume to 40", "set a timer
  for 5 minutes" are matched on their words (`littlebot/chatgpt/quick.py`) and LittleBot runs the
  tool itself: the voice gets the weather service's facts (or the time, or "done") in a few hundred
  milliseconds, and the forecast or timer card goes up at the same time. Only clear, simple requests
  match: another place's time, "why does it rain?", "cancel the egg timer" or "play baby shark
  louder" go to the text model as before, and so does one whose tool fails or takes over 3 s. The
  journal then says something like `hand-off received (kind=quick:weather); answered in 160 ms without the text model`.
* **The first sentence goes to the voice as soon as it is written** (camera looks already did this):
  the voice starts saying it while the text model writes the rest, which follows as a note ("More of
  the answer ..."). Words that only say what's coming ("Let me check.") are skipped; if the answer
  then turns out different (it looked something up first), the whole answer follows instead.
* **Simple questions get a lighter request**: a short question about something that doesn't change
  ("how tall is a giraffe?", "facts about Kublai Khan") is asked with less reasoning (`minimal`) and
  without web search. Anything about what's current (news, today, scores, opening hours, prices, who
  someone is now, ages, "look it up") keeps the full request with web search. The journal says, e.g.,
  `the text model answered in 1840 ms (light: minimal effort, no web search; first sentence at +950 ms)`.
* **A card doesn't hold the answer up.** The text model is asked to write its answer and call
  `show_on_screen` in the same reply; then no second request is needed for the words, and a card
  whose picture is still loading after 0.4 s goes up by itself while the voice speaks.
* The text model's connection is opened while the call connects, so the first hand-off doesn't pay
  for it.

While it works and the voice is quiet, a soft two-note "thinking" sound plays (after 1.5 s, then every
3 s, at most 4 times; `chatgpt.live_thinking_sound: false` turns it off). When home is set on the
dashboard, its forecast is fetched while a call connects, so the first weather question is instant.

If no answer (or first sentence) is ready within **3.5 seconds**, the voice is told straight away to
say it's thinking, and the answer follows when it comes (25 s at most; after that the voice says
it didn't work out).

### YouTube videos

* **Search** needs no API key: LittleBot runs **yt-dlp** (`python -m yt_dlp --flat-playlist -J
  "ytsearch5:QUERY"`, a child process with a 20 s timeout), which reads YouTube's search results
  (ids, titles, channels, lengths). yt-dlp is installed with LittleBot (a pure-Python package,
  fine on the Pi). On a robot installed before this, run `cd ~/LittleBot && .venv/bin/python -m pip
  install -e .` (or re-run `scripts/install_pi.sh --yes`). YouTube changes often: if search stops
  working, update it with `.venv/bin/python -m pip install -U yt-dlp`.
* **Playing** uses the same on-screen player as your approved list (the official YouTube player in
  the Chromium kiosk) with the same rules: the session and daily **time limits**, the **age gate**
  (videos are off under 18 months unless you allow them on the Videos page), **Videos** on/off,
  offline mode, and the player's check that the video in the player is the one that was asked for.
  If YouTube won't play a video in an embedded player (removed, private, or "playback on other
  sites" disabled: errors 100/101/150), the next search result plays instead. **Skip** moves
  through the other results. A found video is **not** added to your approved list; **Now playing**
  shows its title. A video that is on your approved list plays as that item.
* **Any video can play (for now).** `media.allow_search: true` (the default) lets ChatGPT play any
  video it finds. With `false`, only your approved list plays: "play twinkle twinkle" then plays an
  approved video whose title matches.
* **Words are fine.** Since October 2026 LittleBot's check of what it says no longer stops words
  like YouTube, search, online, website, app or link (and a live call that says them goes on).
  Video titles are still shortened before ChatGPT gets them, so it says "Baby Shark" rather than
  a long title.

### Weather, time and finding things out

LittleBot can answer the everyday questions a smart speaker answers, in typed chat, push to talk and
live voice (with `chatgpt.tools_enabled`, the default):

* **Weather** (`get_weather`): now (temperature, feels-like, conditions in plain words, wind, humidity),
  the next hours in three-hour blocks, and today's, tomorrow's or up to 7 days' forecast (high, low,
  conditions, chance of rain or snow, wind, sunrise and sunset). From [Open-Meteo](https://open-meteo.com):
  free, no account or key. At home, or anywhere ChatGPT names ("weather in Paris"), found with
  Open-Meteo's free place search.
* **Time and date** (`get_time`): the time, the day of the week and the date from the robot's own clock,
  in the robot's time zone (`info.timezone`, else the Pi's), or in another place's time zone.
* **Anything else that changes** (news, sports scores, opening hours, events): ChatGPT's own **web
  search** (`chatgpt.web_search: true`, the default), run by OpenAI as part of the answer. LittleBot
  removes the links and citation marks before it speaks.

ChatGPT is told to use these instead of guessing or saying it doesn't know. The live voice is told
to hand such questions to LittleBot, and those hand-offs always go to ChatGPT with the tools (never to
the camera, even when they say "looking like" or "show me the weather").

**Where home is.** Open **Settings → Weather & time** on the dashboard. It shows, for example,
"Weather location: **Toronto, Ontario** (from your internet connection, approximate)". Press **Change**
to type a town ("Toronto", or "Springfield, Illinois" when there are several) or a latitude and longitude
("43.65, -79.38"), and to choose °C or °F. **Use my internet connection** goes back to automatic.
**Check the weather now** shows what ChatGPT would get. In order, home is:

1. the place you chose on the dashboard ("set on the dashboard"), else
2. `info.location` in `config/local.yaml` ("from LittleBot's config file"), else
3. approximately where the robot's internet connection is ("from your internet connection, approximate"):
   LittleBot asks a free service where its public IP address is (ipinfo.io, then ipwho.is, then
   ipapi.co). This is usually the right city, but can be your internet provider's nearest town. It is
   kept in LittleBot's database and asked again after a week.

**Units.** Automatic: °F and mph if home is in the US (and the few other countries that use them),
else °C and km/h (mph in the UK). Weather somewhere else is given in your units.

**Speed.** Each weather answer gets at most 3 s for its requests (`info.timeout_seconds`); a request
that runs out of time carries on in the background, so asking again finds it ready. A forecast is
reused for 10 minutes (`info.cache_minutes`), so a second weather question is instant, and in a live
call a plain weather question is answered without the text model (see "Waiting for an answer"), in
well under a second.

**Web search caveat.** Web search is sent the way the Codex CLI sends it (`{"type": "web_search"}` in
the request's tools), but it has **not been tried against OpenAI's real endpoint yet**. If the endpoint
refuses it, LittleBot asks again without it, and doesn't offer it again until it restarts (the journal
says "not offering web_search any more"). Answers then work as before, without web search.

**Needs:** cloud communication on in Privacy (nothing is sent while it is off, or in offline mode).
Typing a place name on the dashboard needs it too (coordinates don't).

### What goes where

The tool names, their results (status numbers, activity, game and video names) and your words go
to OpenAI, as with any ChatGPT answer. A search sends only the search words to YouTube, from the
robot. The robot's Wi-Fi network name is included in the status if `iwgetid` is installed.
For the weather, Open-Meteo gets the place's coordinates (and a place name to search); the IP
location service sees the robot's public IP address (once a week at most, only when you haven't
set home); the weather report sent to OpenAI contains the place's name. Web searches are run by OpenAI.

### Settings

```yaml
chatgpt:
  tools_enabled: true          # ChatGPT may control LittleBot when asked (false: it only answers;
                               # then also no weather, time or web search)
  max_tool_rounds: 6           # tool calls per answer ("play baby shark" = search + play)
  web_search: true             # OpenAI's web search for news, scores, opening hours...
info:
  location: ""                 # home for the weather: "Toronto", "Springfield, Illinois" or "43.65, -79.38";
                               # "" = from the internet connection (the dashboard's choice wins)
  units: auto                  # auto | celsius | fahrenheit (the dashboard's choice wins)
  locate_by_ip: true           # no home set: approximate it from the robot's public IP address
  timezone: ""                 # for "what time is it?"; "" = the robot's own time zone
  timeout_seconds: 3           # per weather answer, all requests together
  cache_minutes: 10            # a forecast is reused this long
media:
  allow_search: true           # play any video found on YouTube, not only the approved list
  search_results: 5            # results per search (the others are what Skip plays)
  search_timeout_seconds: 20
  ytdlp_command: null          # null = this Python's yt_dlp module, else yt-dlp on PATH
```

## 6. Settings you may want to change

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
  live_barge_in: auto             # talking over the voice: auto = only with echo cancelling | on | off
  live_barge_in_speech_ms: 300    # this much grown-up speech interrupts it
  live_barge_in_over_echo_db: 8   # and it must be this much louder than what is left of its voice
audio:
  echo_cancel: auto               # use PipeWire's echo-cancelled devices when they exist | on | off
```

All keys and defaults are listed in the [technical section](#configuration).

<a id="5-troubleshooting"></a>
<a id="6-troubleshooting"></a>

## 7. Troubleshooting

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
| Activities still speak with the robot voice | Their lines aren't recorded yet, or a line changed (another nickname, your own activity) | **Activities** → **Activity voices** → **Record activity voices in the ChatGPT voice**. `journalctl --user-unit littlebot \| grep "speech in"` shows which voice each line used |
| Activity voices: some lines "didn't come out right" | The voice said something else (its transcript didn't match the line) | Press the button again: only those lines are asked again. A sound word it keeps saying differently stays in the robot voice |
| After a voice chat starts an activity, its first sound is cut off | The speaker was still being let go by the call | LittleBot now waits for the speaker (up to 3 s) before starting; if it still happens, the journal says "speaker still not back" |
| "LittleBot is busy with something else. Stop it first." | A video or another parent action holds the speaker | Stop it, then start live voice |
| "The speaker or microphone could not be opened." | The USB devices are missing or in use by another program | **Audio & devices**: check both devices. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#no-sound--sound-from-the-wrong-device) |
| It hears itself, echoes or answers itself | Its own voice reaches the microphone after the gate opens | Lower the volume, move the microphone away from the speaker, raise `chatgpt.live_echo_tail_ms` (for example to 700). Keep `live_half_duplex: true`. With echo cancelling: `scripts/setup_echo_cancel.sh --check` |
| It doesn't react when you talk | It was still talking (without echo cancelling the microphone is shut then), or your voice is too quiet | Wait until it stops, speak closer. Raise **Audio & devices → Microphone level** (for example 1.5–2×) |
| It doesn't stop when I talk over it | Echo cancelling isn't set up (the journal says "audio: echo cancelling off (...)" and "one at a time"), your voice is too quiet next to its voice, or it is high (above `live_barge_in_max_pitch_hz`, 260 Hz, counts as a child) | `scripts/setup_echo_cancel.sh --check` (see [Talking over it](#talking-over-it-barge-in)); speak closer or louder; lower the volume; lower `chatgpt.live_barge_in_over_echo_db` (for example 6); for a high voice raise `chatgpt.live_barge_in_max_pitch_hz` (for example 300). `journalctl --user-unit littlebot \| grep "interrupted by"` shows each time it did stop, and "but it didn't stop" each time it heard a grown-up and why it didn't |
| It stops by itself in the middle of a sentence | Something loud and voice-like (a TV, another grown-up nearby) or its own echo, when echo cancelling works poorly | The journal says "false alarm (...), the voice carried on" when nobody's words followed. Raise `chatgpt.live_barge_in_over_echo_db` (for example 14) or `live_barge_in_speech_ms` (600); lower the volume; or `chatgpt.live_barge_in: off` for one at a time |
| Too quiet or too loud | The speaker volume | **Audio & devices** → volume (capped at `audio.speaker.max_volume`). Or set `chatgpt.live_volume` for live voice only |
| "hung up after 90 s of quiet" | Nobody talked | Normal. Change `chatgpt.live_idle_hangup_seconds` if it's too short |
| Wake word: **The wake-word model is missing** / **isn't available** | The vosk library or its model is not installed | `.venv/bin/python -m pip install --prefer-binary -e '.[local-stt]'` and `.venv/bin/python scripts/download_models.py vosk-en`, then `systemctl --user restart littlebot` |
| Wake word: "uh-oh" after the listening sound | Live voice couldn't start; the status line under the switch says why (often: not signed in, or cloud communication off) | Fix that reason, then say it again |
| Wake word: nothing happens | Paused (the status line says why), too far away, or the microphone is too quiet | Speak within about a metre; raise **Audio & devices → Microphone level**; check **Audio & devices** → test the microphone |
| Live voice starts by itself | A false start on "Hey Bot" | "Hey bot" already needs `wake.short_min_confidence` (0.7) or a face in front of the camera; the journal says "'hey bot' ignored" for the ones it skipped. Raise it (0.85), or set `wake.phrases: ["hey little bot", "hey lil bot"]` |
| A video pauses by itself and the voice says hello | The wake word heard something like "hey little bot" in the video | Nobody answering, the video carries on after `wake.video_no_words_seconds` (15 s); say "keep watching" to carry on at once. If it happens often: raise `wake.video_min_confidence` (0.85), or `wake.during_videos: false` |
| Wake word: nothing happens during a video | The video is louder than you at the microphone, or you said "Hey Bot" (only "Hey LittleBot" counts during a video) | Say "Hey LittleBot" louder or closer; lower the video volume; try `wake.video_duck: true`. The log shows what was heard and ignored ("ignored while a video plays") |
| "Keep watching" doesn't carry on | The video stopped meanwhile (2 minutes after the chat: `media.chat_pause_minutes`, or the day's video time ran out) | Start it again from the Videos page |

The app log (`data/logs/littlebot.log`) has the HTTP status and end reason of each call. It never
contains what was said.

---

## 8. Technical details

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

**Function calling** (`responses.py` + `tools.py`, ported from WatchGPT's `ChatGptBrain` /
`Toolbox`): `reply(..., tools=[{name, description, parameters}], run_tool=fn)` adds
`tools: [{"type": "function", name, description, "strict": false, parameters}]`,
`tool_choice: "auto"` and `parallel_tool_calls: false`. Every `response.output_item.done` item is
kept. For each `function_call` item (`name`, `arguments` as JSON text, `call_id`) LittleBot runs the
tool, then, because the endpoint keeps nothing (`store: false`), sends the history again plus the
model's items (reasoning with its `encrypted_content`, the call; without their `id`, as Codex does)
and `{"type": "function_call_output", "call_id": …, "output": …}`. Up to `chatgpt.max_tool_rounds`
rounds, then the text of the last answer is used (validated as always). A tool never raises: a
crash becomes `error: the action failed` for the model. In typed chat the whole answer may take
30 s longer than `ai.timeout_seconds`; a video / activity / sleep a tool asked for is queued and
runs after the reply is spoken (`ConversationEngine._after_turn`). In live voice, hand-offs that ask
for something ("start the peekaboo activity", "show me a video", "go to sleep"; `CONTROL_WORDS`)
go to the tool-enabled text model instead of the face / camera paths, and the queued take-over runs
after the call ends with "handed over" (`LiveCall._handover_due`: the voice spoke and went quiet, or
`HANDOVER_MAX_S`). `LiveCall.run_handover` runs it after the call's clean-up (`AudioService.resume`)
and `AudioService.speaker_back()`: that opens the speaker and plays 0.2 s of silence, retrying every
0.1 s for up to 3 s while the device still fails, then lets it go again (a video may need it). Without
it, a first sound that hit the just-released device failed part-way, and the failure marked the speaker
as unplugged, so everything for the next `reconnect_seconds` (5 s) played silently. Hand-off timing: `HANDOFF_INTERIM_S` (3.5 s) then `STILL_WORKING`, the answer later
as a `session.context.append` note, `HANDOFF_TIMEOUT_S` (25 s) in all.

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
after (with barge-in on, someone talking over the voice opens it: [Barge-in](#barge-in-echo-cancelling)).
While a reply is in progress, its latest ~320 characters are re-checked as caption text
arrives (at most every 0.1 s, or at once at the end of a sentence). The whole turn is checked
again when it is done.

### Barge-in (echo cancelling)

**Devices.** `scripts/setup_echo_cancel.sh` loads PipeWire's `libpipewire-module-echo-cancel`
(WebRTC echo canceller, `aec/libspa-aec-webrtc`) for the user, between the USB microphone and the
USB speaker. It makes two PipeWire nodes, the source `littlebot_ec_mic` and the sink
`littlebot_ec_speaker`, and makes them PipeWire's defaults, so Chromium's YouTube sound goes
through the canceller too (the wake word then hears less of the video). PortAudio has no PipeWire
host API, so `~/.config/alsa/asoundrc` gets two ALSA PCMs of the same names (`type pipewire`, from the
`pipewire-alsa` package) that PortAudio lists like any ALSA device. Details and undo:
[PI_SETUP.md](PI_SETUP.md#echo-cancelling). `littlebot/audio/devices.py` (`audio.echo_cancel: auto`)
picks the pair for an automatic device (`audio.speaker.device` / `audio.microphone.device` not set or
`auto`) when **both** exist, else the USB `hw:` devices as before; a device named in the config wins.
The audio service (and so the wake word, which reads its microphone) and live voice then open the
same two devices; through PipeWire they aren't exclusive. The journal says "audio: echo cancelling
on (PipeWire: littlebot_ec_mic / littlebot_ec_speaker)" or "audio: echo cancelling off (why)".

**When.** `chatgpt.live_barge_in: auto` turns barge-in on in `LiveAudio` when both of its devices
are the echo-cancelled ones; `on` forces it (a headset, a speakerphone with its own echo
cancelling), `off` keeps half-duplex. It needs `live_half_duplex: true` (the gate is what it opens).

**The detector** (`BargeInDetector`, `live_audio.py`) looks at each 20 ms microphone frame while the
voice is audible:

* *Loud enough:* the frame's RMS must be at least `live_barge_in_min_level` (0.008), 10 dB above the
  room's noise floor (measured while the voice is quiet) and `live_barge_in_over_echo_db` (8 dB)
  above the residual echo. The residual echo is `coupling` x the loudest the speaker played in the
  last 0.3 s; `coupling` (microphone level / speaker level) is measured all the time while only the
  voice plays, tracked near the top of its spread (up quickly in capped steps, down slowly) and
  frozen for 1 s after anything loud, so someone's speech doesn't teach it. Nothing interrupts in
  the first 0.3 s of voice of a call, while it measures.
* *A grown-up's voice:* each loud frame's pitch is estimated by normalised autocorrelation on the
  last 40 ms at 8 kHz (one 1024-point FFT pair; the shortest period whose peak is within 85 % of the
  best wins, so a baby's 450 Hz never reads as 225 Hz). Voiced (correlation 0.5 or more) at 75 Hz to
  `live_barge_in_max_pitch_hz` (260) counts as a grown-up, above it as a child.
* *Long enough:* a run of loud frames (gaps under 0.12 s are consonants) fires after
  `live_barge_in_speech_ms` (300 ms) when at least 40 % of its frames are grown-up voiced and more
  than twice as many as child voiced. A cough (no pitch), a short "mm" (too short), the baby (too
  high), the voice's own echo (not louder than itself) and a quiet TV (the floor) don't.
* *Voice recognition* (optional): after 0.2 s of a run, `ctx.voices.identify(samples, 16000)` is
  asked once in the background, if `ctx.voices` has it. A grown-up's match lets it fire after
  0.25 s; `is_child` vetoes the run (and lets a held voice carry on). The detector never waits for it.

Measured on the development machine: about 46 µs per loud 20 ms frame (pitch computed) and 23 µs
per quiet one, under 0.25 % of one core; it adds no delay beyond the speech it waits for. Not
measured on the Pi 5 (expect a few times slower: still well under 1 %).

**The flow.** On firing, the voice fades out over 50 ms in the PortAudio callback and **is held**:
what keeps arriving waits in the queue (up to 1.5 s). The microphone gate opens and the last ~0.5 s
(from 0.1 s before their speech began, out of a 0.8 s ring buffer in memory) goes out first, one
frame per frame (pauses longer than 0.3 s shortened until it is back in real time). The call's face
and state go to listening (`LiveCall._barge_in_changed`). Then either:

* **ChatGPT's caption of their words arrives** (`input_transcript.added` / `turn.done`, passed by
  `LiveCall._barge_in_words` to `LiveAudio.barge_in_heard`): the held voice is dropped, and so is
  the rest of the turn they cut into as it still arrives (the server's own turn detection usually
  cancels it). The next sound after a 0.25 s pause in the voice, once they stopped talking for 0.25 s
  or their words were final, is a new turn and plays (at the latest after 15 s). If their first final
  words are only "oops, go ahead" or the like (`GO_AHEAD`, the whole utterance), the call sends the
  note "They didn't mean to interrupt: carry on from where you stopped, without starting over." The
  instructions also say: "If someone talks while you are speaking, stop and listen to them. If all
  they say is something like "oops, go ahead" or "sorry, carry on", carry on from where you stopped,
  without starting over." (only when barge-in is on).
* **No words within `live_barge_in_false_alarm_ms`** (2.5 s; longer while they are still making
  sound, at most 4 s): a false alarm. The gate shuts and the held voice fades back in where it
  stopped (a little late for the rest of that turn).

One INFO line each time, never any sound: "live voice: interrupted by a grown-up after 0.3 s of
speech (pitch 120 Hz, voice Ali)", "live voice: false alarm (no words within 2.5 s), the voice
carried on", "live voice: they didn't mean to interrupt ("go ahead"); the voice carries on".

Known limits: a TV or a radio voice louder than the threshold interrupts like a person; after a
false alarm on a noise, OpenAI's own turn detection may already have cut its reply short; "go
ahead" meant as "yes, do it" gets the carry-on note too; GPT-Live's data channel has no documented
"cancel the reply" event, so the cut is LittleBot's (the voice that still arrives is dropped).

Dashboard API (parent session + CSRF, like every route): `GET /api/chatgpt`,
`POST /api/chatgpt/sign-in`, `/sign-in/cancel`, `/sign-out`,
`/live/start {"parent_present": true}`, `/live/stop`. Responses contain only the masked e-mail, the
plan, the one-time code and the live status, never tokens.

**Recorded voice lines** (`littlebot/chatgpt/voice_record.py`, `littlebot/app/spoken_lines.py`,
`littlebot/activities/lines.py`, `littlebot/audio/voice_lines.py`). `spoken_lines.collect` gathers
the fixed lines: the activities' `say` texts and sung words, placeholders filled the way the engine
fills them (profile values; every item of the `pick` / `for_each` data lists; `set` values; `{round}`;
at most 40 combinations per sentence), the peekaboo game's `host.say` noises, the offline provider's
scripted replies for the child's age (`MockProvider.scripted_lines`), the conversation's fallbacks and
the safety lines (`safety.content.fixed_lines`). `record_lines` makes a call with the same offer and
voice fall-backs as live voice (`open_call`, shared with `scripts/record_giggle.py`, as are the call's
silent microphone `mic_track` and `tidy`). For each line it sends the note "Say exactly, word for word,
nothing else: <line>" (`session.context.append`), keeps the voice's audio until its `turn.done` and
then 1 s of quiet, trims and levels it (22.05 kHz), and compares the transcript with the line
(`similarity` ≥ 0.8 on letters only, numbers as words, repeated letters once). No sound for 6 s: the
line is asked out loud too (espeak-ng through the call's microphone). A new call every 40 lines; a
dropped call is called again (3 failures in a row end the recording). `VoiceLineJob` saves each
match through `VoiceLineStore.save` (atomic writes, an index lock file shared with other processes).
`AudioService.say` looks the line up first (`line_hash` = SHA-1 of the normalised text) and falls
back to text-to-speech; the index is re-read when the file changes. Dashboard: `GET /api/voice-lines`
(counts, minutes, why it can't start, the job's progress), `POST /api/voice-lines/record
{"with_name": bool|null, "again": false}`, `POST /api/voice-lines/stop`.

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
| `live_half_duplex` | `true` | shut the microphone while LittleBot's voice plays. Turn off only with a headset or hardware echo cancellation (barge-in then stays off: the microphone is always open) |
| `live_echo_tail_ms` | `450` | how long the microphone stays shut after the voice stops |
| `live_barge_in` | `auto` | a grown-up talking over the voice stops it ([Barge-in](#barge-in-echo-cancelling)): `auto` = only when both devices are PipeWire's echo-cancelled ones (`scripts/setup_echo_cancel.sh`), `on` = always (a headset or hardware echo cancelling), `off` = one at a time |
| `live_barge_in_speech_ms` | `300` | (150-2000) this much voiced grown-up speech interrupts it. A known grown-up (voice recognition) needs 200 ms |
| `live_barge_in_over_echo_db` | `8.0` | how much louder than what is left of its own voice (and than the room's noise) speech must be |
| `live_barge_in_min_level` | `0.008` | and at least this loud (microphone RMS, full scale 1.0) |
| `live_barge_in_max_pitch_hz` | `260.0` | (150-400) voiced above this counts as a child and doesn't interrupt |
| `live_barge_in_false_alarm_ms` | `2500` | (300-5000) no words of theirs from ChatGPT by then: the voice carries on |
| `live_thinking_sound` | `true` | a soft sound while a hand-off is worked on and the voice is quiet |
| `live_voice_level` | `0.012` | speaker RMS (0..1) above which the voice counts as speaking |
| `live_volume` | `null` | `null` = the speaker volume setting. Always capped at `audio.speaker.max_volume` |
| `tools_enabled` | `true` | ChatGPT may control LittleBot when asked: typed chat, push to talk and live-voice hand-offs ([section 5](#5-asking-littlebot-to-do-things)) |
| `max_tool_rounds` | `6` | tool calls per answer |
| `web_search` | `true` | offer OpenAI's hosted web search with the tools (`{"type": "web_search"}`). Refused by the endpoint: asked again without it, then not offered until restart |

Echo cancelling (`audio:` section, `AudioConfig`): `echo_cancel` (`auto`: use `littlebot_ec_mic` /
`littlebot_ec_speaker` for automatic devices when both exist; `on`: the same, and a warning when they
don't; `off`: never).

Live information (`info:` section, `InfoConfig`): `location` (`""`: a place or "lat, lon"; empty = from
the internet connection), `units` (`auto` | `celsius` | `fahrenheit`), `locate_by_ip` (`true`),
`timezone` (`""` = the robot's own), `timeout_seconds` (`3`), `cache_minutes` (`10`). The parent's
choice on the dashboard (settings `info.location`, `info.units`) wins over `location` and `units`. See
[Weather, time and finding things out](#weather-time-and-finding-things-out). Dashboard API: `GET
/api/info` (`?lookup=true` finds home first if it isn't known), `PUT /api/info {"location", "units"}`
(`""` = automatic), `GET /api/info/weather`.

Wake word (`wake:` section, `WakeConfig`): `enabled` (`true`, the switch's default), `phrases`,
`model_path` (`models/vosk-model-small-en-us-0.15`), `min_confidence` (`0.5`), `distractors`
(`true`), `cooldown_seconds` (`4`), `sound` (`listen`); during a video: `during_videos` (`true`),
`video_phrases` (`["hey little bot", "hey lil bot"]`), `video_min_confidence` (`0.7`), `video_duck` (`false`),
`video_duck_volume` (`0.3`), `video_duck_seconds` (`2`), `video_pause_wait_seconds` (`1`),
`video_no_words_seconds` (`15`); and `media.chat_pause_minutes` (`2`). See [Wake word](#wake-word-hey-littlebot)
and [YOUTUBE_ON_PI.md section 10](YOUTUBE_ON_PI.md#10-talking-to-littlebot-during-a-video).

YouTube search (`media:` section, `MediaConfig`): `allow_search` (`true`: any video found may play;
`false`: only the approved list), `search_results` (`5`), `search_timeout_seconds` (`20`),
`ytdlp_command` (`null` = `python -m yt_dlp`, else `yt-dlp` on PATH).

### Files

| Path | Contents |
|---|---|
| `littlebot/chatgpt/auth.py` | device-code sign-in, token file, refresh, sign-out |
| `littlebot/chatgpt/responses.py` | the `chatgpt` conversation provider |
| `littlebot/chatgpt/wire.py` | GPT-Live data-channel events |
| `littlebot/chatgpt/live.py` | `LiveVoiceService` (`ctx.live`): gates, safety checks, limits, delegations, clean-up |
| `littlebot/chatgpt/live_audio.py` | microphone / speaker bridge with the half-duplex gate; barge-in (`BargeInDetector`, `estimate_pitch`, `GO_AHEAD`) |
| `scripts/setup_echo_cancel.sh` | PipeWire's echo canceller for the USB microphone and speaker (`--check`, `--remove`, `--dry-run`; `scripts/install_pi.sh --echo-cancel`) |
| `littlebot/audio/devices.py` | device choice, including the echo-cancelled pair (`echo_cancel_devices`) |
| `littlebot/chatgpt/tools.py` | `RobotToolbox`: the tools ChatGPT may use (status, volume, faces, videos, activities, games, sleep, camera, weather, time, info cards; web search) |
| `littlebot/screens/director.py` | `ScreenDirector`: which info card the screen shows during a live call |
| `littlebot/info/weather.py`, `littlebot/info/clock.py` | the weather (Open-Meteo, place search, home from the dashboard / config / IP address, cache) and the time |
| `littlebot/dashboard/routes/info_routes.py`, `static/js/pages/settings.js` | the "Weather & time" card on the Settings page |
| `littlebot/media/search.py` | YouTube search through yt-dlp (no API key); `MediaController.search` / `play_video` play the results |
| `littlebot/chatgpt/look.py` | `CameraLook`: one photo + question to ChatGPT, with the privacy gates (section 4) |
| `littlebot/audio/wakeword.py` | `WakeWordService` (`ctx.wake`): "Hey LittleBot" heard offline with Vosk, then `ctx.live.start_session`; shares the microphone with `AudioService` (`attach_background_listener`) |
| `littlebot/dashboard/routes/chatgpt_routes.py`, `static/js/pages/ai.js` | the "ChatGPT account", "Live voice (GPT-Live)" and "Ask what LittleBot sees" cards (`POST /api/chatgpt/look` returns the answer text only, never the photo) |
| `littlebot/chatgpt/voice_record.py`, `littlebot/app/spoken_lines.py`, `littlebot/activities/lines.py` | recording the fixed lines in the GPT-Live voice, and working out which lines those are |
| `littlebot/audio/voice_lines.py` | `VoiceLineStore`: the recordings `AudioService.say` plays instead of text-to-speech |
| `scripts/record_voice_lines.py`, `dashboard/routes/voice_routes.py`, `static/js/pages/activities.js` | the command and the Activities page's "Activity voices" card |
| `data/voice_lines/` | the recorded lines (`<hash>.wav` + `index.json`) |
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
* **Barge-in and echo cancelling** (`tests/chatgpt/test_barge_in.py`, `test_live_barge_in.py`,
  `tests/audio/test_echo_cancel_devices.py`, `tests/test_echo_cancel_script.py`): the detector on
  synthetic signals (a grown-up at 90-230 Hz over the residual echo fires after 0.4 s; a 450 Hz
  babble, a 330 Hz squeal, a cough, a 0.25 s "mm", a quiet TV voice, the echo alone, a 20 dB louder
  echo and an echo jumping 6 dB up and down frame to frame don't; voice recognition's grown-up /
  child); the pitch estimate (85-600 Hz within 3 %); the "go ahead" phrases; the whole flow through
  `LiveAudio` on a fake PortAudio (the fade, the pre-roll frame by frame, the held voice, the drop
  until the next turn, the false alarm playing the held voice on, barge-in off, a stalled stream
  reopened); the call's hooks against the local WebRTC peer (instructions, the face listening at
  once, the "go ahead" note); the device choice with and without the echo-cancelled pair; the setup
  script against stand-ins for `pw-dump`, `wpctl` and `systemctl` (dry run, install twice, check,
  remove, the installer flag) and shellcheck. **Not tested on a real Pi or in a real room:** the
  script with Bookworm's real PipeWire 0.3.65 / WirePlumber 0.4.13, how well WebRTC cancels the USB
  speaker at the USB microphone (the more of it is left, the louder you must talk over it), the ALSA
  `pipewire` PCMs through PortAudio, YouTube in Chromium through the echo-cancelled sink, the
  detector with real voices, the baby, coughs and a TV, the timing of OpenAI's captions after
  someone cuts in, and the Pi 5's CPU.
* **Letting the AI see:** the dashboard route and card are tested with a stand-in for the camera
  look (answer text only reaches the browser, blocked → a plain reason, "say it out loud", the
  Privacy switch off by default) and in a phone-sized browser.
* **Asking LittleBot to do things** (`tests/chatgpt/test_tools.py`, `test_responses.py`,
  `test_live_tools.py`, `tests/brain/test_brain_tools.py`, `tests/media/test_media_search.py`): every
  tool against fakes; the function-call loop against mocked event streams (the replayed items, the
  round cap, a crashing tool); chat turns that queue a video / sleep until the reply is spoken; live
  hand-offs answered with tools, the interim answer and late note, quick answers, the first sentence early
  (`test_live_fast.py`), the hand-over after the call
  and STOP ALL cancelling it; search results playing in the real media controller (limits, age gate,
  next result on errors 101/150, skip); the yt-dlp child process with a stand-in script. One real
  search through yt-dlp worked from a development machine (2 s); not from the Pi, and the found
  videos have not been played in the Pi's kiosk.
* **Weather, time and web search** (`tests/info/`, `tests/chatgpt/test_info_tools.py`,
  `test_web_search.py`, `test_live_tools.py`, `tests/dashboard/test_dashboard_info.py`, the browser
  test): the weather against stand-ins for Open-Meteo and the IP services (home from the dashboard,
  config or IP address, the next IP service when one fails, the kept IP location, the 10-minute cache,
  the 3 s limit with the answer kept for next time, units, places with a region, errors, nothing sent
  with cloud off); the time in any zone; the tools; live hand-offs about the weather and the time going
  to the tools, not the camera, within the 5 s window; the dashboard card. The real Open-Meteo forecast
  and place search, and the three IP services, were called once from a development machine and their
  answers read correctly. Web search was tested only against mocked event streams (the request, the
  `web_search_call` items and citations, and every refusal falling back without it): **not against
  OpenAI's real endpoint**.
* **Wake word** (`tests/audio/test_wakeword.py`, `tests/dashboard/test_dashboard_wake.py`): a stand-in
  recogniser on a mock microphone that, like the Pi's USB microphone, can't be opened twice, with
  the real `AudioService`: earcon + exactly one start, "uh-oh" when live voice is blocked or doesn't
  connect, the cool-down, the hand-over to push to talk / the microphone test / live voice and back,
  the pauses (live voice, video, Privacy, switch off, LittleBot making sound), a missing model or
  library. The real Vosk recogniser was run once on a development PC with espeak-ng voices (it heard
  all "Hey LittleBot" / "Hey Bot" and none of the look-alikes); not on the Pi, and not with real voices.
  Saying it in one go: the words after the wake phrase (cut where Vosk says the phrase ends), no earcon
  while they talk, the hand-over at a pause, nothing kept when live voice can't start, and none of
  LittleBot's own sound; in `tests/chatgpt/test_live.py` and `tests/audio/test_wakeword_robot.py` those
  words reach the local WebRTC peer first, in real time, and the call then catches up. Once, on a
  development PC, the real Vosk and wake word on espeak-ng speech carried exactly "what's the weather
  today" from "Hey LittleBot, what's the weather today?" (checked by dictating the carried audio).
* **Wake word during a video** (`tests/audio/test_wakeword_video.py`, `tests/media/test_media_hold.py`,
  `tests/chatgpt/test_live_video.py`): the stricter rules during a video, the video paused (position,
  face back, player closed) before the earcon, the note to the voice, "keep watching" / "stop the
  video" from the person's words, a hand-off and the tools, the video carrying on when the chat
  can't start or nobody talks, the 2-minute stop after the chat, the duck. Over a video's sound the
  real Vosk recogniser was measured with synthetic voices and music only
  ([YOUTUBE_ON_PI.md section 10](YOUTUBE_ON_PI.md#what-was-measured-and-what-wasnt)).
* **Recorded voice lines** (`tests/chatgpt/test_voice_record.py`, `test_record_voice_lines.py`,
  `tests/activities/test_activity_lines.py`, `tests/audio/test_voice_lines.py`,
  `tests/dashboard/test_dashboard_voice_lines.py`): many lines in one call against the local WebRTC
  peer (the note per line, only the voice's own sound kept, a mismatch asked again, a line that never
  matched reported and not saved, a new call every N lines, stop, a refused call); the lines worked out
  from every built-in activity, checked against what the real engine says when it runs each one with
  random answers; the store; `say()` playing a recording with volume, mouth, events and stop, and text-
  to-speech for the rest; the command's flags and the dashboard routes. **Not tested against OpenAI's
  real voice**: whether it says each line word for word, how long its turns take (the time estimate is
  a guess) and how its transcripts spell things.
* **The hand-over to an activity** (`tests/audio/test_live_handover.py`): the whole robot with the
  real `AudioService` on a fake PortAudio whose USB speaker fails a sound started in the first 0.5 s
  after the call let it go; the activity's first line used to be cut off (and the speaker taken for
  unplugged), now it plays in full. Whether the Pi's real USB speaker behaves like that is not known.
* **Not yet tested:** OpenAI's real service (sign-in, text, live voice or photos), the Pi's real
  microphone, speaker and camera, echo in a real room, echo cancelling and barge-in, latency, and real
  plan limits.
