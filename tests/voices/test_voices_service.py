"""Recognising the family's voices (littlebot/voices/service.py, store.py), with a fake model.

Nothing here listens to a real voice: ``FakeSpeakerEngine`` (littlebot/testing.py) makes fake speaker k a
steady tone, with a voiceprint of its own, so this tests the matching, the storage, the learning rules and
the privacy gates, not how well the real model tells people apart (test_voices_real_model.py does a little).
"""

from __future__ import annotations

import logging
import sqlite3

import numpy as np
import pytest
from voice_helpers import add_person, enrol, say

from littlebot.testing import FakeSpeakerEngine, make_test_context, speaker_tone
from littlebot.voices import service as S
from littlebot.voices import store as vstore
from littlebot.voices.engine import (
    EngineUnavailable,
    speech_only,
    to_model_rate,
    windows,
)
from littlebot.voices.service import VoiceRecognition


# ------------------------------------------------------------------ the sound helpers
def test_sound_is_resampled_to_16k_mono_and_pauses_are_left_out():
    pcm = speaker_tone(0, 1.0, 48000, int16=True, silence=1.0)  # 1 s of talking, then 1 s of quiet
    x = to_model_rate(pcm, 48000)
    assert x.dtype == np.float32 and len(x) == 32000 and 0.15 < float(np.abs(x).max()) < 0.25
    speech = speech_only(x)
    assert 0.95 <= len(speech) / 16000 <= 1.1  # the pause is gone (a few frames of hangover stay)
    stereo = np.stack([speaker_tone(0, 0.5), speaker_tone(0, 0.5)], axis=1)
    assert to_model_rate(stereo, 16000).shape == (8000,)
    assert len(speech_only(np.zeros(16000, np.float32))) == 0  # silence is never speech


def test_speech_is_cut_into_windows_with_a_short_end_joining_the_one_before():
    parts = windows(np.zeros(int(7.5 * 16000), np.float32), 3.0, min_seconds=1.0)
    assert [len(p) / 16000 for p in parts] == [3.0, 3.0, 1.5]
    parts = windows(np.zeros(int(6.5 * 16000), np.float32), 3.0, min_seconds=1.0)
    assert [len(p) / 16000 for p in parts] == [3.0, 3.5]


# ------------------------------------------------------------------ matching
def test_best_match_needs_the_threshold_and_a_clear_margin():
    e = FakeSpeakerEngine()
    ali, mum = vstore.Voice(1, "Ali", "dad", e.table[0]), vstore.Voice(2, "Sara", "mum", e.table[1])
    who, best, second, closest = S.best_match(e.table[0], [ali, mum])
    assert who is ali and best == pytest.approx(1.0) and second < 0.3
    blend = (e.table[0] + e.table[1]) / np.linalg.norm(e.table[0] + e.table[1])
    who, best, second, closest = S.best_match(blend, [ali, mum])
    assert who is None and best > S.MATCH_THRESHOLD and closest in (ali, mum)  # close to both: unsure
    assert S.best_match(e.table[2], [ali, mum])[0] is None  # nobody's voice
    assert S.best_match(e.table[0], [])[:3] == (None, 0.0, -1.0)


async def test_identify_names_who_is_talking_and_says_nothing_when_unsure(voices):
    ali, sara = add_person(voices, "Ali", "dad"), add_person(voices, "Sara", "mum")
    assert (await enrol(voices, ali, 0))["ok"] and (await enrol(voices, sara, 1))["ok"]
    m = await voices.identify(say(0, 2.0), 48000)
    assert m is not None and (m.person_id, m.name, m.role, m.is_child) == (ali, "Ali", "dad", False)
    assert m.score == pytest.approx(1.0) and m.label == "Ali (dad)"
    assert (await voices.identify(say(1, 1.5), 48000)).name == "Sara"
    assert await voices.identify(say(2, 2.0), 48000) is None  # someone else: never a guess
    assert await voices.identify(say(0, 2.0) + say(1, 2.0), 48000) is None  # two at once: unsure
    heard = await voices.analyse(say(0, 2.0) + say(1, 2.0), 48000)
    assert heard.reason == "unsure" and heard.best_id in (ali, sara) and heard.match is None
    # 16 kHz float works as well as the call's 48 kHz int16
    assert (await voices.identify(speaker_tone(0, 1.5), 16000)).name == "Ali"


async def test_too_little_speech_is_never_named(voices):
    ali = add_person(voices, "Ali", "dad")
    await enrol(voices, ali, 0)
    heard = await voices.analyse(say(0, 0.5, silence=2.0), 48000)
    assert heard.reason == "too_short" and heard.match is None and heard.speech_s < S.MIN_SPEECH_S
    assert await voices.identify(say(0, 0.8), 48000) is None  # below MIN_SPEECH_S by default
    # a caller may ask for less (down to SHORT_MIN_SPEECH_S), with a stricter threshold
    assert (await voices.identify(say(0, 0.8), 48000, min_speech_s=0.6)).name == "Ali"
    assert (await voices.analyse(say(0, 0.4), 48000, min_speech_s=0.1)).reason == "too_short"  # never below 0.6


async def test_a_short_clip_needs_a_closer_voice(vctx):
    e = FakeSpeakerEngine()
    voices = VoiceRecognition(vctx, engine=e)
    ali = add_person(voices, "Ali", "dad")
    other = add_person(voices, "Gran")
    # Ali's voiceprint is only just close enough to speaker 0's (0.52)
    a = e.table[0]
    u = e.table[5] - np.dot(e.table[5], a) * a
    voices.store.add(ali, 0.52 * a + np.sqrt(1 - 0.52 ** 2) * u / np.linalg.norm(u), vstore.RECORDED)
    voices.store.add(other, e.table[6], vstore.RECORDED)
    score = float(np.dot(a, voices.store.known()[0].centre))
    assert S.MATCH_THRESHOLD <= score < S.MATCH_THRESHOLD + S.SHORT_EXTRA
    assert (await voices.analyse(say(0, 1.5), 48000)).reason == "ok"
    assert (await voices.analyse(say(0, 0.8), 48000, min_speech_s=0.6)).reason == "unsure"


async def test_the_threshold_setting_is_used_and_kept_sensible(vctx):
    voices = VoiceRecognition(vctx, engine=FakeSpeakerEngine())
    assert voices.threshold == S.MATCH_THRESHOLD
    vctx.config.voices.match_threshold = 5.0
    assert voices.threshold == 0.9
    vctx.config.voices.match_threshold = 0.0
    assert voices.threshold == 0.3


async def test_identify_now_works_from_other_threads_at_once(voices):
    import concurrent.futures

    ali, sara = add_person(voices, "Ali", "dad"), add_person(voices, "Sara", "mum")
    await enrol(voices, ali, 0)
    await enrol(voices, sara, 1)
    clips = [(say(k % 2, 1.5), "Ali" if k % 2 == 0 else "Sara") for k in range(12)]
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        names = list(pool.map(lambda c: voices.identify_now(c[0], 48000).name, clips))
    assert names == [c[1] for c in clips]


async def test_the_little_ones_voice_is_marked_as_the_child(voices, vctx):
    vctx.settings.set("profile", {"nickname": "Amara"})
    amara, gran = add_person(voices, "Amara", ""), add_person(voices, "Gran", "the baby")
    await enrol(voices, amara, 0)
    await enrol(voices, gran, 1)
    assert (await voices.identify(say(0, 2.0), 48000)).is_child  # the nickname
    assert (await voices.identify(say(1, 2.0), 48000)).is_child  # the role


# ------------------------------------------------------------------ the dashboard's recording
async def test_a_recording_keeps_a_few_voiceprints_and_never_the_sound(voices, vctx):
    ali = add_person(voices, "Ali", "dad")
    result = await enrol(voices, ali, 0, seconds=12.0)
    assert result["ok"] and result["samples"] == S.ENROL_MAX_SAMPLES and result["speech_s"] == pytest.approx(12, abs=0.3)
    assert voices.store.counts()[ali] == {"samples": 4, "recorded": 4, "learned": 0,
                                          "last_at": voices.store.counts()[ali]["last_at"]}
    rows = vctx.db.query("SELECT features, model, source FROM voice_samples")
    assert len(rows) == 4 and all(len(r["features"]) == 192 * 4 and r["source"] == "dashboard" for r in rows)
    assert {r["model"] for r in rows} == {"fake-speaker.onnx"}
    # only numbers fit: a row can't hold a recording
    with pytest.raises(sqlite3.IntegrityError):
        vctx.db.execute("INSERT INTO voice_samples (person_id, features, model, source, taken_at) "
                        "VALUES (?, ?, 'x', 'dashboard', 'now')", (ali, b"\0" * 32000))


async def test_a_recording_that_is_too_short_or_not_one_voice_is_not_kept(voices):
    ali, sara = add_person(voices, "Ali", "dad"), add_person(voices, "Sara", "mum")
    short = await enrol(voices, ali, 0, seconds=2.0)
    assert not short["ok"] and short["error"] == "too_little" and "2 seconds" in short["detail"]
    mixed = np.concatenate([say(0, 6.0), say(2, 6.0)])  # Ali, then someone else
    many = await voices.enrol(ali, mixed, 48000)
    assert not many["ok"] and many["error"] == "many_voices" and "only Ali talks" in many["detail"]
    assert voices.store.count(ali) == 0
    assert (await enrol(voices, sara, 1))["ok"]
    other = await enrol(voices, ali, 1)  # Sara read Ali's sentences
    assert not other["ok"] and other["error"] == "looks_like_other" and "more like Sara than Ali" in other["detail"]
    assert voices.store.count(ali) == 0 and voices.store.count(sara) == 4
    assert (await enrol(voices, sara, 1))["ok"]  # Sara again: fine (it's her own)
    with pytest.raises(KeyError):
        await enrol(voices, 999, 0)


async def test_recordings_are_capped_and_the_oldest_goes(voices, vctx):
    ali = add_person(voices, "Ali", "dad")
    for _ in range(3):
        await enrol(voices, ali, 0)
    c = voices.store.counts()[ali]
    assert c["recorded"] == vstore.MAX_RECORDED and c["samples"] == vstore.MAX_RECORDED
    ids = [r["id"] for r in vctx.db.query("SELECT id FROM voice_samples ORDER BY id")]
    assert min(ids) > 4  # the first recording's are gone


# ------------------------------------------------------------------ learning in chats
async def test_saying_im_ali_learns_their_voice_from_that_turn(voices):
    ali = add_person(voices, "Ali", "dad")
    result = await voices.learn(ali, say(0, 3.0), 48000)
    assert result == {"ok": True, "reason": "claimed", "first": True, "samples": 1}
    assert voices.store.counts()[ali]["learned"] == 1
    assert (await voices.identify(say(0, 2.0), 48000)).name == "Ali"  # recognised from then on
    again = await voices.learn(ali, say(0, 3.0), 48000)
    assert again["ok"] and not again["first"] and again["samples"] == 2


async def test_learning_refuses_what_isnt_clearly_their_voice(voices, vctx):
    vctx.settings.set("profile", {"nickname": "Amara"})
    ali, sara, amara = add_person(voices, "Ali", "dad"), add_person(voices, "Sara", "mum"), add_person(voices, "Amara")
    await enrol(voices, sara, 1)
    assert (await voices.learn(ali, say(0, 0.5), 48000))["reason"] == "too_short"
    assert (await voices.learn(ali, np.concatenate([say(0, 1.5), say(2, 1.5)]), 48000))["reason"] == "many_voices"
    assert (await voices.learn(ali, say(1, 3.0), 48000))["reason"] == "someone_else"  # that's Sara's voice
    assert (await voices.learn(amara, say(2, 3.0), 48000))["reason"] == "child"
    assert (await voices.learn(999, say(0, 3.0), 48000))["reason"] == "unknown_person"
    assert voices.store.count(ali) == 0 and voices.store.count(amara) == 0
    await enrol(voices, ali, 0)
    # Ali has a voiceprint: someone clearly different saying "I'm Ali" isn't learned
    assert (await voices.learn(ali, say(3, 3.0), 48000))["reason"] == "different_voice"
    assert voices.store.counts()[ali]["learned"] == 0


async def test_learned_voiceprints_are_capped_and_never_push_out_the_recording(voices):
    ali = add_person(voices, "Ali", "dad")
    await enrol(voices, ali, 0)
    for _ in range(vstore.MAX_LEARNED + 3):
        assert (await voices.learn(ali, say(0, 3.0), 48000))["ok"]
    c = voices.store.counts()[ali]
    assert c["learned"] == vstore.MAX_LEARNED and c["recorded"] == 4


async def test_being_very_sure_improves_the_voiceprint_at_most_so_often(voices, monkeypatch):
    ali = add_person(voices, "Ali", "dad")
    await enrol(voices, ali, 0)
    heard = await voices.analyse(say(0, 3.0), 48000, one_voice=True)
    assert heard.one_voice is True and heard.match.margin >= S.LEARN_MARGIN
    assert voices.improve(heard) and voices.store.counts()[ali]["learned"] == 1
    assert not voices.improve(heard)  # again so soon: no
    monkeypatch.setattr(S, "LEARN_GAP_S", 0.0)
    assert voices.improve(heard)
    assert not voices.improve(await voices.analyse(say(0, 1.5), 48000, one_voice=True))  # too little speech
    assert not voices.improve(await voices.analyse(say(0, 3.0), 48000))  # one voice not checked
    await enrol(voices, add_person(voices, "Sara", "mum"), 1)
    both = await voices.analyse(say(0, 3.0) + say(1, 3.0), 48000, one_voice=True)  # both talking at once
    assert both.match is None and not voices.improve(both)
    assert voices.store.counts()[ali]["learned"] == 2


# ------------------------------------------------------------------ the store
async def test_deleting_a_person_or_forgetting_deletes_their_voiceprints(voices, vctx):
    ali, sara = add_person(voices, "Ali", "dad"), add_person(voices, "Sara", "mum")
    await enrol(voices, ali, 0)
    await enrol(voices, sara, 1)
    assert voices.forget(ali) == 4 and voices.store.count(ali) == 0 and voices.store.count(sara) == 4
    assert await voices.identify(say(0, 2.0), 48000) is None
    voices.store.people.delete_person(sara)  # the person goes: so do their voiceprints
    assert vctx.db.query("SELECT COUNT(*) AS n FROM voice_samples")[0]["n"] == 0
    assert not voices.store.has_samples() and voices.store.known() == []
    with pytest.raises(KeyError):
        voices.store.add(sara, FakeSpeakerEngine().table[0], vstore.RECORDED)


async def test_a_rename_shows_at_once_and_another_models_voiceprints_are_ignored(voices, vctx):
    ali = add_person(voices, "Ali", "dad")
    await enrol(voices, ali, 0)
    voices.store.people.update_person(ali, name="Alistair")
    assert (await voices.identify(say(0, 2.0), 48000)).name == "Alistair"
    other = vstore.VoiceStore(vctx.db, "another-model.onnx")
    assert other.known() == [] and other.counts() == {} and not other.has_samples()
    other.add(ali, np.ones(256, np.float32), vstore.RECORDED)
    assert [v.samples for v in voices.store.known()] == [4]  # its own model's only
    assert voices.forget(ali) == 5  # forgetting takes every model's


async def test_forget_all_faces_also_forgets_voices(voices, vctx):
    ali = add_person(voices, "Ali", "dad")
    await enrol(voices, ali, 0)
    assert voices.store.forget_all() == 4 and not voices.store.has_samples()


# ------------------------------------------------------------------ privacy: switched off, nothing is processed
async def test_switched_off_nothing_is_looked_at(vctx, caplog):
    engine = FakeSpeakerEngine()
    voices = VoiceRecognition(vctx, engine=engine)
    await voices.start()
    ali = add_person(voices, "Ali", "dad")
    await enrol(voices, ali, 0)
    calls = engine.calls
    vctx.privacy.set(voice_recognition_enabled=False)
    assert voices.blocked_reason() == "off"
    assert (await voices.analyse(say(0, 2.0), 48000)).reason == "off"
    assert await voices.identify(say(0, 2.0), 48000) is None
    assert voices.identify_now(say(0, 2.0), 48000) is None
    with pytest.raises(EngineUnavailable):
        await enrol(voices, ali, 0)
    assert (await voices.learn(ali, say(0, 3.0), 48000))["ok"] is False
    assert engine.calls == calls  # the model never saw a thing
    vctx.privacy.set(voice_recognition_enabled=True, microphone_enabled=False)
    assert voices.blocked_reason() == "microphone_off" and await voices.identify(say(0, 2.0), 48000) is None
    assert engine.calls == calls
    await voices.stop()


def test_off_by_default():
    ctx = make_test_context()
    try:
        assert ctx.privacy.voice_recognition_enabled is False
        assert ctx.privacy.snapshot()["voice_recognition_enabled"] is False
        assert VoiceRecognition(ctx, engine=FakeSpeakerEngine()).status()["blocked"] == "off"
    finally:
        ctx.db.close()


async def test_logs_never_hold_names_or_numbers(voices, caplog):
    caplog.set_level(logging.DEBUG, logger="littlebot.voices")
    ali = add_person(voices, "Ali", "dad")
    await enrol(voices, ali, 0)
    await voices.learn(ali, say(0, 3.0), 48000)
    await voices.identify(say(0, 2.0), 48000)
    voices.forget(ali)
    assert "recording kept" in caplog.text and "Ali" not in caplog.text and "dad" not in caplog.text


# ------------------------------------------------------------------ not installed: degrades with what to do
async def test_without_the_package_or_model_it_says_what_to_install(vctx, tmp_path, monkeypatch):
    monkeypatch.setattr(S, "sherpa_installed", lambda: False)
    voices = VoiceRecognition(vctx, root=tmp_path)
    st = voices.status()
    assert st["blocked"] == "no_package" and not st["models_ready"] and "pip install -e '.[voices]'" in st["models_detail"]
    assert (await voices.analyse(say(0, 2.0), 48000)).reason == "no_package"
    assert await voices.identify(say(0, 2.0), 48000) is None
    monkeypatch.setattr(S, "sherpa_installed", lambda: True)
    voices._package = None
    st = voices.status()
    assert st["blocked"] == "no_model" and "download_models.py voices" in st["blocked_detail"]
    ali = add_person(voices, "Ali", "dad")
    with pytest.raises(EngineUnavailable) as e:
        await voices.enrol(ali, say(0, 9.0), 48000)
    assert e.value.reason == "no_model"


async def test_a_broken_model_file_says_load_failed(vctx, tmp_path, monkeypatch):
    pytest.importorskip("sherpa_onnx")
    (tmp_path / "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx").write_bytes(b"not a model" * 100)
    voices = VoiceRecognition(vctx, root=tmp_path)
    assert voices.models_problem() is None  # looks fine until it's loaded
    heard = await voices.analyse(say(0, 2.0), 48000)
    assert heard.reason == "load_failed"
    assert voices.status()["blocked"] == "load_failed" and "--force" in voices.status()["blocked_detail"]
    vctx.privacy.set(voice_recognition_enabled=False)  # switching off and on tries again
    vctx.privacy.set(voice_recognition_enabled=True)
    assert voices.models_problem() is None


async def test_the_model_is_loaded_on_first_use_and_dropped_when_switched_off(vctx, monkeypatch):
    made: list[FakeSpeakerEngine] = []

    def fake_engine(path, *, threads=2):
        made.append(FakeSpeakerEngine())
        return made[-1]

    monkeypatch.setattr(S, "SpeakerEngine", fake_engine)
    monkeypatch.setattr(S, "sherpa_installed", lambda: True)
    voices = VoiceRecognition(vctx)
    monkeypatch.setattr(voices, "model_path", lambda: type("P", (), {"is_file": lambda self: True})())
    await voices.start()
    assert not voices.status()["loaded"] and not made  # nothing loaded up front
    ali = add_person(voices, "Ali", "dad")
    assert (await voices.enrol(ali, say(0, 9.0), 48000))["ok"]
    assert voices.status()["loaded"] and len(made) == 1
    await voices.identify(say(0, 2.0), 48000)
    assert len(made) == 1  # kept while in use
    vctx.privacy.set(voice_recognition_enabled=False)
    assert not voices.status()["loaded"]
    vctx.privacy.set(voice_recognition_enabled=True)
    voices._unload_if_idle()  # nothing to drop
    await voices.identify(say(0, 2.0), 48000)
    assert len(made) == 2
    monkeypatch.setattr(S, "UNLOAD_IDLE_S", 0.0)
    voices._unload_if_idle()
    assert not voices.status()["loaded"]
    await voices.stop()


async def test_hey_littlebot_warms_the_model_up(vctx, monkeypatch):
    import asyncio

    made: list[FakeSpeakerEngine] = []
    monkeypatch.setattr(S, "SpeakerEngine", lambda path, *, threads=2: made.append(FakeSpeakerEngine()) or made[-1])
    monkeypatch.setattr(S, "sherpa_installed", lambda: True)
    voices = VoiceRecognition(vctx)
    monkeypatch.setattr(voices, "model_path", lambda: type("P", (), {"is_file": lambda self: True})())
    await voices.start()
    vctx.bus.publish(S.WAKE_TOPIC, {"phrase": "hey little bot"})
    await asyncio.sleep(0.1)
    assert not made  # nobody has a voiceprint: nothing to recognise, nothing loaded
    ali = add_person(voices, "Ali", "dad")
    voices.store.add(ali, FakeSpeakerEngine().table[0], vstore.RECORDED)
    vctx.bus.publish(S.WAKE_TOPIC, {"phrase": "hey little bot"})
    for _ in range(50):
        if voices.status()["loaded"]:
            break
        await asyncio.sleep(0.02)
    assert voices.status()["loaded"] and len(made) == 1
    await voices.stop()


def test_view_has_counts_and_the_sentences_but_never_numbers(vctx):
    voices = VoiceRecognition(vctx, engine=FakeSpeakerEngine())
    ali = add_person(voices, "Ali", "dad")
    voices.store.add(ali, FakeSpeakerEngine().table[0], vstore.RECORDED)
    view = voices.view()
    assert view["counts"][str(ali)]["samples"] == 1 and len(view["sentences"]) == 3
    assert view["record_seconds"] == S.RECORD_SECONDS and view["people_with_voices"] == 1
    assert "features" not in repr(view) and "centre" not in repr(view)


def test_get_voices_makes_one_and_keeps_it(vctx):
    from littlebot.voices import get_voices

    first = get_voices(vctx)
    assert get_voices(vctx) is first and vctx.voices is first
