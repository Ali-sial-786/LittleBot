"""littlebot/core/keys.py: where each API key comes from (dashboard > environment / .env > older key file), saving
and removing them, checking them with one request, and never showing one."""

from __future__ import annotations

import json
import os
import stat
import sys

import httpx
import pytest
from pydantic import SecretStr

from littlebot.brain.providers.claude import ClaudeProvider
from littlebot.brain.providers.openai import OpenAIProvider
from littlebot.core import keys
from littlebot.core.config import load_config
from littlebot.testing import make_test_context

FAKE = "fake-cartesia-key-FROM-DASHBOARD-0001"
FAKE_ENV = "fake-cartesia-key-FROM-ENV-0002"
FAKE_FILE = "fake-cartesia-key-FROM-FILE-0003"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="file modes (chmod 600) are POSIX only")


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch, tmp_path):
    """Never the real .env nor a key set in this computer's environment."""
    monkeypatch.setattr(keys, "ENV_FILE", tmp_path / "no.env")
    for s in keys.KEYS:
        monkeypatch.delenv(s.env, raising=False)


def _cfg(tmp_path, env=None, mode="simulation"):
    return load_config(mode=mode, environ=env or {}, use_dotenv=False, include_local=False,
                       overrides={"data_dir": str(tmp_path)})


def _save_file(tmp_path, value=FAKE):
    folder = tmp_path / "keys"
    folder.mkdir(exist_ok=True)
    (folder / "cartesia").write_text(value + "\n", encoding="utf-8")
    os.chmod(folder / "cartesia", 0o600)


def test_the_environment_is_used_when_nothing_is_saved(tmp_path):
    cfg = _cfg(tmp_path, {"CARTESIA_API_KEY": FAKE_ENV, "ANTHROPIC_API_KEY": "fake-anthropic-0004"})
    assert cfg.secret("cartesia_api_key") == FAKE_ENV
    assert cfg.secret("anthropic_api_key") == "fake-anthropic-0004"
    assert cfg.deepgram_api_key is None


def test_a_key_saved_on_the_dashboard_wins_over_the_environment(tmp_path):
    _save_file(tmp_path)
    cfg = _cfg(tmp_path, {"CARTESIA_API_KEY": FAKE_ENV})
    assert cfg.secret("cartesia_api_key") == FAKE
    st = keys.key_status(cfg, keys.spec("cartesia"), {"CARTESIA_API_KEY": FAKE_ENV})
    assert st["source"] == "dashboard" and st["ends_with"] == FAKE[-4:] and st["fallback"] == "env"
    assert FAKE not in json.dumps(st) and FAKE_ENV not in json.dumps(st)


@posix_only
def test_the_older_key_file_comes_last(tmp_path):
    (tmp_path / "cartesia_api_key").write_text(FAKE_FILE)
    os.chmod(tmp_path / "cartesia_api_key", 0o600)
    assert _cfg(tmp_path).secret("cartesia_api_key") == FAKE_FILE
    assert _cfg(tmp_path, {"CARTESIA_API_KEY": FAKE_ENV}).secret("cartesia_api_key") == FAKE_ENV
    _save_file(tmp_path)
    cfg = _cfg(tmp_path, {"CARTESIA_API_KEY": FAKE_ENV})
    assert cfg.secret("cartesia_api_key") == FAKE


def test_offline_mode_loads_no_key(tmp_path):
    _save_file(tmp_path)
    cfg = _cfg(tmp_path, {"CARTESIA_API_KEY": FAKE_ENV, "OPENAI_API_KEY": "fake-openai-0005"}, mode="offline")
    assert all(getattr(cfg, s.attr) is None for s in keys.KEYS)


def test_save_uses_the_key_at_once_and_remove_falls_back_to_the_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "test.env"
    env_file.write_text(f"CARTESIA_API_KEY={FAKE_ENV}\n", encoding="utf-8")
    monkeypatch.setattr(keys, "ENV_FILE", env_file)
    ctx = make_test_context(data_dir=str(tmp_path))
    try:
        st = keys.save(ctx, "cartesia", "  " + FAKE + "\n")
        assert ctx.config.secret("cartesia_api_key") == FAKE
        assert st["source"] == "dashboard" and st["saved_here"] and st["fallback"] == "env"
        assert (tmp_path / "keys" / "cartesia").read_text(encoding="utf-8").strip() == FAKE
        assert [p.name for p in (tmp_path / "keys").iterdir()] == ["cartesia"]  # no temporary file left
        assert FAKE not in json.dumps(st) and FAKE[-4:] == st["ends_with"]

        st = keys.remove(ctx, "cartesia")
        assert st["removed"] and not (tmp_path / "keys" / "cartesia").exists()
        assert ctx.config.secret("cartesia_api_key") == FAKE_ENV and st["source"] == "env"
        assert "FROM-ENV" not in json.dumps(st)
        assert os.environ.get("CARTESIA_API_KEY") is None  # the .env is read into a dict, never os.environ

        env_file.write_text("# nothing\n", encoding="utf-8")
        keys.save(ctx, "cartesia", FAKE)
        st = keys.remove(ctx, "cartesia")
        assert ctx.config.cartesia_api_key is None and st["set"] is False and st["source"] is None
        assert keys.remove(ctx, "cartesia")["removed"] is False
    finally:
        ctx.db.close()


def test_saving_in_offline_mode_keeps_the_key_for_later(tmp_path):
    ctx = make_test_context(mode="offline", data_dir=str(tmp_path))
    try:
        keys.save(ctx, "deepgram", "fake-deepgram-key-0006")
        assert (tmp_path / "keys" / "deepgram").is_file() and ctx.config.deepgram_api_key is None
    finally:
        ctx.db.close()


@posix_only
def test_saved_keys_are_private(tmp_path):
    ctx = make_test_context(data_dir=str(tmp_path))
    try:
        keys.save(ctx, "elevenlabs", "fake-elevenlabs-key-0007")
        assert stat.S_IMODE((tmp_path / "keys").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "keys" / "elevenlabs").stat().st_mode) == 0o600
    finally:
        ctx.db.close()


@pytest.mark.parametrize("bad", ["", "short", "has space-in-the-middle", "tab\tinside-key", "x" * 301,
                                 "line\nbreak-inside"])
def test_things_that_are_not_keys_are_refused_without_repeating_them(bad):
    with pytest.raises(ValueError) as e:
        keys.clean_value(bad)
    assert not bad.strip() or bad.strip() not in str(e.value)


def test_unknown_key():
    with pytest.raises(KeyError):
        keys.spec("made_up")


def test_a_live_key_follows_the_config_and_never_shows(tmp_path):
    cfg = _cfg(tmp_path)
    live = keys.live_key(cfg, "openai_api_key")
    p = OpenAIProvider(api_key=live)
    assert not p.configured() and live.get_secret_value() == ""
    cfg.openai_api_key = SecretStr("fake-openai-key-0008")
    assert p.configured() and p._secret() == "fake-openai-key-0008"
    assert "fake-openai" not in repr(live) and "fake-openai" not in str(live) and len(live) == 20


def test_claude_makes_a_new_client_when_the_key_changes(tmp_path):
    pytest.importorskip("anthropic")
    cfg = _cfg(tmp_path)
    cfg.anthropic_api_key = SecretStr("fake-anthropic-key-0012")
    p = ClaudeProvider(api_key=keys.live_key(cfg, "anthropic_api_key"), model="claude-haiku-5-5")
    first = p._get_client()
    assert p._get_client() is first
    cfg.anthropic_api_key = SecretStr("fake-anthropic-key-0013")
    second = p._get_client()
    assert second is not first and second.api_key == "fake-anthropic-key-0013"
    cfg.anthropic_api_key = None
    assert p.missing() == "no_key"


# ------------------------------------------------------------------ checking a key
class _Service:
    def __init__(self, status=200, body="{}", error=None):
        self.status, self.body, self.error = status, body, error
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, text=self.body)


@pytest.fixture
def cctx(tmp_path, monkeypatch):
    ctx = make_test_context(data_dir=str(tmp_path), skills={"home_assistant_url": "http://homeassistant.local:8123"})
    ctx.privacy.set(cloud_enabled=True)
    yield ctx
    ctx.db.close()


def _serve(monkeypatch, **kw) -> _Service:
    svc = _Service(**kw)
    monkeypatch.setattr(keys, "check_transport", httpx.MockTransport(svc))
    return svc


@pytest.mark.parametrize("name,url,header,value", [
    ("anthropic", "https://api.anthropic.com/v1/models", "x-api-key", "{k}"),
    ("openai", "https://api.openai.com/v1/models", "authorization", "Bearer {k}"),
    ("deepgram", "https://api.deepgram.com/v1/projects", "authorization", "Token {k}"),
    ("cartesia", "https://api.cartesia.ai/voices", "authorization", "Bearer {k}"),
    ("assemblyai", "https://api.assemblyai.com/v2/transcript?limit=1", "authorization", "{k}"),
    ("elevenlabs", "https://api.elevenlabs.io/v1/user", "xi-api-key", "{k}"),
    ("home_assistant", "http://homeassistant.local:8123/api/", "authorization", "Bearer {k}"),
])
async def test_each_check_is_one_read_only_request(cctx, monkeypatch, name, url, header, value):
    svc = _serve(monkeypatch)
    res = await keys.check(cctx, name, "fake-key-for-check-0009")
    assert res["result"] == "ok" and "fake-key" not in res["detail"]
    (req,) = svc.requests
    assert req.method == "GET" and str(req.url) == url
    assert req.headers[header] == value.format(k="fake-key-for-check-0009")
    if name == "anthropic":
        assert req.headers["anthropic-version"] == "2023-06-01"
    if name == "cartesia":
        from littlebot.assistant.tts.cartesia import API_VERSION

        assert req.headers["cartesia-version"] == API_VERSION


async def test_refused_unchecked_and_elevenlabs_without_permissions(cctx, monkeypatch):
    _serve(monkeypatch, status=401)
    assert await keys.check(cctx, "cartesia", "fake-key-0010") == {"result": "refused",
                                                                   "detail": "Cartesia refused that key."}
    _serve(monkeypatch, status=403)
    assert (await keys.check(cctx, "deepgram", "fake-key-0010"))["result"] == "refused"
    _serve(monkeypatch, status=401, body='{"detail": {"status": "missing_permissions"}}')
    assert (await keys.check(cctx, "elevenlabs", "fake-key-0010"))["result"] == "ok"
    _serve(monkeypatch, status=503)
    assert (await keys.check(cctx, "cartesia", "fake-key-0010"))["result"] == "unchecked"
    _serve(monkeypatch, error=httpx.ConnectError("no route"))
    res = await keys.check(cctx, "cartesia", "fake-key-0010")
    assert res["result"] == "unchecked" and "Couldn't reach Cartesia" in res["detail"]
    _serve(monkeypatch, error=httpx.ReadTimeout("slow"))
    assert "didn't answer" in (await keys.check(cctx, "openai", "fake-key-0010"))["detail"]


async def test_no_request_when_the_cloud_is_off_or_it_cant_be_checked(cctx, monkeypatch):
    svc = _serve(monkeypatch)
    assert (await keys.check(cctx, "typesafe", "fake-key-0011"))["result"] == "unchecked"
    cctx.privacy.set(cloud_enabled=False)
    res = await keys.check(cctx, "cartesia", "fake-key-0011")
    assert res["result"] == "unchecked" and "Cloud communication is off" in res["detail"]
    assert (await keys.check(cctx, "home_assistant", "fake-key-0011"))["result"] == "ok"  # at home: not the cloud
    assert [str(r.url) for r in svc.requests] == ["http://homeassistant.local:8123/api/"]
    assert (await keys.check(cctx, "cartesia"))["result"] == "missing"  # none in use
