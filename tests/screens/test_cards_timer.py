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
    assert c.command("new", {"seconds": 900, "label": "rice"}) and c.title == "4 timers"
    assert not c.command("new", {"seconds": 60, "label": "one too many"})  # at most 4
    preview.surface(c, seconds=0.1)  # two by two
    assert c.command("cancel", {"label": "rice"}) and c.title == "3 timers"
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


def test_each_timer_says_when_it_will_be_done():
    clock = Clock()
    wall = [1_800_000_000.0]  # 2027-01-15 08:00 UTC
    host = base.RecordingHost(clock=clock)
    utc = timer._dt.UTC
    c = TimerCard({"seconds": 600, "label": "pasta"}, host, wall=lambda: wall[0], tz=utc)
    assert c.end_label(c.timers[0]) == "8:10 am"
    c.twelve = False
    assert c.end_label(c.timers[0]) == "08:10"
    clock.t += 120
    wall[0] += 120
    assert c.command("add", {"seconds": 300}) and c.end_label(c.timers[0]) == "08:15"  # five more minutes
    assert c.command("pause", {})
    clock.t += 600
    wall[0] += 600
    assert c.command("resume", {}) and c.end_label(c.timers[0]) == "08:25"  # ten minutes later
    assert c.command("new", {"seconds": 3600, "label": "bread"}) and c.end_label(c.timers[1]) == "09:12"
    for state in ("running", "paused", "ringing"):  # every state draws, with one timer and with two
        c.timers[0].state = state
        preview.surface(c, seconds=0.1)
        if state == "running":
            c.command("cancel", {"label": "bread"})
            preview.surface(c, seconds=0.1)
            c.command("new", {"seconds": 60, "label": "tea"})
    one = TimerCard({"seconds": 60}, host, wall=lambda: wall[0], tz=utc)
    one.timers[0].state = "ringing"
    preview.surface(one, seconds=0.1)


def test_layouts_stay_on_the_screen():
    for n, (r, _, centres) in timer.LAYOUT.items():
        assert len(centres) == n
        for cx, cy in centres:
            assert r <= cx <= base.W - r and timer.TOP <= cy - r and cy + r <= timer.BOTTOM
        xs = sorted(centres)
        for (ax, ay), (bx, by) in zip(xs, xs[1:]):  # no two rings overlap
            assert (ax - bx) ** 2 + (ay - by) ** 2 >= (2 * r) ** 2
    assert timer.SIDE_X - timer.SIDE_W // 2 > timer.LAYOUT[1][2][0][0] + timer.LAYOUT[1][0]


def test_helpers():
    assert clock_text(0) == "0:00" and clock_text(0.2) == "0:01" and clock_text(65) == "1:05"
    assert clock_text(3725) == "1:02:05"
    assert clean_label("Egg Timer") == "egg" and clean_label("  BBQ ") == "BBQ" and clean_label(None) == ""
    assert timer.spoken_length(5400) == "1 hour 30 minutes" and timer.spoken_length(1) == "1 second"
    assert timer.length_label(600) == "10-minute timer" and timer.length_label(7200) == "2-hour timer"
    assert timer.length_label(45) == "45-second timer" and timer.length_label(5400) == "90-minute timer"
    assert timer.length_label(3725) == "1 hour 2 minutes 5 seconds timer"
