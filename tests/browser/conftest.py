"""Fixtures for the browser view tests (helpers live in ``browser_testkit``)."""

from __future__ import annotations

from typing import Any

import pytest
from browser_testkit import Env, build_env, close_env  # type: ignore[import-not-found]

from littlebot.browser import service as service_mod


@pytest.fixture(autouse=True)
def fast_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_mod, "WATCH_S", 0.01)


@pytest.fixture
async def make_env(tmp_path: Any) -> Any:
    envs: list[Env] = []

    async def factory(**kw: Any) -> Env:
        env = await build_env(tmp_path, **kw)
        envs.append(env)
        return env

    yield factory
    for env in envs:
        await close_env(env)


@pytest.fixture
async def env(make_env: Any) -> Env:
    return await make_env()
