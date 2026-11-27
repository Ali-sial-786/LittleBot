"""Dashboard: recording and forgetting voices on Who's who (/api/people/{id}/voice), with a fake model.

The fake audio "hears" ``ctx.audio.clip``; the fake model makes speaker k a tone (littlebot/testing.py).
Only counts reach the browser, never a voiceprint's numbers or the recording.
"""

from __future__ import annotations

from typing import Any

import pytest

from littlebot.testing import FakeSpeakerEngine, speaker_tone
from littlebot.voices.service import SENTENCES, VoiceRecognition


@pytest.fixture
def voices(dctx):
    v = dctx.voices = VoiceRecognition(dctx, engine=FakeSpeakerEngine())
    return v


def no_numbers(data: Any) -> None:
    """No voiceprint or sound anywhere in a response."""
    if isinstance(data, dict):
        assert not {"features", "centre", "voiceprint", "embedding", "samples_data"} & set(data), data.keys()
        for v in data.values():
            no_numbers(v)
    elif isinstance(data, list):
        assert len(data) < 100
        for v in data:
            no_numbers(v)


async def add(authed, name: str, role: str = "") -> int:
    return (await authed.post("/api/people", json={"name": name, "role": role})).json()["person"]["id"]


async def test_recording_needs_a_login(client, voices):
    assert (await client.post("/api/people/1/voice", json={})).status_code in (401, 403)
    assert (await client.delete("/api/people/1/voice")).status_code in (401, 403)


async def test_switched_off_by_default_and_who_is_who_shows_the_voice_status(authed, voices):
    assert (await authed.get("/api/privacy")).json()["voice_recognition_enabled"] is False
    pid = await add(authed, "Ali", "dad")
    d = (await authed.get("/api/people")).json()
    assert d["voices"]["enabled"] is False and d["voices"]["blocked"] == "off"
    assert "Turn it on in Privacy" in d["voices"]["blocked_detail"]
    assert d["voices"]["sentences"] == list(SENTENCES) and d["voices"]["record_seconds"] == 20
    assert d["people"][0]["voice_samples"] == 0 and d["people"][0]["id"] == pid
    no_numbers(d)


async def test_recording_needs_the_privacy_switch_and_the_microphone(authed, voices, dctx):
    pid = await add(authed, "Ali", "dad")
    dctx.audio.clip = (speaker_tone(0, 12.0, 48000, int16=True), 48000)
    r = await authed.post(f"/api/people/{pid}/voice", json={})
    assert r.status_code == 409 and "switched off" in r.json()["detail"]
    assert not dctx.audio.called("record_clip")  # nothing was recorded
    assert (await authed.put("/api/privacy", json={"voice_recognition_enabled": True})).status_code == 200
    assert dctx.privacy.voice_recognition_enabled is True
    await authed.put("/api/privacy", json={"microphone_enabled": False})
    r = await authed.post(f"/api/people/{pid}/voice", json={})
    assert r.status_code == 409 and "microphone is turned off" in r.json()["detail"]
    assert not dctx.audio.called("record_clip")


async def test_recording_a_voice_keeps_voiceprints_only(authed, voices, dctx):
    dctx.privacy.set(voice_recognition_enabled=True)
    pid = await add(authed, "Ali", "dad")
    dctx.audio.clip = (speaker_tone(0, 12.0, 48000, int16=True), 48000)
    r = await authed.post(f"/api/people/{pid}/voice", json={"seconds": 20})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["samples"] == 4 and d["people"][0]["voice_samples"] == 4
    assert d["voices"]["counts"][str(pid)]["recorded"] == 4 and d["voices"]["people_with_voices"] == 1
    assert "samples" not in d["person"] or d["person"]["samples"] == 0  # face samples untouched
    no_numbers(d)
    assert dctx.audio.called("record_clip") == [((20.0,), {})]
    assert (await voices.identify(speaker_tone(0, 2.0, 48000, int16=True), 48000)).name == "Ali"
    rows = dctx.db.query("SELECT * FROM voice_samples")
    assert len(rows) == 4 and all(len(r["features"]) == 768 for r in rows)  # 192 numbers, no sound


async def test_a_recording_that_wasnt_good_says_why(authed, voices, dctx):
    dctx.privacy.set(voice_recognition_enabled=True)
    pid = await add(authed, "Ali", "dad")
    dctx.audio.clip = (speaker_tone(0, 2.0, 48000, int16=True, silence=10.0), 48000)
    d = (await authed.post(f"/api/people/{pid}/voice", json={})).json()
    assert d["ok"] is False and d["error"] == "too_little" and "Read all three sentences" in d["detail"]
    assert d["people"][0]["voice_samples"] == 0
    dctx.audio.clip = None  # the microphone gave nothing
    d = (await authed.post(f"/api/people/{pid}/voice", json={})).json()
    assert d["ok"] is False and d["error"] == "recording"


async def test_odd_requests_are_refused(authed, voices, dctx):
    dctx.privacy.set(voice_recognition_enabled=True)
    assert (await authed.post("/api/people/999/voice", json={})).status_code == 404
    assert (await authed.delete("/api/people/999/voice")).status_code == 404
    pid = await add(authed, "Ali")
    assert (await authed.post(f"/api/people/{pid}/voice", json={"seconds": 600})).status_code == 422
    assert (await authed.post(f"/api/people/{pid}/voice", json={"samples": [0.1] * 100})).status_code == 422


async def test_missing_package_or_model_says_what_to_install(authed, dctx, tmp_path, monkeypatch):
    from littlebot.voices import service as S

    monkeypatch.setattr(S, "sherpa_installed", lambda: False)
    dctx.voices = VoiceRecognition(dctx, root=tmp_path)
    dctx.privacy.set(voice_recognition_enabled=True)
    d = (await authed.get("/api/people")).json()
    assert d["voices"]["blocked"] == "no_package" and "pip install -e '.[voices]'" in d["voices"]["models_detail"]
    pid = await add(authed, "Ali")
    r = await authed.post(f"/api/people/{pid}/voice", json={})
    assert r.status_code == 503 and "isn't installed" in r.json()["detail"]
    monkeypatch.setattr(S, "sherpa_installed", lambda: True)
    dctx.voices._package = None
    r = await authed.post(f"/api/people/{pid}/voice", json={})
    assert r.status_code == 503 and "download_models.py voices" in r.json()["detail"]
    assert not dctx.audio.called("record_clip")


async def test_forgetting_a_voice_deleting_a_person_and_forget_all(authed, voices, dctx):
    dctx.privacy.set(voice_recognition_enabled=True)
    ali, sara = await add(authed, "Ali", "dad"), await add(authed, "Sara", "mum")
    for pid, k in ((ali, 0), (sara, 1)):
        dctx.audio.clip = (speaker_tone(k, 12.0, 48000, int16=True), 48000)
        assert (await authed.post(f"/api/people/{pid}/voice", json={})).json()["ok"]
    d = (await authed.delete(f"/api/people/{ali}/voice")).json()
    assert [p["voice_samples"] for p in d["people"]] == [0, 4]
    dctx.privacy.set(voice_recognition_enabled=False)  # forgetting works switched off too
    await authed.delete(f"/api/people/{sara}")
    assert dctx.db.query("SELECT COUNT(*) AS n FROM voice_samples")[0]["n"] == 0
    dctx.privacy.set(voice_recognition_enabled=True)
    dctx.audio.clip = (speaker_tone(0, 12.0, 48000, int16=True), 48000)
    await authed.post(f"/api/people/{ali}/voice", json={})
    d = (await authed.post("/api/people/forget-all", json={})).json()
    assert d["people"] == [] and not voices.store.has_samples()


async def test_the_default_voices_service_is_made_without_any_setup(authed, dctx):
    d = (await authed.get("/api/people")).json()
    assert d["voices"]["blocked"] == "off" and dctx.voices is not None


async def test_voice_check_says_who_and_how_close_without_keeping_anything(authed, voices, dctx):
    dctx.privacy.set(voice_recognition_enabled=True)
    pid = await add(authed, "Ali", "dad")
    dctx.audio.clip = (speaker_tone(0, 12.0, 48000, int16=True), 48000)
    assert (await authed.post(f"/api/people/{pid}/voice", json={"seconds": 20})).json()["ok"]
    before = len(dctx.db.query("SELECT * FROM voice_samples"))
    dctx.audio.clip = (speaker_tone(0, 4.0, 48000, int16=True), 48000)
    d = (await authed.post("/api/people/voice-check")).json()
    assert d["ok"] and d["who"] == "Ali (dad)" and d["scores"][0]["name"] == "Ali (dad)"
    assert d["scores"][0]["score"] >= d["threshold"]
    no_numbers({k: v for k, v in d.items() if k != "scores"})
    assert len(dctx.db.query("SELECT * FROM voice_samples")) == before  # nothing learned


async def test_voice_check_needs_the_switch(authed, voices):
    r = await authed.post("/api/people/voice-check")
    assert r.status_code == 409
