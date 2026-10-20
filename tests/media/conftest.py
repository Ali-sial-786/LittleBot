"""Fixtures for the media (YouTube playback) tests (helpers live in ``media_testkit``)."""

from __future__ import annotations

from typing import Any, Callable

import pytest
from media_testkit import FakeBackend, MediaEnv, _build  # type: ignore[import-not-found]


@pytest.fixture
def make_env() -> Any:
    envs: list[MediaEnv] = []

    def factory(**kw: Any) -> MediaEnv:
        env = _build(**kw)
        envs.append(env)
        return env

    yield factory
    for env in envs:
        env.ctx.db.close()


@pytest.fixture
async def env(make_env: Callable[..., MediaEnv]) -> Any:
    e = make_env()
    await e.media.start()
    yield e
    await e.media.close()


@pytest.fixture
def fake_backend_cls() -> type[FakeBackend]:
    return FakeBackend
