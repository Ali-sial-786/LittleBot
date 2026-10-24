"""Lines recorded in the ChatGPT voice (data/voice_lines) and AudioService.say() playing them; the
speaker check after a hand-back (speaker_back)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import numpy as np
import pytest

from littlebot.audio.devices import MockOutput
from littlebot.audio.service import AudioService
from littlebot.audio.stt import MockSTT
from littlebot.audio.tts import MockTTS
from littlebot.audio.voice_lines import VoiceLineStore, get_store, line_hash, normalise
from littlebot.core.events import Topics, collect
from littlebot.testing import make_test_context

SR = 22050


def tone(seconds: float = 0.8, level: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (level * np.sin(2 * np.pi * 220 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)


# ------------------------------------------------------------------ the store
def test_lines_match_whatever_the_case_punctuation_and_spacing():
    assert normalise("Peekaboo! I see you,  Amara.") == "peekaboo i see you amara"
    assert normalise("Let's play!") == normalise("lets play") == "lets play"
    assert normalise("Hi 🐶\nfriend") == "hi friend"
    assert line_hash("Boing!") == line_hash("boing") != line_hash("Boing, boing!")
    assert len(line_hash("Boing!")) == 16


def test_store_saves_finds_and_indexes_a_line(tmp_path):
    store = VoiceLineStore(tmp_path / "voice_lines")
    assert store.get("Boing!") is None and len(store) == 0
    entry = store.save("Boing!", tone(), SR, voice="bubbie", source="activity:again_again")
    key = line_hash("Boing!")
    assert (tmp_path / "voice_lines" / f"{key}.wav").is_file()
    index = json.loads((tmp_path / "voice_lines" / "index.json").read_text())
    saved = index["lines"][key]
    assert saved["text"] == "Boing!" and saved["hash"] == key and saved["voice"] == "bubbie"
    assert saved["date"] == entry["date"] and saved["source"] == "activity:again_again" and saved["seconds"] == 0.8
    samples, sr, found = store.get("boing")  # the same line, written differently
    assert sr == SR and len(samples) == len(tone()) and found["voice"] == "bubbie"
    assert store.recorded(["Boing!", "Moo!"]) == {key}
    assert store.remove("Boing!") and store.get("Boing!") is None


def test_a_recording_made_elsewhere_is_seen_at_once(tmp_path):
    folder = tmp_path / "voice_lines"
    robot, recorder = VoiceLineStore(folder), VoiceLineStore(folder)  # the robot, and the script / dashboard
    assert robot.get("Moo!") is None
    recorder.save("Moo!", tone(), SR, voice="sol")
    assert robot.get("Moo!") is not None
    recorder.save("Moo!", tone(0.5), SR, voice="bubbie")  # re-recorded: the new clip, not the cached one
    samples, _sr, entry = robot.get("Moo!")
    assert entry["voice"] == "bubbie" and len(samples) == len(tone(0.5))


def test_a_broken_index_or_missing_clip_means_text_to_speech(tmp_path):
    folder = tmp_path / "voice_lines"
    store = VoiceLineStore(folder)
    store.save("Moo!", tone(), SR, voice="sol")
    (folder / f"{line_hash('Moo!')}.wav").unlink()
    assert store.get("Moo!") is None and store.recorded(["Moo!"]) == set()
    (folder / "index.json").write_text("{not json")
    assert store.get("Moo!") is None and len(store) == 0


def test_writers_never_lose_each_others_lines(tmp_path):
    folder = tmp_path / "voice_lines"
    errors: list[Exception] = []

    def write(prefix: str) -> None:
        try:
            s = VoiceLineStore(folder)
            for i in range(15):
                s.save(f"{prefix} line {i}", tone(0.05), SR, voice="sol")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=write, args=(p,)) for p in ("dashboard", "script")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(VoiceLineStore(folder)) == 30


# ------------------------------------------------------------------ say()
@pytest.fixture
def vctx(tmp_path):
    c = make_test_context(fakes=False, data_dir=str(tmp_path))
    yield c
    c.db.close()


async def test_say_plays_the_recording_with_mouth_volume_and_events(vctx, make_service):
    store = get_store(vctx)
    store.save("Tap again, and I'll do my trick!", tone(1.2), SR, voice="bubbie", source="activity:again_again")
    tts, out = MockTTS(), MockOutput(time_scale=0.0)
    svc = make_service(vctx, output=out, tts=tts)
    await svc.start()
    vctx.settings.set("audio.volume", 0.4)
    events = collect(vctx.bus, ["audio.*"])
    assert await svc.say("Tap again, and I'll do my trick!") is True
    assert tts.calls == []  # not text-to-speech
    played = out.played[-1]
    assert played.label == "speech:Tap again, and I'll do my trick!" and played.completed
    assert 1.2 < played.duration < 1.4 and played.sample_rate == SR
    assert played.peak == pytest.approx(0.4 * float(np.abs(tone(1.2)).max()), rel=0.05)  # at the parent's volume
    topics = [e.topic for e in events if e.topic in (Topics.AUDIO_SPEECH_STARTED, Topics.AUDIO_SPEECH_FINISHED)]
    assert topics == [Topics.AUDIO_SPEECH_STARTED, Topics.AUDIO_SPEECH_FINISHED]
    mouth = [e.data for e in events if e.topic == Topics.AUDIO_VISEME]
    assert max(m["level"] for m in mouth) > 0.3 and mouth[-1] == {"level": 0.0, "shape": "closed"}  # lip-sync
    # a line that wasn't recorded: text-to-speech, as before
    assert await svc.say("Boing!") is True
    assert tts.calls == [("Boing!", pytest.approx(svc._tts_rate))]


async def test_each_line_logs_its_voice_once(vctx, make_service, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.audio")
    vctx.settings.set("profile", {**vctx.settings.get("profile"), "nickname": "Amara"})
    get_store(vctx).save("Hi Amara! Let's play peekaboo!", tone(), SR, voice="bubbie")
    svc = make_service(vctx)
    for _ in range(2):
        await svc.say("Hi Amara! Let's play peekaboo!")
        await svc.say("Boing!")
    voice_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("speech in")]
    assert len(voice_lines) == 2
    assert "the recorded ChatGPT voice (bubbie)" in voice_lines[0] and "Amara" not in voice_lines[0]
    assert "Hi <child>! Let's play peekaboo!" in voice_lines[0]
    assert "text-to-speech (mock; not recorded)" in voice_lines[1] and "Boing!" in voice_lines[1]


async def test_a_recording_stops_like_speech(vctx, make_service):
    get_store(vctx).save("A long recorded line", tone(3.0), SR, voice="bubbie")
    out = MockOutput(time_scale=1.0)
    svc = make_service(vctx, output=out)
    finished = collect(vctx.bus, [Topics.AUDIO_SPEECH_FINISHED])
    task = asyncio.create_task(svc.say("A long recorded line"))
    await vctx.bus.wait_for(Topics.AUDIO_SPEECH_STARTED, timeout=3)
    t0 = time.monotonic()
    svc.stop_output()
    assert await asyncio.wait_for(task, 2) is False and time.monotonic() - t0 < 0.5
    assert finished[-1].data["interrupted"] is True and not out.played[-1].completed


async def test_status_counts_the_recorded_lines(vctx, make_service):
    get_store(vctx).save("Moo!", tone(), SR, voice="sol")
    assert make_service(vctx).status()["recorded_lines"] == 1


# ------------------------------------------------------------------ speaker_back()
async def test_speaker_back_waits_until_the_usb_speaker_plays_again(fake_sd, tmp_path):
    ctx = make_test_context(fakes=False, data_dir=str(tmp_path), audio={"speaker": {"backend": "sounddevice"}})
    svc = AudioService(ctx, tts=MockTTS(), stt=MockSTT())
    try:
        await svc.start()
        await svc.suspend("live voice")
        assert await svc.speaker_back(0.2) is False  # still lent
        svc.resume("live voice")
        fake_sd.fail_write_after = 0  # just let go of by the call: a sound fails at once...
        threading.Timer(0.3, lambda: setattr(fake_sd, "fail_write_after", None)).start()  # ...for 0.3 s
        events = collect(ctx.bus, ["audio.speech_*", "audio.sound_*"])
        t0 = time.monotonic()
        assert await svc.speaker_back() is True
        assert 0.25 < time.monotonic() - t0 < 2.0
        assert svc.status()["speaker"]["status"] == "ok" and events == []  # silent: no events
        assert fake_sd.out_streams[-1].closed  # let go again (a video may need the device next)
        assert await svc.say("Tap again, and I'll do my trick!") is True
        # never back: False, and the speaker is reported as failed
        fake_sd.fail_write_after = 0
        assert await svc.speaker_back(0.3) is False
        assert svc.status()["speaker"]["status"] == "disconnected"
    finally:
        fake_sd.fail_write_after = None
        await svc.stop()
        ctx.db.close()


async def test_speaker_back_with_a_simulated_speaker_returns_at_once(vctx, make_service):
    out = MockOutput(time_scale=1.0)
    svc = make_service(vctx, output=out)
    t0 = time.monotonic()
    assert await svc.speaker_back() is True
    assert time.monotonic() - t0 < 0.5 and out.played == []
