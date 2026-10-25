"""One JSON POST for the decision clients, over a connection kept open per event loop (a decision is ~0.2 s; a
new TLS connection each time would double that on the Pi). Errors become ``DecideError``; bodies never reach
the log."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from littlebot.decide.base import DecideError

log = logging.getLogger("littlebot.decide")

_clients: dict[tuple[int, int], httpx.AsyncClient] = {}  # (loop, transport) -> client


def _client(transport: Any) -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    key = (id(loop), id(transport))
    client = _clients.get(key)
    if client is None or client.is_closed:
        for k in [k for k in _clients if k[0] != id(loop) or _clients[k].is_closed]:
            _clients.pop(k, None)  # a test's or an earlier loop's: dropped (never reused across loops)
        client = _clients[key] = httpx.AsyncClient(transport=transport, follow_redirects=False,
                                                   timeout=httpx.Timeout(10.0, connect=5.0))
    return client


async def post_json(url: str, body: dict[str, Any], *, key: str, timeout: float, transport: Any = None) -> Any:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        resp = await asyncio.wait_for(
            _client(transport).post(url, content=json.dumps(body, ensure_ascii=False).encode(), headers=headers),
            timeout)
    except TimeoutError as e:
        raise DecideError("timeout") from e
    except httpx.HTTPError as e:
        raise DecideError("network", type(e).__name__) from e
    if resp.status_code != 200:
        raise DecideError(f"http_{resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise DecideError("bad_answer", "not JSON") from e


async def close_all() -> None:
    for client in list(_clients.values()):
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - closing at shutdown: nothing more to do
            log.debug("decide: closing a connection failed", exc_info=True)
    _clients.clear()


__all__ = ["close_all", "post_json"]
