"""Every game headless: update / draw / tap logic, sounds asked for, win / lose rules."""

from __future__ import annotations

import itertools
import random

import pytest

from games_helpers import distinct_colors, run
from littlebot.games import art
from littlebot.games.base import FINALE_SECONDS, RecordingHost
from littlebot.games.catalog import GAMES

pygame = pytest.importorskip("pygame")


def make(game_id, host, seed=3, **kw):
    info = next(g for g in GAMES if g.id == game_id)
    game = info.load()(host, seed=seed, **kw)
    game.begin()
    return game


# ------------------------------------------------------------------ every game
@pytest.mark.parametrize("info", GAMES, ids=[g.id for g in GAMES])
def test_every_game_plays_draws_and_survives_random_taps(info, host, clock, surface):
    game = make(info.id, host)
    rng = random.Random(1)
    for i in range(240):  # 8 s at 30 fps with a tap storm and some keys
        clock.advance(1 / 30)
        game.update(1 / 30)
        if i % 3 == 0:
            game.tap(rng.uniform(0, game.w), rng.uniform(0, game.h))
        if i % 40 == 0:
            game.key(rng.choice(["1", "2", "3", "space", "left", "q"]))
        if i % 20 == 0:
            game.draw(surface)
    game.draw(surface)
    assert distinct_colors(surface) > 6  # a real picture, not a blank screen
    st = game.status()
    assert {"score", "taps", "summary", "finale"} <= set(st)
    assert st["taps"] == 80 and isinstance(st["summary"], str)
    assert host.sounds, "every game makes sounds"


@pytest.mark.parametrize("info", GAMES, ids=[g.id for g in GAMES])
def test_every_game_has_a_finale_then_finishes(info, host, surface):
    game = make(info.id, host)
    run(game, 1.0)
    game.wrap_up()
    assert game.in_finale and host.sounds[-1] == "tada"
    taps = game.taps
    assert game.tap(10, 10) is False and game.taps == taps  # no input during the finale
    run(game, FINALE_SECONDS / 2)
    game.draw(surface)
    assert not game.finished
    run(game, FINALE_SECONDS / 2 + 0.1)
    assert game.finished
    game.wrap_up()  # idempotent


def test_games_draw_at_any_size(host):
    from littlebot.games.memory import MemoryLights

    game = MemoryLights(host, seed=1)
    game.begin()
    run(game, 0.5)
    game.draw(pygame.Surface(game.size))  # the service scales the canvas, games always draw 720x480
    assert game.size == (art.W, art.H)


# ------------------------------------------------------------------ Bubble Pop
def test_bubbles_pop_on_tap_and_new_ones_on_empty_taps(host):
    from littlebot.games.bubbles import BubblePop

    game = BubblePop(host, seed=2)
    game.begin()
    run(game, 0.5)
    b = game.bubbles[0]
    assert game.tap(b.x + 5, b.y - 5) is True
    assert b not in game.bubbles and game.popped == 1 and game.score == 1
    assert host.sounds[-1] == "pop" and len(game.particles) > 0
    n = len(game.bubbles)
    game.bubbles.clear()
    game.tap(360, 240)  # empty space: blow a new bubble (cause and effect for every tap)
    assert len(game.bubbles) == 1 and host.sounds[-1] == "blip"
    game.tap(362, 242)  # pops the new one (tap slop)
    assert game.popped == 2
    assert n >= 0


def test_bubbles_pop_by_themselves_quietly(host):
    from littlebot.games.bubbles import BubblePop

    game = BubblePop(host, seed=4)
    game.begin()
    run(game, 25)
    assert game.self_popped > 5 and game.popped == 0
    assert all(b.y > 40 for b in game.bubbles)
    assert len(game.bubbles) <= 8


def test_golden_bubble_appears_and_sparkles(host):
    from littlebot.games.bubbles import BubblePop

    game = BubblePop(host, seed=5)
    game.begin()
    game.golden_in = 0.01
    run(game, 0.5)
    gold = next(b for b in game.bubbles if b.golden)
    game.tap(gold.x, gold.y)
    assert host.sounds[-1] == "sparkle" and game.eyes.mood == "happy"


# ------------------------------------------------------------------ Peekaboo Friends
def test_peekaboo_animals_peek_by_themselves(host):
    from littlebot.games.peekaboo import PHRASES, PeekabooFriends

    game = PeekabooFriends(host, seed=1)
    game.begin()
    run(game, 12)
    assert game.peeks >= 2
    assert "peekaboo" in host.sounds
    assert host.spoken and all(s in PHRASES.values() for s in host.spoken)


def test_peekaboo_tap_block_reveals_and_tap_animal_giggles(host):
    from littlebot.games.peekaboo import BLOCK_TOP, PHRASES, PeekabooFriends

    game = PeekabooFriends(host, seed=2)
    game.begin()
    game.next_peek = 99  # no automatic peeks in this test
    sp = game.spots[0]
    game.tap(sp.x, BLOCK_TOP + 60)
    assert sp.state == "rising" and game.peeks == 1 and game.score == 1
    assert host.spoken[-1] == PHRASES[sp.animal]
    run(game, 0.6)
    assert sp.state == "up"
    game.tap(sp.x, BLOCK_TOP - 40)  # the animal's head
    assert game.giggles == 1 and host.sounds[-1] == "giggle" and sp.jump > 0
    run(game, 4.5)
    assert sp.state == "hidden"
    assert game.tap(10, 10) is True  # above everything: a little sparkle, never nothing


def test_peekaboo_does_not_repeat_the_same_animal_back_to_back(host):
    from littlebot.games.peekaboo import PeekabooFriends

    game = PeekabooFriends(host, seed=3)
    picks = [game.pick_animal() for _ in range(30)]
    assert all(a != b for a, b in zip(picks, picks[1:]))


def test_peekaboo_animals_all_draw(surface):
    from littlebot.games.peekaboo import ANIMALS, animal_sprite

    for name in ANIMALS:
        spr = animal_sprite(name)
        assert spr.get_width() > 100 and spr.get_at((spr.get_width() // 2, spr.get_height() // 2))[3] == 255


# ------------------------------------------------------------------ Colour Burst
def test_colour_burst_paints_and_plays_notes_low_to_high(host):
    from littlebot.games.colour_burst import NOTES, ColourBurst

    game = ColourBurst(host, seed=1)
    game.begin()
    game.tap(10, 240)
    game.tap(game.w - 10, 240)
    assert game.painted == 2 and len(game.marks) == 2
    assert host.melodies[0][0][0] == NOTES[0] and host.melodies[1][0][0] == NOTES[-1]
    assert {m.kind for m in game.marks} == {"splat", "shape"}


def test_colour_burst_fades_and_never_overflows(host):
    from littlebot.games.colour_burst import LIFE, MAX_MARKS, ColourBurst

    game = ColourBurst(host, seed=2)
    game.begin()
    for i in range(40):
        game.tap(40 + i * 15, 100 + (i % 5) * 60)
        run(game, 0.05)
    live = [m for m in game.marks if m.dying < 0]
    assert len(live) <= MAX_MARKS
    run(game, 1.0)
    assert len(game.marks) <= MAX_MARKS
    run(game, LIFE + 1)
    assert all(m.auto for m in game.marks)  # the painting faded; only LittleBot's own invitations remain


def test_colour_burst_invites_when_idle(host):
    from littlebot.games.colour_burst import ColourBurst

    game = ColourBurst(host, seed=3)
    game.begin()
    run(game, 9)
    assert any(m.auto for m in game.marks) and game.painted == 0


# ------------------------------------------------------------------ Follow the Star
def test_star_moves_and_eyes_follow(host):
    from littlebot.games.follow_star import FollowTheStar

    game = FollowTheStar(host, seed=1)
    game.begin()
    x0, y0 = game.star.x, game.star.y
    run(game, 2)
    assert (game.star.x, game.star.y) != (x0, y0)
    gx = game.eyes.target_gaze[0]
    assert (gx > 0) == (game.star.x > game.eyes.x)  # looks towards the star


def test_star_catch_and_send(host):
    from littlebot.games.follow_star import FollowTheStar

    game = FollowTheStar(host, seed=2)
    game.begin()
    run(game, 0.6)
    st = game.star
    game.tap(st.x + 30, st.y + 30)
    assert game.caught == 1 and host.sounds[-1] == "sparkle" and st.gone > 0
    run(game, 1.2)
    assert st.gone <= 0 and host.sounds[-1] == "whoosh"  # a new star flew in
    game.tap(600, 400) if abs(st.x - 600) > 120 else game.tap(100, 80)
    assert (st.tx, st.ty) in ((600, 400), (100, 80)) and game.caught == 1


def test_star_looks_at_the_child_when_the_camera_sees_one(host):
    from littlebot.games.follow_star import FollowTheStar

    host.gaze = (0.9, 0.5)  # the child is on LittleBot's right
    game = FollowTheStar(host, seed=3)
    game.begin()
    game.child_look_in = 0.05
    run(game, 0.3)
    assert game.child_looks == 1
    assert game.eyes.target_gaze[0] > 0.5
    assert game.status()["sees_child"] is True
    host.gaze = None
    assert game.child_point() is None


# ------------------------------------------------------------------ Memory Lights
def play_back(game, pads):
    for p in pads:
        r = game.press(p)
    return r


def test_memory_shows_sequence_then_waits_for_input(host):
    from littlebot.games.memory import PAD_NOTES, MemoryLights

    game = MemoryLights(host, seed=1)
    game.begin()
    assert game.phase == "intro" and game.round == 1
    assert game.press(0) == "ignored"  # not your turn yet
    run(game, 2.2)
    assert game.phase == "input"
    assert [m[0][0] for m in host.melodies] == [PAD_NOTES[game.sequence[0]]]


def test_memory_correct_rounds_grow_and_mistake_restarts(host):
    from littlebot.games.memory import MemoryLights

    game = MemoryLights(host, seed=2)
    game.begin()
    for expected_round in (1, 2, 3, 4):
        while game.phase != "input":
            run(game, 0.1)
        assert game.round == expected_round
        assert play_back(game, game.sequence) == "round"
        assert game.best == expected_round
    while game.phase != "input":
        run(game, 0.1)
    wrong = (game.sequence[0] + 1) % 4
    assert game.press(wrong) == "fail"
    assert game.phase == "fail" and host.sounds[-1] == "uhoh" and game.fail_pad == game.sequence[0]
    run(game, 2.5)
    assert game.round == 1 and game.best == 4 and game.games == 2  # new game, best kept
    assert game.summary() == "Best: round 4"


def test_memory_partial_sequence_and_replay_hint(host):
    from littlebot.games.memory import REPLAY_AFTER, MemoryLights

    game = MemoryLights(host, seed=3)
    game.begin()
    game.sequence = [1, 2, 3]
    game.set_phase("input")
    assert game.press(1) == "ok" and game.press(2) == "ok" and game.input_i == 2
    run(game, REPLAY_AFTER + 0.1)
    assert game.phase == "show" and game.round == 3  # shown again, no penalty


def test_memory_pad_hit_testing(host):
    from littlebot.games.memory import MemoryLights

    game = MemoryLights(host)
    for i in range(4):
        assert game.pad_at(*game.pad_center(i)) == i
    assert game.pad_at(game.eyes.x, game.eyes.y) is None  # LittleBot's hub in the middle


# ------------------------------------------------------------------ Tic-Tac-Toe
def b(cells: str) -> list[str]:
    """Board from a string, "." = empty: b("XO.......")."""
    return ["" if c == "." else c for c in cells]


def test_tictactoe_rules():
    from littlebot.games.tictactoe import best_moves, immediate_win, winner, winning_line

    assert winner(b("XXX......")) == "X"
    assert winner(b("O...O...O")) == "O"
    assert winner(b("XOXXOOOXX")) == "draw"
    assert winner(b("XO.......")) is None
    assert winning_line(b("..X.X.X..")) == (2, 4, 6)
    assert immediate_win(b("OO......."), "O") == 2
    assert best_moves(b("OO.XX...."), "O") == [2]  # win beats blocking
    assert best_moves(b("XX.O....."), "O") == [2]  # must block


def test_perfect_littlebot_never_loses(host):
    from littlebot.games.tictactoe import BOT, HUMAN, TicTacToe

    rng = random.Random(9)
    results = []
    for game_no in range(40):
        game = TicTacToe(host, seed=game_no, skill=1.0, adaptive=False)
        game.starter = BOT if game_no % 2 else HUMAN
        game.board = [""] * 9
        game.turn = game.starter
        game.set_phase("thinking" if game.turn == BOT else "play")
        while game.phase != "over":
            if game.turn == HUMAN:
                empty = [i for i in range(9) if not game.board[i]]
                game.place(rng.choice(empty), HUMAN)
            else:
                game.place(game.choose_move(), BOT)
        results.append(game.result)
    assert HUMAN not in results and BOT in results


def test_tictactoe_tap_play_and_bot_reply(host):
    from littlebot.games.tictactoe import BOT, HUMAN, TicTacToe

    game = TicTacToe(host, seed=1)
    game.begin()
    assert game.turn == HUMAN and game.phase == "play"
    assert game.tap(*game.cell_center(4)) is True
    assert game.board[4] == HUMAN and game.phase == "thinking"
    assert game.tap(*game.cell_center(0)) is False  # LittleBot's turn
    run(game, 1.2)
    assert game.board.count(BOT) == 1 and game.phase == "play"
    assert game.tap(*game.cell_center(4)) is False  # taken


def test_tictactoe_win_adapts_skill_and_next_round_alternates(host):
    from littlebot.games.tictactoe import BOT, HUMAN, TicTacToe

    game = TicTacToe(host, seed=2, skill=0.5)
    game.begin()
    game.board = [HUMAN, HUMAN, "", BOT, BOT, "", "", "", ""]
    game.turn = HUMAN
    game.set_phase("play")
    game.tap(*game.cell_center(2))
    assert game.result == HUMAN and game.wins == 1 and game.line == (0, 1, 2)
    assert host.sounds[-1] == "tada" and game.skill == pytest.approx(0.65)
    run(game, 2.8)
    assert game.board == [""] * 9 and game.starter == BOT and game.phase in ("thinking", "play")
    run(game, 1.2)
    assert game.board.count(BOT) == 1  # LittleBot opened this round


def test_tictactoe_loss_makes_littlebot_gentler(host):
    from littlebot.games.tictactoe import BOT, HUMAN, SKILL_MIN, TicTacToe

    game = TicTacToe(host, seed=3, skill=0.3)
    game.board = [BOT, BOT, "", HUMAN, HUMAN, "", HUMAN, "", ""]
    game.turn = BOT
    game.set_phase("thinking")
    run(game, 1.2)
    assert game.result == BOT and game.losses == 1 and game.skill == SKILL_MIN  # it always takes a win


# ------------------------------------------------------------------ Reaction Race
def test_reaction_measures_milliseconds(host, clock, surface):
    from littlebot.games.reaction import ReactionRace, rating

    game = ReactionRace(host, seed=1)
    game.begin()
    assert game.press() == "start" and game.phase == "wait"
    run(game, game.delay + 0.05, clock=clock)
    assert game.phase == "go"
    game.draw(surface)  # green reaches the screen now
    clock.advance(0.287)
    assert game.press() == "result"
    assert game.last_ms == 287 and game.times == [287] and game.best_ms == 287
    assert rating(287) == "Super quick!" and rating(200) == "Lightning fast!" and rating(900) == "Good try!"


def test_reaction_too_soon_does_not_count(host, clock):
    from littlebot.games.reaction import ReactionRace

    game = ReactionRace(host, seed=2)
    game.begin()
    game.press()
    assert game.press() == "too_soon"
    assert game.times == [] and game.too_soon == 1 and host.sounds[-1] == "uhoh"
    run(game, 1.8, clock=clock)
    assert game.phase == "wait"  # tries again by itself


def test_reaction_too_slow_and_full_round(host, clock):
    from littlebot.games.reaction import TOO_SLOW, TRIES, ReactionRace

    game = ReactionRace(host, seed=3)
    game.begin()
    game.press()
    run(game, game.delay + TOO_SLOW + 0.1, clock=clock)
    assert game.phase == "too_slow" and game.times == []
    for i in range(TRIES):
        while game.phase != "go":
            run(game, 0.05, clock=clock)
        clock.advance(0.2 + i * 0.05)
        assert game.press() == "result"
        run(game, 2.0, clock=clock)
    assert game.phase == "summary" and len(game.times) == TRIES and host.sounds[-1] == "tada"
    assert game.best_ms == min(game.times) and game.average_ms == round(sum(game.times) / TRIES)
    run(game, 1.0, clock=clock)
    assert game.press() == "new_round" and game.times == []


# ------------------------------------------------------------------ Rock Paper Scissors
def test_rps_outcomes():
    from littlebot.games.rps import CHOICES, outcome

    table = {(a, b): outcome(a, b) for a, b in itertools.product(CHOICES, CHOICES)}
    assert table[("rock", "scissors")] == "win" and table[("scissors", "paper")] == "win"
    assert table[("paper", "rock")] == "win" and table[("rock", "paper")] == "lose"
    assert all(table[(c, c)] == "draw" for c in CHOICES)
    assert sum(1 for v in table.values() if v == "win") == 3


def test_rps_round_by_tap(host):
    from littlebot.games.rps import BEAT_S, CHOICES, RockPaperScissors, outcome

    game = RockPaperScissors(host, seed=4)
    game.begin()
    r = game.button_rect(1)
    assert game.tap(*r.center) is True and game.you == "paper" and game.phase == "count"
    assert game.tap(*game.button_rect(0).center) is False  # one pick per round
    run(game, BEAT_S * 3 + 0.05)
    assert game.phase == "reveal" and game.bot in CHOICES
    assert game.result == outcome("paper", game.bot)
    assert game.wins + game.losses + game.draws == 1
    assert host.sounds.count("blip") == 3
    run(game, 2.5)
    assert game.phase == "pick"


def test_rps_hears_choices_and_keys(host):
    from littlebot.games.rps import RockPaperScissors

    game = RockPaperScissors(host, seed=5)
    game.begin()
    assert game.hear("hmm let me think") is False
    assert game.hear("I choose STONE") is True and game.you == "rock"
    assert game.hear("paper") is False  # already picked this round
    game.set_phase("pick")
    assert game.key("3") is True and game.you == "scissors"


def test_rps_littlebot_picks_all_three(host):
    from littlebot.games.rps import BEAT_S, RockPaperScissors

    game = RockPaperScissors(host, seed=6)
    seen = set()
    for _ in range(30):
        game.set_phase("pick")
        game.choose("rock")
        run(game, BEAT_S * 3 + 0.05)
        seen.add(game.bot)
    assert seen == {"rock", "paper", "scissors"}


# ------------------------------------------------------------------ shared art
def test_eye_state_blinks_and_moods():
    eyes = art.EyeState(100, 100, 1.0)
    rng = random.Random(1)
    eyes.feel("happy", 0.5)
    assert eyes.mood == "happy"
    blinked = False
    for _ in range(300):
        eyes.update(1 / 30, rng)
        blinked = blinked or eyes.openness() < 0.5
    assert blinked and eyes.mood == "neutral"
    s = pygame.Surface((200, 200))
    for mood in ("neutral", "happy", "surprised", "sad"):
        art.draw_eyes(s, (100, 100), 1.0, mood=mood, gaze=(1, 1))


def test_recording_host_defaults():
    h = RecordingHost()
    h.sound("pop")
    h.melody([("C4", 1)])
    h.say("hi")
    assert h.sounds == ["pop"] and h.melodies == [[("C4", 1)]] and h.spoken == ["hi"]
    assert h.child_gaze() is None and h.now() > 0


def test_faded_blit_keeps_the_sprite_blending():
    """Regression: restoring a cached sprite with set_alpha(None) switched off its per-pixel
    blending, so its transparent corners later drew as black squares."""
    spr = art.circle(6, art.LEMON)
    dst = pygame.Surface((40, 40))
    dst.fill((10, 200, 10))
    art.blit_center(dst, spr, (20, 20), alpha=100)
    art.blit_center(dst, spr, (20, 20))
    assert dst.get_at((14, 14))[:3] == (10, 200, 10)  # the sprite's transparent corner
