"""API keys and tokens: which ones LittleBot uses, where each one comes from, and saving, checking and removing them
from the dashboard (littlebot/dashboard/routes/keys_routes.py, docs/KEYS.md).

Where a key comes from (the first that has one):

  1. data/keys/<name>, saved on the dashboard (folder 0700, file 0600)
  2. the environment, or the git-ignored .env file (LittleBot never writes or edits it)
  3. the older key file under data/ (scripts/set_key.py), used only when no other user can read it

Offline mode loads none. A key is never logged, returned or put in an error: only its last four characters are
ever shown (``ends_with``), and log lines say which key and what happened.

A key saved or removed here is used at once: the speech services, the smart home and the decider read the key when
they need it, and the brains are given a :class:`LiveKey` that does the same (``live_key``).
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from littlebot.core.paths import PROJECT_ROOT

log = logging.getLogger("littlebot.keys")

KEYS_DIR = "keys"  # under data_dir
ENV_FILE = PROJECT_ROOT / ".env"  # read (never written) to find a key the dashboard's one falls back to
CHECK_TIMEOUT_S = 10.0
MIN_LEN, MAX_LEN = 8, 300
check_transport: Any = None  # tests: an httpx transport the key checks use instead of the network


@dataclass(frozen=True)
class KeySpec:
    name: str  # the dashboard's name for it ("cartesia"); also its file under data/keys/
    attr: str  # LittleBotConfig attribute
    env: str  # environment / .env variable
    legacy_file: str | None  # the older key file under data_dir (None: there never was one)
    label: str
    what: str  # what LittleBot uses it for, in a few plain words
    get_url: str  # where to get one
    checkable: bool = True
    live: bool = True  # False: LittleBot needs a restart before it uses a changed key


KEYS: tuple[KeySpec, ...] = (
    KeySpec("anthropic", "anthropic_api_key", "ANTHROPIC_API_KEY", "anthropic_api_key", "Anthropic (Claude)",
            "Claude, the brain: it answers, uses the tools and puts cards up.",
            "https://console.anthropic.com/settings/keys"),
    KeySpec("openai", "openai_api_key", "OPENAI_API_KEY", None, "OpenAI",
            "Backup speech-to-text and voice, and GPT when it is chosen instead of Claude.",
            "https://platform.openai.com/api-keys"),
    KeySpec("deepgram", "deepgram_api_key", "DEEPGRAM_API_KEY", "deepgram_api_key", "Deepgram",
            "Fast speech-to-text that knows when you have finished talking.",
            "https://console.deepgram.com/"),
    KeySpec("cartesia", "cartesia_api_key", "CARTESIA_API_KEY", "cartesia_api_key", "Cartesia",
            "The fastest natural voice LittleBot talks with.",
            "https://play.cartesia.ai/keys"),
    KeySpec("elevenlabs", "elevenlabs_api_key", "ELEVENLABS_API_KEY", "elevenlabs_api_key", "ElevenLabs",
            "A natural voice, used when Cartesia isn't set or doesn't answer.",
            "https://elevenlabs.io/app/settings/api-keys"),
    KeySpec("home_assistant", "home_assistant_token", "HOME_ASSISTANT_TOKEN", "home_assistant_token",
            "Home Assistant", "Turns your lights, plugs and fans on and off (a long-lived access token).",
            "https://www.home-assistant.io/docs/authentication/#your-account-profile"),
    KeySpec("typesafe", "typesafe_api_key", "TYPESAFE_API_KEY", None, "TypeSafe (Jev)",
            "Jev, the fast decider for LittleBot's small yes/no choices (only when it is chosen).",
            "https://typesafe.ai/", checkable=False),
)
BY_NAME = {k.name: k for k in KEYS}
NAMES = tuple(BY_NAME)
ATTRS = frozenset(k.attr for k in KEYS)


def spec(name: str) -> KeySpec:
    """The key called ``name``; KeyError (404 on the dashboard) when there is none."""
    try:
        return BY_NAME[name]
    except KeyError:
        raise KeyError("There is no key with that name.") from None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def keys_dir(cfg: Any) -> Path:
    return cfg.data_path / KEYS_DIR


def _read_private(path: Path) -> str | None:
    """A saved key file's first line. On Linux only when no other user can read it (read_key_file); Windows has
    no such file modes (a desktop preview keeps data/ in the user's own folder), so there it is just read."""
    from littlebot.core.config import read_key_file  # noqa: PLC0415 - config imports this module

    if os.name != "nt":
        return read_key_file(path)
    try:
        key = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return key.splitlines()[0].strip() if key else None


def saved_value(cfg: Any, s: KeySpec) -> str | None:
    """The key saved on the dashboard, or None."""
    return _read_private(keys_dir(cfg) / s.name)


def legacy_path(cfg: Any, s: KeySpec) -> Path | None:
    if s.attr == "anthropic_api_key":
        return cfg.data_path / cfg.claude.key_file  # its name can be changed (claude.key_file)
    return cfg.data_path / s.legacy_file if s.legacy_file else None


def legacy_value(cfg: Any, s: KeySpec) -> str | None:
    from littlebot.core.config import read_key_file  # noqa: PLC0415

    path = legacy_path(cfg, s)
    return read_key_file(path) if path is not None else None


def current_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment with the .env file's values added, as load_config sees it (never put into os.environ)."""
    from littlebot.core.config import load_dotenv  # noqa: PLC0415

    env = dict(os.environ) if environ is None else dict(environ)
    load_dotenv(ENV_FILE, env)
    return env


def resolve(cfg: Any, s: KeySpec, env: dict[str, str], *, saved: bool = True) -> tuple[str | None, str | None]:
    """(the key, where it came from: "dashboard" | "env" | "file") by the precedence above, or (None, None).
    ``saved=False``: what it falls back to without the dashboard's one."""
    if saved:
        value = saved_value(cfg, s)
        if value:
            return value, "dashboard"
    value = (env.get(s.env) or "").strip()
    if value:
        return value, "env"
    value = legacy_value(cfg, s)
    if value:
        return value, "file"
    return None, None


def load_into(cfg: Any, env: dict[str, str]) -> None:
    """Set every key of ``KEYS`` on ``cfg`` (load_config; not in offline mode)."""
    for s in KEYS:
        value, _source = resolve(cfg, s, env)
        if value:
            setattr(cfg, s.attr, SecretStr(value))


class LiveKey(SecretStr):
    """A SecretStr that is always the key now in ``config`` (``config.<attr>``): a brain given one uses a key
    changed on the dashboard straight away. Like a SecretStr it never shows the key (str and repr hide it)."""

    def __init__(self, config: Any, attr: str) -> None:
        super().__init__("")
        self._config = config
        self._attr = attr

    def get_secret_value(self) -> str:
        try:
            return self._config.secret(self._attr)
        except Exception:  # noqa: BLE001 - can't tell: no key
            return ""

    def __len__(self) -> int:
        return len(self.get_secret_value())

    def _display(self) -> str:
        return "**********" if self.get_secret_value() else ""


def live_key(config: Any, attr: str) -> LiveKey:
    return LiveKey(config, attr)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _source_of(cfg: Any, s: KeySpec, value: str, env: dict[str, str]) -> str:
    if saved_value(cfg, s) == value:
        return "dashboard"
    if (env.get(s.env) or "").strip() == value:
        return "env"
    if legacy_value(cfg, s) == value:
        return "file"
    return "env"  # given to LittleBot some other way when it started (e.g. the service's own environment)


def key_status(cfg: Any, s: KeySpec, env: dict[str, str] | None = None) -> dict[str, Any]:
    """One key: whether it is set, where the key in use came from, its last four characters, whether one is saved
    on the dashboard and what removing that falls back to. Never the key itself."""
    env = current_env() if env is None else env
    value = cfg.secret(s.attr)
    saved = saved_value(cfg, s) is not None
    fallback, fallback_source = resolve(cfg, s, env, saved=False)
    return {
        "name": s.name, "label": s.label, "what": s.what, "get_url": s.get_url, "env": s.env,
        "set": bool(value), "source": _source_of(cfg, s, value, env) if value else None,
        "ends_with": value[-4:] if value else None,
        "saved_here": saved, "fallback": fallback_source if fallback else None,
        "checkable": s.checkable, "restart_needed": not s.live,
    }


def status(cfg: Any) -> list[dict[str, Any]]:
    env = current_env()
    return [key_status(cfg, s, env) for s in KEYS]


# ---------------------------------------------------------------------------
# Saving and removing
# ---------------------------------------------------------------------------

def clean_value(value: Any) -> str:
    """The key as typed, stripped; ValueError (never saying what was typed) when it can't be a key."""
    v = str(value or "").strip()
    if not MIN_LEN <= len(v) <= MAX_LEN:
        raise ValueError(f"A key is {MIN_LEN} to {MAX_LEN} characters long.")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in v):
        raise ValueError("A key has no spaces or line breaks in it. Copy it again and paste only the key.")
    return v


def _write_private(folder: Path, name: str, value: str) -> None:
    """Write ``folder/name`` so that only the robot's user can read it: a temporary file in the same folder, then
    renamed over the old one (nobody ever reads half a key)."""
    folder.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(folder, 0o700)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{name}.", suffix=".tmp")  # mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, folder / name)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _apply(ctx: Any, s: KeySpec, value: str | None) -> None:
    """Use ``value`` from now on (None: no key), and give a voice, speech-to-text or brain that stopped using this
    key after it was refused another go with the new one."""
    setattr(ctx.config, s.attr, SecretStr(value) if value else None)
    assistant = getattr(ctx, "assistant", None)
    for chain in (getattr(assistant, "_tts", None), getattr(assistant, "_stt", None)):
        resting = getattr(chain, "_resting", None)  # tts/stt chains: provider name -> (why, until)
        if isinstance(resting, dict):
            resting.pop(s.name, None)
    refused = getattr(getattr(ctx, "brain", None), "_auth_errors", None)  # brain/conversation.py: provider names
    if isinstance(refused, set):
        refused.discard(s.name)


def save(ctx: Any, name: str, value: str) -> dict[str, Any]:
    """Save ``value`` as the key ``name`` in data/keys/ and use it now (not in offline mode, which loads none).
    Returns the key's status."""
    s = spec(name)
    value = clean_value(value)
    _write_private(keys_dir(ctx.config), s.name, value)
    if not ctx.config.is_offline:
        _apply(ctx, s, value)
    return key_status(ctx.config, s)


def remove(ctx: Any, name: str) -> dict[str, Any]:
    """Delete the dashboard's key ``name``: the .env one (read again now) or the older key file is used instead,
    else none. Returns the key's status; ``removed`` says whether there was one to delete."""
    s = spec(name)
    path = keys_dir(ctx.config) / s.name
    removed = path.is_file()
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    if not ctx.config.is_offline:
        value, _source = resolve(ctx.config, s, current_env(), saved=False)
        _apply(ctx, s, value)
    return {**key_status(ctx.config, s), "removed": removed}


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

def _home_url(cfg: Any) -> str:
    raw = str(getattr(cfg.skills, "home_assistant_url", "") or "").strip().rstrip("/")
    return raw[:-4] if raw.endswith("/api") else raw


def _request(s: KeySpec, key: str, cfg: Any) -> tuple[str, dict[str, str]]:
    if s.name == "anthropic":
        return "https://api.anthropic.com/v1/models", {"x-api-key": key, "anthropic-version": "2023-06-01"}
    if s.name == "openai":
        return "https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"}
    if s.name == "deepgram":
        return "https://api.deepgram.com/v1/projects", {"Authorization": f"Token {key}"}
    if s.name == "cartesia":
        from littlebot.assistant.tts.cartesia import API_VERSION  # noqa: PLC0415 - the version the voice sends

        return "https://api.cartesia.ai/voices", {"Cartesia-Version": API_VERSION, "Authorization": f"Bearer {key}"}
    if s.name == "elevenlabs":
        return "https://api.elevenlabs.io/v1/user", {"xi-api-key": key}
    if s.name == "home_assistant":
        return _home_url(cfg) + "/api/", {"Authorization": f"Bearer {key}"}
    raise ValueError("That key can't be checked.")


def _unchecked(detail: str) -> dict[str, Any]:
    return {"result": "unchecked", "detail": detail}


def _why_not_checked(ctx: Any, s: KeySpec) -> str | None:
    if not s.checkable:
        return f"{s.label} keys can't be checked from here."
    if ctx.config.is_offline:
        return "LittleBot is in offline mode, so the key wasn't checked."
    if s.name == "home_assistant":
        url = _home_url(ctx.config)
        if not url.startswith(("http://", "https://")):
            return "Home Assistant's address isn't set (skills.home_assistant_url), so the token wasn't checked."
        from littlebot.skills.home import is_lan  # noqa: PLC0415 - Home Assistant at home isn't the cloud

        if is_lan(url):
            return None
    if not ctx.privacy.cloud_enabled:
        return "Cloud communication is off (Privacy page), so the key wasn't checked."
    return None


async def check(ctx: Any, name: str, value: str | None = None) -> dict[str, Any]:
    """Try ``value`` (default: the key in use) with one cheap read-only request. ``result``: "ok", "refused" (the
    service said no: 401 / 403) or "unchecked" (no answer, the service's own trouble, or not allowed to ask);
    ``detail`` says so in plain words. Never the key, nor anything the service answered."""
    s = spec(name)
    key = value if value is not None else ctx.config.secret(s.attr)
    if not key:
        return {"result": "missing", "detail": f"No {s.label} key is set."}
    why = _why_not_checked(ctx, s)
    if why:
        return _unchecked(why)
    url, headers = _request(s, key, ctx.config)
    try:
        async with httpx.AsyncClient(transport=check_transport, timeout=CHECK_TIMEOUT_S,
                                     follow_redirects=False) as client:
            r = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        return _unchecked(f"{s.label} didn't answer within {CHECK_TIMEOUT_S:.0f} seconds, so the key wasn't checked.")
    except (httpx.HTTPError, OSError):
        return _unchecked(f"Couldn't reach {s.label}, so the key wasn't checked.")
    if s.name == "elevenlabs" and r.status_code == 401 and "missing_permissions" in r.text:
        return {"result": "ok", "detail": f"{s.label} accepted the key (it may not read the account, which is fine)."}
    if r.status_code in (401, 403):
        return {"result": "refused", "detail": f"{s.label} refused that key."}
    if 200 <= r.status_code < 300:
        return {"result": "ok", "detail": f"{s.label} accepted the key."}
    return _unchecked(f"{s.label} answered with an error ({r.status_code}), so the key wasn't checked.")


# For other secrets kept the same way (the calendar's per-person iCal addresses, the phones' ntfy topics).
read_private = _read_private
write_private = _write_private

__all__ = ["BY_NAME", "KEYS", "KeySpec", "LiveKey", "NAMES", "check", "clean_value", "current_env", "key_status",
           "live_key", "load_into", "read_private", "remove", "resolve", "save", "spec", "status", "write_private"]
