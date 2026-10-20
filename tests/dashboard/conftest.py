"""Fixtures for dashboard tests: a fully faked context, the ASGI app and logged-in clients."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from littlebot.dashboard import create_app
from littlebot.testing import make_test_context

from dashboard_fakes import FAKE_ANTHROPIC_KEY, FAKE_OPENAI_KEY, do_setup, install_fakes, make_client


@pytest.fixture
def dctx():
    # Host names the in-process test clients use ("https://robot.local" in the Secure-cookie test).
    ctx = make_test_context(dashboard={"allowed_hosts": ["testserver", "robot.local"]})
    ctx.config.anthropic_api_key = SecretStr(FAKE_ANTHROPIC_KEY)
    ctx.config.openai_api_key = SecretStr(FAKE_OPENAI_KEY)
    ctx.extras["fakes"] = install_fakes(ctx)
    yield ctx
    ctx.db.close()


@pytest.fixture
def fakes(dctx) -> dict[str, Any]:
    return dctx.extras["fakes"]


@pytest.fixture
def app(dctx):
    return create_app(dctx)


@pytest.fixture
async def client(app):
    async with make_client(app) as c:
        yield c


@pytest.fixture
async def authed(app):
    """A client that completed first-run setup (logged in, CSRF header set)."""
    async with make_client(app) as c:
        await do_setup(c, app)
        yield c
