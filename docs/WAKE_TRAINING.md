# Training LittleBot's own wake word

LittleBot listens for "Hey LittleBot" with Vosk, a general speech recogniser that is told to look for those words.
It works, but it can mishear (it heard "little" in other words, so `min_confidence` and extra phrases had to be
tuned). A wake word model trained only on "Hey LittleBot" is smaller, faster and should mishear less. This page is
how that model is made, on the robot itself, with [openWakeWord](https://github.com/dscripka/openWakeWord).

> **Status (6 November 2026).**
>
> | Run | Clips | Network, steps | Caught at 0.5 | False alarms an hour | Where |
> |---|---|---|---|---|---|
> | 1 (1 November, 03:16 to 06:52) | 10,000 wake word, 8,000 near misses | 32 wide, 50,000 | 68% | about 1.1 | `~/wwtrain/hey_little_bot/run1` |
> | 2 (1 November, 11:57 to 22:22) | 40,000 wake word, 24,000 near misses, each in two rooms | 64 wide, 200,000 | 78% | about 2.5 | `~/wwtrain/hey_little_bot/run2` |
> | 3 (3 November 15:03 to 4 November 13:12, stopped and carried on twice) | as run 2, made again, plus 111 of Ali's own (89 trained on, 22 held back) | 64 wide, 200,000 | 78% | about 8 | `~/wwtrain/hey_little_bot` |
>
> On the 22 held-back clips of Ali's own voice (each in 4 rooms) run 3 catches **72%** at 0.5, run 2 only **15%**:
> the made-up voices alone hardly knew a real voice. But run 3 also has about 8 false alarms an hour on everyday sound
> (still 4.2 at 0.9), against run 2's 2.5. **On the robot since 6 November 2026, 02:56**, in `data/wake/` in place
> of run 2 (still primary at 0.45, with `wake.oww_primary_agree` holding back the model's wakes Vosk heard nothing
> of). Run 2 is in `data/wake/run2/`; `data/wake/MODELS.md` has the one line to go back.
>
> On its own no run is good enough to replace Vosk. The model wakes LittleBot first ([primary
> mode](#primary-mode-on-the-robot-the-owners-choice) below), but only when Vosk heard part of the wake phrase or a
> face is in view, and Vosk stays the backup.

## How it works, in plain words

1. **Made-up voices.** A free voice generator (LibriTTS-R, 904 speakers, mixed two at a time) says "hey little
   bot" and "hey lil bot" 40,000 times in different voices and speeds. It also says about 80 things that sound
   close but aren't it, 24,000 times: "hey little boat", "a little bit", "hey Bob", "little bot" without the
   "hey" and so on.
2. **Real rooms.** Each clip gets a real room's echo (270 recorded rooms) and everyday background sound (AudioSet:
   talk, TV, music, kitchens) at a random loudness, so it sounds like a living room, not a studio. Each training
   clip is done twice, in two different rooms with two different sounds.
3. **Training.** openWakeWord's own trainer teaches a small network to say yes to the wake word and no to the near
   misses and to about 2,000 hours of everyday sound. It keeps the version that catches the most wake words with
   the fewest false alarms, measured on 11 more hours it never trained on.
4. **Check.** It counts how many held-back "hey little bot" clips it catches, how many false alarms an hour, and
   which near misses fool it, at a few thresholds (`check.json`).

## Running it

Everything is in `~/wwtrain`, with its own Python environment: LittleBot's own is not touched. It needs about
23 GB of disk (the robot's card had 34 GB free before) and no sudo.

```
bash ~/LittleBot/scripts/wake_training/setup.sh       # the tools, ~2 GB, a few minutes
cd ~/wwtrain && (nohup bash ~/LittleBot/scripts/wake_training/download.sh > prep.log 2>&1 < /dev/null &)
```

The download is about 19 GB and takes about an hour. The training can start at the same time: `run_all.sh` makes
the voices and features first and waits for the download before the training itself. Every step runs with a memory
limit, so it can't take the robot down (see below):

```
cd ~/wwtrain && (nohup bash ~/LittleBot/scripts/wake_training/run_all.sh > train.log 2>&1 < /dev/null &)
```

Watch it with `tail -f ~/wwtrain/train.log`.

| Step | Time on the Pi 5 (from the first run) | What you see in `train.log` |
|---|---|---|
| `voices` | about 2.6 wake word clips and 1.9 near misses a second: 6 hours from nothing | `positive_train: 2000 of 40000` ... |
| `features` | about 26 clips a second: under 2 hours | `features: positive_train ...` |
| `train` | about 20 steps a second: 3 to 4 hours for 200,000 | openWakeWord's progress bars, then `train: saved ... hey_little_bot.onnx` |
| `check` | a few seconds | the catch rate and false alarms per hour |

To train again with other settings, move the old model out of `~/wwtrain/hey_little_bot` first (the `train` step
skips if `hey_little_bot.onnx` is there). More clips: raise `COUNTS` in `train.py`; only the missing clips are made,
and the features are made again when their count doesn't match. `--steps` and `--layer-dim` change the training.

Each step can be run on its own (`train.py voices`, `features`, `train`, `check`) and carries on where it stopped.
`train.py all --smoke` is a 10-minute test with 48 clips of each kind (in `~/wwtrain/smoke`).

The model ends up in `~/wwtrain/hey_little_bot/hey_little_bot.onnx` (about 400 KB for run 2). LittleBot runs it
with onnxruntime and openWakeWord's two shared feature models (`melspectrogram.onnx`, `embedding_model.onnx`), see
below.

## Shadow mode: the model next to Vosk

LittleBot keeps hearing "Hey LittleBot" with Vosk, exactly as before. With `wake.oww_mode: shadow` the trained
model hears the same microphone sound on its own thread (`littlebot/audio/oww.py`) and writes down what it would
have said:

* **Every time Vosk wakes LittleBot:** Vosk's word confidence and the model's best score in the 2 seconds before
  (and up to 0.6 s after). In the log: `wake model: Vosk woke it (word confidence 0.83); the model's score: 0.91 at
  the wake, 0.95 up to 0.6 s after (shadow: it changes nothing)`.
* **How the chat after it went,** once it ends: a real request, dismissed at once ("stop"), nobody said anything
  (most likely a false wake), or it didn't connect. In the log: `wake model: after the wake at 18:02:11: it became
  a real request (2 turns, 35 s)`.
* **When the model scores 0.5 or more and Vosk doesn't wake:** `wake model: score 0.72 with no Vosk wake`, and
  whether Vosk heard a wake phrase there but wasn't sure enough (perhaps a wake Vosk missed).
* Once an hour, how much sound it scored, its highest score and the CPU it took.

All of it also goes to `data/wake/shadow.jsonl`, one line each: the time, the numbers and the outcome. **No sound
and no words** are ever kept or logged by it.

It runs openWakeWord's own steps (the same as its `AudioFeatures` and `Model.predict`): 80 ms of sound at a time
→ mel spectrogram → 96-number embedding → the last 16 embeddings (1.28 s) → a score from 0 to 1. On the robot its
scores were the same as the openwakeword package's to 6 decimals on 40 test clips. It takes about 5 to 6% of one
CPU core of the Pi 5 while the wake word listens (almost all of it the shared embedding model), and only then.

If anything is wrong with it (a file missing, onnxruntime not installed, an error), it switches itself off with one
warning (`wake model: switched off (...)`) and the wake word goes on with Vosk alone.

### Setting it up

The files live in `data/wake/` (not in git): `hey_little_bot.onnx`, `melspectrogram.onnx`, `embedding_model.onnx`,
`check.json` and `MODELS.md` (where they came from). onnxruntime is the `wake-model` extra:

```
.venv/bin/python -m pip install -e '.[wake-model]'
mkdir -p data/wake && cp ~/wwtrain/hey_little_bot/{hey_little_bot.onnx,check.json} \
    ~/wwtrain/openWakeWord/openwakeword/resources/models/{melspectrogram,embedding_model}.onnx data/wake/
.venv/bin/python scripts/set_config.py wake.oww_mode shadow
systemctl --user restart littlebot
```

| Setting | Default | What it does |
|---|---|---|
| `wake.oww_mode` | `off` | `off`, `shadow` (log only), `confirm`, `rescue` or `primary` (see below). The robot has `primary` (since 2026-11-02). |
| `wake.oww_primary_threshold` | `0.45` | primary: the model's score that wakes LittleBot |
| `wake.oww_primary_agree` | `true` | primary: only with a second opinion (Vosk heard part of a wake phrase, or a face is in front of the camera; see below) |
| `wake.oww_threshold` | `0.5` | confirm: the score Vosk's wake needs |
| `wake.oww_rescue_threshold` | `0.7` | rescue: the model's score that wakes it when Vosk wasn't sure |
| `wake.oww_rescue_seconds` | `5.0` | rescue: how far back the model's score is looked at (Vosk's result comes after the sentence) |
| `wake.oww_peak_threshold` | `0.5` | a score this high with no Vosk wake is logged |
| `wake.oww_window_seconds` | `2.0` | how far back before Vosk's wake the model's score is looked at |
| `wake.oww_confirm_wait_seconds` | `0.3` | confirm: how long to wait for the model to catch up |
| `wake.oww_model_dir` | `wake` | the folder in `data/` with the files |

### Reading the report

Use LittleBot normally for a few days, then:

```
.venv/bin/python scripts/wake_shadow_report.py
.venv/bin/python scripts/wake_shadow_report.py --since 2026-11-03     # only from that day on
```

It says how many times Vosk woke LittleBot and what happened after (real requests, dismissed, nobody spoke), how
many of those wakes the model agreed with at 0.1, 0.3, 0.5, 0.7 and 0.9, how many peaks of its own it had per hour
of listening, and which threshold would have kept every real request and dropped the most false wakes. "Nobody
said anything" and "dismissed" count as false wakes, "a real request" as a real one. With fewer than 20 wakes it
says to wait.

What to look for: if the model scores high on (nearly) every real request and low on the false wakes, confirm mode
would cut Vosk's mishears without missing you. If it scores low on real requests too, it isn't ready.

### Confirm mode, later

With `wake.oww_mode: confirm`, Vosk's wake only counts when the model's best score in the last 2 seconds is at least
`wake.oww_threshold`; otherwise the log says `... below wake.oww_threshold 0.50: not waking` and LittleBot keeps
listening. If the model can't give a score (still loading, switched off), Vosk decides alone, as now. Confirm looks
at the score at the moment Vosk wakes ("at the wake" in the report); with `wake.early` Vosk can wake just before the
phrase ends, so compare that column, not the "0.6 s later" one. It is built and tested but **not switched on**:
only once the report shows it helps, and with your OK:

```
.venv/bin/python scripts/set_config.py wake.oww_threshold 0.5     # the report's suggestion
.venv/bin/python scripts/set_config.py wake.oww_mode confirm
systemctl --user restart littlebot
```

To go back: `scripts/set_config.py wake.oww_mode shadow` (or `off`) and restart.

### Rescue mode (on the robot)

The other way round: Vosk still wakes LittleBot on its own, and when Vosk heard a wake phrase but wasn't sure of it
(an unsure "hey bot", "... bot" with "bot" not the first word, a word below `wake.min_confidence`), a model score of
at least `wake.oww_rescue_threshold` in the last `wake.oww_rescue_seconds` wakes it all the same. Not while music
plays. The log says `Vosk wasn't sure of 'hey bot', but the wake model's score is 0.90 ...: waking`. On the robot's
log of 2026-11-02 seven first tries that needed a second or third go had both (an unsure Vosk phrase and a model
peak); the model alone was not used, as it also peaked about four times an hour with nobody waking it.

### Primary mode (on the robot, the owner's choice)

The model first: its score reaching `wake.oww_primary_threshold` (0.45) wakes LittleBot by itself, at once, with
what was said after it carried to the chat as for Vosk's early wake. Vosk stays the backup, with the robot's
`wake.min_confidence` raised to 0.6 (an unsure "hey bot" still needs `wake.short_min_confidence`, 0.7). Not while a
video plays: then only Vosk's stricter video rules wake it. The log says `wake model: it woke LittleBot (score
0.62, ...)`, and the journal has a `model_wake` line with how the chat went after it ("nobody said anything" is a
false wake). On 2026-11-02 the model scored 0.5 or more about four times an hour with nobody waking LittleBot, so
expect some wakes on their own; each listens for a few seconds and closes. Back to Vosk first:
`scripts/set_config.py wake.oww_mode rescue` (or `shadow`) and restart.

**A second opinion (since 3 November).** Alone, the model woke LittleBot about 50 times in its first day with
nobody speaking after (a cricket game on the TV, the room's noise; scores up to 0.99), against about 15 real
requests. In those, Vosk had heard nothing of a wake phrase; when someone says it, Vosk hears at least part of it.
So with `wake.oww_primary_agree` (on by default) the model's wake counts only when Vosk heard a word of a wake
phrase ("little", "lil" or "bot", sure of it or not) in the same few seconds (`AGREE_SECONDS`, 3), or someone's
face is in front of the camera (`wake.short_face_seconds`). Otherwise the log says `wake model: score 0.97, but Vosk
heard no part of a wake phrase and nobody is in front of the camera: not waking`, and the journal has a
`model_wake` line with `"blocked": true`. A wake that went through says what agreed (`; agreed: Vosk` or `; agreed:
a face`). The model alone again: `scripts/set_config.py wake.oww_primary_agree false` and restart.

Vosk's own mishears on TV speech are not caught this way (on election night it heard "lil bot" at 0.82-1.00 in a
newsreader's words, the model scoring 0.00). For those the brain is told that the first words after a wake may be
the TV, a video, a game or people talking to each other, and to end the chat without a word if they plainly are.

## Memory: run it with the limit

The robot has 4 GB and LittleBot itself uses about 800 MB. On 1 November 2026 a step of the download that read a
whole 700 MB file at once, at the same time as a test run, filled the memory and the swap, and the robot restarted
by itself. The download now reads that file a little at a time, and every heavy step runs under `systemd-run
--user --scope -p MemoryMax=...`: if it ever wants more, only the training stops, never LittleBot or the robot.

## The data, and where it comes from

All public, for research and training:

| What | Size | From |
|---|---|---|
| Everyday sound as features (~2,000 hours) | 17.3 GB | huggingface.co/datasets/davidscripka/openwakeword_features |
| 11 more hours to count false alarms | 0.2 GB | the same |
| 270 room echoes | 8 MB | huggingface.co/datasets/davidscripka/MIT_environmental_impulse_responses |
| Background sound (AudioSet, 2 parts) | 1.4 GB | huggingface.co/datasets/agkphysics/AudioSet |
| The voice generator (LibriTTS-R) | 200 MB | github.com/rhasspy/piper-sample-generator |

Runs 1 and 2 used nothing recorded in the house: every voice was made up. To make room again afterwards, delete
`~/wwtrain/data` (the 17 GB file is only needed for training).

## The family's own clips

The Assistant page's "Wake word: your voices" card takes a recording made on a phone or PC (any format PyAV reads,
up to 15 minutes / 50 MB) of someone saying "hey little bot" over and over, with or without breaks. On the robot,
Vosk held to the wake phrases marks each word, and every "hey little bot" is cut out as a 16 kHz WAV in
`data/wake_clips/<name>/` (littlebot/audio/wake_clips.py). Only the clips are kept, never the recording, and nothing
leaves the robot. About 100 clips per person is enough. Run together, a free Vosk found none of 20 phrases and the
held one found all 20 (espeak, 2026-11-03).

The training uses them (from run 3): the `features` step reads `~/LittleBot/data/wake_clips/` (or `$WAKE_CLIPS`),
holds back every 5th clip of each person for the check and puts each of the others in 100 rooms and sounds (the
made-up ones get 2); `train` puts 8 of them in every batch next to the 50 made-up ones; `check` adds how many of
the held-back ones it catches (`family_caught` in `check.json`, and `the family's own clips caught` in the log).
With the made-up clips and their features still in `~/wwtrain/hey_little_bot`, only the family's features are new
(a few minutes), so a run is about the training's 3 to 4 hours. Run 2's were deleted on 3 November (at 01:25), so
run 3 makes them again: about 13 hours in all (voices 7 to 8, features 1.5, training 3.6). Move the last model out
first (the robot's own copy in `data/wake/` is not touched):

```
cd ~/wwtrain/hey_little_bot && mkdir -p run2 && mv hey_little_bot.onnx hey_little_bot.pt check.json run2/
cd ~/wwtrain && (nohup bash ~/LittleBot/scripts/wake_training/run_all.sh > train.log 2>&1 < /dev/null &)
```

A new recording changes how many clips there are, so their features are made again on the next run.

Code: `scripts/wake_training/setup.sh`, `download.sh`, `train.py`, `run_all.sh`; on LittleBot `littlebot/audio/oww.py`
and `scripts/wake_shadow_report.py`.
