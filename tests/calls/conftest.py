"""Fixtures for the WhatsApp calls tests (helpers live in ``calls_testkit``)."""

from __future__ import annotations

from typing import Any

import pytest
from calls_testkit import Env, build_env, close_env  # type: ignore[import-not-found]


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
