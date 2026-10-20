"""Shared state and helpers for dashboard routes.

``DashState`` is created once per app (``app.state.dash``) and gives routes
access to the robot context, the (optional) robot facade, the auth manager
and a small registry of background tasks (face demo, gestures) that are
cancelled when the dashboard stops.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Any, Coroutine

from fastapi import HTTPException, Request

from littlebot import __version__
from littlebot.core.context import RobotContext
from littlebot.core.events import Topics
from littlebot.core.state import RobotState
from littlebot.dashboard.auth import AuthManager, Session
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
