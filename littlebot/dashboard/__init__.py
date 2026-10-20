"""Parent dashboard: FastAPI server + static vanilla-JS frontend.

``create_service(ctx)`` returns a :class:`DashboardService` that runs uvicorn
as a task on the *current* asyncio loop (the robot's AsyncRuntime loop) -
no extra threads or loops. ``create_app(ctx, robot=None)`` builds the ASGI
app on its own (used by tests).

Binding: 127.0.0.1 by default; 0.0.0.0 only when ``dashboard.lan_access`` is
explicitly enabled (config or ``--lan``).
"""

from __future__ import annotations

import asyncio
import errno
import socket
from typing import Any

from littlebot.core.context import RobotContext
from littlebot.core.events import Subscription, Topics
from littlebot.core.service import Service, ServiceStatus
from littlebot.dashboard.app import create_app
from littlebot.dashboard.auth import log_setup_code
from littlebot.dashboard.deps import DashState, dashboard_urls, resolve_bind_host

__all__ = ["DashboardService", "create_app", "create_service", "resolve_bind_host"]


class DashboardService(Service):
    name = "dashboard"

    def __init__(self, ctx: RobotContext, *, port: int | None = None) -> None:
        super().__init__()
        self.ctx = ctx
        self.app = create_app(ctx)
        self.dash: DashState = self.app.state.dash
        self.host = resolve_bind_host(ctx.config.dashboard)
        self.port = int(ctx.config.dashboard.port if port is None else port)
        self._server: Any = None
        self._task: asyncio.Task[Any] | None = None
        self._sock: socket.socket | None = None
        self._stop_sub: Subscription | None = None

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
        return f"http://{host}:{self.port}/"

    @property
    def auth(self) -> Any:
        return self.dash.auth

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        import uvicorn

        self.set_status(ServiceStatus.STARTING)
        try:
            self._sock = _bind_socket(self.host, self.port)
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                detail = f"port {self.port} is already in use (is another LittleBot running?)"
            else:
                detail = f"cannot listen on {self.host}:{self.port}: {e.strerror or e}"
            self.log.error("dashboard not started: %s. Change dashboard.port or use --port.", detail)
            self.set_status(ServiceStatus.FAILED, detail)
            return
        self.port = self._sock.getsockname()[1]
        self.dash.bind_host, self.dash.port = self.host, self.port
        self.dash.urls = await asyncio.to_thread(dashboard_urls, self.host, self.port)

        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning", lifespan="off",
                                access_log=False, server_header=False, timeout_graceful_shutdown=2)
        self._server = uvicorn.Server(config)
        self._task = asyncio.get_running_loop().create_task(self._serve(), name="dashboard-server")
        loop = asyncio.get_running_loop()
        self._stop_sub = self.ctx.bus.subscribe_async(Topics.STOP_ALL, self._on_stop_all, loop)

        for _ in range(100):  # wait (bounded) until uvicorn is accepting connections
            if self._server.started or self._task.done():
                break
            await asyncio.sleep(0.02)
        if self._task.done():
            return  # _serve already recorded the failure
        where = ", ".join(self.dash.urls) or self.url
        self.set_status(ServiceStatus.RUNNING, where)
        self.log.info("parent dashboard at %s%s", where, "" if self.host == "0.0.0.0" else " (this computer only)")
        if self.auth.setup_required:
            log_setup_code(self.auth.issue_setup_code(), self.dash.urls[0] if self.dash.urls else self.url)

    async def _serve(self) -> None:
        try:
            await self._server.serve(sockets=[self._sock])
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - uvicorn may sys.exit(); never let it kill the robot loop
            self.log.error("dashboard server stopped unexpectedly: %r", e)
            self.set_status(ServiceStatus.FAILED, "server error")
        finally:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass

    def _on_stop_all(self, _event: Any) -> None:
        self.dash.cancel("face_demo")
        self.dash.cancel("motion")

    async def stop(self) -> None:
        if self._stop_sub is not None:
            self._stop_sub.cancel()
            self._stop_sub = None
        await self.dash.cancel_all()
        task, server = self._task, self._server
        if task is not None and server is not None and not task.done():
            server.should_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=4.0)
            except asyncio.TimeoutError:
                server.force_exit = True
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                self.log.exception("error while stopping the dashboard")
        elif self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._task = None
        self._server = None
        if self.service_status != ServiceStatus.FAILED:
            self.set_status(ServiceStatus.STOPPED)

    def health(self) -> dict[str, Any]:
        h = super().health()
        h.update(host=self.host, port=self.port, lan_access=self.host == "0.0.0.0")
        return h


def _bind_socket(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(64)
        sock.setblocking(False)
        sock.set_inheritable(True)
    except OSError:
        sock.close()
        raise
    return sock


def create_service(ctx: RobotContext) -> DashboardService:
    return DashboardService(ctx)
