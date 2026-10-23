"""Rock Paper Scissors played by showing a hand to the camera (littlebot.games.hand_sign)."""

from __future__ import annotations

import asyncio
import types

import numpy as np
import pygame
import pytest

from games_helpers import run
from littlebot.games import hand_sign
from littlebot.games.rps import BEAT_S, CHOICES, LOOK_GIVE_UP_S, RockPaperScissors


# ------------------------------------------------------------------ reading a hand
def _hand(ratios: tuple[float, float, float, float]) -> np.ndarray:
    """21 joints with the wrist at 0 and each finger's tip ``ratio`` times as far as its middle joint."""
    w = np.zeros((21, 3))
    for (mid, tip), ratio, x in zip(hand_sign._FINGERS, ratios, (-0.02, -0.007, 0.007, 0.02)):
        w[mid] = (x, 0.06, 0.0)
        w[tip] = (x * ratio, 0.06 * ratio, 0.0)
    return w


@pytest.mark.parametrize("ratios,sign", [
    ((0.95, 0.9, 0.9, 0.85), "rock"),
    ((1.15, 0.9, 0.9, 0.85), "rock"),  # index a bit out of the fist: still a fist
    ((1.3, 1.35, 1.4, 1.3), "paper"),
    ((1.3, 1.35, 1.4, 1.16), "paper"),  # little finger a bit bent
    ((1.38, 1.4, 0.9, 0.85), "scissors"),
    ((1.38, 1.4, 0.9, 1.16), "scissors"),
    ((1.38, 0.9, 0.9, 0.85), None),  # pointing
    ((1.38, 1.4, 1.4, 0.85), None),  # three fingers
    ((1.16, 1.16, 1.16, 1.16), None),  # all half bent
])
def test_reads_the_sign_from_the_fingers(ratios, sign):
    assert hand_sign.sign_from_fingers(hand_sign.fingers_straight(_hand(ratios))) == sign


class FakeCounter:
    def __init__(self, ratios=None) -> None:
        self.ratios = ratios

    def palms(self, img):
        return [] if self.ratios is None else [np.array([0, 0, 10, 10, 0.9]), np.array([0, 0, 50, 50, 0.9])]

    def landmarks(self, img, palm):
        assert palm[2] == 50  # the biggest hand
        return None, _hand(self.ratios), 1.0


def test_read_sign_uses_the_biggest_hand_and_says_why_not():
    img = np.zeros((48, 64, 3), np.uint8)
    assert hand_sign.read_sign(FakeCounter((1.3, 1.35, 1.4, 1.3)), img) == ("paper", "")
    assert hand_sign.read_sign(FakeCounter(None), img) == (None, "no_hand")
    assert hand_sign.read_sign(FakeCounter((1.38, 0.9, 0.9, 0.85)), img) == (None, "unclear_sign")


def _ctx(camera_on=True, vision=True, photos=None):
    async def capture_photo(**kw):
        if photos is not None:
            photos.append(kw)
        return b"jpeg"

    return types.SimpleNamespace(privacy=types.SimpleNamespace(camera_enabled=camera_on),
                                 vision=types.SimpleNamespace(capture_photo=capture_photo) if vision else None)


@pytest.fixture
def fake_reader(monkeypatch):
    """The camera "sees" these signs, one per frame (None = unsure)."""
    seen: list = []
    monkeypatch.setattr(hand_sign, "load_finger_counter", lambda: object())
    monkeypatch.setattr(hand_sign, "_cv2", lambda: object())
    monkeypatch.setattr(hand_sign, "_decode", lambda cv2, jpeg: np.zeros((4, 4, 3), np.uint8))
    def read(counter, img):
        sign = seen.pop(0) if seen else None
        return (sign, "") if sign else (None, "no_hand")

    monkeypatch.setattr(hand_sign, "read_sign", read)
    return seen


async def test_a_sign_counts_once_two_frames_in_a_row_agree(fake_reader):
    fake_reader += ["rock", "paper", "paper", "scissors"]  # a fist being pumped, then paper held
    photos: list = []
    look = hand_sign.HandLook()
    await hand_sign.look_for_sign(_ctx(photos=photos), look, settle=0.0, limit=2.0)
    assert look.done and look.sign == "paper" and look.reads == ["rock", "paper", "paper"]
    assert len(photos) == 3 and photos[0]["max_width"] == 640


async def test_no_steady_sign_means_ask(fake_reader):
    fake_reader += ["rock", None, "paper"] + [None] * 50
    look = hand_sign.HandLook()
    await hand_sign.look_for_sign(_ctx(), look, settle=0.0, limit=0.05)
    assert look.done and look.sign is None and look.why


@pytest.mark.parametrize("ctx,why", [(_ctx(camera_on=False), "camera_off"), (_ctx(vision=False), "no_camera")])
async def test_never_looks_when_it_may_not(fake_reader, ctx, why):
    look = hand_sign.HandLook()
    await hand_sign.look_for_sign(ctx, look, settle=0.0)
    assert look.done and look.sign is None and look.why == why and look.reads == []


async def test_without_the_hand_models_it_doesnt_look(monkeypatch):
    monkeypatch.setattr(hand_sign, "load_finger_counter", lambda: None)
    assert hand_sign.blocked_reason(_ctx()) == "no_models"


def test_an_unplugged_camera_means_tapping(fake_reader):
    ctx = _ctx()
    ctx.vision.status = lambda: {"camera": "disconnected"}
    assert hand_sign.blocked_reason(ctx) == "no_camera"
    ctx.vision.status = lambda: {"camera": "ok"}
    assert hand_sign.blocked_reason(ctx) is None


async def test_a_broken_camera_ends_the_look(fake_reader):
    from littlebot.vision.service import PhotoUnavailable

    async def broken(**kw):
        raise PhotoUnavailable("timeout", "no frame")

    ctx = _ctx()
    ctx.vision.capture_photo = broken
    look = hand_sign.HandLook()
    await hand_sign.look_for_sign(ctx, look, settle=0.0)
    assert look.done and look.sign is None and look.why == "timeout"


# ------------------------------------------------------------------ the game
@pytest.fixture
def camera_host(host):
    host.can_see = True
    return host


def _game(host, seed=3):
    game = RockPaperScissors(host, seed=seed)
    game.begin()
    return game


def test_a_camera_round_reads_the_hand_on_shoot(camera_host):
    camera_host.hand_signs = ["scissors"]
    game = _game(camera_host)
    assert game.camera and game.phase == "pick"
    assert game.tap(50, 50) is True and game.phase == "count" and game.you is None  # anywhere starts it
    run(game, BEAT_S * 3 - 0.1)
    assert camera_host.looks == []  # not before "Shoot!"
    run(game, 0.2)
    assert len(camera_host.looks) == 1
    run(game, 0.1)
    assert game.phase == "reveal" and game.you == "scissors" and game.seen and game.bot in CHOICES
    assert game.wins + game.losses + game.draws == 1
    run(game, 2.5)
    assert game.phase == "pick"


def test_when_the_camera_isnt_sure_it_asks(camera_host):
    camera_host.hand_signs = [None]
    game = _game(camera_host)
    assert game.hear("go!") is True
    run(game, BEAT_S * 3 + 0.2)
    assert game.phase == "ask" and game.shows_buttons()
    assert game.tap(*game.button_rect(0).center) is True
    assert game.phase == "reveal" and game.you == "rock" and not game.seen


def test_a_look_that_never_answers_asks_too(camera_host, monkeypatch):
    game = _game(camera_host)
    monkeypatch.setattr(camera_host, "see_hand", lambda: hand_sign.HandLook())  # never done
    game.go()
    run(game, BEAT_S * 3 + LOOK_GIVE_UP_S + 0.1)
    assert game.phase == "ask"


def test_saying_a_choice_still_plays_without_the_camera_look(camera_host):
    game = _game(camera_host)
    assert game.hear("paper") is True and game.you == "paper"
    run(game, BEAT_S * 3 + 0.05)
    assert game.phase == "reveal" and camera_host.looks == []


def test_camera_switched_off_falls_back_to_tapping(camera_host):
    game = _game(camera_host)
    camera_host.can_see = False
    assert game.go() is False and not game.camera and game.phase == "pick"
    assert game.tap(*game.button_rect(2).center) is True and game.you == "scissors"


def test_every_camera_phase_draws(camera_host):
    camera_host.hand_signs = ["paper", None]
    game = _game(camera_host)
    surf = pygame.Surface(game.size)
    for _ in range(2):
        game.draw(surf)  # pick: the Go button
        game.go()
        for _ in range(int((BEAT_S * 3 + 0.3) * 30)):
            game.update(1 / 30)
            game.draw(surf)
        game.update(1 / 30)
        game.draw(surf)
        if game.phase == "ask":
            game.choose("rock")
            game.draw(surf)
        run(game, 2.5)
    assert game.wins + game.losses + game.draws == 2
    assert game.status()["mode"] == "camera"


async def test_the_real_host_runs_the_look_on_the_loop(monkeypatch):
    from littlebot.games.service import GamesService
    from littlebot.testing import make_test_context

    ctx = make_test_context()
    try:
        svc = GamesService(ctx)
        seen = []

        async def fake_look(ctx_, look):
            seen.append(ctx_)
            look.finish("rock")

        monkeypatch.setattr(hand_sign, "look_for_sign", fake_look)
        from littlebot.games.service import _Host

        look = _Host(svc).see_hand()
        await asyncio.sleep(0.01)
        assert look.done and look.sign == "rock" and seen == [ctx]
    finally:
        ctx.db.close()
