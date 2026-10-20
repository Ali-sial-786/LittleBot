"""Display loop / simulator window (SDL dummy driver): lifecycle, fallbacks, input handling."""

from __future__ import annotations

import threading
import time

import pytest
from face_stubs import StubActivities, StubBrain, StubRobot, StubVision

from littlebot.core.events import Topics, collect
from littlebot.core.runtime import AsyncRuntime
from littlebot.face import display as D
from littlebot.face.controller import FaceController
from littlebot.testing import FakeAudio, FakeFace, FakeMotion, make_test_context

pygame = pytest.importorskip("pygame")


@pytest.fixture
def runtime():
    rt = AsyncRuntime("face-test")
    rt.start()
    yield rt
    rt.stop()


def _robot(runtime=None, *, sim_panel=True, scale=1.0, **extra):
    ctx = make_test_context(fakes=False, display={"headless": False, "show_sim_panel": sim_panel, "scale": scale,
                                                  **extra})
    ctx.face = FaceController(ctx, seed=2)
    return StubRobot(ctx, runtime)


def _close(robot):
    robot.ctx.face.stop_ticker()
    robot.ctx.db.close()


def _wait_until(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# ------------------------------------------------------------------ lifecycle
def test_run_display_exits_when_shutdown_event_set():
    robot = _robot()
    timer = threading.Timer(0.4, robot.shutdown_event.set)
    timer.start()
    t0 = time.monotonic()
    try:
        D.run_display(robot)
    finally:
        timer.cancel()
        _close(robot)
    assert time.monotonic() - t0 < 5
    assert robot.ctx.face.display_attached is False  # handed back to the headless ticker
    assert robot.ctx.face.animator.t > 0  # the loop drove the animation
    assert not pygame.display.get_init()


def test_run_display_demo_mode_runs():
    robot = _robot()
    threading.Timer(0.6, robot.shutdown_event.set).start()
    try:
        D.run_display(robot, demo=True)
    finally:
        _close(robot)
    # the demo drove the face at least to its first steps
    assert robot.ctx.face.animator.t > 0.3


def test_run_display_falls_back_when_display_cannot_open(monkeypatch, caplog):
    robot = _robot()

    def boom(*a, **k):
        raise pygame.error("No available video device")

    monkeypatch.setattr(pygame.display, "set_mode", boom)
    threading.Timer(0.3, robot.shutdown_event.set).start()
    try:
        with caplog.at_level("ERROR", logger="littlebot.face.display"):
            D.run_display(robot)
    finally:
        _close(robot)
    assert "could not open the face display" in caplog.text
    assert robot.ctx.face.display_attached is False


def test_run_display_without_renderable_face_waits(caplog):
    ctx = make_test_context()  # FakeFace has no render()
    robot = StubRobot(ctx)
    threading.Timer(0.2, robot.shutdown_event.set).start()
    with caplog.at_level("ERROR", logger="littlebot.face.display"):
        D.run_display(robot)
    ctx.db.close()
    assert "cannot render" in caplog.text


def test_window_close_requests_shutdown():
    robot = _robot()
    loop = D.DisplayLoop(robot, max_frames=50)
    loop.open()
    try:
        pygame.event.post(pygame.event.Event(pygame.QUIT))
        assert loop.run() == "quit"
    finally:
        loop.close()
        _close(robot)
    assert robot.shutdown_reasons == ["window closed"]


def test_repeated_frame_errors_fall_back(monkeypatch):
    robot = _robot()
    loop = D.DisplayLoop(robot, max_frames=100)
    loop.open()
    monkeypatch.setattr(robot.ctx.face, "render", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad")))
    try:
        assert loop.run() == "error"
    finally:
        loop.close()
        _close(robot)


def test_window_layout_with_and_without_panel():
    robot = _robot(sim_panel=True, scale=1.5)
    loop = D.DisplayLoop(robot, max_frames=3)
    loop.open()
    try:
        w, h = loop.window.get_size()
        assert w == 720 + D.PANEL_WIDTH and h == max(480, D.PANEL_MIN_HEIGHT)
        assert loop.face_rect.size == (720, 480)
        assert loop.run() == "max_frames"
        assert loop.window.get_at((loop.face_rect.right + 20, 200))[:3] != (0, 0, 0)  # panel drawn
    finally:
        loop.close()
        _close(robot)
    robot = _robot(sim_panel=False, scale=1.0)
    loop = D.DisplayLoop(robot, max_frames=2)
    loop.open()
    try:
        assert loop.window.get_size() == (480, 320) and loop.panel_rect is None
        loop.run()
    finally:
        loop.close()
        _close(robot)


def test_fullscreen_toggle_and_screenshot(tmp_path):
    robot = _robot()
    loop = D.DisplayLoop(robot, max_frames=2)
    loop.open()
    try:
        loop.toggle_fullscreen()
        assert loop.fullscreen and not loop.show_panel
        loop.run()
        loop.toggle_fullscreen()
        assert not loop.fullscreen and loop.show_panel
        loop.max_frames = loop.frames + 2
        loop.run()
        path = loop.save_screenshot(tmp_path)
        assert path is not None and path.exists() and path.stat().st_size > 1000
    finally:
        loop.close()
        _close(robot)


# ------------------------------------------------------------------ keyboard
@pytest.fixture
def loop_env(runtime):
    robot = _robot(runtime)
    ctx = robot.ctx
    ctx.audio = None
    ctx.vision = StubVision()
    ctx.activities = StubActivities()
    ctx.brain = StubBrain()
    ctx.motion = FakeMotion()
    loop = D.DisplayLoop(robot, max_frames=2)
    loop.open()
    yield loop, robot
    loop.close()
    _close(robot)


def test_expression_animation_and_state_keys(loop_env):
    loop, robot = loop_env
    face = robot.ctx.face
    for i, name in enumerate(D.DIGIT_EXPRESSIONS):
        key = pygame.K_0 if i == 9 else pygame.K_1 + i
        assert loop.handle_key(key, 0)
        assert face.status()["expression"] == name
    for key, name in ((pygame.K_j, "playful"), (pygame.K_k, "speaking"), (pygame.K_l, "affectionate"),
                      (pygame.K_v, "celebrating")):
        loop.handle_key(key, 0)
        assert face.status()["expression"] == name
    loop.handle_key(pygame.K_1, pygame.KMOD_SHIFT)
    assert "bounce" in face.status()["animations"]
    loop.handle_key(pygame.K_0, pygame.KMOD_LSHIFT)
    assert "peek" in face.status()["animations"]
    for key, state in ((pygame.K_F2, "listening"), (pygame.K_F5, "sleeping"), (pygame.K_F7, "hidden"),
                       (pygame.K_F1, "idle")):
        loop.handle_key(key, 0)
        assert face.status()["state"] == state


def test_misc_keys(loop_env):
    loop, robot = loop_env
    face = robot.ctx.face
    seen = collect(robot.ctx.bus, ["face.blink"])
    loop.handle_key(pygame.K_b, 0)
    loop.handle_key(pygame.K_n, 0)
    face.update(0.01)
    assert [e.data["double"] for e in seen] in ([False], [True], [False, True])
    loop.handle_key(pygame.K_LEFT, 0)
    loop.handle_key(pygame.K_LEFT, 0)
    for _ in range(40):
        face.update(1 / 30)
    assert face.status()["gaze"]["x"] < 0.1
    loop.handle_key(pygame.K_r, 0)
    assert loop._gaze == [0.5, 0.5]  # noqa: SLF001
    loop.handle_key(pygame.K_o, 0)
    assert face.status()["eyes_closed"] is True
    loop.handle_key(pygame.K_o, 0)
    assert face.status()["eyes_closed"] is False
    loop.handle_key(pygame.K_g, 0)
    assert loop.show_debug
    loop.handle_key(pygame.K_h, 0)
    assert loop.show_help
    assert loop.run() == "max_frames"  # overlays draw without errors
    assert loop.handle_key(pygame.K_ESCAPE, 0) is True and not loop.show_help  # Esc closes help first
    loop.handle_key(pygame.K_m, 0)
    assert loop.demo is not None
    loop.handle_key(pygame.K_m, 0)
    assert loop.demo is None


def test_robot_action_keys(loop_env):
    loop, robot = loop_env
    ctx = robot.ctx
    loop.handle_key(pygame.K_p, 0)
    loop.handle_key(pygame.K_c, 0)
    loop.handle_key(pygame.K_x, 0)
    loop.handle_key(pygame.K_SPACE, 0)
    loop.handle_key(pygame.K_w, 0)
    loop.handle_key(pygame.K_e, 0)
    assert _wait_until(lambda: "start" in ctx.activities.names() and robot.stop_all_calls
                       and "converse_once" in ctx.brain.names())
    assert ("start", ("peekaboo",), {"source": "keyboard"}) in ctx.activities.calls
    assert ("respond", ("done",), {}) in ctx.activities.calls
    assert robot.stop_all_calls == ["keyboard"]
    assert ctx.motion.estop is True
    assert _wait_until(lambda: ctx.motion.called("run_gesture"))


def test_refused_activity_shows_confused(loop_env):
    loop, robot = loop_env
    robot.ctx.activities = StubActivities(refuse=True)
    loop.handle_key(pygame.K_p, 0)
    assert _wait_until(lambda: robot.ctx.face.status()["expression"] == "confused")


def test_keys_are_safe_without_subsystems():
    robot = _robot(None)
    loop = D.DisplayLoop(robot, max_frames=2)
    loop.open()
    try:
        for key in (pygame.K_p, pygame.K_c, pygame.K_x, pygame.K_SPACE, pygame.K_w, pygame.K_e, pygame.K_t):
            assert loop.handle_key(key, 0)
        loop.handle_click((100, 100), 1, 0)
        loop.handle_click((100, 100), 3, 0)
        assert loop.run() == "max_frames"
    finally:
        loop.close()
        _close(robot)


def test_test_speech_without_audio_publishes_events(loop_env):
    loop, robot = loop_env
    seen = collect(robot.ctx.bus, ["audio.*"])
    loop.handle_key(pygame.K_t, 0)
    assert seen[0].topic == Topics.AUDIO_SPEECH_STARTED
    assert robot.ctx.face.status()["talking"] is True
    loop._run_timers(time.monotonic() + 10)  # noqa: SLF001
    assert seen[-1].topic == Topics.AUDIO_SPEECH_FINISHED


def test_quit_keys(loop_env):
    loop, robot = loop_env
    assert loop.handle_key(pygame.K_q, 0) is False
    assert robot.shutdown_reasons == ["keyboard quit"]


# ------------------------------------------------------------------ mouse / touch
def test_click_simulates_camera_face_in_sim_mode(loop_env):
    loop, robot = loop_env
    r = loop.face_rect
    loop.handle_click((r.x + r.w // 4, r.y + r.h // 2), 1, 0)
    name, args, _ = robot.ctx.vision.calls[-1]
    assert name == "simulate_face"
    assert args[0] == pytest.approx(0.25, abs=0.01) and args[1] == pytest.approx(0.5, abs=0.01)
    loop.handle_click((r.x + 5, r.y + 5), 3, 0)
    assert robot.ctx.vision.calls[-1][0] == "simulate_no_face"
    n = len(robot.ctx.vision.calls)
    loop.handle_click((r.right + 50, 10), 1, 0)  # panel area (not the e-stop) -> ignored
    assert len(robot.ctx.vision.calls) == n


def test_panel_estop_click(loop_env):
    loop, robot = loop_env
    assert loop.run() == "max_frames"  # draws the panel, sets estop_rect
    loop.handle_click(loop.panel.estop_rect.center, 1, 0)
    assert robot.ctx.motion.estop is True


def test_tap_reaction_on_robot_screen(runtime):
    robot = _robot(runtime, sim_panel=False)
    robot.ctx.audio = FakeAudio(robot.ctx.bus)
    loop = D.DisplayLoop(robot, max_frames=2)
    loop.open()
    try:
        r = loop.face_rect
        loop.handle_click((r.centerx, r.centery), 1, 0)
        st = robot.ctx.face.status()
        assert st["expression"] == "happy" and "bounce" in st["animations"]
        assert _wait_until(lambda: robot.ctx.audio.sounds == [D.TAP_REACTION_SOUND])
        loop.handle_click((r.centerx, r.centery), 1, 0)  # cooldown: no second giggle
        time.sleep(0.1)
        assert robot.ctx.audio.sounds == [D.TAP_REACTION_SOUND]
    finally:
        loop.close()
        _close(robot)


def test_tap_reaction_can_be_disabled(monkeypatch):
    robot = _robot(None, sim_panel=False)
    loop = D.DisplayLoop(robot, max_frames=2)
    loop.open()
    monkeypatch.setattr(D, "TAP_REACTION_ENABLED", False)
    try:
        assert loop.tap_reaction(0.5, 0.5) is False
        assert robot.ctx.face.status()["expression"] == "neutral"
    finally:
        loop.close()
        _close(robot)


def test_fps_drops_when_calm(loop_env):
    loop, robot = loop_env
    cfg = robot.ctx.config.display
    face = robot.ctx.face
    face.animator.cfg = face.animator.cfg.model_copy(update={"idle_gaze": False})
    face.animator.next_blink = 1e9
    for _ in range(60):
        face.update(1 / 30)
    assert loop.target_fps() == cfg.idle_fps
    face.play_animation("wiggle")
    face.update(0.01)
    assert loop.target_fps() == cfg.fps


def test_fake_face_has_no_render():
    assert not hasattr(FakeFace(), "render")
