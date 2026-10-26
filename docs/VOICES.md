# Recognising family voices

LittleBot can learn the voices of the people you added under **Who's who** (the same list as
face recognition), so that in a voice chat it knows which grown-up is talking, from "Hey
LittleBot" on. It is **off by default** and happens entirely on the robot: only a voiceprint
(192 numbers per sample) is kept, never a recording.

Code: `littlebot/voices/` (`service.py` is `ctx.voices`; `call.py` is what a live voice call does
with it). Privacy summary: [AI_SAFETY.md §4](AI_SAFETY.md#4-privacy).

## 1. Set it up

On the robot (once):

```
.venv/bin/python -m pip install -e '.[voices]'            # sherpa-onnx (~17 MB, prebuilt for the Pi)
.venv/bin/python scripts/download_models.py voices        # the speaker model (26 MB, checksum verified)
systemctl --user restart littlebot
```

Then on the dashboard:

1. **Privacy** → switch on **Recognise family voices** (the **Microphone** switch must be on too).
2. **Profile** → **Who's who**: add the grown-ups if they aren't there yet (a name and a role such
   as "dad").
3. Press **Record voice** next to a person. The three sentences appear on the screen; that person
   reads them out loud to LittleBot, about an arm's length away, in a quiet room (about 20
   seconds). Only they should talk. The row then shows **Voice: set up (4 samples)**. Recording
   again adds samples (at most 8 from recordings; the oldest go first).

Until the package and model are installed, Who's who says what to run instead of a Record
button that can't work.

**Learning in a chat.** In a voice chat a grown-up can also just say who they are: "I'm Ali",
"it's Ali", "this is her dad". LittleBot then keeps a voiceprint of what they said in that turn
(or in their next one, if "I'm Ali" alone was too short), if it was clearly one voice and, when
Ali already has voiceprints, not clearly someone else's voice. The first time, the voice may say
"I'll recognise your voice now". A name that isn't on Who's who is never added: the voice is told
that a grown-up can add them on the dashboard. When LittleBot is very sure who is talking, it
keeps that voiceprint too, so it gets better over time (at most one every 2 minutes per person,
at most 8 learned per person, the oldest go first; recordings are never pushed out by these).
The little one's voice is never learned in a chat.

## 2. What it does in a voice chat

* **Who woke it.** With the switch on, the wake word keeps "Hey LittleBot" itself (and at most 5 s
  of what followed) in memory; while the call connects, LittleBot works out whose voice it was.
  If it is sure in time (1.5 s), the call's instructions say "The grown-up who woke you sounds like
  Ali (dad), by voice."; if it is sure a little later, the voice gets that as one quiet note.
* **Who is talking now.** The call keeps a copy of what the microphone sends since the last turn
  (at most 20 s, in memory). When ChatGPT's transcript says their turn has ended, LittleBot
  identifies that stretch. When it is a grown-up the voice wasn't told about yet, ONE quiet note
  goes to the voice, at a moment when nobody is talking: "By voice, the grown-up talking now is
  Ali (dad). Don't announce this; just know it." Nothing is said for the same person again, for
  someone it isn't sure about, or for the little one.
* **"Do you know who I am?"** The answer puts together face recognition (if it is on and can look)
  and the voice. When they agree, the voice hears both; when the face says Ali but the voice
  sounds like Sara, the voice is told they disagree and to ask, never to guess.
* **When unsure, nothing.** Too little speech (under about 1.5 s of talking), a voice that isn't
  clearly one person's, two people at once: LittleBot says nothing about who it is.

### The `voice.speaker` event (for other parts of LittleBot)

Every identified turn (and the wake phrase) publishes `Topics.VOICE_SPEAKER` (`"voice.speaker"`,
`littlebot/core/events.py`) on the event bus:

```
{"person_id": 1, "name": "Ali", "role": "dad", "is_child": false, "score": 0.71,
 "speech_s": 1.8, "source": "turn"}          # source: "wake" | "turn"
```

When LittleBot isn't sure (or heard too little), `person_id`, `name` and `role` are None and
`is_child` is None; `score` is still the closest voice's similarity. It is published on every
identified turn, not only on a change, never carries sound, and only while the switch is on and
someone has recorded a voice. `is_child` follows the same rule as faces (the role says "baby" /
"child" / "toddler" ..., or the name is the child's nickname).

## 3. Privacy

* **What is kept, where:** table `voice_samples` in LittleBot's database on the robot
  (`data/littlebot.db`): per sample, the voiceprint (192 numbers from the speaker model, 768
  bytes), which model made it, whether it came from **Record voice** or was learned in a chat, and
  when. The table only accepts rows of at most 2 KB, far too small for sound. **No recording is
  ever stored**: the dashboard's 20 s recording, the wake phrase and the call's copy of a turn are
  looked at in memory and dropped.
* **What leaves the robot:** never sound or voiceprints (they are never logged or shown either;
  logs say counts and scores only, never names). During a voice chat, only the **name and role**
  of who is talking go to the voice (OpenAI), as for faces. With `ai.share_child_name` off, the
  child's nickname in them becomes "the little one". (The call's microphone audio goes to OpenAI
  anyway, switch or no switch: that is how live voice works.)
* **Switched off:** nothing is processed at all. The wake word keeps no copy, the call buffers
  nothing, no voiceprint is computed, and the model is unloaded. Switching it off during a call
  clears the call's copy at once and tells the voice it no longer knows who is talking. The
  voiceprints stay until you delete them.
* **How to delete:** Profile → Who's who: **Forget voice** next to a person deletes their
  voiceprints; the bin deletes the person with their face samples and voiceprints; **Forget all
  faces** deletes everyone, faces and voices. Deleted rows are overwritten in the database file
  (`secure_delete`).

## 4. How well it works, and what it costs

The model is 3D-Speaker's ERes2Net (trained on VoxCeleb, Apache-2.0), run with
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx). It turns up to 3 s of speech (pauses left
out) into 192 numbers. A person is the average of their samples; a voice is theirs when the cosine
similarity is at least `voices.match_threshold` (0.5) **and** at least 0.10 more than to anyone
else. Under 1 s of actual speech (about 1.5 s of talking) it doesn't try; callers that ask for
less (down to 0.6 s) get a threshold 0.05 stricter.

Measured on an x86 development machine (Xeon 2.8 GHz, 2 threads), **not on the Pi** (expect
roughly 2-3 times slower there):

| | |
|---|---|
| Loading the model | ~0.3 s, ~75 MB of RAM |
| One voiceprint of 1 s / 1.5 s / 2 s / 3 s of speech | ~30 / ~45 / ~65 / ~110 ms (+~20 MB while computing 3 s) |
| Memory in use after a session of enrolling and identifying | ~130-140 MB above what LittleBot used before |

The model loads when it is first needed ("Hey LittleBot" loads it straight away when someone has a
voiceprint) and is dropped after 2 minutes unused or when switched off.

On a small public set of real recordings (sherpa-onnx's speaker-recognition test data: three men,
Mandarin, enrolled from 5-9 s each), resampled to the call's 48 kHz: of 2 s clips 13 of 14 were
named correctly and 1 was "unsure"; of 1.5 s clips 14 of 19 correct, 3 unsure, 2 too short; none
was ever named as the wrong person. That is a tiny sample. **Not verified:** real family voices
on the robot's USB microphone across a room, children's voices, a cold or a whisper, how often
it is unsure in practice, and the Pi's timing and memory.

## 5. Troubleshooting

| What you see | What to do |
|---|---|
| "The voice recognition package isn't installed yet." | `.venv/bin/python -m pip install -e '.[voices]'`, then `systemctl --user restart littlebot` |
| "The voice model isn't downloaded yet." | `.venv/bin/python scripts/download_models.py voices` |
| "The voice model couldn't be loaded." | `.venv/bin/python scripts/download_models.py voices --force`, then switch it off and on in Privacy |
| "LittleBot heard only N seconds of talking." | Read all three sentences, closer to the robot; check **Audio & devices → Test microphone** |
| "That didn't sound like one voice." | Only that person talks, in a quiet room, no TV. One odd piece (a cough, the baby) is left out by itself; more than a third of the recording unlike the rest is not. The log line "voice recognition: recording of ... piece(s), each like the rest [...]" shows how alike the pieces were (`grep -a "piece(s)" data/logs/littlebot.log`) |
| "That sounds more like Sara than Ali." | Someone else read the sentences, or their voices are very alike; try again with only Ali |
| "The microphone is in use by live voice" | End the voice chat first; the recording needs the microphone |
| It never says who is talking | Check the switch and that the person shows **Voice: set up**; the log says `live voice: the wake phrase was not recognised (unsure)` when it wasn't sure. Record again in the room where you talk to it, at the distance you usually talk from |
| It names the wrong person | Delete that person's voice (**Forget voice**) and record again; tell us (it should never guess). You can raise `voices.match_threshold` (0.3-0.9) in `config/local.yaml` |

## 6. For developers

`ctx.voices` (`littlebot.voices.get_voices(ctx)`) is a `VoiceRecognition`; it may be None in a
context the robot didn't build, so check:

* `await identify(samples, rate, *, min_speech_s=1.0) -> VoiceMatch | None`: mono float (-1..1) or
  int16 at any rate (resampled to 16 kHz). `VoiceMatch(person_id, name, role, score, is_child,
  margin)`. None when switched off, nobody has a voiceprint, too little speech, or unsure.
  Thread-safe; `identify_now(...)` is the blocking version for a worker thread. Uses the last 3 s
  of speech.
* `await analyse(samples, rate, *, min_speech_s, take="last"|"first", one_voice=False) -> Heard`:
  the same with the reason (`ok`, `unsure`, `too_short`, `nobody`, or why it is blocked).
* `await enrol(person_id, samples, rate)`, `await learn(person_id, samples, rate)`,
  `improve(heard)`, `forget(person_id)`, `status()`, `view()`, `blocked_reason()`.

Config (`voices:` in `config/*.yaml`): `model` (path, default
`models/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx`), `match_threshold` (0.5), `threads`
(2). Tests use `littlebot.testing.FakeSpeakerEngine` (fake speaker k is a steady tone) and
`speaker_tone`; `tests/voices/test_voices_real_model.py` runs the real model when it is installed.
