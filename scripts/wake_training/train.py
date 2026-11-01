#!/usr/bin/env python3
"""Trains LittleBot's own wake word, "Hey LittleBot", with openWakeWord (docs/WAKE_TRAINING.md).

Runs on the robot, in the training environment that setup.sh made (download.sh first):

    cd ~/wwtrain && (nohup bash ~/LittleBot/scripts/wake_training/run_all.sh > train.log 2>&1 < /dev/null &)

(run_all.sh runs each step under a memory limit, so a step that wants too much stops on its own instead of taking
the robot down, and waits for download.sh before the training itself).

The steps, each safe to run again (it carries on where it stopped):

* ``voices``: thousands of made-up voices saying "hey little bot" (and "hey lil bot"), and as many saying things that
  sound close but aren't it ("hey little boat", "a little bit", "hey Bob", "little bot" without the "hey"...), with the
  LibriTTS-R voice generator (904 speakers, mixed two at a time, at different speeds);
* ``features``: each clip put in a real-sounding room (room echoes), under everyday background sound (AudioSet: talk,
  TV, music, kitchens), at a random loudness, then turned into openWakeWord's features (what the model looks at);
* ``train``: openWakeWord's own training (``Model.auto_train``): a small network learns to say yes to the clips of the
  wake word and no to the near misses and to ~2,000 hours of everyday sound; the version with the fewest false
  alarms per hour on 11 more hours (that it never trained on) and the most wake words caught is kept;
* ``check``: how many of the held-back "hey little bot" clips it catches, how many false alarms an hour, and which
  near misses fool it, at a few thresholds. Written to ``check.json`` next to the model.

The model ends up in ~/wwtrain/hey_little_bot/hey_little_bot.onnx (~200 KB). LittleBot runs it with
openWakeWord's two shared feature models (melspectrogram.onnx, embedding_model.onnx) and onnxruntime.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import math
import os
import random
import sys
import time
import types
import uuid
import wave
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(os.environ.get("WWTRAIN", Path.home() / "wwtrain"))
DATA = ROOT / "data"
GEN_REPO = ROOT / "piper-sample-generator"
GENERATOR = GEN_REPO / "models" / "en_US-libritts_r-medium.pt"
FEATURE_MODELS = ROOT / "openWakeWord" / "openwakeword" / "resources" / "models"
MODEL_NAME = "hey_little_bot"
OUT = ROOT / MODEL_NAME
EVERYDAY = DATA / "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"  # what it must not answer: ~2,000 hours
RATE = 16000

# How many clips of each kind. On the Pi 5 the generator makes about 2 a second (3 threads).
COUNTS = {"positive_train": 10000, "positive_test": 1500, "negative_train": 8000, "negative_test": 1500}

# What the wake word is. Repeats weigh the usual way of saying it more.
POSITIVE = ["hey little bot", "hey little bot", "hey little bot", "hey little bot.", "hey, little bot",
            "hey little bot!", "hey lil bot", "hey lil bot", "hey littlebot"]

# Near misses it must not answer, on top of openWakeWord's own phonetic neighbours of "hey little bot".
NEAR_MISSES = """hey little | little bot | a little bot | the little bot | that little bot | hey bot | hey bob | hey robot
| hey little robot | little robot | hey little boat | little boat | hey little bit | a little bit | just a little bit
| hey middle bot | hey little buddy | hey little one | hey little boy | hey little girl | hey lady | hey libby
| hey little dot | hey little pot | hey little hot | it's a little hot | hey lil bit | little by little
| a little bottle | hey listen | hey look | hey baby | hey google | alexa | hey siri | hey little brother
| hey little sister | hey let's go | hey little bunny | hey kiddo | hey little guy | hey little bear | hey there buddy
| hey little bird | a lot | hey what's up | they'll lit a pot | hey little bud | hey little butt | hey little bat
| hey little bed | hey little ball | hey little bottle | hey little doll | play little bot | stay little bot
| hey lil | hey there | okay little | hey bobby | hey buddy | hey but | hey little cat | hey little dog
| hey it's a lot | hey lolly | a lil bot | little bots | hey littler | hey letter box | hey little box""".replace("\n", " ")
NEAR_MISSES_LIST = [t.strip() for t in NEAR_MISSES.split("|") if t.strip()]

log = logging.getLogger("wake_training")


# ---------------------------------------------------------------- helpers
def _stub_missing() -> None:
    """openwakeword.data imports a few libraries only its own augmentation uses (this script augments by itself):
    stand-ins when they aren't installed, so the parts used here import."""
    stubs = {"speechbrain": [], "speechbrain.dataio": [], "speechbrain.dataio.dataio": ["read_audio"],
             "speechbrain.processing": [], "speechbrain.processing.signal_processing": ["reverberate"],
             "acoustics": [], "torch_audiomentations": []}
    for name, attrs in stubs.items():
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - any import problem: use the stand-in
            mod = types.ModuleType(name)
            for a in attrs:
                setattr(mod, a, _not_here)
            sys.modules[name] = mod


def _not_here(*_a, **_k):
    raise RuntimeError("not installed in the training environment")


def wav_count(d: Path) -> int:
    return sum(1 for _ in d.glob("*.wav")) if d.is_dir() else 0


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Any WAV (16-bit, 24-bit, float: the room echoes are 32-bit float) as mono float32."""
    import soundfile  # noqa: PLC0415

    x, rate = soundfile.read(str(path), dtype="float32", always_2d=True)
    return x.mean(axis=1), int(rate)


def to_16k(x: np.ndarray, rate: int) -> np.ndarray:
    if rate == RATE:
        return x
    from scipy.signal import resample_poly  # noqa: PLC0415

    g = math.gcd(RATE, rate)
    return resample_poly(x, RATE // g, rate // g).astype(np.float32)


def manifest_path(kind: str) -> Path:
    return OUT / f"{kind}.jsonl"


# ---------------------------------------------------------------- 1. voices
def generate(kind: str, texts: list[str], total: int, threads: int) -> None:
    """``total`` clips of ``texts`` in OUT/kind/ (only the ones missing), with random pairs of speakers mixed."""
    import torch  # noqa: PLC0415

    out = OUT / kind
    out.mkdir(parents=True, exist_ok=True)
    have = wav_count(out)
    if have >= total * 0.98:
        log.info("%s: %d clips already", kind, have)
        return
    sys.path.insert(0, str(GEN_REPO))
    from piper_sample_generator.__main__ import audio_float_to_int16, generate_audio, get_phonemes  # noqa: PLC0415

    torch.set_num_threads(threads)
    model = torch.load(GENERATOR, weights_only=False)
    model.eval()
    config = json.loads(Path(f"{GENERATOR}.json").read_text(encoding="utf-8"))
    voice, rate, speakers = config["espeak"]["voice"], config["audio"]["sample_rate"], config["num_speakers"]
    rng = random.Random(f"{kind}-{have}")
    # The test clips use other speakers than the training ones where they can (the last 100 of the 904).
    pool = list(range(speakers - 100, speakers)) if kind.endswith("_test") else list(range(speakers - 100))
    batch = 8 if kind.startswith("positive") else 4
    t0, made = time.monotonic(), 0
    with manifest_path(kind).open("a", encoding="utf-8") as man:
        while have + made < total:
            n = min(batch, total - have - made)
            batch_texts = [rng.choice(texts) for _ in range(n)]
            s1 = torch.LongTensor([rng.choice(pool) for _ in range(n)])
            s2 = torch.LongTensor([rng.choice(pool) for _ in range(n)])
            ids = [get_phonemes(voice, config, t, False, False) for t in batch_texts]
            width = max(len(i) for i in ids)
            ids = [i + [1] * (width - len(i)) for i in ids]
            with torch.no_grad():
                audio, phoneme_samples = generate_audio(
                    model, s1, s2, ids, slerp_weight=rng.choice((0.3, 0.5, 0.7)),
                    noise_scale=rng.choice((0.667, 0.8, 0.98)), noise_scale_w=rng.choice((0.8, 0.98)),
                    length_scale=rng.choice((0.75, 0.85, 1.0, 1.1, 1.25)), max_len=None)
            for i in range(audio.shape[0]):
                audio[i, 0, int(phoneme_samples[i].flatten().sum().item()) + 1:] = 0
            pcm = audio_float_to_int16(audio.cpu().numpy())
            for i in range(pcm.shape[0]):
                data = np.trim_zeros(pcm[i].flatten())
                if len(data) < rate // 4:
                    continue
                name = uuid.uuid4().hex + ".wav"
                with wave.open(str(out / name), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(rate)
                    w.writeframes(data.tobytes())
                man.write(json.dumps({"file": name, "text": batch_texts[i]}) + "\n")
                made += 1
            if made and made % 400 < n:
                rate_s = made / max(1e-6, time.monotonic() - t0)
                log.info("%s: %d of %d (%.1f a second, ~%.0f min to go)", kind, have + made, total, rate_s,
                         (total - have - made) / max(rate_s, 1e-6) / 60)
    log.info("%s: done, %d clips", kind, have + made)


def negative_texts(n: int) -> list[str]:
    _stub_missing()
    from openwakeword.data import generate_adversarial_texts  # noqa: PLC0415

    near = generate_adversarial_texts(input_text="hey little bot", N=n, include_partial_phrase=1.0,
                                      include_input_words=0.2)
    # About a third are the hand-picked near misses, the rest openWakeWord's phonetic neighbours.
    return NEAR_MISSES_LIST * max(1, n // (3 * len(NEAR_MISSES_LIST))) + near


def step_voices(threads: int) -> None:
    generate("positive_train", POSITIVE, COUNTS["positive_train"], threads)
    generate("positive_test", POSITIVE, COUNTS["positive_test"], threads)
    neg = negative_texts(COUNTS["negative_train"])
    generate("negative_train", neg, COUNTS["negative_train"], threads)
    generate("negative_test", negative_texts(COUNTS["negative_test"]) + NEAR_MISSES_LIST * 5, COUNTS["negative_test"],
             threads)


# ---------------------------------------------------------------- 2. features
class Augmenter:
    """A clip in a room, under background sound, at a random loudness: like the robot's microphone hears it."""

    def __init__(self, rng: np.random.Generator) -> None:
        self.rng = rng
        self.rirs = [to_16k(*read_wav(p)) for p in sorted((DATA / "rirs").glob("*.wav"))]
        self.background = sorted((DATA / "background").glob("*.wav"))
        if not self.rirs or not self.background:
            raise SystemExit("room echoes or background sounds are missing: run download.sh first")

    def _background(self, n: int) -> np.ndarray:
        for _ in range(5):
            x, rate = read_wav(self.background[int(self.rng.integers(len(self.background)))])
            x = to_16k(x, rate)
            if len(x) >= n and np.sqrt(np.mean(x ** 2)) > 1e-4:
                start = int(self.rng.integers(0, len(x) - n + 1))
                return x[start:start + n]
        return np.zeros(n, dtype=np.float32)

    def __call__(self, clip: np.ndarray, total: int) -> np.ndarray:
        from scipy.signal import butter, fftconvolve, sosfilt  # noqa: PLC0415

        rng = self.rng
        speech = clip.astype(np.float32)
        if rng.random() < 0.5:  # a room
            rir = self.rirs[int(rng.integers(len(self.rirs)))]
            wet = fftconvolve(speech, rir)[:len(speech) + RATE // 4]
            speech = (wet * (np.sqrt(np.mean(speech ** 2)) / max(1e-6, np.sqrt(np.mean(wet ** 2))))).astype(np.float32)
        # Placed at the end of the window (where the model decides), a little early at random.
        out = np.zeros(total, dtype=np.float32)
        end = total - int(rng.integers(0, int(0.25 * RATE)))
        n = min(len(speech), end)  # a clip longer than the window keeps its end (the "bot")
        out[end - n:end] = speech[-n:]
        level = np.sqrt(np.mean(speech ** 2)) + 1e-6
        if rng.random() < 0.85:  # background sound
            bg = self._background(total)
            snr = rng.uniform(-3.0, 18.0)
            bg_level = np.sqrt(np.mean(bg ** 2)) + 1e-6
            out += bg * (level / bg_level) / (10 ** (snr / 20))
        if rng.random() < 0.3:  # hiss
            noise = rng.standard_normal(total).astype(np.float32)
            if rng.random() < 0.5:  # pinkish
                noise = np.cumsum(noise) * 0.02
                noise -= np.convolve(noise, np.ones(400) / 400, mode="same")
            out += noise * (level / (np.sqrt(np.mean(noise ** 2)) + 1e-6)) / (10 ** (rng.uniform(15, 40) / 20))
        if rng.random() < 0.3:  # a duller or thinner microphone
            if rng.random() < 0.5:
                sos = butter(2, rng.uniform(2500, 7000), "lowpass", fs=RATE, output="sos")
            else:
                sos = butter(2, rng.uniform(100, 400), "highpass", fs=RATE, output="sos")
            out = sosfilt(sos, out).astype(np.float32)
        peak = np.max(np.abs(out)) + 1e-6
        out *= rng.uniform(0.05, 0.9) / peak  # near or far
        return np.clip(out * 32767, -32768, 32767).astype(np.int16)


def clip_length() -> int:
    """openWakeWord's rule: the median clip plus 0.75 s, rounded; at least 2 s (16 frames of features)."""
    files = sorted((OUT / "positive_test").glob("*.wav"))[:200]
    lengths = [len(to_16k(*read_wav(f))) for f in files]
    total = int(round(np.median(lengths) / 1000) * 1000) + 12000
    return 32000 if total < 32000 or abs(total - 32000) <= 4000 else total


def step_features(threads: int) -> None:
    _stub_missing()
    from numpy.lib.format import open_memmap  # noqa: PLC0415

    from openwakeword.utils import AudioFeatures  # noqa: PLC0415

    total = clip_length()
    log.info("features: windows of %.2f s", total / RATE)
    feats = AudioFeatures(melspec_model_path=str(FEATURE_MODELS / "melspectrogram.onnx"),
                          embedding_model_path=str(FEATURE_MODELS / "embedding_model.onnx"), inference_framework="onnx",
                          device="cpu", ncpu=threads)
    for kind in COUNTS:
        dst = OUT / f"{kind}_features.npy"
        if dst.exists():
            log.info("features: %s already", kind)
            continue
        files = sorted((OUT / kind).glob("*.wav"))
        aug = Augmenter(np.random.default_rng(zlib.crc32(kind.encode())))
        shape = feats.embed_clips(np.zeros((1, total), dtype=np.int16), batch_size=1).shape[1:]
        tmp = OUT / f"{kind}_features.tmp.npy"
        arr = open_memmap(str(tmp), mode="w+", dtype=np.float32, shape=(len(files), *shape))
        t0 = time.monotonic()
        for i in range(0, len(files), 256):
            chunk = files[i:i + 256]
            batch = np.stack([aug(to_16k(*read_wav(f)), total) for f in chunk])
            arr[i:i + len(chunk)] = feats.embed_clips(batch, batch_size=64, ncpu=threads)
            if (i // 256) % 8 == 0:
                log.info("features: %s %d of %d (%.0f s)", kind, i + len(chunk), len(files), time.monotonic() - t0)
        arr.flush()
        del arr
        tmp.replace(dst)
        names = [f.name for f in files]
        (OUT / f"{kind}_features.files.json").write_text(json.dumps(names), encoding="utf-8")
        log.info("features: %s done (%d clips, %.0f s)", kind, len(files), time.monotonic() - t0)


# ---------------------------------------------------------------- 3. train
def false_alarm_set(frames: int, chunk: int = 20000):
    """openWakeWord's 11.3 h false-alarm check set, cut into the model's windows a chunk at a time (all of it at once
    would need ~3 GB: more than the Pi has to spare)."""
    import torch  # noqa: PLC0415

    data = np.load(DATA / "validation_set_features.npy", mmap_mode="r")

    class Chunks(torch.utils.data.IterableDataset):
        def __iter__(self):
            last = data.shape[0] - frames
            for s in range(0, last, chunk):
                idx = np.arange(s, min(s + chunk, last))[:, None] + np.arange(frames)[None, :]
                x = torch.from_numpy(np.asarray(data[idx], dtype=np.float32))
                yield x, torch.zeros(x.shape[0])

    return torch.utils.data.DataLoader(Chunks(), batch_size=None)


def step_train(threads: int, steps: int) -> None:
    import torch  # noqa: PLC0415

    _stub_missing()
    from openwakeword.data import mmap_batch_generator  # noqa: PLC0415
    from openwakeword.train import Model  # noqa: PLC0415

    onnx_path = OUT / f"{MODEL_NAME}.onnx"
    if onnx_path.exists():
        log.info("train: %s already (delete it to train again)", onnx_path.name)
        return
    torch.set_num_threads(threads)
    pos_test = np.load(OUT / "positive_test_features.npy")
    neg_test = np.load(OUT / "negative_test_features.npy")
    input_shape = pos_test.shape[1:]
    oww = Model(n_classes=1, input_shape=input_shape, model_type="dnn", layer_dim=32,
                seconds_per_example=1280 * input_shape[0] / 16000)

    def fit(x, n=input_shape[0]):
        """The everyday-sound features come in other lengths: cut into the model's windows."""
        if x.shape[1] == n:
            return x
        x = np.vstack(x)
        return np.array([x[i:i + n, :] for i in range(0, x.shape[0] - n, n)])

    files = {"ACAV100M_sample": str(EVERYDAY),
             "positive": str(OUT / "positive_train_features.npy"),
             "adversarial_negative": str(OUT / "negative_train_features.npy")}
    batches = mmap_batch_generator(
        files, n_per_class={"ACAV100M_sample": 1024, "adversarial_negative": 50, "positive": 50},
        data_transform_funcs={"ACAV100M_sample": fit},
        label_transform_funcs={"positive": lambda x: [1 for _ in x], "ACAV100M_sample": lambda x: [0 for _ in x],
                               "adversarial_negative": lambda x: [0 for _ in x]})

    class Batches(torch.utils.data.IterableDataset):
        def __iter__(self):
            return batches

    train = torch.utils.data.DataLoader(Batches(), batch_size=None, num_workers=1, prefetch_factor=16)
    labels = np.hstack((np.ones(pos_test.shape[0]), np.zeros(neg_test.shape[0]))).astype(np.float32)
    val = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(
        torch.from_numpy(np.vstack((pos_test, neg_test)).astype(np.float32)), torch.from_numpy(labels)),
        batch_size=len(labels))
    t0 = time.monotonic()
    best = oww.auto_train(X_train=train, X_val=val, false_positive_val_data=false_alarm_set(input_shape[0]),
                          steps=steps, max_negative_weight=1500, target_fp_per_hour=0.2)
    log.info("train: done in %.0f min", (time.monotonic() - t0) / 60)
    best = best.to("cpu").eval()
    torch.save(best.state_dict(), OUT / f"{MODEL_NAME}.pt")  # the weights: the class is local to openWakeWord's Model
    export(best, input_shape, onnx_path)
    log.info("train: saved %s (%d KB)", onnx_path, onnx_path.stat().st_size // 1024)


def export(model, input_shape, path: Path) -> None:
    """To ONNX: the classic exporter, else the newer one (needs onnxscript) when this PyTorch no longer has it."""
    import torch  # noqa: PLC0415

    example = torch.zeros((1, *input_shape))
    try:  # any number of windows at once (LittleBot gives one; the check gives thousands)
        torch.onnx.export(model, example, str(path), opset_version=13, input_names=["input"],
                          output_names=[MODEL_NAME], dynamic_axes={"input": {0: "batch"}, MODEL_NAME: {0: "batch"}},
                          dynamo=False)
    except Exception as e:  # noqa: BLE001
        log.info("export: the classic exporter failed (%s); trying the new one", type(e).__name__)
        torch.onnx.export(model, (example,), str(path), opset_version=18, input_names=["input"],
                          output_names=[MODEL_NAME], dynamo=True, external_data=False)


# ---------------------------------------------------------------- 4. check
def step_check() -> None:
    import onnxruntime as ort  # noqa: PLC0415

    sess = ort.InferenceSession(str(OUT / f"{MODEL_NAME}.onnx"), providers=["CPUExecutionProvider"])
    name = sess.get_inputs()[0].name
    step = 1 if sess.get_inputs()[0].shape[0] == 1 else 4096  # a model made for one window at a time

    def scores(x: np.ndarray) -> np.ndarray:
        out = [sess.run(None, {name: x[i:i + step].astype(np.float32)})[0].reshape(-1) for i in range(0, len(x), step)]
        return np.concatenate(out) if out else np.zeros(0)

    pos = scores(np.load(OUT / "positive_test_features.npy"))
    neg_x = np.load(OUT / "negative_test_features.npy")
    neg = scores(neg_x)
    neg_files = json.loads((OUT / "negative_test_features.files.json").read_text(encoding="utf-8"))
    texts = {}
    for line in manifest_path("negative_test").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        texts[r["file"]] = r["text"]
    frames = neg_x.shape[1]
    data = np.load(DATA / "validation_set_features.npy", mmap_mode="r")
    fa_scores = []
    for s in range(0, data.shape[0] - frames, 20000):
        idx = np.arange(s, min(s + 20000, data.shape[0] - frames))[:, None] + np.arange(frames)[None, :]
        fa_scores.append(scores(np.asarray(data[idx], dtype=np.float32)))
    fa = np.concatenate(fa_scores)
    report = {"model": f"{MODEL_NAME}.onnx", "positive_clips": int(len(pos)), "near_miss_clips": int(len(neg)),
              "false_alarm_hours": 11.3, "thresholds": {}}
    for th in (0.3, 0.5, 0.7, 0.9):
        over = fa >= th  # a false alarm: a stretch over the threshold, counted once (the robot waits 4 s anyway)
        alarms = int(np.sum(over[1:] & ~over[:-1]) + (1 if len(over) and over[0] else 0))
        fooled: dict[str, int] = {}
        for f, sc in zip(neg_files, neg):
            if sc >= th:
                fooled[texts.get(f, "?")] = fooled.get(texts.get(f, "?"), 0) + 1
        report["thresholds"][str(th)] = {
            "caught": round(float(np.mean(pos >= th)), 3),
            "near_misses_fooled": round(float(np.mean(neg >= th)), 3),
            "false_alarms_per_hour": round(alarms / 11.3, 2),
            "fooled_most_by": sorted(fooled.items(), key=lambda kv: -kv[1])[:12]}
    (OUT / "check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for th, r in report["thresholds"].items():
        log.info("check at %s: caught %.1f%%, near misses fooled %.1f%%, %.2f false alarms an hour", th,
                 100 * r["caught"], 100 * r["near_misses_fooled"], r["false_alarms_per_hour"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("step", choices=("voices", "features", "train", "check", "all"))
    ap.add_argument("--threads", type=int, default=3, help="CPU threads (the robot keeps one for itself)")
    ap.add_argument("--steps", type=int, default=50000, help="training steps (openWakeWord's default)")
    ap.add_argument("--smoke", action="store_true",
                    help="a tiny run in ~/wwtrain/smoke that only checks every step works (the check set stands in "
                         "for the 17 GB of everyday sound)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    if args.smoke:
        global OUT, EVERYDAY  # noqa: PLW0603
        OUT, EVERYDAY = ROOT / "smoke", DATA / "validation_set_features.npy"
        COUNTS.update({k: 48 for k in COUNTS})
        args.steps = min(args.steps, 400)
    for noisy in ("piper_sample_generator", "piper_sample_generator.__main__", "urllib3", "numba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    OUT.mkdir(parents=True, exist_ok=True)
    steps = ("voices", "features", "train", "check") if args.step == "all" else (args.step,)
    for step in steps:
        log.info("step: %s", step)
        if step == "voices":
            step_voices(args.threads)
        elif step == "features":
            step_features(args.threads)
        elif step == "train":
            step_train(args.threads, args.steps)
        else:
            step_check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
