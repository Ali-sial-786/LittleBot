"""The display loop hooks: an info card is drawn instead of the face (a game wins), gets taps and keys."""

from __future__ import annotations

import threading

import pytest
from screens_helpers import CARD_RGB, DummyCard

from littlebot.core.runtime import AsyncRuntime
from littlebot.face import display as D
from littlebot.face.controller import FaceController
from littlebot.games.service import GamesService
from littlebot.screens.base import PRESENTER_BOX, H, W
from littlebot.screens.service import ScreenService
from littlebot.testing import make_test_context

pygame = pytest.importorskip("pygame")


class StubRobot:
    def __init__(self, ctx, runtime=None):
        self.ctx = ctx
        self.runtime = runtime
        self.shutdown_event = threading.Event()

    def status(self):
        return {"name": "LittleBot", "state": self.ctx.state.snapshot()}

    async def stop_all(self, source="parent"):
        return {}

    def request_shutdown(self, reason="requested"):
        self.shutdown_event.set()


class Vision:
    def __init__(self):
        self.calls = []

    def simulate_face(self, x, y, size=0.25):
        self.calls.append(("simulate_face", x, y))

    def simulate_no_face(self):
        self.calls.append(("simulate_no_face",))

    def status(self):
        return {}


@pytest.fixture
def env():
    rt = AsyncRuntime("screens-display-test")
    rt.start()
    ctx = make_test_context(fakes=False, display={"headless": False, "show_sim_panel": True, "scale": 1.0})
    ctx.runtime = rt
    ctx.face = FaceController(ctx, seed=2)
    ctx.vision = Vision()
    ctx.games = GamesService(ctx, seed=1)
    ctx.screens = ScreenService(ctx)
    loop = D.DisplayLoop(StubRobot(ctx, rt), max_frames=3)
    loop.open()
    yield loop, ctx, rt
    loop.close()
    ctx.screens.detach()
    ctx.games.close()
    ctx.face.stop_ticker()
    rt.stop()
    ctx.db.close()


def test_card_replaces_the_face(env):
    loop, ctx, _rt = env
    assert loop._screens() is None
    ctx.screens.show_card(DummyCard({"title": "x"}), source="test")
    assert loop.target_fps() == ctx.config.display.fps  # popping in: full frame rate
    before = len(ctx.face._render_times)
    assert loop.run() == "max_frames"
    assert len(ctx.face._render_times) == before  # the face was not drawn
    r = loop.face_rect
    got = tuple(loop.window.get_at((r.right - 8, r.bottom - 8)))[:3]
    assert all(abs(a - b) <= 4 for a, b in zip(got, CARD_RGB))  # (scaled into the face rect)
    assert ctx.screens.status()["drawn"] is True


def test_calm_card_runs_at_the_idle_rate(env):
    loop, ctx, _rt = env
    ctx.screens.show_card(DummyCard({"title": "x"}))
    ctx.screens._session.t = 5.0  # long popped in, nobody talking
    assert loop.target_fps() >= ctx.config.display.idle_fps


def test_taps_and_keys_go_to_the_card(env):
    loop, ctx, _rt = env
    ctx.screens.show_card(DummyCard({"title": "x"}))
    card = ctx.screens.current
    r = loop.face_rect
    loop.handle_click((r.centerx, r.centery), 1, 0)
    assert len(card.taps) == 1 and ctx.vision.calls == []  # not the simulated camera
    assert card.taps[0] == pytest.approx((W / 2, H / 2), abs=3)
    assert loop.handle_key(pygame.K_RIGHT, 0) is True and card.keys == ["right"]
    x, y, w, h = PRESENTER_BOX
    loop.handle_click((r.x + int((x + w / 2) * r.w / W), r.y + int((y + h / 2) * r.h / H)), 1, 0)
    assert not ctx.screens.active  # a tap on the presenter's eyes closed it
    loop.handle_click((r.centerx, r.centery), 1, 0)
    assert ctx.vision.calls  # the face (simulated camera) gets clicks again


def test_escape_closes_the_card_not_the_window(env):
    loop, ctx, _rt = env
    ctx.screens.show_card(DummyCard({"title": "x"}))
    assert loop.handle_key(pygame.K_ESCAPE, 0) is True
    assert not ctx.screens.active and not loop.robot.shutdown_event.is_set()


def test_a_game_wins_over_a_card(env):
    loop, ctx, rt = env
    ctx.screens.show_card(DummyCard({"title": "x"}))
    assert rt.run(ctx.games.start("colours", source="test"))
    assert not ctx.screens.active  # the game took the screen
    assert ctx.screens.status()["last"]["reason"].startswith(("preempted", "game"))
    assert loop.run() == "max_frames"
    assert ctx.games.status()["drawn"] is True
