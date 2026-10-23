"""The display loop hook: a running game is drawn instead of the face and gets taps and keys."""

from __future__ import annotations

import pytest

from games_helpers import StubRobot
from littlebot.core.runtime import AsyncRuntime
from littlebot.face import display as D
from littlebot.face.controller import FaceController
from littlebot.games.service import GamesService
from littlebot.testing import make_test_context

pygame = pytest.importorskip("pygame")


class Vision:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def simulate_face(self, x, y, size=0.25):
        self.calls.append(("simulate_face", x, y))

    def simulate_no_face(self):
        self.calls.append(("simulate_no_face",))

    def status(self):
        return {}


@pytest.fixture
def env():
    rt = AsyncRuntime("games-display-test")
    rt.start()
    ctx = make_test_context(fakes=False, display={"headless": False, "show_sim_panel": True, "scale": 1.0})
    ctx.runtime = rt
    ctx.face = FaceController(ctx, seed=2)
    ctx.vision = Vision()
    ctx.games = GamesService(ctx, seed=1)
    robot = StubRobot(ctx, rt)
    loop = D.DisplayLoop(robot, max_frames=3)
    loop.open()
    yield loop, ctx, rt
    loop.close()
    ctx.games.close()
    ctx.face.stop_ticker()
    rt.stop()
    ctx.db.close()


def test_face_draws_when_no_game_runs(env):
    loop, ctx, _rt = env
    assert loop.run() == "max_frames"
    assert len(ctx.face._render_times) == 3
    assert loop._games() is None


def test_running_game_replaces_the_face_and_is_scaled_into_it(env):
    loop, ctx, rt = env
    assert rt.run(ctx.games.start("colours", source="test"))
    assert loop.target_fps() == ctx.config.display.fps  # games always get the full frame rate
    rendered_before = len(ctx.face._render_times)
    assert loop.run() == "max_frames"
    assert len(ctx.face._render_times) == rendered_before  # the face was not drawn
    st = ctx.games.status()
    assert st["drawn"] is True
    r = loop.face_rect
    inside = loop.window.get_at((r.x + 5, r.y + 5))[:3]
    assert inside != (0, 0, 0)  # the game's light canvas fills the face area


def test_clicks_go_to_the_game_not_the_simulated_camera(env):
    loop, ctx, rt = env
    rt.run(ctx.games.start("colours"))
    r = loop.face_rect
    loop.handle_click((r.centerx, r.centery), 1, 0)
    game = ctx.games._session.game
    assert game.painted == 1 and ctx.vision.calls == []
    loop.handle_click((r.centerx, r.centery), 3, 0)  # right click keeps its simulator meaning
    assert ctx.vision.calls == [("simulate_no_face",)]


def test_escape_stops_the_game_instead_of_quitting(env):
    loop, ctx, rt = env
    rt.run(ctx.games.start("memory"))
    assert loop.handle_key(pygame.K_1, 0) is True  # a game key
    assert loop.handle_key(pygame.K_ESCAPE, 0) is True
    assert not ctx.games.active
    assert loop.run() == "max_frames"
    assert len(ctx.face._render_times) >= 3  # the face is back
    assert loop.handle_key(pygame.K_ESCAPE, 0) is False  # Esc quits again without a game


def test_stop_key_still_stops_everything_during_a_game(env):
    loop, ctx, rt = env
    rt.run(ctx.games.start("bubbles"))
    assert loop.handle_key(pygame.K_x, 0) is True
    rt.run(_settle())
    assert loop.robot.stop_all_calls == ["keyboard"]


async def _settle():
    import asyncio

    await asyncio.sleep(0.05)


def test_tap_reaction_is_suppressed_while_a_game_runs(env):
    loop, ctx, rt = env
    loop.sim_mode = False
    rt.run(ctx.games.start("bubbles"))
    r = loop.face_rect
    loop.handle_click((r.centerx, r.centery), 1, 0)
    assert ctx.face.status()["expression"] == "neutral"  # no giggle under the game
    assert ctx.games._session.taps == 1
