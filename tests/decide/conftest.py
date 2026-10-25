"""The decider's tests: ``use_model(answer)`` puts a stand-in model in (provider jev, cloud on)."""

from __future__ import annotations

from typing import Any, Callable

import pytest

from littlebot.testing import FakeDecisionModel, use_decision_model


@pytest.fixture
def use_model(ctx: Any) -> Callable[..., FakeDecisionModel]:
    def _use(answer: Callable[..., Any], *, modes: dict[str, str] | None = None) -> FakeDecisionModel:
        return use_decision_model(ctx, answer, modes=modes)

    return _use
