"""The timer card (littlebot/screens/cards/timer.py), driven by a fake clock."""

from __future__ import annotations

from littlebot.screens import base, preview
from littlebot.screens.cards import timer
from littlebot.screens.cards.timer import (
    CHIME_EVERY,
    CHIMES,
    DONE_SECONDS,
    TimerCard,
    clean_label,
    clock_text,
)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(**spec) -> tuple[TimerCard, Clock, base.RecordingHost]:
    clock = Clock()
    host = base.RecordingHost(clock=clock)
    return TimerCard({"card": "timer", **spec}, host), clock, host


def run(card: TimerCard, clock: Clock, seconds: float, step: float = 0.5) -> None:
    t = 0.0
    while t < seconds - 1e-9:
        clock.t += step
        card.update(step)
        t += step


def test_registered_with_fields_actions_and_samples():
    ct = base.card_types()["timer"]
    assert set(ct.fields) == {"seconds", "label"} and ct.required == ("seconds",)
    assert {"add", "pause", "resume", "cancel", "new"} <= set(ct.actions)
    assert all(s["card"] == "timer" for s in timer.SAMPLES)


async def test_samples_build_and_draw():
    for spec in timer.SAMPLES:
        c = await base.build(spec, None, base.RecordingHost())
        assert isinstance(c, TimerCard) and c.sticky
        assert preview.surface(c, seconds=0.3).get_size() == (base.W, base.H)


async def test_bad_specs_make_no_card():
    host = base.RecordingHost()
    for spec in ({"card": "timer"}, {"card": "timer", "seconds": 0}, {"card": "timer", "seconds": "soon"},
                 {"card": "timer", "seconds": 10 ** 9}):
        assert await base.build(spec, None, host) is None
    c = await base.build({"card": "timer", "seconds": 30, "minutes": 2}, None, host)
    assert c.timers[0].total == 150


def test_counts_down_and_rings():
    c, clock, host = make(seconds=600, label="Egg Timer")
    assert c.title == "Egg timer" and c.describe() == "Egg timer: 10:00 left" and c.sticky
    run(c, clock, 223)
    assert c.describe() == "Egg timer: 6:17 left" and host.sounds == []
    run(c, clock, 377)
    assert c.timers[0].state == "ringing" and c.describe() == "Egg timer: done!"
    assert host.sounds == ["chime"] and host.spoken == ["The egg timer is done!"]
    assert host.told and "egg timer" in host.told[0] and "10 minutes" in host.told[0]
    assert c.mood == "happy" and c.sticky and not c.done
    run(c, clock, CHIME_EVERY * CHIMES + 1)
    assert host.sounds == ["chime"] * CHIMES  # a few gentle chimes, then quiet
    assert host.spoken == ["The egg timer is done!"]
    preview.surface(c, seconds=0.1)
    run(c, clock, DONE_SECONDS)
    assert c.done and not c.timers and not c.sticky


def test_pause_resume_add_and_cancel():
    c, clock, host = make(seconds=60, label="tea")
    run(c, clock, 10)
    assert c.command("pause", {})
    run(c, clock, 100)
    assert c.describe() == "Tea timer: paused with 0:50 left" and not host.sounds
    assert c.command("add", {"seconds": 60}) and c.describe() == "Tea timer: paused with 1:50 left"
    assert c.command("resume", {"label": "tea"})
    run(c, clock, 30)
    assert c.describe() == "Tea timer: 1:20 left"
    assert c.command("add", {"seconds": -30}) and c.describe() == "Tea timer: 0:50 left"
    assert not c.command("pause", {"label": "pizza"}) and not c.command("dance", {})
    assert c.command("cancel", {}) and c.done and not c.timers


def test_more_time_on_a_finished_timer_starts_it_again():
    c, clock, host = make(seconds=5)
    run(c, clock, 6)
    assert c.timers[0].state == "ringing" and host.spoken == ["The timer is done!"]
    assert c.command("add", {"seconds": 300}) and c.describe() == "Timer: 5:00 left"


def test_several_timers_on_one_card():
    c, clock, host = make(seconds=600, label="pasta")
    assert c.command("new", {"seconds": 120, "label": "garlic bread"}) and c.title == "2 timers"
    assert c.command("new", {"seconds": 300, "label": "sauce"})
    assert not c.command("new", {"seconds": 60, "label": "one too many"})  # at most 3
    run(c, clock, 121)
    assert c.describe() == "Pasta timer: 7:59 left; Garlic bread timer: done!; Sauce timer: 2:59 left"
    assert host.spoken == ["The garlic bread timer is done!"]
    preview.surface(c, seconds=0.1)
    assert c.command("pause", {"label": "sauce"}) and c.timers[2].state == "paused"
    assert c.timers[0].state == "running"
    cx, cy, _ = c._layout()[1]
    assert c.tap(cx, cy) and len(c.timers) == 2  # a tap stops the finished one
    assert not c.tap(c._layout()[0][0], c._layout()[0][1])  # a running timer ignores taps
    assert c.command("cancel", {"label": "pasta"}) and c.title == "Sauce timer"


async def test_a_new_timer_joins_the_card_on_screen(ctx):
    clock = Clock()
    host = base.RecordingHost(clock=clock)
    first = await base.build({"card": "timer", "seconds": 600, "label": "pasta"}, ctx, host)
    second = await base.build({"card": "timer", "seconds": 60, "label": "egg"}, ctx, host)
    assert second is not first  # the first was never on screen (never updated)
    first.update(0.03)
    third = await base.build({"card": "timer", "seconds": 120, "label": "tea"}, ctx, host)
    assert third is first and [t.label for t in first.timers] == ["pasta", "tea"]
    clock.t += 10  # off screen for a while: a new card
    assert await base.build({"card": "timer", "seconds": 60}, ctx, host) is not first
    other = await base.build({"card": "timer", "seconds": 60}, None, host)
    assert other is not first


def test_helpers():
    assert clock_text(0) == "0:00" and clock_text(0.2) == "0:01" and clock_text(65) == "1:05"
    assert clock_text(3725) == "1:02:05"
    assert clean_label("Egg Timer") == "egg" and clean_label("  BBQ ") == "BBQ" and clean_label(None) == ""
    assert timer.spoken_length(5400) == "1 hour 30 minutes" and timer.spoken_length(1) == "1 second"
