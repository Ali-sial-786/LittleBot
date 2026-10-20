"""Fixtures for brain tests (helpers live in brain_helpers.py)."""

from __future__ import annotations

from typing import Any

import pytest
from brain_helpers import FakeAPI

from littlebot.brain.conversation import ConversationEngine


@pytest.fixture
async def make_brain():
    """Factory: make_brain(ctx, api=FakeAPI(), start=True) -> started ConversationEngine."""
    engines: list[ConversationEngine] = []

    async def _make(ctx, api: FakeAPI | None = None, *, start: bool = True, **kw: Any) -> ConversationEngine:
        engine = ConversationEngine(ctx, http_transport=api.transport if api is not None else None, **kw)
        if start:
            await engine.start()
        engines.append(engine)
        return engine

    yield _make
    for e in engines:
        await e.stop()
        e.ctx.db.close()
