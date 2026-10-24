"""The browser view and videos never share the screen (one Chromium at a time): a video starting closes the
browser before its player opens, and the browser opening stops a playing video. A real MediaController with
the media tests' fake player backend, a real BrowserView with the fake Chromium."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "media"))
from media_testkit import (  # type: ignore[import-not-found]  # noqa: E402
    FakeBackend,
    _build,
)

from littlebot.core.arbiter import Resource  # noqa: E402
from littlebot.core.state import RobotState  # noqa: E402


class OrderedBackend(FakeBackend):
    """Notes, when the player opens, whether the browser's Chromium had exited and its profile was gone."""

    def __init__(self, env: Any) -> None:
        super().__init__()
        self.env = env
        self.browser_gone_at_open: list[bool] = []

    async def open(self, url: str) -> None:
        env = self.env
        gone = not env.browser.active and all(p.returncode is not None for p in env.launcher.procs) \
            and env.profiles() == []
        self.browser_gone_at_open.append(gone)
        await super().open(url)


async def _media(env: Any) -> Any:
    backend = OrderedBackend(env)
    menv = _build(ctx=env.ctx, backend=backend)
    env.ctx.settings.set("profile", {"age_months": 24})
    env.ctx.media = menv.media
    await menv.media.start()
    return menv


async def test_a_video_closes_the_browser_before_its_player_opens(env: Any) -> None:
    menv = await _media(env)
    try:
        await env.open("giraffes")
        assert env.browser.active
        item = menv.add()
        await menv.media.play(item["id"])
        assert menv.backend.browser_gone_at_open == [True]
        assert not env.browser.active and env.last("browser.closed") is not None
        assert env.ctx.arbiter.holder(Resource.SCREEN).owner == "media"
        assert env.ctx.state.state == RobotState.PLAYING_MEDIA
    finally:
        await menv.media.close()


async def test_the_browser_stops_a_playing_video(env: Any) -> None:
    menv = await _media(env)
    try:
        item = menv.add()
        await menv.media.play(item["id"])
        assert menv.backend.running
        out = await env.open("giraffes")
        assert out["ok"], out
        assert menv.backend.closed >= 1 or not menv.backend.running
        assert menv.media.status()["state"] == "idle"
        assert env.ctx.arbiter.holder(Resource.SCREEN).owner == "browser"
    finally:
        await menv.media.close()
