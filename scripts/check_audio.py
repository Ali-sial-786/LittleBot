#!/usr/bin/env python3
"""LittleBot audio diagnostics: list devices, play a test chime + sentence, record a level meter.

    .venv/bin/python scripts/check_audio.py                    # same as --list
    .venv/bin/python scripts/check_audio.py --list             # devices, config, tools, hints (exit 0)
    .venv/bin/python scripts/check_audio.py --play             # chime + spoken sentence on the speaker
    .venv/bin/python scripts/check_audio.py --play --output-device "USB"   # pick a speaker by name / index
    .venv/bin/python scripts/check_audio.py --record 5         # 5 s live level meter (NOT saved)
    .venv/bin/python scripts/check_audio.py --record 5 --save /tmp/test.wav   # explicitly keep the recording
    .venv/bin/python scripts/check_audio.py --play --record 3 --input-device 2 --volume 0.4

Prints PASS / WARN / FAIL lines with hints. Exit status: 0 when every requested
check passed (plain --list is informational and exits 0 unless --strict),
1 when a check failed. Nothing is recorded to disk unless --save is given.
"""

from __future__ import annotations

import argparse
import os
import re
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np  # noqa: E402

from littlebot.audio import devices as dev  # noqa: E402
from littlebot.audio import dsp  # noqa: E402
from littlebot.audio.errors import AudioDeviceError  # noqa: E402

TEST_SENTENCE = "Hello! I am LittleBot. Can you hear me?"


class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.warned = 0
        self.hints: list[str] = []

    def line(self, status: str, msg: str) -> None:
        if status == "FAIL":
            self.failed += 1
        elif status == "WARN":
            self.warned += 1
        print(f"  [{status:4s}] {msg}", flush=True)

    def hint(self, text: str) -> None:
        if text not in self.hints:
            self.hints.append(text)


def section(title: str) -> None:
    print(f"\n== {title} ==", flush=True)


def _run(cmd: list[str], timeout: float = 4.0) -> tuple[int | None, str]:
    if not shutil.which(cmd[0]):
        return None, ""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _load_config(mode: str | None) -> Any:
    try:
        from littlebot.core.config import load_config  # noqa: PLC0415

        return load_config(mode=mode)
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] could not load config ({e}); using built-in defaults")
        from littlebot.core.config import LittleBotConfig  # noqa: PLC0415

        return LittleBotConfig()


def _parse_device(value: str | None) -> int | str | None:
    if value is None:
        return None
    v = value.strip()
    return int(v) if v.lstrip("-").isdigit() else v


# ------------------------------------------------------------------ sections
def check_platform(r: Report) -> None:
    section("Platform")
    try:
        from littlebot.hardware.platform import describe_platform  # noqa: PLC0415

        info = describe_platform()
    except Exception:  # noqa: BLE001
        info = {"system": platform.system(), "machine": platform.machine(), "pi_model": None}
    model = info.get("pi_model")
    print(f"  {info.get('system')} {info.get('machine')}, Python {platform.python_version()}"
          + (f", {model}" if model else ""))
    if model and "Raspberry Pi 5" in str(model):
        r.line("INFO", "Raspberry Pi 5: there is NO 3.5 mm headphone jack - use a USB speaker (or HDMI audio).")


def check_config(r: Report, cfg: Any, args: argparse.Namespace) -> tuple[Any, Any]:
    section("Configuration (config/*.yaml + local.yaml)")
    a = cfg.audio
    out_dev = _parse_device(args.output_device) if args.output_device is not None else a.speaker.device
    in_dev = _parse_device(args.input_device) if args.input_device is not None else a.microphone.device
    print(f"  speaker:    backend={a.speaker.backend} device={a.speaker.device!r} volume={a.speaker.volume} "
          f"max_volume={a.speaker.max_volume}")
    print(f"  microphone: backend={a.microphone.backend} device={a.microphone.device!r} rate={a.microphone.sample_rate} "
          f"vad_threshold={a.microphone.vad_threshold}")
    print(f"  tts={a.tts.provider} (voice {a.tts.voice}, rate {a.tts.rate})  stt={a.stt.provider}  "
          f"half_duplex={a.half_duplex}  store_recordings={a.store_recordings}")
    if a.store_recordings:
        r.line("WARN", "audio.store_recordings is TRUE: utterances are written to disk")
    return out_dev, in_dev


def check_devices(r: Report, out_dev: Any, in_dev: Any) -> tuple[bool, bool]:
    section("PortAudio devices (sounddevice)")
    sd = dev.get_sounddevice()
    if sd is None:
        r.line("FAIL", f"sounddevice/PortAudio unavailable: {dev.sounddevice_error()}")
        r.hint("Install PortAudio + sounddevice: sudo apt install libportaudio2 && .venv/bin/pip install sounddevice")
        return False, False
    try:
        print(f"  {sd.get_portaudio_version()[1]}")
    except Exception:  # noqa: BLE001
        pass
    listing = dev.list_devices()
    for kind, key in (("Outputs (speakers)", "outputs"), ("Inputs (microphones)", "inputs")):
        print(f"  {kind}:")
        if not listing[key]:
            print("    (none)")
        for d in listing[key]:
            mark = "*" if d["default"] else " "
            print(f"   {mark}#{d['index']:<3d} {d['name'][:48]:48s} {d['channels']} ch  "
                  f"{d['default_samplerate']:.0f} Hz  [{d['hostapi']}]")
    print("  (* = system default)")
    have_out = have_in = False
    try:
        d = dev.find_device(out_dev, "output")
        r.line("PASS", f"speaker selection {out_dev!r} -> #{d['index']} {d['name']}")
        have_out = True
    except AudioDeviceError as e:
        r.line("FAIL", f"speaker selection {out_dev!r}: {e}")
        r.hint("No speaker visible to PortAudio. Plug in the USB speaker, check `aplay -l`, and set "
               "audio.speaker.device in config/local.yaml to a name substring (e.g. \"USB\") or index from the list.")
    try:
        d = dev.find_device(in_dev, "input")
        r.line("PASS", f"microphone selection {in_dev!r} -> #{d['index']} {d['name']}")
        have_in = True
    except AudioDeviceError as e:
        r.line("FAIL", f"microphone selection {in_dev!r}: {e}")
        r.hint("No microphone visible to PortAudio. Plug in the USB microphone, check `arecord -l`, and set "
               "audio.microphone.device in config/local.yaml.")
    return have_out, have_in


def check_default_sink(r: Report) -> None:
    """Videos (Chromium) and other apps play on the desktop's default output: it should be the USB speaker."""
    rc, out = _run(["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"])
    props = dict(re.findall(r'(node\.name|node\.description) = "([^"]*)"', out or "")) if rc == 0 else {}
    name, label = props.get("node.name", ""), props.get("node.description") or props.get("node.name") or "?"
    if not name:
        r.line("WARN", "PipeWire has no default output right now.")
    elif "hdmi" in name.lower():
        r.line("WARN", f"the desktop's default output is the HDMI screen ({label}): videos play there, not on the speaker")
        r.hint("Make the USB speaker the default for good: scripts/set_default_speaker.sh  (one-off: `wpctl status`, "
               "then `wpctl set-default <the USB speaker's number>`). LittleBot's own sounds already prefer it.")
    elif ".usb-" in name.lower():
        r.line("PASS", f"the desktop's default output is the USB speaker ({label})")
    else:
        r.line("INFO", f"the desktop's default output: {label}")


def check_system_tools(r: Report) -> None:
    section("System audio (ALSA / PipeWire)")
    for cmd, label in ((["aplay", "-l"], "playback cards (aplay -l)"), (["arecord", "-l"], "capture cards (arecord -l)")):
        rc, out = _run(cmd)
        if rc is None:
            r.line("INFO", f"{cmd[0]} not installed (sudo apt install alsa-utils) - skipping {label}")
            continue
        cards = [ln for ln in out.splitlines() if ln.startswith("card ")]
        if cards:
            r.line("INFO", f"{label}:")
            for ln in cards:
                print(f"         {ln}")
        else:
            r.line("WARN", f"{label}: no sound cards found")
    if shutil.which("wpctl") or shutil.which("pw-cli"):
        rc, out = _run(["wpctl", "status"]) if shutil.which("wpctl") else (None, "")
        running = rc == 0 and "PipeWire" in out
        r.line("INFO", "PipeWire " + ("is running" if running else "is installed (not reachable from this session)")
               + ": PortAudio sees it through the 'pipewire'/'pulse'/'default' devices.")
        if running:
            check_default_sink(r)
    elif shutil.which("pactl"):
        r.line("INFO", "PulseAudio tools present (pactl): use `pactl set-default-sink <name>` to pick the USB speaker.")
    else:
        r.line("INFO", "No PipeWire/PulseAudio tools: plain ALSA. Select the USB card by name (e.g. device: \"USB\").")


def check_tts(r: Report, cfg: Any) -> None:
    section("Speech engines")
    from littlebot.audio.tts import EspeakTTS, PiperTTS  # noqa: PLC0415
    from littlebot.audio.stt import VoskSTT  # noqa: PLC0415

    es = EspeakTTS(voice=cfg.audio.tts.voice, pitch=cfg.audio.tts.pitch)
    if es.available():
        r.line("PASS", f"espeak-ng found ({es.binary})")
    else:
        r.line("WARN", "espeak-ng not installed: LittleBot will use the silent mock voice")
        r.hint("Install a local voice: sudo apt install espeak-ng")
    piper = PiperTTS(cfg.audio.tts.piper_model)
    r.line("INFO", "piper: " + ("available" if piper.available() else piper.unavailable_reason()))
    vosk = VoskSTT(cfg.audio.stt.vosk_model_path)
    r.line("INFO", "vosk STT: " + ("available" if vosk.available() else vosk.unavailable_reason()))


def _open_output(args: argparse.Namespace, cfg: Any, out_dev: Any) -> tuple[dev.OutputBackend | None, str]:
    backend = args.backend or cfg.audio.speaker.backend
    if backend in ("mock", "none", "auto"):
        backend = "auto"
    res = dev.resolve_output(backend, out_dev, cfg.audio.speaker.sample_rate, time_scale=1.0)
    if res.backend.real and res.backend.status == dev.STATUS_OK:
        return res.backend, ""
    return None, res.note or res.backend.detail


def do_play(r: Report, args: argparse.Namespace, cfg: Any, out_dev: Any) -> None:
    section("Speaker test (--play)")
    out, why = _open_output(args, cfg, out_dev)
    if out is None:
        r.line("FAIL", f"no real speaker could be opened: {why}")
        r.hint("Raspberry Pi 5 has no 3.5 mm jack: plug in a USB speaker, run `aplay -l`, then retry with "
               "--output-device \"<name part>\". Test ALSA directly: `speaker-test -c 2 -t sine -l 1`.")
        return
    max_vol = float(cfg.audio.speaker.max_volume)
    volume = max(0.0, min(max_vol, args.volume if args.volume is not None else float(cfg.audio.speaker.volume)))
    print(f"  playing on {out.name}: {out.device_name} ({out.detail}) at volume {volume:.2f} (ceiling {max_vol:.2f})")
    from littlebot.audio.sfx import SoundBank  # noqa: PLC0415

    ctrl = dev.PlaybackControl(gain=lambda: volume, label="check")
    chime = SoundBank().get("chime")
    try:
        assert chime is not None
        t0 = time.monotonic()
        ok = out.play(chime[0], chime[1], ctrl)
        r.line("PASS" if ok else "FAIL", f"chime played ({time.monotonic() - t0:.1f}s) - did you hear a soft two-note bell?")
    except AudioDeviceError as e:
        r.line("FAIL", f"chime playback failed: {e}")
        r.hint("The device may be busy (another program, or pygame holding it) or need a different sample rate. "
               "Try --backend pygame, or route through PipeWire (device: null).")
        out.close()
        return
    from littlebot.audio.tts import build_tts  # noqa: PLC0415

    tts, local = build_tts(cfg.audio.tts if args.tts is None else cfg.audio.tts.model_copy(update={"provider": args.tts}),
                           cloud_allowed=lambda: False, api_key=lambda: None)
    voice = tts if not tts.is_cloud else local
    try:
        samples, sr = voice.synthesize(TEST_SENTENCE, float(cfg.audio.tts.rate))
        ok = out.play(samples, sr, ctrl)
        msg = f"sentence spoken with {voice.name} ({len(samples) / sr:.1f}s)"
        if voice.name == "mock":
            r.line("WARN", msg + " - mock voice is only quiet babble; install espeak-ng for real speech")
        else:
            r.line("PASS" if ok else "FAIL", msg + f": \"{TEST_SENTENCE}\"")
    except Exception as e:  # noqa: BLE001
        r.line("FAIL", f"speech test failed: {e}")
    finally:
        out.close()


def do_record(r: Report, args: argparse.Namespace, cfg: Any, in_dev: Any) -> None:
    seconds = max(0.5, min(60.0, float(args.record)))
    section(f"Microphone test (--record {seconds:g})")
    mic = cfg.audio.microphone
    inp = dev.SoundDeviceInput(in_dev, mic.sample_rate, mic.channels)
    try:
        inp.probe()
        inp.open()
    except AudioDeviceError as e:
        r.line("FAIL", f"could not open a microphone: {e}")
        r.hint("Plug in a USB microphone (or a USB webcam with a mic), run `arecord -l`, then retry with "
               "--input-device \"<name part>\". Test ALSA directly: `arecord -d 3 -f S16_LE -r 16000 /dev/null`.")
        return
    print(f"  recording from {inp.device_name} ({inp.detail}) - speak or clap near the microphone. "
          + ("The audio will be SAVED to " + args.save if args.save else "Nothing is saved."))
    keep: list[np.ndarray] = []
    need = int(seconds * inp.sample_rate)
    got = 0
    peak = 0.0
    sumsq = 0.0
    tty = sys.stdout.isatty()
    last_print = 0.0
    deadline = time.monotonic() + seconds * 2 + 2
    error = ""
    try:
        while got < need and time.monotonic() < deadline:
            try:
                chunk = inp.read(0.25)
            except AudioDeviceError as e:
                error = str(e)
                break
            if chunk is None or not len(chunk):
                continue
            got += len(chunk)
            peak = max(peak, dsp.peak(chunk))
            sumsq += float(np.sum(chunk.astype(np.float64) ** 2))
            if args.save:
                keep.append(chunk.copy())
            level = dsp.rms(chunk)
            now = time.monotonic()
            if tty or now - last_print > 0.5:
                last_print = now
                db = max(-60.0, dsp.dbfs(level))
                bar = "#" * int((db + 60) / 60 * 40)
                text = f"  {got / inp.sample_rate:5.1f}s |{bar:<40s}| {db:6.1f} dBFS"
                print(("\r" + text) if tty else text, end="" if tty else "\n", flush=True)
    except KeyboardInterrupt:
        error = "interrupted"
    finally:
        inp.close()
        if tty:
            print()
    if error:
        r.line("FAIL", f"recording stopped: {error}")
        return
    rms = (sumsq / got) ** 0.5 if got else 0.0
    if got < need * 0.5:
        r.line("FAIL", f"only {got / inp.sample_rate:.1f}s of audio arrived (device stalled?)")
    elif peak < 1e-4:
        r.line("FAIL", "digital silence: the capture channel is probably muted")
        r.hint("Unmute / raise the capture level: `alsamixer -c <card>` (F4 = capture), or in PipeWire `wpctl set-volume`.")
    else:
        status = "PASS" if rms >= mic.vad_threshold / 3 else "WARN"
        r.line(status, f"heard audio: peak {dsp.dbfs(peak):.1f} dBFS, average {dsp.dbfs(rms):.1f} dBFS "
                       f"(speech threshold {dsp.dbfs(mic.vad_threshold):.1f} dBFS)")
        if status == "WARN":
            r.hint("The microphone is quiet: move it closer, raise the capture level (alsamixer), "
                   "or raise audio.input_gain in the dashboard.")
    if args.save and keep:
        path = Path(args.save).expanduser()
        dsp.write_wav(path, np.concatenate(keep), inp.sample_rate)
        r.line("INFO", f"saved recording to {path} (you asked for it with --save)")
    keep.clear()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LittleBot audio diagnostics (speaker + microphone).",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--list", action="store_true", help="list devices, config and hints (default action)")
    ap.add_argument("--play", action="store_true", help="play a test chime and a spoken sentence")
    ap.add_argument("--record", type=float, metavar="SECONDS", help="record SECONDS with a live level meter")
    ap.add_argument("--save", metavar="PATH", help="with --record: save the recording as WAV (otherwise nothing is kept)")
    ap.add_argument("--output-device", metavar="DEV", help="speaker index or name substring (default: config / system)")
    ap.add_argument("--input-device", metavar="DEV", help="microphone index or name substring (default: config / system)")
    ap.add_argument("--backend", choices=["auto", "sounddevice", "pygame"], help="speaker backend for --play")
    ap.add_argument("--volume", type=float, help="test volume 0..1 (clamped to audio.speaker.max_volume)")
    ap.add_argument("--tts", choices=["auto", "espeak", "piper", "mock"], help="voice for the --play sentence")
    ap.add_argument("--mode", choices=["simulation", "pi", "offline"], help="config mode to load (default: auto)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when no speaker or microphone is found")
    args = ap.parse_args(argv)
    if args.save and args.record is None:
        ap.error("--save only makes sense with --record")

    print("LittleBot audio check")
    r = Report()
    check_platform(r)
    cfg = _load_config(args.mode)
    out_dev, in_dev = check_config(r, cfg, args)
    have_out, have_in = check_devices(r, out_dev, in_dev)
    check_system_tools(r)
    check_tts(r, cfg)
    listing_failures = r.failed
    if args.play:
        do_play(r, args, cfg, out_dev)
    if args.record is not None:
        do_record(r, args, cfg, in_dev)

    if r.hints:
        section("Hints")
        for h in r.hints:
            print(f"  - {h}")
    action_failures = r.failed - listing_failures
    section("Result")
    if args.play or args.record is not None:
        print("  FAIL" if action_failures else "  PASS", f"({r.failed} failure(s), {r.warned} warning(s))")
        return 1 if action_failures else 0
    if args.strict and not (have_out and have_in):
        print(f"  FAIL (--strict: speaker found={have_out}, microphone found={have_in})")
        return 1
    print(f"  listed (speaker found={have_out}, microphone found={have_in}; {r.failed} failure(s), "
          f"{r.warned} warning(s)). Use --play / --record N to test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
