"""Choosing PipeWire's echo-cancelled devices (scripts/setup_echo_cancel.sh) when they exist, on a fake PortAudio.

With them, the audio service, the wake word (it reads the service's own microphone) and live voice all
open the same two devices; without them everything is as before (the USB hw: devices).
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from littlebot.audio import devices as dev
from littlebot.audio.devices import SoundDeviceInput, SoundDeviceOutput
from littlebot.audio.service import AudioService
from littlebot.audio.stt import MockSTT
from littlebot.audio.tts import MockTTS
from littlebot.testing import make_test_context

# PortAudio lists an ALSA plugin PCM with channels both ways (opening it the other way just reaches PipeWire's default)
EC_MIC = {"name": "littlebot_ec_mic", "max_input_channels": 64, "max_output_channels": 64, "default_samplerate": 44100.0}
EC_SPK = {"name": "littlebot_ec_speaker", "max_input_channels": 64, "max_output_channels": 64,
          "default_samplerate": 44100.0}


def add(fake_sd, *devs) -> list[int]:
    out = []
    for d in devs:
        i = len(fake_sd.devices)
        fake_sd.devices.append({"index": i, "hostapi": 0, **d})
        fake_sd.rates[i] = {44100, 48000}
        out.append(i)
    return out


@pytest.fixture(autouse=True)
def _auto_mode(monkeypatch):
    monkeypatch.setattr(dev, "_ec_mode", "auto")
    monkeypatch.setattr(dev, "_ec_missing_warned", False)


@pytest.fixture
def with_ec(fake_sd):
    mic, spk = add(fake_sd, EC_MIC, EC_SPK)
    return fake_sd, mic, spk


def test_auto_picks_the_echo_cancelled_pair(with_ec):
    _sd, mic, spk = with_ec
    assert dev.find_device(None, "input")["index"] == mic
    assert dev.find_device(None, "output")["index"] == spk  # not the mic's PCM, though it lists outputs too
    assert dev.find_device("auto", "output")["index"] == spk and dev.find_device("default", "input")["index"] == mic
    assert dev.echo_cancel_devices() == (dev.find_device(None, "input"), dev.find_device(None, "output"))
    # a device named in config wins: the parent chose it
    assert dev.find_device("USB PnP", "input")["name"].startswith("USB PnP")
    assert dev.find_device("system", "output")["index"] == 1
    assert dev.is_echo_cancel_device("littlebot_ec_mic", "input") and not dev.is_echo_cancel_device("littlebot_ec_mic",
                                                                                                    "output")
    assert not dev.is_echo_cancel_device({"name": "USB PnP Sound Device: Mic (hw:3,0)"})


def test_without_both_it_is_the_usb_devices_as_before(fake_sd):
    assert dev.echo_cancel_devices() is None
    assert dev.find_device(None, "output")["name"].startswith("USB Audio")
    add(fake_sd, EC_MIC)  # only half of it: cancels nothing, so not used
    assert dev.echo_cancel_devices() is None
    assert dev.find_device(None, "input")["name"].startswith("USB PnP")


def test_off_never_uses_them_and_on_warns_once_when_missing(fake_sd, caplog):
    add(fake_sd, EC_MIC, EC_SPK)
    dev.set_echo_cancel("off")
    assert dev.find_device(None, "input")["name"].startswith("USB PnP")
    assert "audio.echo_cancel: off" in dev.echo_cancel_note()
    del fake_sd.devices[-2:]
    dev.set_echo_cancel("on")
    caplog.set_level(logging.WARNING, logger="littlebot.audio.devices")
    for _ in range(3):
        assert dev.find_device(None, "output")["name"].startswith("USB Audio")
    assert caplog.text.count("echo-cancelled devices") == 1 and "setup_echo_cancel.sh" in caplog.text
    dev.set_echo_cancel("bogus")
    assert dev.echo_cancel_mode() == "auto"


def test_backends_open_them_by_default(with_ec):
    sd, mic, _spk = with_ec
    out = SoundDeviceOutput(None, 22050)
    out.open()
    assert out.device_name == "littlebot_ec_speaker" and out.sample_rate == 44100  # PipeWire takes any rate
    inp = SoundDeviceInput(None, 16000)
    inp.probe()
    inp.open()
    try:
        assert inp.device_name == "littlebot_ec_mic" and sd.in_streams[-1].device == mic
    finally:
        inp.close()


async def test_audio_service_says_which_way_the_sound_goes(fake_sd, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.audio")
    ctx = make_test_context(fakes=False, audio={"speaker": {"backend": "auto"}, "microphone": {"backend": "auto"}})
    svc = AudioService(ctx, tts=MockTTS(), stt=MockSTT())
    try:
        await svc.start()
        svc._ensure_devices()
        assert not svc.echo_cancelling
        assert "audio: echo cancelling off (not set up on this robot: scripts/setup_echo_cancel.sh)" in caplog.text
    finally:
        await svc.stop()
    add(fake_sd, EC_MIC, EC_SPK)
    caplog.clear()
    svc = AudioService(ctx, tts=MockTTS(), stt=MockSTT())
    try:
        await svc.start()
        svc._ensure_devices()
        assert svc.echo_cancelling
        assert "audio: echo cancelling on (PipeWire: littlebot_ec_mic / littlebot_ec_speaker)" in caplog.text
        assert svc.status()["speaker"]["device"] == "littlebot_ec_speaker"
    finally:
        await svc.stop()


async def test_the_service_wake_word_and_live_voice_share_the_same_devices(with_ec):
    """Through PipeWire the devices are not exclusive: the call opens the same ones the service (and the wake
    word, which reads the service's microphone) use, and barge-in comes on with them."""
    pytest.importorskip("av")
    from littlebot.chatgpt.live_audio import BargeInSettings, LiveAudio

    sd, mic, spk = with_ec
    ctx = make_test_context(fakes=False, audio={"speaker": {"backend": "auto"}, "microphone": {"backend": "auto"}})
    svc = AudioService(ctx, tts=MockTTS(), stt=MockSTT())
    await svc.start()
    try:
        svc._ensure_devices()
        bg = svc.background_input()  # what the wake word reads
        assert bg is svc._inp and bg.device_name == "littlebot_ec_mic"
        bg.open()  # the wake word listening ...
        audio = LiveAudio(mic=None, speaker=None, volume=lambda: 0.5, sd=sd, barge_in=BargeInSettings())
        audio.open(asyncio.get_running_loop())  # ... while a call opens the same microphone
        try:
            assert audio.device_names == ("littlebot_ec_mic", "littlebot_ec_speaker")
            assert {s.device for s in sd.in_streams if not s.closed} == {mic}
            assert sd.out_streams[-1].device == spk
            assert audio.echo_cancelled and audio.barge_in_on
            assert await asyncio.wait_for(audio.read_frame(), 2) is not None  # the call hears it too
        finally:
            audio.close()
            bg.close()
    finally:
        await svc.stop()


async def test_live_audio_on_the_usb_devices_stays_half_duplex(fake_sd):
    pytest.importorskip("av")
    from littlebot.chatgpt.live_audio import BargeInSettings, LiveAudio

    audio = LiveAudio(mic=None, speaker=None, volume=lambda: 0.5, sd=fake_sd, barge_in=BargeInSettings(mode="auto"))
    audio.open(asyncio.get_running_loop())
    try:
        assert not audio.echo_cancelled and not audio.barge_in_on
        assert "half-duplex" in audio._mode_note()
    finally:
        audio.close()
    forced = LiveAudio(mic=None, speaker=None, volume=lambda: 0.5, sd=fake_sd, barge_in=BargeInSettings(mode="on"))
    forced.open(asyncio.get_running_loop())  # a headset / hardware echo cancelling: the parent forces it
    try:
        assert forced.barge_in_on and not forced.echo_cancelled
    finally:
        forced.close()
