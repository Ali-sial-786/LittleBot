"""Dashboard API: each section calls the right protocol methods and maps errors properly."""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
import types

import pytest
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from littlebot.core.events import Topics, collect
from littlebot.core.interfaces import EXPRESSIONS
from littlebot.core.state import RobotState
from littlebot.dashboard import create_app

from dashboard_fakes import do_setup, make_client


async def settle(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


# ------------------------------------------------------------------ status / robot
async def test_status_aggregates_everything(authed):
    r = await authed.get("/api/status")
    assert r.status_code == 200
    s = r.json()
    for key in ("state", "face", "audio", "vision", "motion", "media", "brain", "activities", "system", "privacy",
                "mode", "uptime_seconds", "age_group_label", "settings"):
        assert key in s, key
    assert s["state"]["state"] == "IDLE"
    assert s["system"]["cpu_percent"] == 12.0
    assert s["media"]["player"]["url"].startswith("http://127.0.0.1:8765/player")
    assert s["uptime_seconds"] >= 120  # from the fake robot's started_at


async def test_status_marks_missing_subsystems_unavailable(dctx, app):
    dctx.vision = None
    dctx.brain = None
    async with make_client(app) as c:
        await do_setup(c, app)
        s = (await c.get("/api/status")).json()
        assert s["vision"] == {"status": "unavailable"}
        assert s["brain"] == {"status": "unavailable"}
        assert (await c.post("/api/vision/simulate-no-face")).status_code == 503
        assert (await c.post("/api/ai/test", json={"provider": "mock"})).status_code == 503


async def test_status_without_robot_facade(dctx):
    dctx.extras.pop("robot")
    app = create_app(dctx)
    async with make_client(app) as c:
        await do_setup(c, app)
        s = (await c.get("/api/status")).json()
        assert s["state"]["state"] == "IDLE" and s["activities"]["running"] is False


async def test_stop_all_uses_robot(authed, fakes, dctx):
    await fakes["activities"].start("peekaboo")
    r = await authed.post("/api/stop")
    assert r.status_code == 200 and r.json()["ok"]
    assert fakes["robot"].called("stop_all") == [(("dashboard",), {})]
    assert fakes["activities"].running is None


async def test_stop_all_without_robot_stops_everything(dctx):
    fakes = dctx.extras["fakes"]
    dctx.extras.pop("robot")
    app = create_app(dctx)
    events = collect(dctx.bus, [Topics.STOP_ALL])
    async with make_client(app) as c:
        await do_setup(c, app)
        await fakes["media"].play(fakes["media"].add_item("https://youtu.be/abcdefghijk")["id"])
        dctx.state.transition(RobotState.PLAYING_MEDIA, "test")
        assert (await c.post("/api/stop")).status_code == 200
    assert events and events[0].get("source") == "dashboard"
    assert dctx.motion.called("cancel") and dctx.audio.called("stop_output")
    assert fakes["activities"].called("stop") and fakes["media"].state == "idle"
    assert dctx.state.state == RobotState.IDLE


async def test_sleep_and_wake(authed, dctx, fakes):
    assert (await authed.post("/api/sleep")).status_code == 200
    assert dctx.state.state == RobotState.SLEEPING
    assert (await authed.post("/api/wake")).status_code == 200
    assert dctx.state.state == RobotState.IDLE
    assert fakes["robot"].called("sleep") and fakes["robot"].called("wake")


async def test_works_with_real_littlebot_facade(dctx):
    from littlebot.app.robot import LittleBot

    dctx.extras.pop("robot")
    bot = LittleBot(dctx)  # registers itself in ctx.extras["robot"]
    app = create_app(dctx)
    async with make_client(app) as c:
        await do_setup(c, app)
        dctx.state.transition(RobotState.PLAYING_ACTIVITY, "test")
        r = await c.post("/api/stop")
        assert r.status_code == 200
        assert dctx.state.state == RobotState.IDLE
        assert (await c.post("/api/sleep")).status_code == 200
        assert dctx.state.state == RobotState.SLEEPING
        assert (await c.get("/api/status")).json()["version"]
    assert bot.ctx is dctx


# ------------------------------------------------------------------ face
async def test_face_expression_and_validation(authed, dctx):
    r = await authed.post("/api/face/expression", json={"name": "happy"})
    assert r.status_code == 200
    assert dctx.face.called("set_expression")[-1] == (("happy",), {"hold": None})
    r = await authed.post("/api/face/expression", json={"name": "angry"})
    assert r.status_code == 422
    assert "unknown expression" in r.json()["detail"]
    r = await authed.post("/api/face/expression", json={"name": "happy", "extra": 1})
    assert r.status_code == 422
    for name in EXPRESSIONS:
        assert (await authed.post("/api/face/expression", json={"name": name})).status_code == 200


async def test_face_blink_gaze_animation(authed, dctx):
    assert (await authed.post("/api/face/blink", json={"double": True})).status_code == 200
    assert dctx.face.called("blink")[-1] == ((), {"double": True})
    assert (await authed.post("/api/face/gaze", json={"x": 0.2, "y": 0.8})).status_code == 200
    assert dctx.face.gaze == (0.2, 0.8)
    assert (await authed.post("/api/face/gaze", json={"x": 1.5, "y": 0.5})).status_code == 422
    assert (await authed.post("/api/face/gaze/clear")).status_code == 200
    assert dctx.face.called("clear_gaze")
    assert (await authed.post("/api/face/animation", json={"name": "hearts"})).status_code == 200
    assert dctx.face.called("play_animation")[-1] == (("hearts",), {})
    assert (await authed.post("/api/face/animation", json={"name": "explode"})).status_code == 422
    info = (await authed.get("/api/face")).json()
    assert info["available"] and "happy" in info["expressions"] and "bounce" in info["animations"]


async def test_face_demo_runs_scripted_sequence(authed, dctx, app):
    r = await authed.post("/api/face/demo")
    assert r.status_code == 200 and r.json()["started"]
    for _ in range(50):
        await asyncio.sleep(0)
        if "face_demo" not in app.state.dash.named_tasks:
            break
    exprs = [a[0] for a, _ in dctx.face.called("set_expression")]
    assert exprs[0] == "happy" and exprs[-1] == "neutral" and len(exprs) >= 5
    assert dctx.face.called("play_animation") and dctx.face.called("look_at")
    assert dctx.face.eyes_closed is False


async def test_face_unavailable_returns_503(dctx, app):
    dctx.face = None
    async with make_client(app) as c:
        await do_setup(c, app)
        r = await c.post("/api/face/expression", json={"name": "happy"})
        assert r.status_code == 503 and "unavailable" in r.json()["detail"]
        assert (await c.get("/api/face")).json()["available"] is False


# ------------------------------------------------------------------ activities
async def test_activities_list_and_filters(authed, fakes, dctx):
    r = await authed.get("/api/activities")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 3 and "social" in body["categories"] and body["available"]
    r = await authed.get("/api/activities?age_months=10&category=social")
    assert [i["id"] for i in r.json()["items"]] == ["peekaboo"]
    assert fakes["activities"].called("list")[-1] == ((), {"age_months": 10, "category": "social"})
    r = await authed.get("/api/activities?age_group=preschool")
    assert [i["id"] for i in r.json()["items"]] == ["counting"]
    assert (await authed.get("/api/activities?category=cooking")).status_code == 422
    assert (await authed.get("/api/activities?age_group=teen")).status_code == 422
    assert (await authed.get("/api/activities?age_months=-1")).status_code == 422
    item = (await authed.get("/api/activities/peekaboo")).json()
    assert item["parent_instructions"] and item["safety_notes"] and item["age_label"] == "6–24 months"
    assert (await authed.get("/api/activities/nope")).status_code == 404
    assert (await authed.get("/api/activities/..%2Fetc")).status_code == 404


async def test_activity_controls(authed, fakes):
    acts = fakes["activities"]
    r = await authed.post("/api/activities/peekaboo/start")
    assert r.status_code == 200 and r.json()["status"]["running"]
    assert acts.called("start") == [(("peekaboo",), {"source": "dashboard"})]
    assert (await authed.post("/api/activities/pause")).json()["ok"] is True
    assert (await authed.post("/api/activities/resume")).json()["ok"] is True
    st = (await authed.get("/api/activities/status")).json()
    assert st["prompt"]["prompt_id"] == "p1"
    r = await authed.post("/api/activities/respond", json={"response": "done", "prompt_id": "p1"})
    assert r.status_code == 200
    assert acts.called("respond") == [(("done", "p1"), {})]
    assert (await authed.post("/api/activities/respond", json={"response": "done"})).status_code == 409
    assert (await authed.post("/api/activities/respond", json={"response": ""})).status_code == 422
    assert (await authed.post("/api/activities/stop")).status_code == 200
    assert acts.running is None


async def test_activity_start_errors(authed, fakes):
    assert (await authed.post("/api/activities/unknown/start")).status_code == 404
    fakes["activities"].refuse_start = "LittleBot is busy with a video."
    r = await authed.post("/api/activities/peekaboo/start")
    assert r.status_code == 403 and r.json()["detail"] == "LittleBot is busy with a video."


async def test_activity_preferences_stored_in_profile(authed, dctx):
    r = await authed.put("/api/activities/peekaboo/preferences", json={"favourite": True})
    assert r.status_code == 200 and r.json()["favourite"] is True and r.json()["enabled"] is True
    await authed.put("/api/activities/animal_sounds/preferences", json={"enabled": False})
    prefs = dctx.settings.get("profile")["activity_preferences"]
    assert prefs == {"peekaboo": {"enabled": True, "favourite": True}, "animal_sounds": {"enabled": False, "favourite": False}}
    items = {i["id"]: i for i in (await authed.get("/api/activities")).json()["items"]}
    assert items["peekaboo"]["favourite"] and not items["animal_sounds"]["enabled"]
    favs = (await authed.get("/api/activities?favourites=true")).json()["items"]
    assert [i["id"] for i in favs] == ["peekaboo"]
    await authed.put("/api/activities/peekaboo/preferences", json={"favourite": False})
    assert "peekaboo" not in dctx.settings.get("profile")["activity_preferences"]
    assert (await authed.put("/api/activities/ghost/preferences", json={"favourite": True})).status_code == 404
    assert (await authed.put("/api/activities/peekaboo/preferences", json={"favourite": "yes please"})).status_code == 422


# ------------------------------------------------------------------ media
async def test_media_add_list_remove(authed, fakes):
    r = await authed.post("/api/media/items", json={"url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "Songs"})
    assert r.status_code == 201
    item = r.json()["item"]
    assert item["youtube_id"] == "abcdefghijk"
    info = (await authed.get("/api/media")).json()
    assert [i["id"] for i in info["items"]] == [item["id"]]
    assert info["settings"]["daily_limit_minutes"] == 30 and info["policy"]["under_min_age"] is True
    assert (await authed.delete(f"/api/media/items/{item['id']}")).status_code == 200
    assert (await authed.delete(f"/api/media/items/{item['id']}")).status_code == 404
    assert fakes["media"].called("remove_item")


async def test_media_add_invalid_url_is_422(authed, fakes):
    r = await authed.post("/api/media/items", json={"url": "https://evil.example/video"})
    assert r.status_code == 422
    assert "YouTube" in r.json()["detail"]
    assert (await authed.post("/api/media/items", json={"url": "https://youtu.be/a b"})).status_code == 422
    assert (await authed.post("/api/media/items", json={"url": "x" * 600})).status_code == 422
    assert fakes["media"].list_items() == []


async def test_media_rename_falls_back_to_readd(authed, fakes):
    item = fakes["media"].add_item("https://youtu.be/abcdefghijk", "Old")
    r = await authed.patch(f"/api/media/items/{item['id']}", json={"title": "New name"})
    assert r.status_code == 200 and r.json()["item"]["title"] == "New name"
    assert [i["title"] for i in fakes["media"].list_items()] == ["New name"]
    assert (await authed.patch("/api/media/items/999", json={"title": "x"})).status_code == 404


async def test_media_playback_controls(authed, fakes, dctx):
    item = fakes["media"].add_item("https://youtu.be/abcdefghijk", "Song")
    r = await authed.post("/api/media/play", json={"item_id": item["id"]})
    assert r.status_code == 200 and r.json()["status"]["state"] == "playing"
    assert fakes["media"].called("play") == [((item["id"],), {"parent_override": False})]
    assert (await authed.post("/api/media/pause")).json()["ok"] is True
    assert (await authed.post("/api/media/resume")).json()["ok"] is True
    assert (await authed.post("/api/media/skip")).status_code == 200
    assert (await authed.post("/api/media/stop")).status_code == 200
    assert fakes["media"].state == "idle"
    r = await authed.post("/api/media/volume", json={"volume": 1.0})
    assert r.json()["volume"] == 0.8 and dctx.settings.get("media.volume") == 0.8
    assert (await authed.post("/api/media/volume", json={"volume": 2})).status_code == 422
    assert (await authed.post("/api/media/play", json={"item_id": 404})).status_code == 404


async def test_media_play_under_age_is_403(authed, fakes):
    item = fakes["media"].add_item("https://youtu.be/abcdefghijk", "Song")
    fakes["media"].under_age = True
    r = await authed.post("/api/media/play", json={"item_id": item["id"]})
    assert r.status_code == 403
    assert "18 months" in r.json()["detail"]


async def test_media_limits_and_under_age_authorisation(authed, dctx):
    r = await authed.put("/api/media/settings", json={"session_limit_minutes": 10, "daily_limit_minutes": 20})
    assert r.status_code == 200
    assert dctx.settings.get("media.session_limit_minutes") == 10 and dctx.settings.get("media.daily_limit_minutes") == 20
    assert (await authed.put("/api/media/settings", json={"session_limit_minutes": 30})).status_code == 422
    assert (await authed.put("/api/media/settings", json={"daily_limit_minutes": 9999})).status_code == 422
    # Authorising video under 18 months needs the explicit confirmation flag.
    r = await authed.put("/api/media/settings", json={"allow_under_min_age": True})
    assert r.status_code == 422 and dctx.settings.get("media.allow_under_min_age") is False
    r = await authed.put("/api/media/settings", json={"allow_under_min_age": True, "confirm_under_min_age": True})
    assert r.status_code == 200 and dctx.settings.get("media.allow_under_min_age") is True
    assert r.json()["policy"]["video_allowed"] is True
    r = await authed.put("/api/media/settings", json={"allow_under_min_age": False})
    assert r.status_code == 200 and dctx.settings.get("media.allow_under_min_age") is False
    r = await authed.put("/api/media/settings", json={"enabled": False})
    assert dctx.settings.get("media.enabled") is False


# ------------------------------------------------------------------ profile
async def test_profile_get_and_update(authed, dctx):
    r = await authed.get("/api/profile")
    assert r.status_code == 200 and r.json()["age_source"] == "unset" and r.json()["age_group"] == "unknown"
    today = dt.date.today()
    y, m = (today.year - 1, today.month)
    r = await authed.put("/api/profile", json={
        "nickname": "Sami", "birth_month": f"{y:04d}-{m:02d}", "language": "en",
        "favourite_animals": ["duck", "Duck", " cat "], "interests": ["balls"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["age_months"] == 12 and body["age_group"] == "young_toddler" and body["age_source"] == "birth_month"
    assert body["profile"]["favourite_animals"] == ["duck", "cat"]
    r = await authed.put("/api/profile", json={"age_months": 30})
    assert r.json()["age_months"] == 30 and r.json()["profile"]["birth_month"] == ""
    assert dctx.settings.child_age_months() == 30
    # Activity preferences survive profile edits.
    await authed.put("/api/activities/peekaboo/preferences", json={"favourite": True})
    await authed.put("/api/profile", json={"nickname": "Sam"})
    assert dctx.settings.get("profile")["activity_preferences"]["peekaboo"]["favourite"] is True
    r = await authed.post("/api/profile/clear")
    assert r.json()["profile"]["nickname"] == "" and dctx.settings.get("profile")["activity_preferences"] == {}


@pytest.mark.parametrize("bad", [
    {"birth_month": "2025-13"}, {"birth_month": "25-01"}, {"birth_month": "2025/01"}, {"birth_month": "abc"},
    {"birth_month": "2999-01"}, {"birth_month": "1990-01"}, {"age_months": -1}, {"age_months": 500},
    {"nickname": "<script>alert(1)</script>"}, {"nickname": "x" * 31}, {"language": "klingon"},
    {"favourite_animals": ["x" * 41]}, {"favourite_animals": ["a"] * 13}, {"email": "a@b.c"},
])
async def test_profile_validation(authed, bad):
    r = await authed.put("/api/profile", json=bad)
    assert r.status_code == 422, (bad, r.text)


# ------------------------------------------------------------------ AI
async def test_ai_settings(authed, dctx, fakes):
    info = (await authed.get("/api/ai")).json()
    assert info["settings"]["enabled"] is False and info["keys"]["has_anthropic_key"] is True
    assert info["policy"]["cloud_ai_age_ok"] is False and info["brain"]["usage"]["requests_today"] == 3
    r = await authed.put("/api/ai/settings", json={"enabled": True, "provider": "anthropic", "max_words": 20,
                                                   "max_requests_per_hour": 10, "max_requests_per_day": 50,
                                                   "daily_budget_usd": 0.25})
    assert r.status_code == 200
    assert dctx.settings.get("ai.enabled") is True and dctx.settings.get("ai.provider") == "anthropic"
    assert dctx.settings.get("ai.daily_budget_usd") == 0.25
    assert (await authed.put("/api/ai/settings", json={"provider": "skynet"})).status_code == 422
    assert (await authed.put("/api/ai/settings", json={"max_words": 1000})).status_code == 422
    providers = (await authed.get("/api/ai/providers")).json()["providers"]
    assert {p["name"] for p in providers} == {"mock", "anthropic", "openai"}


async def test_ai_provider_test(authed, dctx, fakes):
    r = await authed.post("/api/ai/test", json={"provider": "mock"})
    assert r.status_code == 200 and r.json()["result"]["ok"] is True
    assert fakes["brain"].called("test_provider") == [(("mock",), {})]
    r = await authed.post("/api/ai/test", json={"provider": "anthropic"})
    assert r.status_code == 403  # cloud communication is off
    dctx.privacy.set(cloud_enabled=True)
    assert (await authed.post("/api/ai/test", json={"provider": "anthropic"})).status_code == 200


async def test_ai_cannot_be_enabled_offline():
    from pydantic import SecretStr  # noqa: F401
    from littlebot.testing import make_test_context

    from dashboard_fakes import install_fakes

    ctx = make_test_context(mode="offline")
    install_fakes(ctx)
    app = create_app(ctx)
    async with make_client(app) as c:
        await do_setup(c, app)
        assert (await c.put("/api/ai/settings", json={"enabled": True})).status_code == 403
        r = await c.put("/api/privacy", json={"cloud_enabled": True})
        assert r.status_code == 403 and "offline" in r.json()["detail"]
    ctx.db.close()


# ------------------------------------------------------------------ privacy
async def test_privacy_toggles_change_manager_and_publish(authed, dctx):
    events = collect(dctx.bus, [Topics.PRIVACY_CHANGED])
    r = await authed.put("/api/privacy", json={"camera_enabled": False, "microphone_enabled": False})
    assert r.status_code == 200
    assert dctx.privacy.camera_enabled is False and dctx.privacy.microphone_enabled is False
    assert events and events[-1].get("camera_enabled") is False
    assert r.json()["camera_enabled"] is False
    r = await authed.put("/api/privacy", json={"cloud_enabled": True})
    assert dctx.privacy.cloud_enabled is True
    assert (await authed.put("/api/privacy", json={})).status_code == 422
    assert (await authed.put("/api/privacy", json={"spy_mode": True})).status_code == 422
    # Microphone test refuses while the microphone is off.
    assert (await authed.post("/api/audio/test-microphone", json={"seconds": 1})).status_code == 403


async def test_privacy_clear_history_and_stop(authed, fakes):
    r = await authed.post("/api/privacy/clear-history")
    assert r.status_code == 200 and set(r.json()["cleared"]) == {"activities", "brain"}
    assert fakes["activities"].history_cleared and fakes["brain"].called("clear_history")
    assert (await authed.post("/api/privacy/stop-interactions")).status_code == 200
    assert fakes["robot"].called("stop_all") == [(("privacy",), {})]


# ------------------------------------------------------------------ audio / vision
async def test_audio_endpoints(authed, dctx):
    info = (await authed.get("/api/audio")).json()
    assert info["available"] and "chime" in info["sounds"]
    r = await authed.put("/api/audio/settings", json={"volume": 0.95, "tts_rate": 1.1})
    assert r.json()["settings"] == {"volume": 0.8, "tts_rate": 1.1}
    assert dctx.audio.volume == 0.8 and dctx.settings.get("audio.tts_rate") == 1.1
    assert (await authed.put("/api/audio/settings", json={"tts_rate": 3})).status_code == 422
    assert (await authed.post("/api/audio/test-speaker")).json()["result"]["ok"] is True
    assert (await authed.post("/api/audio/test-microphone", json={"seconds": 1})).json()["result"]["ok"] is True
    assert (await authed.get("/api/audio/devices")).json() == {"inputs": [], "outputs": []}
    assert (await authed.post("/api/audio/say", json={"text": "Hello friend"})).status_code == 200
    assert (await authed.post("/api/audio/sound", json={"name": "chime"})).status_code == 200
    assert (await authed.post("/api/audio/sound", json={"name": "../../etc/passwd"})).status_code == 422
    await settle()
    assert "Hello friend" in dctx.audio.spoken and "chime" in dctx.audio.sounds
    assert (await authed.post("/api/audio/stop")).status_code == 200


async def test_vision_endpoints(authed, dctx, fakes):
    info = (await authed.get("/api/vision")).json()
    assert info["status"]["camera"] == "ok" and info["follow_faces"] is True
    assert (await authed.post("/api/vision/simulate-face", json={"x": 0.3, "y": 0.4})).status_code == 200
    assert fakes["vision"].called("simulate_face") == [((0.3, 0.4, 0.25), {})]
    assert (await authed.post("/api/vision/simulate-no-face")).status_code == 200
    assert (await authed.put("/api/vision/settings", json={"follow_faces": False})).status_code == 200
    assert dctx.settings.get("vision.follow_faces") is False


# ------------------------------------------------------------------ motion
async def test_motion_info_is_simulation_only(authed):
    info = (await authed.get("/api/motion")).json()
    assert info["simulation_only"] is True and info["physical_enabled"] is False and info["armed"] is False
    assert "wave" in info["gestures"] and set(info["servos"]) == {"head", "left_arm", "right_arm"}
    assert info["reason"] == "fake motion"  # the motion module's own explanation is passed through
    assert info["status"]["joints"]["left_arm"]["position"] == 0.0


async def test_motion_gesture_joint_neutral(authed, dctx):
    assert (await authed.post("/api/motion/gesture", json={"name": "wave"})).status_code == 200
    await settle()
    assert dctx.motion.called("run_gesture") == [(("wave",), {})]
    assert (await authed.post("/api/motion/gesture", json={"name": "backflip"})).status_code == 404
    assert (await authed.post("/api/motion/joint", json={"joint": "head", "angle": 100})).status_code == 200
    await settle()
    assert dctx.motion.called("move_joint")[-1] == (("head", 100.0), {"speed_dps": None})
    r = await authed.post("/api/motion/joint", json={"joint": "head", "angle": 170})
    assert r.status_code == 422 and "between" in r.json()["detail"]
    assert (await authed.post("/api/motion/joint", json={"joint": "tail", "angle": 90})).status_code == 404
    assert (await authed.post("/api/motion/neutral")).status_code == 200
    await settle()
    assert dctx.motion.called("neutral")


async def test_motion_emergency_stop(authed, dctx):
    r = await authed.post("/api/motion/estop")
    assert r.status_code == 200 and r.json()["status"]["estop"] is True
    assert dctx.motion.called("emergency_stop") == [(("dashboard",), {})]
    r = await authed.post("/api/motion/gesture", json={"name": "wave"})
    assert r.status_code == 409
    assert (await authed.post("/api/motion/estop/reset")).status_code == 200
    assert dctx.motion.estop is False
    assert (await authed.post("/api/motion/gesture", json={"name": "wave"})).status_code == 200


async def test_motion_arming_refused_when_physical_disabled(authed, dctx):
    r = await authed.post("/api/motion/arm", json={"armed": True, "confirmation": "ARM MOTORS"})
    assert r.status_code == 403 and "simulation" in r.json()["detail"].lower()
    assert dctx.settings.get("motion.physical_armed") is False


async def test_motion_arming_requires_typed_confirmation():
    from littlebot.testing import make_test_context

    from dashboard_fakes import install_fakes

    ctx = make_test_context(motion={"physical_enabled": True})
    install_fakes(ctx)
    app = create_app(ctx)
    async with make_client(app) as c:
        await do_setup(c, app)
        assert (await c.post("/api/motion/arm", json={"armed": True})).status_code == 422
        assert (await c.post("/api/motion/arm", json={"armed": True, "confirmation": "yes"})).status_code == 422
        assert ctx.settings.get("motion.physical_armed") is False
        r = await c.post("/api/motion/arm", json={"armed": True, "confirmation": "arm motors"})
        assert r.status_code == 200 and r.json()["arm_requested"] is True
        assert ctx.settings.get("motion.physical_armed") is True
        # E-stop disarms immediately.
        await c.post("/api/motion/estop")
        assert ctx.settings.get("motion.physical_armed") is False
        await c.post("/api/motion/estop/reset")
        await c.post("/api/motion/arm", json={"armed": True, "confirmation": "ARM MOTORS"})
        assert (await c.post("/api/motion/arm", json={"armed": False})).json()["arm_requested"] is False
        assert ctx.settings.get("motion.physical_armed") is False
    ctx.db.close()


# ------------------------------------------------------------------ simulator
async def test_sim_speech_routes(authed, dctx, fakes):
    r = await authed.post("/api/sim/speech", json={"text": "doggy!"})
    assert r.status_code == 200 and r.json()["route"] == "brain" and r.json()["reply"] == "You said doggy!!"
    assert fakes["brain"].called("handle_utterance") == [(("doggy!",), {"source": "simulated", "speak": True})]
    await fakes["activities"].start("animal_sounds")
    r = await authed.post("/api/sim/speech", json={"text": "moo"})
    assert r.json()["route"] == "microphone"
    assert dctx.audio.called("inject_utterance") == [(("moo",), {})]
    r = await authed.post("/api/sim/speech", json={"text": "hi", "route": "brain", "speak": False})
    assert fakes["brain"].called("handle_utterance")[-1] == (("hi",), {"source": "simulated", "speak": False})
    assert (await authed.post("/api/sim/speech", json={"text": "   "})).status_code == 422
    assert (await authed.post("/api/sim/speech", json={"text": "x" * 201})).status_code == 422


async def test_sim_caregiver_and_camera(authed, dctx, fakes):
    assert (await authed.post("/api/sim/caregiver", json={"response": "done"})).status_code == 409
    await fakes["activities"].start("peekaboo")
    assert (await authed.post("/api/sim/caregiver", json={"response": "done", "prompt_id": "p1"})).status_code == 200
    r = await authed.post("/api/sim/camera", json={"event": "face", "x": 0.7, "y": 0.3})
    assert r.json()["via"] == "vision" and fakes["vision"].face == (0.7, 0.3)
    await authed.post("/api/sim/camera", json={"event": "person_left"})
    assert fakes["vision"].face is None
    assert (await authed.post("/api/sim/camera", json={"event": "explode"})).status_code == 422


async def test_sim_camera_without_vision_publishes_events(dctx, app):
    dctx.vision = None
    seen = collect(dctx.bus, ["vision.*"])
    async with make_client(app) as c:
        await do_setup(c, app)
        r = await c.post("/api/sim/camera", json={"event": "face", "x": 0.2, "y": 0.5})
        assert r.json()["via"] == "events"
        await c.post("/api/sim/camera", json={"event": "person_left"})
    topics = [e.topic for e in seen]
    assert Topics.VISION_FACES in topics and Topics.VISION_PERSON_PRESENT in topics and Topics.VISION_PERSON_LEFT in topics


# ------------------------------------------------------------------ system
async def test_system_info_config_and_events(authed, dctx):
    info = (await authed.get("/api/system")).json()
    assert info["mode"] == "simulation" and info["dashboard"]["lan_access"] is False
    cfg = (await authed.get("/api/system/config")).json()
    assert cfg["dashboard"]["host"] == "127.0.0.1" and cfg["has_openai_key"] is True
    assert cfg["ai"]["max_output_tokens"] == 120  # not mistaken for a secret
    dctx.bus.publish("test.event", {"value": 42})
    ev = (await authed.get("/api/system/events?n=5")).json()["events"]
    assert ev[0]["topic"] == "test.event" and ev[0]["data"] == {"value": 42}
    assert (await authed.get("/api/system/events?n=5000")).status_code == 422
    assert (await authed.get("/api/system/events?pattern=<x>")).status_code == 422


async def test_power_requires_confirmation(authed, fakes):
    r = await authed.post("/api/system/power", json={"action": "shutdown"})
    assert r.status_code == 422
    assert fakes["robot"].called("request_shutdown") == []
    r = await authed.post("/api/system/power", json={"action": "restart", "confirm": True})
    assert r.status_code == 200
    await asyncio.sleep(0.7)
    assert fakes["robot"].shutdown_requested == "dashboard restart"
    assert (await authed.post("/api/system/power", json={"action": "reboot", "confirm": True})).status_code == 422


# ------------------------------------------------------------------ media player router
async def test_player_router_mounted_without_parent_auth(dctx, monkeypatch):
    mod = types.ModuleType("littlebot.media.player_routes")
    seen = {}

    def create_player_router(media):
        seen["media"] = media
        router = APIRouter()

        @router.get("/player")
        async def player_page():
            return HTMLResponse("<p>player</p>", headers={"Content-Security-Policy": "frame-src https://www.youtube-nocookie.com"})

        @router.get("/player/api/state")
        async def player_state():
            return {"state": "idle"}

        return router

    mod.create_player_router = create_player_router
    monkeypatch.setitem(sys.modules, "littlebot.media.player_routes", mod)
    app = create_app(dctx)
    assert seen["media"] is dctx.media
    async with make_client(app) as c:
        r = await c.get("/player")
        assert r.status_code == 200
        assert r.headers["content-security-policy"] == "frame-src https://www.youtube-nocookie.com"
        assert "x-frame-options" not in r.headers
        assert r.headers["x-content-type-options"] == "nosniff"
        assert (await c.get("/player/api/state")).json() == {"state": "idle"}
        assert (await c.get("/api/status")).status_code == 401  # parent API still protected


# ------------------------------------------------------------------ parent alerts
async def test_attention_alerts_reach_the_parent(authed, dctx):
    dctx.bus.publish(Topics.BRAIN_STATUS, {"provider": "safety", "status": "attention", "detail": "hurt"}, source="brain")
    dctx.bus.publish(Topics.BRAIN_STATUS, {"provider": "safety", "status": "attention", "detail": "hurt"}, source="brain")
    dctx.bus.publish(Topics.BRAIN_STATUS, {"provider": "mock", "status": "ok", "detail": ""}, source="brain")
    dctx.bus.publish(Topics.SYSTEM_ERROR, {"module": "audio", "error": "speaker unplugged", "recoverable": True})
    alerts = (await authed.get("/api/status")).json()["alerts"]
    assert [a["kind"] for a in alerts] == ["attention", "error"]
    assert alerts[0]["count"] == 2 and "hurt" in alerts[0]["detail"]
    r = await authed.post(f"/api/alerts/{alerts[0]['id']}/dismiss")
    assert r.status_code == 200 and [a["kind"] for a in r.json()["alerts"]] == ["error"]
    assert (await authed.post(f"/api/alerts/{alerts[0]['id']}/dismiss")).status_code == 404
    assert (await authed.post("/api/alerts/dismiss-all")).json()["dismissed"] == 1
    assert (await authed.get("/api/status")).json()["alerts"] == []


async def test_ai_sharing_opt_ins(authed, dctx):
    info = (await authed.get("/api/ai")).json()
    assert info["settings"]["share_child_name"] is False and info["settings"]["share_interests"] is False
    r = await authed.put("/api/ai/settings", json={"share_child_name": True, "share_interests": True})
    assert r.status_code == 200
    assert dctx.settings.get("ai.share_child_name") is True and dctx.settings.get("ai.share_interests") is True
    assert (await authed.put("/api/ai/settings", json={"share_child_name": "maybe"})).status_code == 422


async def test_push_to_talk(authed, fakes, dctx, app):
    r = await authed.post("/api/ai/push-to-talk")
    assert r.status_code == 200 and r.json()["ok"] and r.json()["provider"] == "mock"
    assert fakes["brain"].called("converse_once") == [((), {})]
    dctx.brain = None
    assert (await authed.post("/api/ai/push-to-talk")).status_code == 503
