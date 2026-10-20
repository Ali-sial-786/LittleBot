"""Background asyncio runtime.

Pygame must own the main thread, so all asyncio work (dashboard server,
activity engine, conversation, media and motion schedulers) runs on a single
event loop in a dedicated daemon thread. Other threads hand work to it with
``submit`` / ``call``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any, Awaitable, Callable, Coroutine, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


class AsyncRuntime:
    def __init__(self, name: str = "littlebot-async") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("runtime not started")
        return self._loop

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("async runtime failed to start")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(self._on_exception)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    @staticmethod
    def _on_exception(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        log.error("unhandled async error: %s", context.get("message"), exc_info=exc)

    def submit(self, coro: Coroutine[Any, Any, T]) -> "concurrent.futures.Future[T]":
        """Schedule a coroutine from any thread; returns a concurrent Future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = 30) -> T:
        """Run a coroutine on the runtime loop and block until it finishes (not from the loop thread!)."""
        if threading.current_thread() is self._thread:
            raise RuntimeError("AsyncRuntime.run() called from the runtime thread; await instead")
        return self.submit(coro).result(timeout)

    def call_soon(self, fn: Callable[..., Any], *args: Any) -> None:
        self.loop.call_soon_threadsafe(fn, *args)

    def stop(self, timeout: float = 5) -> None:
        if not self.running or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        assert self._thread is not None
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.warning("async runtime did not stop within %.1fs", timeout)
        self._thread = None


async def cancel_and_wait(task: "asyncio.Task[Any] | None", timeout: float = 2.0) -> None:
    """Cancel a task and wait (bounded) for it to finish."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception:  # noqa: BLE001
        log.exception("task raised during cancellation")


async def maybe_await(value: Any) -> Any:
    if isinstance(value, Awaitable):
        return await value
    return value
