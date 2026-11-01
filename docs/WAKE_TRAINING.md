# Training LittleBot's own wake word

LittleBot listens for "Hey LittleBot" with Vosk, a general speech recogniser that is told to look for those words.
It works, but it can mishear (it heard "little" in other words, so `min_confidence` and extra phrases had to be
tuned). A wake word model trained only on "Hey LittleBot" is smaller, faster and should mishear less. This page is
how that model is made, on the robot itself, with [openWakeWord](https://github.com/dscripka/openWakeWord).

> **Status.** The first full run (1 November 2026, 03:16 to 06:52: 10,000 wake word clips, 8,000 near misses, a
> 32-wide network, 50,000 steps) caught 68% of the test clips at 0.5, with about 1 false alarm an hour: not good
> enough. It is kept in `~/wwtrain/hey_little_bot/run1`. The second run, started the same morning, has 40,000 wake
> word clips and 24,000 near misses, each heard in two different rooms, a 64-wide network and 200,000 steps (about
> 11 hours). LittleBot doesn't use either model yet: the better one will be tried on recordings first, next to Vosk,
> and only switched on if it beats it.

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

The model ends up in `~/wwtrain/hey_little_bot/hey_little_bot.onnx` (about 200 KB). LittleBot would run it with
onnxruntime and openWakeWord's two shared feature models (`melspectrogram.onnx`, `embedding_model.onnx`).

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

Nothing recorded in the house is used: every voice is made up. To make room again afterwards, delete
`~/wwtrain/data` (the 17 GB file is only needed for training).

Code: `scripts/wake_training/setup.sh`, `download.sh`, `train.py`, `run_all.sh`.
