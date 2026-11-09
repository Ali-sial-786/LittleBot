"""core/settings.py link_config: the dashboard's choices of the voice and its models go into the config (at start-up
and when changed), are checked as the config file is, and "reset" gives the file back its say, also after a restart."""

from __future__ import annotations

import json
import logging

import pytest

from littlebot.core.context import build_core_context
from littlebot.hardware.system import MockSystemMonitor
from littlebot.testing import make_test_config


def _start(tmp_path, **overrides):
    cfg = make_test_config(claude={"model": "claude-haiku-5-5"}, **overrides)
    ctx = build_core_context(cfg, db_path=tmp_path / "littlebot.db", monitor=MockSystemMonitor())
    return cfg, ctx


def test_a_choice_is_used_after_a_restart_until_it_is_reset(tmp_path, caplog):
    cfg, ctx = _start(tmp_path)
    ctx.settings.set("assistant.stt_shadow", "assemblyai")  # one name, as the config file takes it
    ctx.settings.set("claude.model", "claude-sonnet-5-5")
    assert cfg.assistant.stt_shadow == ["assemblyai"] and cfg.claude.model == "claude-sonnet-5-5"
    ctx.db.close()

    with caplog.at_level(logging.INFO, logger="littlebot.core.settings"):
        cfg, ctx = _start(tmp_path)  # the config file says nothing of it: the kept choice wins
    assert cfg.assistant.stt_shadow == ["assemblyai"] and cfg.claude.model == "claude-sonnet-5-5"
    assert "instead of the config file's: assistant.stt_shadow, claude.model" in caplog.text
    ctx.settings.reset("assistant.stt_shadow")
    ctx.settings.reset("claude.model")
    assert cfg.assistant.stt_shadow == [] and cfg.claude.model == "claude-haiku-5-5"
    ctx.db.close()

    cfg, ctx = _start(tmp_path, assistant={"stt_shadow": ["cartesia"]})  # the file changed since: it decides
    assert cfg.assistant.stt_shadow == ["cartesia"] and not ctx.settings.is_stored("assistant.stt_shadow")
    assert ctx.settings.get("assistant.stt_shadow") == ["cartesia"]
    ctx.db.close()


def test_choices_are_checked_like_the_config_file(tmp_path):
    cfg, ctx = _start(tmp_path)
    for key, bad in (("assistant.stt", ["siri"]), ("assistant.stt_shadow", ["vosk"]), ("assistant.pipeline", "fast"),
                     ("assistant.log_words", "maybe"), ("assistant.tts", "loud")):
        with pytest.raises(ValueError):
            ctx.settings.set(key, bad)
    assert cfg.assistant.stt == ["deepgram", "openai", "vosk"] and not ctx.settings.is_stored("assistant.stt")
    ctx.settings.set("assistant.deep_model", None)  # None (off) is kept as a choice too
    assert ctx.settings.is_stored("assistant.deep_model") and cfg.assistant.deep_model is None
    ctx.db.close()


def test_a_kept_choice_that_no_longer_fits_is_skipped(tmp_path, caplog):
    cfg, ctx = _start(tmp_path)
    ctx.db.kv_set("setting:assistant.stt", ["deepgram", "a-service-since-removed"])
    ctx.db.close()
    with caplog.at_level(logging.WARNING, logger="littlebot.core.settings"):
        cfg, ctx = _start(tmp_path)
    assert cfg.assistant.stt == ["deepgram", "openai", "vosk"]
    assert "the dashboard's assistant.stt no longer fits" in caplog.text
    assert not ctx.settings.is_stored("assistant.stt")  # forgotten
    assert json.loads(json.dumps(ctx.settings.get("assistant.stt"))) == ["deepgram", "openai", "vosk"]
    ctx.db.close()
