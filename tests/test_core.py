"""Unit tests for the core foundation: config, events, state, arbiter, settings, storage."""

from __future__ import annotations

import asyncio
import threading

import pytest

from littlebot.core.arbiter import Priority, Resource, ResourceArbiter
from littlebot.core.config import Mode, env_overrides, load_config, load_dotenv
from littlebot.core.events import EventBus, Topics, collect
from littlebot.core.state import RobotState, StateManager
from littlebot.core.storage import Database
from littlebot.core.settings import Settings, age_months_from_profile, build_defaults
from littlebot.safety.policy import AgePolicy, age_group_for
from littlebot.testing import make_test_config, make_test_context

import datetime as dt


# ----------------------------------------------------------------- config
def test_default_config_loads_for_every_mode():
    for mode in ("simulation", "pi", "offline"):
        cfg = load_config(mode, environ={}, use_dotenv=False, include_local=False)
        assert cfg.mode == Mode(mode)
        assert cfg.display.width == 480 and cfg.display.height == 320
        assert cfg.motion.physical_enabled is False
        assert cfg.dashboard.host == "127.0.0.1"
        assert cfg.dashboard.lan_access is False
        assert cfg.privacy.cloud_enabled is False


def test_env_overrides_are_typed():
    env = {"LITTLEBOT__DASHBOARD__PORT": "9000", "LITTLEBOT__DISPLAY__FULLSCREEN": "true", "OTHER": "x"}
    assert env_overrides(env) == {"dashboard": {"port": 9000}, "display": {"fullscreen": True}}
    cfg = load_config("simulation", environ=env, use_dotenv=False, include_local=False)
    assert cfg.dashboard.port == 9000 and cfg.display.fullscreen is True


def test_secrets_come_from_env_only_and_are_hidden():
    cfg = load_config("simulation", environ={"ANTHROPIC_API_KEY": "sk-ant-secret"}, use_dotenv=False,
                      include_local=False, overrides={"anthropic_api_key": "from-yaml"})
    assert cfg.anthropic_api_key is not None
    assert cfg.anthropic_api_key.get_secret_value() == "sk-ant-secret"
    public = cfg.public_dict()
    assert "sk-ant-secret" not in repr(public)
    assert public["has_anthropic_key"] is True and public["has_openai_key"] is False


def test_offline_mode_forces_cloud_off_and_ignores_keys():
    cfg = load_config("offline", environ={"OPENAI_API_KEY": "sk-x"}, use_dotenv=False, include_local=False,
                      overrides={"privacy": {"cloud_enabled": True}, "ai": {"provider": "openai"}})
    assert cfg.privacy.cloud_enabled is False
    assert cfg.ai.provider == "mock"
    assert cfg.openai_api_key is None


def test_invalid_config_is_rejected():
    with pytest.raises(Exception):
        load_config("simulation", environ={}, use_dotenv=False, include_local=False,
                    overrides={"display": {"not_a_field": 1}})


def test_dotenv_parser(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# comment\nA=1\nexport B='two'\nC=\"3\"\nbad line\n")
    env: dict[str, str] = {"A": "keep"}
    load_dotenv(f, env)
    assert env == {"A": "keep", "B": "two", "C": "3"}


# ----------------------------------------------------------------- events
def test_bus_patterns_and_cancel():
    bus = EventBus()
    seen = collect(bus, ["vision.*"])
    sub = bus.subscribe("state.changed", lambda e: seen.append(e))
    bus.publish("vision.faces", {"faces": []})
    bus.publish("state.changed", {"new": "IDLE"})
    sub.cancel()
    bus.publish("state.changed", {"new": "IDLE"})
    bus.publish("audio.x")
    assert [e.topic for e in seen] == ["vision.faces", "state.changed"]


def test_bus_isolates_failing_subscribers():
    bus = EventBus()
    good: list[str] = []
    bus.subscribe("*", lambda e: 1 / 0)
    bus.subscribe("*", lambda e: good.append(e.topic))
    bus.publish("x.y")
    assert good == ["x.y"]


def test_queue_subscription_is_bounded():
    bus = EventBus()
    q = bus.subscribe_queue("*", maxsize=3)
    for i in range(5):
        bus.publish("t", {"i": i})
    items = q.drain()
    assert [e.data["i"] for e in items] == [2, 3, 4]
    assert q.dropped == 2


async def test_wait_for_from_other_thread():
    bus = EventBus()
    threading.Timer(0.05, lambda: bus.publish("audio.utterance", {"text": "hi"})).start()
    ev = await bus.wait_for("audio.*", timeout=2)
    assert ev is not None and ev.data["text"] == "hi"
    assert await bus.wait_for("never", timeout=0.05) is None


async def test_async_subscription_runs_on_loop():
    bus = EventBus()
    got = asyncio.Event()

    async def handler(_e):
        got.set()

    bus.subscribe_async("x", handler)
    threading.Thread(target=lambda: bus.publish("x")).start()
    await asyncio.wait_for(got.wait(), 2)


# ----------------------------------------------------------------- state
def test_state_transitions_and_events():
    bus = EventBus()
    seen = collect(bus, [Topics.STATE_CHANGED])
    sm = StateManager(bus)
    assert sm.state == RobotState.IDLE
    assert sm.transition(RobotState.LISTENING, "ptt")
    assert sm.transition(RobotState.THINKING)
    assert sm.transition(RobotState.SPEAKING)
    assert sm.transition(RobotState.THINKING, "follow-up")  # SPEAKING -> THINKING allowed
    assert sm.transition(RobotState.IDLE)
    assert seen[0].data["new"] == "LISTENING" and seen[0].data["face_state"] == "listening"


def test_invalid_transition_rejected_and_force():
    sm = StateManager(EventBus())
    sm.transition(RobotState.PLAYING_MEDIA)
    assert not sm.transition(RobotState.THINKING)
    assert sm.state == RobotState.PLAYING_MEDIA
    assert sm.transition(RobotState.THINKING, force=True)
    sm.transition(RobotState.ERROR, "boom")
    assert sm.snapshot()["last_error"] == "boom"
    assert not sm.transition(RobotState.LISTENING)
    assert sm.transition(RobotState.IDLE, "recovered")


def test_release_only_if_still_in_expected_state():
    sm = StateManager(EventBus())
    sm.transition(RobotState.PLAYING_ACTIVITY)
    sm.transition(RobotState.PLAYING_MEDIA)
    assert not sm.release(RobotState.PLAYING_ACTIVITY)
    assert sm.state == RobotState.PLAYING_MEDIA
    assert sm.release(RobotState.PLAYING_MEDIA)
    assert sm.state == RobotState.IDLE


# ----------------------------------------------------------------- arbiter
def test_arbiter_priority_and_preemption():
    arb = ResourceArbiter()
    preempted: list[str] = []
    a = arb.acquire("activity", [Resource.FACE, Resource.AUDIO_OUT], Priority.ACTIVITY,
                    on_preempt=lambda lease, by: preempted.append(by))
    assert a is not None
    assert arb.acquire("conversation", [Resource.AUDIO_OUT], Priority.CONVERSATION) is None
    m = arb.acquire("media", [Resource.SCREEN, Resource.AUDIO_OUT], Priority.MEDIA)
    assert m is not None and preempted == ["media"] and not a.active
    assert arb.is_free(Resource.FACE)
    m.release()
    assert arb.snapshot()["audio_out"] is None


def test_arbiter_preempt_all():
    arb = ResourceArbiter()
    hits: list[str] = []
    arb.acquire("a", ["face"], 10, on_preempt=lambda l, by: hits.append(l.owner))
    arb.acquire("b", ["screen"], 10, on_preempt=lambda l, by: hits.append(l.owner))
    owners = arb.preempt_all("parent")
    assert sorted(owners) == ["a", "b"] and sorted(hits) == ["a", "b"]
    assert all(v is None for v in arb.snapshot().values())


# ----------------------------------------------------------------- storage/settings
def test_settings_persist_and_validate(tmp_path):
    db_path = tmp_path / "s.db"
    cfg = make_test_config()
    bus = EventBus()
    seen = collect(bus, [Topics.SETTINGS_CHANGED])
    s = Settings(Database(db_path), bus, build_defaults(cfg))
    s.set("audio.volume", "0.3")
    s.set("privacy.camera_enabled", "off")
    s.set("motion.physical_armed", True)
    with pytest.raises(KeyError):
        s.set("no.such.key", 1)
    with pytest.raises(ValueError):
        s.set("audio.volume", "loud")
    assert len(seen) == 3
    s2 = Settings(Database(db_path), EventBus(), build_defaults(cfg))
    assert s2.get("audio.volume") == 0.3
    assert s2.get("privacy.camera_enabled") is False
    assert s2.get("motion.physical_armed") is False  # never persisted


def test_profile_age_calculation():
    assert age_months_from_profile({"birth_month": "2025-12"}, dt.date(2026, 10, 20)) == 10
    assert age_months_from_profile({"age_months": "30", "birth_month": "2025-12"}) == 30
    assert age_months_from_profile({}) is None


def test_age_policy():
    infant = AgePolicy(10)
    assert infant.group == "infant"
    assert not infant.video_allowed() and infant.video_allowed(parent_override=True)
    assert not infant.cloud_ai_allowed()
    assert AgePolicy(None).group == "unknown" and not AgePolicy(None).video_allowed()
    assert AgePolicy(40).cloud_ai_allowed() and AgePolicy(40).max_reply_words(30) == 30
    assert age_group_for(20) == "toddler" and age_group_for(3) == "under_6_months"


def test_privacy_manager_events():
    ctx = make_test_context()
    seen = collect(ctx.bus, [Topics.PRIVACY_CHANGED])
    ctx.privacy.set(camera_enabled=False)
    assert seen and seen[-1].data["camera_enabled"] is False
    assert ctx.privacy.cloud_enabled is False


def test_privacy_cloud_refused_offline():
    ctx = make_test_context(mode="offline")
    with pytest.raises(PermissionError):
        ctx.privacy.set(cloud_enabled=True)


def test_kv_store_roundtrip():
    db = Database(":memory:")
    db.kv_set("a", {"x": [1, 2]})
    assert db.kv_get("a") == {"x": [1, 2]}
    assert db.kv_get("missing", 5) == 5
    with db.transaction():
        db.kv_set("b", 1)
    assert db.kv_items() == {"a": {"x": [1, 2]}, "b": 1}


def test_database_file_is_owner_only(tmp_path):
    import os
    import stat

    path = tmp_path / "x.db"
    path.write_bytes(b"")
    os.chmod(path, 0o644)
    db = Database(path)
    db.kv_set("a", 1)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    db.close()
