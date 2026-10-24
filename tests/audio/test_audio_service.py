"""AudioService behaviour with mock / fake devices (no hardware, no network)."""

from __future__ import annotations

import asyncio
import builtins
import io
import os
import time

import numpy as np
import pytest

from littlebot import audio as audio_pkg
from littlebot.audio.devices import MockInput, MockOutput, NullInput, NullOutput, SoundDeviceInput, SoundDeviceOutput
from littlebot.audio.service import MAX_QUEUE, AudioService
from littlebot.audio.stt import MockSTT
from littlebot.audio.tts import MockTTS
from littlebot.core.events import Topics, collect
from littlebot.core.interfaces import AudioAPI
from littlebot.core.service import ServiceStatus
from littlebot.testing import make_test_context

TEXT = "Hello little one, shall we play peekaboo today?"


def _topics(events, exclude=("audio.level",)):
    return [e.topic for e in events if e.topic not in exclude]


async def _wait_event(ctx, topic, timeout=3.0):
    ev = await ctx.bus.wait_for(topic, timeout=timeout)
    assert ev is not None, f"no {topic} event"
    return ev


# ------------------------------------------------------------------ contract
async def test_create_service_implements_audio_api(actx):
    svc = audio_pkg.create_service(actx)
    try:
        assert isinstance(svc, AudioService) and isinstance(svc, AudioAPI)
        status = svc.status()
        for key in ("speaker", "microphone", "volume", "max_volume", "speaking", "listening", "tts_provider",
                    "stt_provider"):
            assert key in status
        for key in ("backend", "device", "status", "detail"):
            assert key in status["speaker"] and key in status["microphone"]
        assert svc.health()["name"] == "audio"
    finally:
        await svc.stop()


# ------------------------------------------------------------------ speech
async def test_say_event_sequence_with_mock_devices(actx, make_service):
    out = MockOutput(time_scale=0.0)
    svc = make_service(actx, output=out)
    await svc.start()
    events = collect(actx.bus, ["audio.*"])
    assert await svc.say(TEXT) is True
    seq = _topics(events, exclude=("audio.level", "audio.device_status"))
    assert seq == [Topics.AUDIO_SPEECH_STARTED, Topics.AUDIO_SPEECH_FINISHED]
    started = next(e for e in events if e.topic == Topics.AUDIO_SPEECH_STARTED)
    finished = next(e for e in events if e.topic == Topics.AUDIO_SPEECH_FINISHED)
    assert started.data["text"] == TEXT and started.data["duration_estimate"] > 2.0
    assert finished.data == {"text": TEXT, "interrupted": False}
    levels = [e for e in events if e.topic == Topics.AUDIO_VISEME]
    assert all(started.seq < e.seq < finished.seq for e in levels)
    values = [e.data["level"] for e in levels]
    assert all(0.0 <= v <= 1.0 for v in values) and max(values) > 0.5 and values[-1] == 0.0
    # every event carries the mouth shape too; they follow the speaker (the mock plays 40 ms chunks:
    # at most 25 a second) and repeat nothing, ending on a closed mouth
    assert {e.data["shape"] for e in levels} <= {"closed", "small", "ah", "ee", "oo"}
    assert len({e.data["shape"] for e in levels}) >= 2
    assert levels[-1].data == {"level": 0.0, "shape": "closed"}
    assert started.data["duration_estimate"] * 4 < len(levels) <= started.data["duration_estimate"] * 25 + 2
    # an unchanged frame is only repeated as a keep-alive (at most every VISEME_REPEAT_S of audio)
    from littlebot.audio.service import VISEME_REPEAT_S

    pairs = [(e.data["level"], e.data["shape"]) for e in levels[:-1]]  # (the last one always closes the mouth)
    repeats = sum(a == b for a, b in zip(pairs, pairs[1:]))
    assert repeats <= started.data["duration_estimate"] / VISEME_REPEAT_S + 1
    assert out.played[-1].label == f"speech:{TEXT}" and out.played[-1].completed
    assert svc.status()["speaking"] is False


async def test_mouth_frames_follow_the_playback_position(actx, make_service, monkeypatch):
    """Each audio.level event is the precomputed mouth frame for what the speaker plays (a bit early)."""
    from littlebot.audio import dsp
    from littlebot.audio import service as service_mod

    shapes = ("closed", "small", "ah", "ee", "oo")
    track_len = 100  # 2 s of 20 ms frames: frame i has shape i % 5 and level i / 100

    def fake_track(samples, sample_rate, hz=50.0):
        return (np.arange(track_len, dtype=np.float32) / track_len, tuple(shapes[i % 5] for i in range(track_len)))

    monkeypatch.setattr(dsp, "viseme_track", fake_track)
    out = MockOutput(time_scale=0.0, chunk_seconds=0.02)
    svc = make_service(actx, output=out)
    await svc.start()
    events = collect(actx.bus, ["audio.level"])
    assert await svc.say(TEXT)
    frames = [(e.data["level"], e.data["shape"]) for e in events][:-1]
    # the mock reports 20 ms steps: frame index = played seconds + lead, in 20 ms frames
    lead = int(round(service_mod.VISEME_LEAD_S * service_mod.LEVEL_HZ))
    first = frames[0]
    assert first == (round((1 + lead) / track_len, 3), shapes[(1 + lead) % 5])
    for k, (level, shape) in enumerate(frames[:40]):
        i = round(level * track_len)
        assert shape == shapes[i % 5] and i == k + 1 + lead


async def test_say_uses_rate_setting_and_override(actx, make_service):
    tts = MockTTS()
    svc = make_service(actx, tts=tts)
    actx.settings.set("audio.tts_rate", 0.7)
    await svc.say("one two three")
    await svc.say("one two three", rate=1.2)
    await svc.say("fast", rate=9)  # clamped
    assert [r for _, r in tts.calls] == [0.7, 1.2, 1.6]


async def test_say_cleans_text_and_ignores_empty(actx, make_service):
    tts = MockTTS()
    svc = make_service(actx, tts=tts)
    assert await svc.say("   ") is True and tts.calls == []
    await svc.say("Hi 🐶\nfriend")
    assert tts.calls[-1][0] == "Hi friend"


async def test_stop_output_interrupts_speech_and_queue(actx, make_service):
    out = MockOutput(time_scale=1.0)
    svc = make_service(actx, output=out)
    events = collect(actx.bus, ["audio.*"])
    task = asyncio.create_task(svc.say(TEXT))
    await _wait_event(actx, Topics.AUDIO_SPEECH_STARTED)
    assert svc.status()["speaking"] is True
    queued = await svc.play_sound("chime", wait=False)
    assert queued is True and svc.status()["queued"] == 1
    t0 = time.monotonic()
    svc.stop_output()
    assert await asyncio.wait_for(task, 2) is False
    assert time.monotonic() - t0 < 0.5
    finished = [e for e in events if e.topic == Topics.AUDIO_SPEECH_FINISHED]
    assert finished and finished[-1].data["interrupted"] is True
    await asyncio.sleep(0.05)
    assert not any(e.topic == Topics.AUDIO_SOUND_STARTED for e in events)  # queued chime never played
    assert svc.status()["playing"] is False
    assert await svc.play_sound("pop") is True  # works again afterwards


async def test_stop_output_during_synthesis_cancels_speech(actx, make_service):
    class SlowTTS(MockTTS):
        def synthesize(self, text, rate=1.0):
            time.sleep(0.2)
            return super().synthesize(text, rate)

    svc = make_service(actx, tts=SlowTTS())
    events = collect(actx.bus, [Topics.AUDIO_SPEECH_STARTED])
    task = asyncio.create_task(svc.say("wait for me"))
    await asyncio.sleep(0.05)
    svc.stop_output()
    assert await task is False and events == []


async def test_cancelling_say_stops_playback(actx, make_service):
    out = MockOutput(time_scale=1.0)
    svc = make_service(actx, output=out)
    finished = collect(actx.bus, [Topics.AUDIO_SPEECH_FINISHED])
    task = asyncio.create_task(svc.say(TEXT))
    await _wait_event(actx, Topics.AUDIO_SPEECH_STARTED)
    t0 = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    while not finished and time.monotonic() - t0 < 2:
        await asyncio.sleep(0.01)
    assert finished and finished[0].data["interrupted"] is True
    assert time.monotonic() - t0 < 0.5 and out.played[-1].completed is False


async def test_tts_failure_falls_back_to_mock_voice(actx, make_service):
    class BrokenTTS(MockTTS):
        name = "broken"

        def synthesize(self, text, rate=1.0):
            raise RuntimeError("voice crashed")

    svc = make_service(actx, tts=BrokenTTS())
    assert await svc.say("still speaking") is True
    assert "voice crashed" in svc.last_tts_error


# ------------------------------------------------------------------ sounds / melodies
async def test_play_sound_and_melody(actx, make_service):
    out = MockOutput(time_scale=0.0)
    svc = make_service(actx, output=out)
    events = collect(actx.bus, ["audio.sound_*"])
    assert await svc.play_sound("tada") is True
    assert await svc.play_melody([("C4", 1), ("F#4", 0.5), ("R", 0.5), ("Bb3", 1)], tempo_bpm=120) is True
    assert [e.topic for e in events] == [Topics.AUDIO_SOUND_STARTED, Topics.AUDIO_SOUND_FINISHED] * 2
    assert events[0].data == {"name": "tada"} and events[1].data["interrupted"] is False
    assert [p.label for p in out.played] == ["sound:tada", "melody:melody"]
    assert await svc.play_melody([("H9", 1)]) is False  # invalid -> False, never raises
    assert await svc.play_melody([]) is False
    assert await svc.play_sound("no-such-sound") is False
    assert await svc.play_sound("../../config/default.yaml") is False
    assert await svc.play_sound("") is False


async def test_play_sound_wait_false_returns_immediately(actx, make_service):
    out = MockOutput(time_scale=1.0)
    svc = make_service(actx, output=out)
    t0 = time.monotonic()
    assert await svc.play_sound("bell", wait=False) is True
    assert time.monotonic() - t0 < 0.2 and svc.status()["playing"] is True
    svc.stop_output()


async def test_queue_is_bounded(actx, make_service):
    out = MockOutput(time_scale=1.0)
    svc = make_service(actx, output=out)
    results = [await svc.play_sound("blip", wait=False) for _ in range(MAX_QUEUE + 4)]
    assert results.count(False) >= 3
    svc.stop_output()


# ------------------------------------------------------------------ volume
async def test_volume_is_clamped_persisted_and_applied(actx, make_service):
    out = MockOutput(time_scale=0.0)
    svc = make_service(actx, output=out)
    assert svc.set_volume(0.95) == 0.8  # safety ceiling (audio.speaker.max_volume)
    assert actx.settings.get("audio.volume") == 0.8
    assert svc.set_volume(-1) == 0.0 and svc.set_volume(0.3) == 0.3
    for bad in ("loud", float("nan"), None):
        with pytest.raises(ValueError):
            svc.set_volume(bad)
    assert svc.status()["volume"] == 0.3
    actx.settings.set("audio.volume", 1.0)  # e.g. the dashboard writing the setting directly
    assert actx.settings.get("audio.volume") == 0.8 and svc.status()["volume"] == 0.8
    await svc.play_sound("chime", volume=1.0)
    assert out.played[-1].peak <= 0.8 * 0.5 + 1e-3  # sfx peak 0.5 * ceiling
    await svc.play_sound("chime", volume=0.5)
    assert out.played[-1].peak == pytest.approx(0.8 * 0.5 * 0.5, abs=0.01)
    svc.set_volume(0.0)
    await svc.play_sound("chime")
    assert out.played[-1].peak == 0.0


async def test_volume_persists_across_restarts(tmp_path):
    db = tmp_path / "lb.db"
    from littlebot.core.context import build_core_context
    from littlebot.hardware.system import MockSystemMonitor
    from littlebot.testing import make_test_config

    ctx = build_core_context(make_test_config(), db_path=db, monitor=MockSystemMonitor())
    svc = AudioService(ctx, output=MockOutput(time_scale=0), input=MockInput(), tts=MockTTS(), stt=MockSTT())
    svc.set_volume(0.42)
    await svc.stop()
    ctx.db.close()
    ctx2 = build_core_context(make_test_config(), db_path=db, monitor=MockSystemMonitor())
    svc2 = AudioService(ctx2, output=MockOutput(time_scale=0), input=MockInput(), tts=MockTTS(), stt=MockSTT())
    assert svc2.status()["volume"] == 0.42
    await svc2.stop()
    ctx2.db.close()


def test_lower_ceiling_clamps_stored_volume():
    ctx = make_test_context(fakes=False, audio={"speaker": {"backend": "mock", "max_volume": 0.4}})
    ctx.settings.set("audio.volume", 0.7)  # stored before the service registers its validator
    svc = AudioService(ctx, output=MockOutput(time_scale=0), input=MockInput(), tts=MockTTS(), stt=MockSTT())
    assert svc.status()["volume"] == 0.4 and svc.status()["max_volume"] == 0.4
    ctx.db.close()


# ------------------------------------------------------------------ listening
async def test_inject_utterance_satisfies_pending_listen(actx, make_service):
    svc = make_service(actx)
    events = collect(actx.bus, ["audio.listening_*", Topics.AUDIO_UTTERANCE])
    task = asyncio.create_task(svc.listen(timeout=3))
    await _wait_event(actx, Topics.AUDIO_LISTENING_STARTED)
    assert svc.status()["listening"] is True
    t0 = time.monotonic()
    svc.inject_utterance("ball")
    assert await asyncio.wait_for(task, 2) == "ball"
    assert time.monotonic() - t0 < 0.5
    assert [e.topic for e in events] == [Topics.AUDIO_LISTENING_STARTED, Topics.AUDIO_LISTENING_STOPPED,
                                         Topics.AUDIO_UTTERANCE]
    assert events[-1].data == {"text": "ball", "confidence": 1.0, "source": "simulated"}
    assert svc.status()["listening"] is False


async def test_injected_before_listen_is_used_by_next_listen(actx, make_service):
    svc = make_service(actx)
    svc.inject_utterance("more")
    svc.inject_utterance("   ")  # ignored
    assert await svc.listen(timeout=1) == "more"
    t0 = time.monotonic()
    assert await svc.listen(timeout=0.2) is None
    assert time.monotonic() - t0 < 1.0


async def test_listen_transcribes_captured_speech(actx, make_service):
    mic = MockInput()
    stt = MockSTT()
    stt.queue_text("hello robot")
    svc = make_service(actx, input=mic, stt=stt)
    mic.inject_speech(0.8, level=0.1)
    events = collect(actx.bus, [Topics.AUDIO_UTTERANCE])
    assert await svc.listen(timeout=2) == "hello robot"
    assert stt.calls == 1 and 0.8 <= stt.last_audio_seconds <= 1.6
    assert events[-1].data["source"] == "stt"
    assert not mic.is_open  # the microphone is only open while listening


async def test_listen_times_out_with_none(actx, make_service):
    stt = MockSTT()
    svc = make_service(actx, stt=stt)
    t0 = time.monotonic()
    assert await svc.listen(timeout=0.3) is None
    assert 0.25 < time.monotonic() - t0 < 1.5 and stt.calls == 0


async def test_listen_waits_for_speech_to_finish(actx, make_service):
    out = MockOutput(time_scale=1.0)
    svc = make_service(actx, output=out)
    events = collect(actx.bus, ["audio.*"])
    speaking = asyncio.create_task(svc.say("one two three"))  # ~1.3 s at rate 0.9
    await _wait_event(actx, Topics.AUDIO_SPEECH_STARTED)
    listening = asyncio.create_task(svc.listen(timeout=3))
    await asyncio.sleep(0.1)
    svc.inject_utterance("yes")
    assert svc.status()["mic_muted"] is True and svc.status()["listening"] is False
    assert await asyncio.wait_for(listening, 5) == "yes"
    assert await speaking is True
    seq = _topics(events, exclude=("audio.level", "audio.device_status"))
    assert seq.index(Topics.AUDIO_SPEECH_FINISHED) < seq.index(Topics.AUDIO_LISTENING_STARTED)


async def test_post_speech_mute_wait(make_service, recording_clock):
    ctx = make_test_context(fakes=False)
    ctx.clock = recording_clock
    svc = make_service(ctx)
    await svc.say("hi")
    svc.inject_utterance("again")
    assert await svc.listen(timeout=1) == "again"
    mute = ctx.config.audio.post_speech_mute_seconds
    assert recording_clock.sleeps and 0 < recording_clock.sleeps[0] <= mute
    assert recording_clock.sleeps[0] == pytest.approx(mute, abs=0.2)
    ctx.db.close()


async def test_mic_is_muted_during_and_after_output(make_service):
    from littlebot.core.clock import Clock

    ctx = make_test_context(fakes=False, audio={"post_speech_mute_seconds": 0.3})
    ctx.clock = Clock(1.0)
    svc = make_service(ctx, output=MockOutput(time_scale=0.0))
    assert svc._mic_muted() is False
    await svc.play_sound("pop")
    assert svc._mic_muted() is True  # just finished: still muted
    await asyncio.sleep(0.4)
    assert svc._mic_muted() is False
    ctx.db.close()


async def test_audio_heard_while_playing_is_discarded(actx, make_service):
    mic = MockInput()
    stt = MockSTT(default_text="echo")
    svc = make_service(actx, input=mic, stt=stt, output=MockOutput(time_scale=1.0))
    task = asyncio.create_task(svc.listen(timeout=1.0))
    await _wait_event(actx, Topics.AUDIO_LISTENING_STARTED)
    await svc.play_sound("bell", wait=False)  # 1.6 s; queued or playing => mic muted from now on
    assert svc.status()["mic_muted"] is True
    mic.inject_speech(0.6, level=0.2, trailing_silence=1.5)  # our own bell "echo" in the mic
    assert await asyncio.wait_for(task, 4) is None
    assert stt.calls == 0
    svc.stop_output()


async def test_concurrent_listen_is_refused(actx, make_service):
    svc = make_service(actx)
    first = asyncio.create_task(svc.listen(timeout=1))
    await _wait_event(actx, Topics.AUDIO_LISTENING_STARTED)
    assert await svc.listen(timeout=1) is None
    svc.inject_utterance("first")
    assert await first == "first"


async def test_stop_all_cancels_listening(actx, make_service):
    svc = make_service(actx)
    task = asyncio.create_task(svc.listen(timeout=5))
    await _wait_event(actx, Topics.AUDIO_LISTENING_STARTED)
    t0 = time.monotonic()
    actx.bus.publish(Topics.STOP_ALL, {"source": "parent"})
    assert await asyncio.wait_for(task, 2) is None
    assert time.monotonic() - t0 < 1.0


# ------------------------------------------------------------------ privacy
async def test_microphone_disabled_listen_returns_none_immediately(actx, make_service):
    mic = MockInput()
    svc = make_service(actx, input=mic)
    actx.privacy.set(microphone_enabled=False)
    events = collect(actx.bus, ["audio.listening_*"])
    svc.inject_utterance("should be ignored")
    t0 = time.monotonic()
    assert await svc.listen(timeout=5) is None
    assert time.monotonic() - t0 < 0.1 and events == [] and mic.opens == 0 and not mic.is_open
    st = svc.status()["microphone"]
    assert st["status"] == "disabled" and "privacy" in st["detail"]
    res = await svc.test_microphone(0.5)
    assert res["ok"] is False and "privacy" in res["detail"]
    actx.privacy.set(microphone_enabled=True)
    assert await svc.listen(timeout=0.2) is None  # the earlier injection was dropped, not queued


async def test_disabling_microphone_mid_listen_closes_stream(actx, make_service):
    mic = MockInput()
    svc = make_service(actx, input=mic)
    events = collect(actx.bus, [Topics.AUDIO_DEVICE_STATUS])
    task = asyncio.create_task(svc.listen(timeout=5))
    await _wait_event(actx, Topics.AUDIO_LISTENING_STARTED)
    await asyncio.sleep(0.05)
    assert mic.is_open
    actx.privacy.set(microphone_enabled=False)
    assert await asyncio.wait_for(task, 2) is None
    assert not mic.is_open
    assert any(e.data["device"] == "microphone" and e.data["status"] == "disabled" for e in events)


async def test_no_files_written_during_listen(actx, make_service, monkeypatch, tmp_path):
    actx.config.data_dir = str(tmp_path)
    writes: list[str] = []
    real_open, real_os_open = builtins.open, os.open

    def guarded_open(file, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax+"):
            writes.append(str(file))
        return real_open(file, mode, *a, **k)

    def guarded_os_open(path, flags, *a, **k):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            writes.append(str(path))
        return real_os_open(path, flags, *a, **k)

    mic = MockInput()
    stt = MockSTT()
    stt.queue_text("all good")
    svc = make_service(actx, input=mic, stt=stt)
    mic.inject_speech(0.6)
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    assert await svc.listen(timeout=2) == "all good"
    monkeypatch.undo()
    assert writes == [] and list(tmp_path.iterdir()) == []


async def test_store_recordings_opt_in_writes_wav(make_service, tmp_path):
    ctx = make_test_context(fakes=False, data_dir=str(tmp_path), audio={"store_recordings": True})
    mic = MockInput()
    stt = MockSTT()
    stt.queue_text("kept")
    svc = make_service(ctx, input=mic, stt=stt)
    mic.inject_speech(0.6)
    assert await svc.listen(timeout=2) == "kept"
    files = list((tmp_path / "recordings").glob("utterance-*.wav"))
    assert len(files) == 1
    ctx.db.close()


# ------------------------------------------------------------------ devices missing / failing
async def test_auto_backends_without_devices_degrade_gracefully(no_devices):
    ctx = make_test_context(fakes=False, audio={"speaker": {"backend": "auto"}, "microphone": {"backend": "auto"}})
    events = collect(ctx.bus, [Topics.AUDIO_DEVICE_STATUS])
    svc = audio_pkg.create_service(ctx)
    try:
        await svc.start()
        st = svc.status()
        assert st["speaker"]["backend"] == "mock" and st["speaker"]["status"] == "simulated"
        assert "no usable speaker" in st["speaker"]["detail"]
        assert st["microphone"]["backend"] == "mock" and st["microphone"]["status"] == "simulated"
        assert svc.service_status == ServiceStatus.DEGRADED and svc.health()["status"] == "degraded"
        assert {(e.data["device"], e.data["status"]) for e in events} >= {("speaker", "simulated"),
                                                                          ("microphone", "simulated")}
        assert await svc.say("I still animate my face") is True  # simulated speaker keeps events flowing
        spk = await svc.test_speaker()
        assert spk["ok"] is False and "USB speaker" in spk["detail"] and "3.5 mm" in spk["detail"]
        mic = await svc.test_microphone(0.3)
        assert mic["ok"] is False and mic["peak"] == 0.0 and "arecord -l" in mic["detail"]
        listing = svc.list_devices()
        assert listing["inputs"] == [] and listing["outputs"] == []
        svc.inject_utterance("simulated still works")
        assert await svc.listen(timeout=1) == "simulated still works"
    finally:
        await svc.stop()
        ctx.db.close()


async def test_explicit_real_backends_missing_never_raise(no_devices):
    ctx = make_test_context(fakes=False, audio={"speaker": {"backend": "sounddevice"},
                                                "microphone": {"backend": "real"}})
    svc = audio_pkg.create_service(ctx)
    try:
        await svc.start()
        st = svc.status()
        assert st["speaker"]["status"] == "unavailable" and st["microphone"]["status"] == "unavailable"
        events = collect(ctx.bus, ["audio.speech_*", "audio.level"])
        t0 = time.monotonic()
        assert await svc.say("nobody hears me") is False  # nothing audible...
        assert await svc.play_sound("pop") is False
        assert time.monotonic() - t0 < 1.0
        # ...but the face still gets the speech events (simulated timing keeps animations / pacing)
        seq = [e.topic for e in events if e.topic != Topics.AUDIO_VISEME]
        assert seq == [Topics.AUDIO_SPEECH_STARTED, Topics.AUDIO_SPEECH_FINISHED]
        assert sum(e.topic == Topics.AUDIO_VISEME for e in events) > 3
        svc.inject_utterance("typed")  # simulator input still works without a mic
        assert await svc.listen(timeout=1) == "typed"
        assert (await svc.test_speaker())["ok"] is False
    finally:
        await svc.stop()
        ctx.db.close()


async def test_backend_none(actx, make_service):
    svc = make_service(actx, output=NullOutput(), input=NullInput())
    assert await svc.say("silent") is False
    assert await svc.play_sound("pop") is False
    assert (await svc.test_speaker())["ok"] is False
    assert (await svc.test_microphone(0.2))["ok"] is False
    svc.inject_utterance("hi")
    assert await svc.listen(timeout=0.5) == "hi"


async def test_speaker_failure_mid_playback_is_handled(actx, make_service):
    out = MockOutput(time_scale=0.0, fail_after_seconds=0.3)
    svc = make_service(actx, output=out)
    svc.reconnect_seconds = 0.0
    await svc.start()
    events = collect(actx.bus, ["audio.*"])
    assert await svc.say(TEXT) is False
    fin = next(e for e in events if e.topic == Topics.AUDIO_SPEECH_FINISHED)
    assert fin.data["interrupted"] is True
    status_ev = [e for e in events if e.topic == Topics.AUDIO_DEVICE_STATUS]
    assert status_ev and status_ev[-1].data["device"] == "speaker" and status_ev[-1].data["status"] == "disconnected"
    assert svc.status()["speaker"]["status"] == "disconnected"
    assert svc.health()["status"] == "degraded"
    out.fail_after_seconds = None  # "plugged back in"
    assert await svc.say("I am back") is True
    assert svc.status()["speaker"]["status"] == "simulated"
    after = events[events.index(status_ev[-1]) + 1:]
    assert any(e.topic == Topics.AUDIO_DEVICE_STATUS and e.data["status"] == "simulated" for e in after)


async def test_disconnected_speaker_is_not_retried_too_often(actx, make_service):
    out = MockOutput(time_scale=0.0, fail_after_seconds=0.0)
    svc = make_service(actx, output=out)
    svc.reconnect_seconds = 60.0
    assert await svc.say("one") is False
    opens = out.opens
    t0 = time.monotonic()
    assert await svc.say("two") is False  # fails fast without hammering the device
    assert out.opens == opens and time.monotonic() - t0 < 0.5


async def test_worker_survives_backend_crash(actx, make_service):
    class Crashy(MockOutput):
        crashed = False

        def play(self, samples, sample_rate, ctrl):
            if not self.crashed:
                self.crashed = True
                raise RuntimeError("driver bug")
            return super().play(samples, sample_rate, ctrl)

    svc = make_service(actx, output=Crashy(time_scale=0.0))
    assert await svc.play_sound("pop") is False
    assert await svc.play_sound("pop") is True


async def test_speaker_missing_at_open_does_not_raise(actx, make_service):
    svc = make_service(actx, output=MockOutput(fail_on_open=True))
    await svc.start()
    assert svc.status()["speaker"]["status"] == "unavailable"
    assert await svc.say("hello") is False


# ------------------------------------------------------------------ "real" devices via fake PortAudio
async def test_real_backends_through_fake_portaudio(fake_sd):
    ctx = make_test_context(fakes=False, audio={"speaker": {"backend": "auto", "device": "usb"},
                                                "microphone": {"backend": "auto"}})
    svc = AudioService(ctx, tts=MockTTS(), stt=MockSTT())
    try:
        await svc.start()
        assert isinstance(svc._out, SoundDeviceOutput) and isinstance(svc._inp, SoundDeviceInput)
        st = svc.status()
        assert st["speaker"]["status"] == "ok" and "USB Audio" in st["speaker"]["device"]
        assert svc.health()["status"] == "running"
        spk = await svc.test_speaker()
        assert spk["ok"] is True and "chime" in spk["detail"]
        written = fake_sd.out_streams[-1].audio()
        assert written.shape[1] == 2 and float(np.max(np.abs(written))) <= 0.8 * 0.5 + 1e-3
        mic = await svc.test_microphone(0.5)
        assert mic["ok"] is True and 0.05 < mic["peak"] < 0.11 and mic["rms"] > 0.03
        assert not svc._inp.is_open and fake_sd.in_streams[-1].closed  # test recording discarded, stream closed
        listing = svc.list_devices()
        assert len(listing["outputs"]) == 2 and listing["selected"]["speaker"]["status"] == "ok"
    finally:
        await svc.stop()
        ctx.db.close()


async def test_recording_a_voice_clip_is_in_memory_and_needs_a_real_microphone(fake_sd, actx, make_service):
    """The dashboard's "Record voice" (littlebot/voices/): sound handed back, the stream closed, nothing kept."""
    ctx = make_test_context(fakes=False, audio={"speaker": {"backend": "auto", "device": "usb"},
                                                "microphone": {"backend": "auto"}})
    svc = AudioService(ctx, tts=MockTTS(), stt=MockSTT())
    try:
        await svc.start()
        clip = await svc.record_clip(1.0)
        assert clip["ok"] is True and clip["rate"] == svc._inp.sample_rate
        assert len(clip["samples"]) >= clip["rate"] * 0.9 and clip["samples"].dtype == np.float32
        assert not svc._inp.is_open and fake_sd.in_streams[-1].closed
        ctx.privacy.set(microphone_enabled=False)
        off = await svc.record_clip(1.0)
        assert off["ok"] is False and off["samples"] is None and "turned off in Privacy" in off["detail"]
    finally:
        await svc.stop()
        ctx.db.close()
    mock = make_service(actx)  # a simulated microphone: no recording
    res = await mock.record_clip(1.0)
    assert res["ok"] is False and "No real microphone" in res["detail"]


async def test_unplugged_usb_speaker_mid_speech(fake_sd):
    ctx = make_test_context(fakes=False, audio={"speaker": {"backend": "sounddevice"}})
    svc = AudioService(ctx, input=MockInput(), tts=MockTTS(), stt=MockSTT())
    svc.reconnect_seconds = 0.0
    try:
        await svc.start()
        fake_sd.fail_write_after = 4096
        assert await svc.say(TEXT) is False
        assert svc.status()["speaker"]["status"] == "disconnected"
        fake_sd.fail_write_after = None
        assert await svc.say("back again") is True
        assert svc.status()["speaker"]["status"] == "ok"
    finally:
        await svc.stop()
        ctx.db.close()


async def test_a_lent_speaker_is_never_probed(fake_sd):
    """Live voice has the USB speaker open: the service doesn't try to "reconnect" it underneath (that can
    stop the call's stream); it does once the speaker is given back."""
    ctx = make_test_context(fakes=False, audio={"speaker": {"backend": "sounddevice"}})
    svc = AudioService(ctx, input=MockInput(), tts=MockTTS(), stt=MockSTT())
    svc.reconnect_seconds = 0.0
    try:
        await svc.start()
        fake_sd.fail_write_after = 0  # busy (a video's browser still had it): the earcon fails
        assert await svc.play_sound("pop") is False
        assert svc.status()["speaker"]["status"] == "disconnected"
        fake_sd.fail_write_after = None
        await svc.suspend("live voice")
        opens: list[int] = []
        real_open = svc._out.open
        svc._out.open = lambda: (opens.append(1), real_open())[1]
        svc._idle_tick(time.monotonic())
        assert svc._try_reconnect_output(svc._out) is False
        assert opens == [] and svc.status()["speaker"]["status"] == "disconnected"
        svc.resume("live voice")
        svc._idle_tick(time.monotonic())
        assert svc.status()["speaker"]["status"] == "ok"
    finally:
        await svc.stop()
        ctx.db.close()


async def test_unplugged_usb_mic_during_listen(fake_sd):
    ctx = make_test_context(fakes=False, audio={"microphone": {"backend": "real"}})
    svc = AudioService(ctx, output=MockOutput(time_scale=0), tts=MockTTS(), stt=MockSTT())
    try:
        await svc.start()
        task = asyncio.create_task(svc.listen(timeout=1.5))
        await _wait_event(ctx, Topics.AUDIO_LISTENING_STARTED)
        await asyncio.sleep(0.1)
        fake_sd.input_dead = True
        svc._inp.stall_seconds = 0.2
        await asyncio.sleep(0.4)
        svc.inject_utterance("typed instead")  # simulated input still answers the listen
        assert await asyncio.wait_for(task, 3) == "typed instead"
        assert svc.status()["microphone"]["status"] == "disconnected"
    finally:
        await svc.stop()
        ctx.db.close()
