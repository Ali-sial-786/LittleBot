"""Shared state and helpers for dashboard routes.

``DashState`` is created once per app (``app.state.dash``) and gives routes
access to the robot context, the (optional) robot facade, the auth manager
and a small registry of background tasks (face demo, gestures) that are
cancelled when the dashboard stops.
"""

from __future__ import annotations

import asyncio
import collections
import ipaddress
import itertools
import logging
import socket
import threading
import time
from typing import Any, Coroutine
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from littlebot import __version__
from littlebot.core.context import RobotContext
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.state import RobotState
from littlebot.dashboard.auth import AuthManager, Session, log_setup_code
from littlebot.safety.policy import age_group_label

log = logging.getLogger("littlebot.dashboard")

# Human-friendly names used in "unavailable" errors.
SUBSYSTEM_LABELS = {
    "face": "The face display",
    "audio": "Audio (speaker and microphone)",
    "vision": "The camera / vision system",
    "motion": "The motion system",
    "media": "The video player",
    "brain": "The conversation system",
    "activities": "The activities engine",
}

# Alerts older than this are no longer shown.
ALERT_TTL_SECONDS = 30 * 60

# Subsystems included in the aggregated status, in display order.
STATUS_SUBSYSTEMS = ("face", "audio", "vision", "motion", "media", "brain", "activities")


class DashState:
    def __init__(self, ctx: RobotContext, robot: Any = None, auth: AuthManager | None = None) -> None:
        self.ctx = ctx
        self._robot = robot
        cfg = ctx.config.dashboard
        self.auth = auth or AuthManager(ctx.db, session_hours=cfg.session_hours,
                                        attempts_per_minute=cfg.login_attempts_per_minute)
        self.tasks: set[asyncio.Task[Any]] = set()
        self.named_tasks: dict[str, asyncio.Task[Any]] = {}
        self.started_at = time.time()
        self.bind_host: str | None = None
        self.port: int | None = None
        self.urls: list[str] = []
        self.hosts = HostAllowlist(self)
        # Parent alerts (e.g. the brain heard "I'm hurt"): category labels only, never the child's words.
        self._alerts: collections.deque[dict[str, Any]] = collections.deque(maxlen=20)
        self._alert_ids = itertools.count(1)
        self._alert_lock = threading.Lock()
        self._subs: list[Subscription] = []
        self.open()

    def open(self) -> None:
        """Subscribe to the bus events that become parent alerts (idempotent)."""
        if not self._subs:
            self._subs = [
                self.ctx.bus.subscribe(Topics.BRAIN_STATUS, self._on_brain_status),
                self.ctx.bus.subscribe(Topics.SYSTEM_ERROR, self._on_system_error),
            ]

    def close(self) -> None:
        for sub in self._subs:
            sub.cancel()
        self._subs.clear()

    def ensure_setup_code(self) -> None:
        """No password and no active setup code (``reset-password`` while running): issue, log
        and announce (``auth.on_setup_code`` -> robot screen) a fresh one."""
        code = self.auth.ensure_setup_code()
        if code is not None:
            log_setup_code(code, self.urls[0] if self.urls else None)

    # ------------------------------------------------------------ alerts
    def _add_alert(self, kind: str, title: str, detail: str, key: str) -> None:
        now = time.time()
        with self._alert_lock:
            for a in self._alerts:  # de-duplicate bursts of the same alert
                if a["key"] == key and not a["dismissed"] and now - a["ts"] < 60:
                    a["ts"], a["count"] = now, a["count"] + 1
                    return
            self._alerts.append({"id": next(self._alert_ids), "kind": kind, "title": title, "detail": detail[:160],
                                 "key": key, "ts": now, "count": 1, "dismissed": False})

    def _on_brain_status(self, event: Event) -> None:
        if event.get("status") == "attention":
            category = str(event.get("detail") or "safety")[:40]
            self._add_alert("attention", "Your child may need you",
                            f"LittleBot heard something that may need a grown-up ({category}) and suggested finding one. "
                            "Please check on your child.",
                            f"attention:{category}")

    def _on_system_error(self, event: Event) -> None:
        module = str(event.get("module") or "system")[:40]
        self._add_alert("error", f"Problem with {module}", str(event.get("error") or "unknown error"), f"error:{module}")

    def alerts(self) -> list[dict[str, Any]]:
        cutoff = time.time() - ALERT_TTL_SECONDS
        with self._alert_lock:
            return [{k: a[k] for k in ("id", "kind", "title", "detail", "ts", "count")}
                    for a in self._alerts if not a["dismissed"] and a["ts"] >= cutoff]

    def dismiss_alert(self, alert_id: int | None = None) -> int:
        n = 0
        with self._alert_lock:
            for a in self._alerts:
                if not a["dismissed"] and (alert_id is None or a["id"] == alert_id):
                    a["dismissed"] = True
                    n += 1
        return n

    # ------------------------------------------------------------ robot
    @property
    def robot(self) -> Any:
        if self._robot is not None:
            return self._robot
        return self.ctx.extras.get("robot")

    def need(self, name: str) -> Any:
        """Return ``ctx.<name>`` or raise a friendly 503 if that subsystem is unavailable."""
        svc = getattr(self.ctx, name, None)
        if svc is None:
            raise HTTPException(status_code=503, detail=f"{SUBSYSTEM_LABELS.get(name, name)} is unavailable right now.")
        return svc

    # ------------------------------------------------------------ tasks
    def spawn(self, coro: Coroutine[Any, Any, Any], name: str | None = None) -> asyncio.Task[Any]:
        """Run ``coro`` in the background on the current loop. A named task replaces its predecessor."""
        if name is not None:
            prev = self.named_tasks.pop(name, None)
            if prev is not None and not prev.done():
                prev.cancel()
        task = asyncio.get_running_loop().create_task(coro, name=f"dashboard:{name or 'task'}")
        self.tasks.add(task)
        if name is not None:
            self.named_tasks[name] = task

        def _done(t: asyncio.Task[Any]) -> None:
            self.tasks.discard(t)
            if name is not None and self.named_tasks.get(name) is t:
                self.named_tasks.pop(name, None)
            if not t.cancelled() and t.exception() is not None:
                log.error("dashboard background task %s failed", name or "task", exc_info=t.exception())

        task.add_done_callback(_done)
        return task

    def cancel(self, name: str) -> bool:
        task = self.named_tasks.pop(name, None)
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    async def cancel_all(self, timeout: float = 2.0) -> None:
        tasks = [t for t in self.tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        self.named_tasks.clear()

    # ------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        """Aggregated, poll-friendly robot status (cheap: every part is a cached snapshot)."""
        ctx = self.ctx
        robot = self.robot
        out: dict[str, Any] | None = None
        if robot is not None and callable(getattr(robot, "status", None)):
            try:
                out = dict(robot.status())
            except Exception:  # noqa: BLE001
                log.exception("robot.status() failed; building status from context")
                out = None
        if out is None:
            out = self._status_from_ctx()
        settings = ctx.settings
        age = out.get("age_months", settings.child_age_months())
        group = out.get("age_group") or ctx.age_policy().group
        out["age_months"] = age
        out["age_group"] = group
        out["age_group_label"] = age_group_label(group)
        for attr in STATUS_SUBSYSTEMS:
            if getattr(ctx, attr) is None:
                out[attr] = {"status": "unavailable"}
            elif not isinstance(out.get(attr), dict):
                out[attr] = _safe_status(getattr(ctx, attr))
        started = getattr(robot, "started_at", None) if robot is not None else None
        out["uptime_seconds"] = round(time.time() - (started or self.started_at), 1)
        prof = settings.get("profile") or {}
        out["child"] = {"nickname": prof.get("nickname") or ""}
        out["settings"] = {
            "audio_volume": settings.get("audio.volume"),
            "media_volume": settings.get("media.volume"),
            "media_enabled": settings.get("media.enabled"),
            "ai_enabled": settings.get("ai.enabled"),
            "follow_faces": settings.get("vision.follow_faces"),
            "motion_armed": settings.get("motion.physical_armed"),
        }
        out["dashboard"] = {"lan_access": ctx.config.dashboard.lan_access, "port": self.port or ctx.config.dashboard.port}
        out["alerts"] = self.alerts()
        out["server_time"] = time.time()
        return out

    def _status_from_ctx(self) -> dict[str, Any]:
        ctx = self.ctx
        return {
            "name": ctx.config.robot_name,
            "version": __version__,
            "mode": ctx.config.mode.value,
            "state": ctx.state.snapshot(),
            "leases": ctx.arbiter.snapshot(),
            "privacy": ctx.privacy.snapshot(),
            "system": _safe_status(ctx.monitor),
            "age_months": ctx.settings.child_age_months(),
            "age_group": ctx.age_policy().group,
            **{attr: (_safe_status(getattr(ctx, attr)) if getattr(ctx, attr) is not None else {"status": "unavailable"})
               for attr in STATUS_SUBSYSTEMS},
            "services": {},
        }

    # ------------------------------------------------------------ actions
    async def stop_all(self, source: str = "dashboard") -> dict[str, Any]:
        """Big STOP: use the robot facade when present, else do the equivalent directly."""
        self.cancel("face_demo")
        self.cancel("motion")
        robot = self.robot
        if robot is not None and callable(getattr(robot, "stop_all", None)):
            await robot.stop_all(source)
            return self.status()
        ctx = self.ctx
        ctx.bus.publish(Topics.STOP_ALL, {"source": source}, source="dashboard")
        if ctx.motion is not None:
            _quiet(ctx.motion.cancel)
        if ctx.audio is not None:
            _quiet(ctx.audio.stop_output)
        if ctx.activities is not None:
            await _quiet_async(ctx.activities.stop("parent stop"))
        if ctx.media is not None:
            await _quiet_async(ctx.media.stop("stopped"))
        ctx.arbiter.preempt_all(by=source)
        ctx.state.transition(RobotState.IDLE, f"stop all ({source})", force=True)
        if ctx.face is not None:
            _quiet(ctx.face.open_eyes)
            _quiet(ctx.face.set_expression, "neutral")
        return self.status()

    def sleep(self, source: str = "dashboard") -> bool:
        robot = self.robot
        if robot is not None and callable(getattr(robot, "sleep", None)):
            return bool(robot.sleep(source))
        return self.ctx.state.transition(RobotState.SLEEPING, f"sleep ({source})")

    def wake(self, source: str = "dashboard") -> bool:
        robot = self.robot
        if robot is not None and callable(getattr(robot, "wake", None)):
            return bool(robot.wake(source))
        if self.ctx.state.state in (RobotState.SLEEPING, RobotState.ERROR):
            return self.ctx.state.transition(RobotState.IDLE, f"wake ({source})")
        return True


def get_dash(request: Request) -> DashState:
    return request.app.state.dash


def get_session(request: Request) -> Session:
    sess = getattr(request.state, "session", None)
    if sess is None:  # middleware guarantees this for protected routes; belt and braces
        raise HTTPException(status_code=401, detail="Please log in.")
    return sess


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _safe_status(svc: Any) -> dict[str, Any]:
    try:
        st = svc.status()
        return dict(st) if isinstance(st, dict) else {"status": "unknown"}
    except Exception as e:  # noqa: BLE001
        log.debug("status() failed for %r", svc, exc_info=True)
        return {"status": "error", "detail": str(e)[:200]}


def _quiet(fn: Any, *args: Any) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        log.exception("stop step failed")


async def _quiet_async(coro: Any) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001
        log.exception("stop step failed")


# ---------------------------------------------------------------------------
# Bind address
# ---------------------------------------------------------------------------

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def resolve_bind_host(cfg: Any) -> str:
    """127.0.0.1 unless ``lan_access`` is explicitly enabled (then 0.0.0.0).

    A non-loopback ``host`` without ``lan_access`` is refused (falls back to
    localhost) so the dashboard is never exposed to the network by accident.
    """
    if getattr(cfg, "lan_access", False):
        return "0.0.0.0"
    host = str(getattr(cfg, "host", "127.0.0.1") or "127.0.0.1").strip()
    if host in _LOOPBACK or host.startswith("127."):
        return "127.0.0.1" if host == "localhost" else host
    log.warning("dashboard.host=%s ignored: LAN access is off (set dashboard.lan_access or use --lan)", host)
    return "127.0.0.1"


def lan_addresses() -> list[str]:
    """Best-effort LAN IPv4 address of this machine (no packets are sent)."""
    addrs: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET address: routing lookup only
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                addrs.append(ip)
    except OSError:
        pass
    return addrs


def split_host(value: str) -> tuple[str, int | None] | None:
    """``Host`` header / config entry -> (lower-case name without trailing dot, port or None); None if malformed."""
    v = value.strip().lower()
    if "://" in v:  # tolerate "http://name:port/" in the config
        v = urlsplit(v).netloc
    if not v or any(c in v for c in "/\\@ \t"):
        return None
    if v.startswith("["):  # [IPv6]:port
        end = v.find("]")
        if end < 0:
            return None
        name, rest = v[1:end], v[end + 1:]
        if rest and not rest.startswith(":"):
            return None
        port_s = rest[1:]
    else:
        if v.count(":") > 1:  # a bare IPv6 address is not a valid Host header
            return None
        name, _, port_s = v.partition(":")
    name = name.rstrip(".")
    if not name:
        return None
    if not port_s:
        return name, None
    if not port_s.isdigit() or len(port_s) > 5 or int(port_s) > 65535:
        return None
    return name, int(port_s)


def _ip(name: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(name)
    except ValueError:
        return None


class HostAllowlist:
    """Which ``Host`` header values the dashboard answers (DNS-rebinding defence).

    A browser always sends the host name from the address bar, so a page on
    ``attacker.example`` whose DNS is re-pointed at the robot still sends
    ``Host: attacker.example`` and is refused before any route runs. Allowed:

    * ``localhost`` and loopback IP literals (``127.x.x.x``, ``::1``);
    * this machine's hostname, its first label and ``<first label>.local``;
    * with ``dashboard.lan_access``: this machine's LAN IP addresses (the address the
      connection actually arrived on, plus ``lan_addresses()`` / the dashboard URLs);
    * every ``dashboard.allowed_hosts`` entry and the host of ``media.player_base_url``.

    Loopback names (``localhost``, ``127.x``, ``::1``) match any port (SSH tunnels). Other
    names given without a port match the dashboard's port or no port; an entry with an
    explicit port (``"robot.example:9000"``) matches only that port.
    """

    REFRESH_SECONDS = 30.0  # how often an unknown IP literal may trigger a LAN address re-scan
    _MAX_LOGGED = 20

    def __init__(self, dash: "DashState") -> None:
        self._dash = dash
        self._names: frozenset[str] | None = None
        self._ips: set[str] = set()
        self._ips_scanned = float("-inf")
        self._logged: set[str] = set()

    def _own_names(self) -> frozenset[str]:
        if self._names is None:
            names = {"localhost"}
            try:
                hostname = socket.gethostname().strip().lower().rstrip(".")
            except OSError:
                hostname = ""
            if hostname:
                short = hostname.split(".")[0]
                names.update({hostname, short, f"{short}.local"})
            self._names = frozenset(names)
        return self._names

    def _configured(self) -> list[tuple[str, int | None]]:
        ctx = self._dash.ctx
        entries = list(getattr(ctx.config.dashboard, "allowed_hosts", None) or [])
        player_base = getattr(getattr(ctx.config, "media", None), "player_base_url", None)
        if player_base:
            entries.append(str(player_base))
        return [h for h in (split_host(str(e)) for e in entries) if h is not None]

    def _is_lan_ip(self, ip: str, server: Any) -> bool:
        if isinstance(server, (tuple, list)) and server and str(server[0]).lower() == ip:
            return True  # the address this very connection was accepted on
        if ip in self._ips:
            return True
        now = time.monotonic()
        if now - self._ips_scanned >= self.REFRESH_SECONDS:  # DHCP may have moved us: re-scan (throttled)
            self._ips_scanned = now
            ips = set(lan_addresses())
            ips.update((urlsplit(u).hostname or "").lower() for u in self._dash.urls)
            self._ips = {i for i in ips if i}
        return ip in self._ips

    def allowed(self, value: str | None, server: Any = None) -> bool:
        if value is None:
            return True  # HTTP/1.0 without Host: not a browser, so not a rebinding attack
        parsed = split_host(value)
        if parsed is None:
            return False
        name, port = parsed
        dash_port = self._dash.port or self._dash.ctx.config.dashboard.port
        for allowed_name, allowed_port in self._configured():
            if name == allowed_name and (port == allowed_port if allowed_port is not None
                                         else port in (None, dash_port)):
                return True
        # Loopback names on any port (SSH tunnels, port forwards on this machine): a rebinding
        # page always sends ITS OWN hostname, never "localhost", so port strictness adds nothing.
        if name == "localhost" or ((ip0 := _ip(name)) is not None and ip0.is_loopback):
            return True
        if port not in (None, dash_port):
            return False
        if name in self._own_names():
            return True
        ip = _ip(name)
        if ip is not None:
            if ip.is_loopback:
                return True
            if self._dash.ctx.config.dashboard.lan_access and self._is_lan_ip(str(ip), server):
                return True
        return False

    def note_rejected(self, value: str | None) -> None:
        """Log the first few distinct refused names (helps a parent who uses an unusual address)."""
        key = (value or "")[:100]
        if key in self._logged or len(self._logged) >= self._MAX_LOGGED:
            return
        self._logged.add(key)
        log.warning("dashboard request for unknown host %r refused (DNS-rebinding protection). If this is "
                    "how you reach your robot, add it to dashboard.allowed_hosts.", key)


def dashboard_urls(bind_host: str, port: int) -> list[str]:
    if bind_host == "0.0.0.0":
        urls = [f"http://{ip}:{port}/" for ip in lan_addresses()]
        try:
            hostname = socket.gethostname()
            if hostname and "." not in hostname:
                urls.append(f"http://{hostname}.local:{port}/")
        except OSError:
            pass
        urls.append(f"http://127.0.0.1:{port}/")
        return urls
    return [f"http://{bind_host}:{port}/"]
