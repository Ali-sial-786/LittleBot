"""Parent authentication for the dashboard.

Design (see docs/modules/dashboard.md for the full write-up):

* There is one parent account and **no default password**. On first run the
  server generates a one-time *setup code*, logs it to the robot's console /
  log file and keeps only its SHA-256 in memory. Whoever can read the robot's
  console types the code into the ``/setup`` screen and chooses a password.
* Passwords are hashed with ``hashlib.scrypt`` (per-password random salt) and
  stored in the SQLite key/value table. Verification is constant-time.
  Hashing runs in a worker thread so it never blocks the asyncio loop.
* Sessions use random 32-byte tokens. Only the SHA-256 of a token is stored
  (``dashboard_sessions`` table, cached in memory). Each session has its own
  CSRF token which the frontend sends as ``X-CSRF-Token`` on every mutation.
* Login, setup-code and change-password attempts are rate limited per client IP
  (the real TCP peer: uvicorn runs with ``proxy_headers=False``). Failed logins and
  setup codes are additionally counted across *all* clients: too many in a minute
  pauses login / setup for everyone for a short lockout (so many addresses cannot
  multiply the guess rate).
* A session is only valid while its row exists in SQLite, so the
  ``reset-password`` CLI (another process) logs every device out immediately.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from littlebot.core.storage import Database

log = logging.getLogger("littlebot.dashboard.auth")

PASSWORD_KEY = "dashboard:password"
SESSION_COOKIE = "littlebot_session"
CSRF_HEADER = "X-CSRF-Token"
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
MAX_SESSIONS = 32  # oldest sessions are dropped beyond this
SETUP_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
SETUP_CODE_LENGTH = 8
SETUP_CODE_MAX_FAILURES = 10  # after this many wrong codes a fresh code is issued ...
SETUP_CODE_MAX_FAILURES_PER_CLIENT = 3  # ... but one client counts at most 3 of them (cannot burn it alone)

# Failed logins / setup codes from all clients together: this many per minute, then everyone waits.
GLOBAL_FAILURES_PER_MINUTE = 20
GLOBAL_LOCKOUT_SECONDS = 30.0

# The client address of the request being handled (set by the dashboard middleware), so the
# setup-code failure accounting can tell clients apart without changing the route signatures.
current_client: contextvars.ContextVar[str | None] = contextvars.ContextVar("littlebot_dashboard_client",
                                                                           default=None)

# scrypt cost: 16 MiB of memory, ~50-150 ms on a Raspberry Pi 5.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


class AuthError(Exception):
    """Authentication failed (wrong password / setup code). Message is user-safe."""


class SetupAlreadyDone(Exception):
    pass


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def validate_new_password(password: str) -> None:
    if not isinstance(password, str):
        raise ValueError("Password must be text.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")
    if not password.strip():
        raise ValueError("Password cannot be only spaces.")


def hash_password(password: str, *, salt: bytes | None = None, n: int = SCRYPT_N, r: int = SCRYPT_R,
                  p: int = SCRYPT_P) -> dict[str, Any]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=SCRYPT_DKLEN,
                            maxmem=128 * r * (n + p + 2) + 1024 * 1024)
    return {"algo": "scrypt", "n": n, "r": r, "p": p, "salt": salt.hex(), "hash": digest.hex(),
            "updated": time.time()}


def verify_password(password: str, record: dict[str, Any] | None) -> bool:
    """Constant-time check of ``password`` against a stored record."""
    if not record or record.get("algo") != "scrypt":
        # Burn comparable time so a missing record is not distinguishable by timing.
        hash_password(password or "", salt=b"\0" * 16)
        return False
    try:
        n, r, p = int(record["n"]), int(record["r"]), int(record["p"])
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["hash"])
    except (KeyError, TypeError, ValueError):
        log.error("stored dashboard password record is corrupt")
        return False
    actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected),
                            maxmem=128 * r * (n + p + 2) + 1024 * 1024)
    return hmac.compare_digest(actual, expected)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalise_setup_code(code: str) -> str:
    return "".join(ch for ch in str(code).upper() if ch.isalnum())


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Sliding-window limiter: at most ``limit`` hits per ``window`` seconds per key."""

    def __init__(self, limit: int, window: float = 60.0, *, now: Callable[[], float] = time.monotonic,
                 max_keys: int = 4096) -> None:
        self.limit = max(1, int(limit))
        self.window = float(window)
        self._now = now
        self._hits: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def hit(self, key: str) -> float | None:
        """Record an attempt. Returns ``None`` if allowed, else seconds until retry."""
        now = self._now()
        with self._lock:
            dq = self._hits.get(key)
            if dq is None:
                if len(self._hits) >= self._max_keys:
                    self._prune(now)
                dq = self._hits[key] = collections.deque()
            while dq and now - dq[0] >= self.window:
                dq.popleft()
            if len(dq) >= self.limit:
                return max(1.0, self.window - (now - dq[0]))
            dq.append(now)
            return None

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def _prune(self, now: float) -> None:
        for k in [k for k, dq in self._hits.items() if not dq or now - dq[-1] >= self.window]:
            del self._hits[k]
        if len(self._hits) >= self._max_keys:  # still full: drop the oldest half
            for k in list(self._hits)[: self._max_keys // 2]:
                del self._hits[k]


class FailureLockout:
    """Failures from *all* clients: ``limit`` within ``window`` s locks everyone out for ``lockout`` s.

    Failures stay in the sliding window, so a failure right after a lockout (while the window
    is still full) starts the next one: overall at most about ``limit`` failures per window.
    """

    def __init__(self, limit: int = GLOBAL_FAILURES_PER_MINUTE, window: float = 60.0,
                 lockout: float = GLOBAL_LOCKOUT_SECONDS, *, now: Callable[[], float] = time.monotonic,
                 name: str = "login") -> None:
        self.limit = max(1, int(limit))
        self.window = float(window)
        self.lockout = float(lockout)
        self.name = name
        self._now = now
        self._fails: collections.deque[float] = collections.deque()
        self._locked_until = 0.0
        self._lock = threading.Lock()

    def retry_after(self) -> float | None:
        """Seconds until attempts are accepted again, or ``None`` when not locked."""
        remaining = self._locked_until - self._now()
        return remaining if remaining > 0 else None

    def failure(self) -> None:
        now = self._now()
        with self._lock:
            while self._fails and now - self._fails[0] >= self.window:
                self._fails.popleft()
            self._fails.append(now)
            if len(self._fails) >= self.limit and now >= self._locked_until:
                self._locked_until = now + self.lockout
                log.warning("%d failed dashboard %s attempts within %.0f s (from several devices?): "
                            "%s paused for everyone for %.0f s", len(self._fails), self.name, self.window,
                            self.name, self.lockout)

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()
            self._locked_until = 0.0


class GuardedLimiter:
    """What the login / setup routes call ``hit(ip)`` on: the global lockout first, then per client.

    Requests refused here never reach the password / setup-code check, so they are not counted
    as failures (neither per client nor globally) and cannot burn the setup code.
    """

    def __init__(self, per_client: RateLimiter, lockout: FailureLockout) -> None:
        self.per_client = per_client
        self.lockout = lockout

    @property
    def limit(self) -> int:
        return self.per_client.limit

    @property
    def window(self) -> float:
        return self.per_client.window

    def hit(self, key: str) -> float | None:
        retry = self.lockout.retry_after()
        if retry is not None:
            return retry
        return self.per_client.hit(key)

    def failure(self) -> None:
        self.lockout.failure()

    def reset(self, key: str) -> None:
        self.per_client.reset(key)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@dataclass
class Session:
    token_hash: str
    csrf_token: str
    created: float
    expires: float
    ip: str = ""
    user_agent: str = ""

    def public(self) -> dict[str, Any]:
        return {"created": self.created, "expires": self.expires}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS dashboard_sessions (
    token_hash TEXT PRIMARY KEY,
    csrf TEXT NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    ip TEXT DEFAULT '',
    user_agent TEXT DEFAULT ''
)
"""


class AuthManager:
    """Password, setup code, sessions and rate limits for the single parent account."""

    def __init__(self, db: Database, *, session_hours: float = 12.0, attempts_per_minute: int = 5,
                 global_failures_per_minute: int | None = None, global_lockout_seconds: float = GLOBAL_LOCKOUT_SECONDS,
                 now: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._db = db
        self._now = now
        self.session_seconds = max(60.0, float(session_hours) * 3600.0)
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._setup_code_hash: str | None = None
        self._setup_failures = 0
        self._setup_failures_by_client: dict[str, int] = {}
        # Called with the new code when one is issued, and with None once setup is complete
        # (the dashboard service uses it to show the code on the robot's own screen).
        self.on_setup_code: Callable[[str | None], None] | None = None
        if global_failures_per_minute is None:  # never below a few devices' worth of per-client attempts
            global_failures_per_minute = max(GLOBAL_FAILURES_PER_MINUTE, 2 * int(attempts_per_minute))
        self.login_limiter = GuardedLimiter(
            RateLimiter(attempts_per_minute, 60.0, now=monotonic),
            FailureLockout(global_failures_per_minute, 60.0, global_lockout_seconds, now=monotonic, name="login"))
        self.setup_limiter = GuardedLimiter(
            RateLimiter(attempts_per_minute, 60.0, now=monotonic),
            FailureLockout(global_failures_per_minute, 60.0, global_lockout_seconds, now=monotonic, name="setup"))
        self.password_limiter = RateLimiter(attempts_per_minute, 60.0, now=monotonic)
        self._db.execute(_SCHEMA)
        self._load_sessions()

    # ------------------------------------------------------------ password
    @property
    def setup_required(self) -> bool:
        return self._password_record() is None

    def _password_record(self) -> dict[str, Any] | None:
        rec = self._db.kv_get(PASSWORD_KEY)
        return rec if isinstance(rec, dict) and rec.get("hash") else None

    def issue_setup_code(self) -> str:
        """Generate a fresh one-time setup code (only the hash is kept). Caller logs it."""
        code = "".join(secrets.choice(SETUP_CODE_ALPHABET) for _ in range(SETUP_CODE_LENGTH))
        with self._lock:
            self._setup_code_hash = _sha256(code)
            self._setup_failures = 0
            self._setup_failures_by_client.clear()
        pretty = f"{code[:4]}-{code[4:]}"
        self._notify_setup_code(pretty)
        return pretty

    def ensure_setup_code(self) -> str | None:
        """Issue a fresh code when setup is needed but none is active, else ``None``. Caller logs it.

        This is how a running robot recovers from ``reset-password`` run in another process:
        the next dashboard visit finds no password and no code, and gets a new one.
        """
        if self._setup_code_hash is not None or not self.setup_required:
            return None
        log.warning("the dashboard has no password and no active setup code (reset-password while running?); "
                    "issuing a new setup code")
        return self.issue_setup_code()

    def _notify_setup_code(self, code: str | None) -> None:
        hook = self.on_setup_code
        if hook is not None:
            try:
                hook(code)
            except Exception:  # noqa: BLE001
                log.exception("setup-code listener failed")

    @property
    def setup_code_active(self) -> bool:
        return self._setup_code_hash is not None

    def _check_setup_code(self, code: str, client: str | None = None) -> bool:
        """Compare against the active code. Wrong codes count towards burning it, but each client
        (``client`` or the request's address from :data:`current_client`) only up to
        ``SETUP_CODE_MAX_FAILURES_PER_CLIENT`` times, so a single neighbour can only be rate
        limited, never deny the parent their code. Calls with no known client always count.
        """
        client = client if client is not None else current_client.get()
        with self._lock:
            expected = self._setup_code_hash
            if expected is None:
                return False
            ok = hmac.compare_digest(_sha256(normalise_setup_code(code)), expected)
            if not ok:
                self.setup_limiter.failure()
                counted = True
                if client is not None:
                    seen = self._setup_failures_by_client.get(client, 0)
                    counted = seen < SETUP_CODE_MAX_FAILURES_PER_CLIENT
                    if counted:
                        self._setup_failures_by_client[client] = seen + 1
                if counted:
                    self._setup_failures += 1
                if self._setup_failures >= SETUP_CODE_MAX_FAILURES:
                    # Too many wrong guesses: burn this code. The service logs a new one.
                    self._setup_code_hash = None
                    log.warning("too many wrong dashboard setup codes; the code has been invalidated")
                    new_code = self.issue_setup_code()
                    log_setup_code(new_code)
            return ok

    async def complete_setup(self, code: str, password: str) -> None:
        """Claim the first-run setup: verify the code, store the password hash."""
        if not self.setup_required:
            raise SetupAlreadyDone("Setup has already been completed.")
        validate_new_password(password)
        if not self._check_setup_code(code):
            raise AuthError("That setup code is not valid. Check the robot's console or log.")
        record = await asyncio.to_thread(hash_password, password)
        with self._lock:
            if not self.setup_required:  # lost a race with a concurrent setup
                raise SetupAlreadyDone("Setup has already been completed.")
            self._db.kv_set(PASSWORD_KEY, record)
            self._setup_code_hash = None
        self._notify_setup_code(None)
        log.info("dashboard password set (first-run setup complete)")

    async def verify_password(self, password: str) -> bool:
        """The login check. A wrong password also counts towards the global login lockout."""
        ok = await self._verify(password)
        if not ok:
            self.login_limiter.failure()
        return ok

    async def _verify(self, password: str) -> bool:
        if not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH:
            return False
        return await asyncio.to_thread(verify_password, password, self._password_record())

    async def change_password(self, current: str, new: str, *, keep_session: str | None = None) -> None:
        validate_new_password(new)
        if not await self._verify(current):  # authenticated: per-client password_limiter only
            raise AuthError("Current password is incorrect.")
        record = await asyncio.to_thread(hash_password, new)
        self._db.kv_set(PASSWORD_KEY, record)
        self.revoke_all(except_hash=keep_session)
        log.info("dashboard password changed; other sessions logged out")

    def reset_password(self) -> None:
        """Forget the password and all sessions (CLI recovery). A new setup code is needed."""
        self._db.kv_delete(PASSWORD_KEY)
        self.revoke_all()

    # ------------------------------------------------------------ sessions
    def _load_sessions(self) -> None:
        now = self._now()
        self._db.execute("DELETE FROM dashboard_sessions WHERE expires <= ?", (now,))
        rows = self._db.query("SELECT token_hash, csrf, created, expires, ip, user_agent FROM dashboard_sessions")
        with self._lock:
            self._sessions = {
                r["token_hash"]: Session(r["token_hash"], r["csrf"], r["created"], r["expires"], r["ip"] or "",
                                         r["user_agent"] or "")
                for r in rows
            }

    def create_session(self, ip: str = "", user_agent: str = "") -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        now = self._now()
        sess = Session(_sha256(token), secrets.token_urlsafe(32), now, now + self.session_seconds, ip[:64],
                       user_agent[:200])
        with self._lock:
            self._prune_locked(now)
            while len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda s: s.created)
                self._drop_locked(oldest.token_hash)
            self._sessions[sess.token_hash] = sess
            self._db.execute(
                "INSERT INTO dashboard_sessions(token_hash, csrf, created, expires, ip, user_agent) VALUES(?,?,?,?,?,?)",
                (sess.token_hash, sess.csrf_token, sess.created, sess.expires, sess.ip, sess.user_agent),
            )
        return token, sess

    def get_session(self, token: str | None) -> Session | None:
        if not token or len(token) > 128:
            return None
        h = _sha256(token)
        with self._lock:
            sess = self._sessions.get(h)
            if sess is None:
                return None
            if sess.expires <= self._now():
                self._drop_locked(h)
                return None
            # The memory cache is only a cache: the row must still exist (primary-key lookup), so
            # sessions deleted by another process - the reset-password CLI - end immediately.
            if self._db.query_one("SELECT 1 AS ok FROM dashboard_sessions WHERE token_hash=?", (h,)) is None:
                self._sessions.pop(h, None)
                return None
            return sess

    def check_csrf(self, session: Session, token: str | None) -> bool:
        if not token:
            return False
        return hmac.compare_digest(session.csrf_token.encode(), token.encode("utf-8", "replace"))

    def revoke(self, token_hash: str) -> None:
        with self._lock:
            self._drop_locked(token_hash)

    def revoke_all(self, *, except_hash: str | None = None) -> int:
        with self._lock:
            victims = [h for h in self._sessions if h != except_hash]
            for h in victims:
                self._sessions.pop(h, None)
            if except_hash is None:
                self._db.execute("DELETE FROM dashboard_sessions")
            else:
                self._db.execute("DELETE FROM dashboard_sessions WHERE token_hash != ?", (except_hash,))
        return len(victims)

    def active_sessions(self) -> int:
        with self._lock:
            self._prune_locked(self._now())
            return len(self._sessions)

    def _prune_locked(self, now: float) -> None:
        for h in [h for h, s in self._sessions.items() if s.expires <= now]:
            self._drop_locked(h)

    def _drop_locked(self, token_hash: str) -> None:
        self._sessions.pop(token_hash, None)
        self._db.execute("DELETE FROM dashboard_sessions WHERE token_hash=?", (token_hash,))


def log_setup_code(code: str, url: str | None = None) -> None:
    """Print the setup code prominently to the robot's console / log (never anywhere else)."""
    where = f" at {url}" if url else ""
    log.warning(
        "\n"
        "  ================================================================\n"
        "   LittleBot parent dashboard - first-run setup\n"
        "   Open the dashboard%s and enter the one-time code below.\n"
        "\n"
        "   Dashboard setup code: %s\n"
        "\n"
        "   (Only people who can see this console can claim the dashboard.)\n"
        "  ================================================================",
        where, code,
    )
