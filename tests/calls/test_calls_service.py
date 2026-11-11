"""CallsService (littlebot/calls/service.py) against a fake Chromium launcher, a fake DevTools peer and a fake
WhatsApp page: what it starts, what it grants, placing / answering / ending a call, the cover staying on when a step
fails, the camera lent and given back, and every reason it shuts down."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from calls_testkit import (  # type: ignore[import-not-found]
    OTHER_FRONT,
    PAGE_SESSION,
    PAGE_TARGET,
    POINTS,
    ROW,
    WHATSAPP_BEHIND,
)

from littlebot.calls import page_scripts as scripts
from littlebot.calls import service as service_mod
from littlebot.calls.service import CLOSED, LINK_SAY, OPENED, PRIORITY, PROBLEMS, STATE, USED
from littlebot.core.arbiter import Priority, Resource
from littlebot.core.events import Topics
from littlebot.core.state import RobotState


def _navigations(env: Any) -> list[str]:
    return [p["url"] for p in env.devtools.params("Page.navigate")]


async def _ready(env: Any, **kw: Any) -> dict[str, Any]:
    out = await env.open(**kw)
    assert out["ok"], out
    await env.until(lambda: env.calls.status()["state"] == "ready")
    return out


# ---------------------------------------------------------------- opening
async def test_open_shows_whatsapp_web_in_a_saved_profile(env: Any) -> None:
    out = await env.open()
    assert out["ok"] is True and out["already"] is False and out["contact"] is None
    assert _navigations(env) == ["https://web.whatsapp.com/"]
    argv = env.launcher.argv
    assert argv[0] == "/usr/bin/chromium" and "--kiosk" in argv
    assert any(a.startswith("--app=data:text/html") for a in argv)  # LittleBot's own page until everything is set
    assert "--window-size=1024,600" in argv
    assert "--remote-debugging-address=127.0.0.1" in argv and "--remote-debugging-port=0" in argv
    assert f"--user-data-dir={env.profile}" in argv and env.profile.is_dir()
    assert "--incognito" not in argv  # the QR link has to survive
    assert "--use-fake-ui-for-media-stream" not in argv and "--mute-audio" not in argv
    assert "--deny-permission-prompts" in argv
    assert env.last(OPENED).data == {"source": "voice"}
    assert env.calls.active and not env.calls.in_call


async def test_only_whatsapp_gets_the_camera_and_the_microphone(env: Any) -> None:
    await env.open()
    dt = env.devtools
    assert dt.params("Browser.grantPermissions") == [
        {"origin": "https://web.whatsapp.com", "permissions": ["audioCapture", "videoCapture"]}]
    assert dt.params("Browser.setPermission") == []  # nothing is granted to anyone else
    assert dt.last("Browser.setDownloadBehavior") == {"behavior": "deny"}
    assert dt.last("Page.setInterceptFileChooserDialog") == {"enabled": True}
    assert dt.last("Fetch.enable") == {"patterns": service_mod.FETCH_PATTERNS}
    page_calls = dt.methods(PAGE_SESSION)
    for before in ("Fetch.enable", "Page.addScriptToEvaluateOnNewDocument"):
        assert page_calls.index(before) < page_calls.index("Page.navigate")
    assert dt.methods().index("Browser.grantPermissions") < dt.methods().index("Page.navigate")


async def test_notifications_only_when_the_chats_are_not_hidden(make_env: Any) -> None:
    env = await make_env()
    await env.open()
    assert "--disable-notifications" in env.launcher.argv  # hidden chats: no pop-ups with message previews
    await env.calls.close("test")
    env.ctx.settings.set("calls.hide_chats", False)
    await env.open()
    assert "--disable-notifications" not in env.launcher.argv
    assert env.devtools.last("Browser.grantPermissions")["permissions"] == ["audioCapture", "videoCapture",
                                                                           "notifications"]


async def test_camera_privacy_off_means_no_camera_for_whatsapp(env: Any) -> None:
    env.ctx.privacy.set(camera_enabled=False)
    assert (await env.open(contact="Dad"))["error"] == "camera_off"  # a video call needs it
    out = await env.open(contact="Dad", kind="voice")
    assert out["ok"], out
    assert env.devtools.last("Browser.grantPermissions")["permissions"] == ["audioCapture"]
    assert env.vision.log == []  # the camera was not borrowed, and Privacy was not touched
    assert env.ctx.privacy.camera_enabled is False


async def test_the_cover_is_injected_before_the_page(env: Any) -> None:
    await env.open()
    dt = env.devtools
    assert dt.last("Runtime.addBinding") == {"name": scripts.BINDING, "executionContextName": scripts.WORLD}
    injected = dt.params("Page.addScriptToEvaluateOnNewDocument")
    assert len(injected) == 1 and injected[0]["worldName"] == scripts.WORLD
    assert "littlebot-cover" in injected[0]["source"] and '"on":true' in injected[0]["source"]
    assert "Opening WhatsApp" in injected[0]["source"]


async def test_it_takes_the_screen_like_a_video_then_holds_it_lower(env: Any) -> None:
    card = env.ctx.arbiter.acquire("screens", [Resource.SCREEN], Priority.CONVERSATION)
    await env.open()
    holder = env.ctx.arbiter.holder(Resource.SCREEN)
    assert holder.owner == "calls" and holder.priority == PRIORITY == 45
    assert not card.active


async def test_a_more_important_screen_holder_means_busy(env: Any) -> None:
    env.ctx.arbiter.acquire("parent", [Resource.SCREEN], Priority.PARENT)
    out = await env.open()
    assert out["ok"] is False and out["error"] == "busy"
    assert env.launcher.argvs == [] and env.vision.log == []


async def test_it_wakes_littlebot_up(env: Any) -> None:
    env.ctx.state.transition(RobotState.SLEEPING, "test")
    assert (await env.open())["ok"]
    assert env.ctx.state.state == RobotState.IDLE and env.calls.active


# ---------------------------------------------------------------- the gates
async def test_gates(make_env: Any) -> None:
    env = await make_env()
    env.memory[0] = 300
    out = await env.open()
    assert out["error"] == "low_memory" and "700 MB" in out["detail"] and env.launcher.argvs == []
    env.memory[0] = 2000
    env.ctx.settings.set("calls.enabled", False)
    assert (await env.open())["error"] == "disabled" and not env.calls.offered()
    env.ctx.settings.set("calls.enabled", True)
    env.ctx.privacy.set(cloud_enabled=False)
    assert (await env.open())["error"] == "cloud_off"
    env.ctx.privacy.set(cloud_enabled=True, microphone_enabled=False)
    assert (await env.open())["error"] == "microphone_off"
    env.ctx.privacy.set(microphone_enabled=True)
    env.ctx.state.transition(RobotState.ERROR, "test")
    assert (await env.open())["error"] == "not_ready"
    assert env.launcher.argvs == []


async def test_a_headless_test_config_never_launches_anything(tmp_path: Any) -> None:
    from littlebot.calls import create_service
    from littlebot.testing import make_test_context

    ctx = make_test_context()
    ctx.privacy.set(cloud_enabled=True)
    svc = create_service(ctx)
    await svc.start()
    assert svc.unavailable_reason() == "no screen (headless run)" and not svc.offered()
    assert (await svc.open(source="dashboard"))["error"] == "unavailable"
    assert svc._profile_dir == ctx.extras["whatsapp_profile_dir"]  # never the real data folder
    await svc.stop()
    ctx.db.close()


async def test_only_a_grown_up(env: Any) -> None:
    env.ctx.bus.publish("voice.speaker", {"is_child": True})
    out = await env.open(contact="Dad")
    assert out["ok"] is False and out["error"] == "child" and env.launcher.argvs == []
    assert (await env.open(source="dashboard"))["ok"]  # the dashboard is a parent's
    await env.calls.close("test")
    env.clock.advance(service_mod.CHILD_RECENT_S + 1)
    assert (await env.open(contact="Dad"))["ok"]


async def test_someone_not_on_the_list(make_env: Any) -> None:
    env = await make_env()
    out = await env.open(contact="Sam")
    assert out["ok"] is False and out["error"] == "unknown_contact" and "You can call Dad or Mum." in out["detail"]
    assert env.launcher.argvs == []
    empty = await make_env(contacts={})
    assert (await empty.open(contact="Dad"))["error"] == "no_contacts"


# ---------------------------------------------------------------- linking
async def test_not_linked_shows_the_qr_and_says_how(env: Any) -> None:
    env.page.linked, env.page.qr = False, True
    await env.open(contact="Dad")
    await env.until(lambda: env.calls.status()["state"] == "linking")
    assert env.words() == [LINK_SAY]
    await env.until(lambda: env.page.cover_states and env.page.cover_states[-1]["title"] == scripts.LINK_TITLE)
    assert env.page.clicks == []  # nothing is pressed until it is linked
    assert env.calls.status()["linked"] is False
    env.page.linked = True  # the phone scanned it
    await env.until(lambda: env.page.call == "in_call")  # ... and the call that was asked for is placed
    assert env.words() == [LINK_SAY]  # said once


async def test_whatsapp_that_never_loads_says_so(env: Any) -> None:
    env.page.linked = False
    await env.open()
    env.clock.advance(env.ctx.config.calls.load_timeout_seconds + 1)
    await env.until(lambda: env.calls.status()["problem"] == PROBLEMS["load"])
    assert env.words()[-1].startswith(PROBLEMS["load"])
    assert env.calls.active


# ---------------------------------------------------------------- placing a call
async def test_video_call_a_contact(env: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="littlebot.calls")
    out = await env.open(contact="my dad")
    assert out["ok"] and out["contact"] == "Dad"
    await env.until(lambda: env.calls.in_call)
    page = env.page
    assert page.typed == ["Ali Khan"]  # the name as it appears in WhatsApp, into its search box
    at = lambda p: (p["x"], p["y"])  # noqa: E731
    assert page.clicks == [at(POINTS["search_box"]), at(ROW), at(POINTS["video_call"])]
    assert page.kinds == ["video_call"]
    assert page.passes == [True, False] * 3  # the cover lets each press through, and only for that instant
    assert env.last(STATE).data == {"state": "ready", "call": "in_call"}
    st = env.calls.status()
    assert st["call"] == "in_call" and st["calling"] == "Dad" and st["closes_in"] is None
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Dad" in text and "Ali" not in text and "Khan" not in text  # the said name only, never WhatsApp's


async def test_voice_call(env: Any) -> None:
    await env.open(contact="Mum", kind="voice")
    env.page.chats.add("Sara")
    await env.until(lambda: env.calls.in_call)
    assert env.page.kinds == ["voice_call"] and env.page.typed[-1] == "Sara"


async def test_a_call_asked_for_while_whatsapp_is_open(env: Any) -> None:
    await _ready(env)
    out = await env.open(contact="Dad")
    assert out["ok"] and out["already"] is True and len(env.launcher.argvs) == 1
    await env.until(lambda: env.calls.in_call)
    assert (await env.open(contact="Mum"))["error"] == "in_call"


async def test_the_voice_chat_ends_when_the_call_connects(env: Any) -> None:
    ended: list[str] = []

    class Voice:
        async def stop_session(self, reason: str) -> None:
            ended.append(reason)
            lease.release()

    env.ctx.assistant = Voice()
    lease = env.ctx.arbiter.acquire("assistant", [Resource.AUDIO_OUT], Priority.PARENT)
    await env.open(contact="Dad")
    await env.until(lambda: bool(ended))
    assert ended == ["a WhatsApp call started"]


@pytest.mark.parametrize("setup,step", [
    (lambda p: p.has.discard("search_box"), "search"),
    (lambda p: p.chats.clear(), "contact"),
    (lambda p: p.has.difference_update({"video_call", "voice_call"}), "call_button"),
    (lambda p: setattr(p, "starts", False), "no_call"),
])
async def test_a_step_that_fails_stops_there_and_stays_covered(env: Any, setup: Any, step: str) -> None:
    setup(env.page)
    env.clock.tick = 0.2  # (the waits run out)
    await env.open(contact="Dad")
    words = PROBLEMS[step].format(name="Dad")
    await env.until(lambda: env.calls.status()["problem"] == words)
    assert env.calls.active and not env.calls.in_call  # WhatsApp stays on the screen
    assert env.words() == [f"{words} {service_mod.COVERED_HINT}"]  # the voice says the same, no "tap this"
    await env.until(lambda: env.page.cover_states and env.page.cover_states[-1]["title"] == words)
    assert all(c["on"] is True for c in env.page.cover_states)  # never uncovered by itself
    assert env.page.injected == 0
    assert env.ctx.settings.get("calls.hide_chats") is True
    if step in ("search", "contact"):
        assert env.page.kinds == []


async def test_a_failed_step_says_what_to_tap_only_when_the_chats_show(env: Any) -> None:
    env.ctx.settings.set("calls.hide_chats", False)
    env.page.has.difference_update({"video_call", "voice_call"})
    env.clock.tick = 0.2
    await env.open(contact="Dad")
    await env.until(lambda: bool(env.words()))
    assert env.words() == ["I couldn't find the call button. Tap the camera at the top of the chat."]


async def test_never_a_call_to_the_wrong_chat(env: Any) -> None:
    """A row was found, but the chat that opened isn't the one asked for: no call button is pressed."""
    real_click = env.page.click

    def click(params: dict[str, Any]) -> dict[str, Any]:
        out = real_click(params)
        if env.page.opened == "Ali Khan":
            env.page.opened = "Somebody Else"
        return out

    env.devtools.answers["Input.dispatchMouseEvent"] = click
    env.clock.tick = 0.2
    await env.open(contact="Dad")
    await env.until(lambda: env.calls.status()["problem"] == "I couldn't open Dad's chat.")
    assert env.page.kinds == [] and env.page.call == "none"


async def test_a_cover_that_went_missing_is_put_back(env: Any) -> None:
    await _ready(env)
    env.page.cover = False
    await env.until(lambda: env.page.injected == 1)
    assert env.page.cover is True


# ---------------------------------------------------------------- answering
async def test_answer_presses_accept_when_a_call_rings(env: Any) -> None:
    out = await env.open(answer=True)
    assert out["ok"]
    await env.until(lambda: env.page.cover_states and env.page.cover_states[-1]["title"] == "Waiting for the call…")
    env.page.call = "incoming"
    await env.until(lambda: env.page.call == "in_call")
    assert env.page.clicks == [(POINTS["accept_call"]["x"], POINTS["accept_call"]["y"])]


async def test_a_ringing_call_is_not_answered_unasked(env: Any) -> None:
    await _ready(env)
    env.page.call = "incoming"
    await env.until(lambda: env.calls.status()["call"] == "incoming")
    await env.settle()
    assert env.page.clicks == [] and env.page.call == "incoming"
    assert env.page.cover_states[-1]["title"] == "Someone is calling"
    out = await env.open(answer=True)  # "Hey LittleBot, answer"
    assert out["already"] is True
    await env.until(lambda: env.page.call == "in_call")


async def test_the_covers_buttons(env: Any) -> None:
    await _ready(env)
    env.page.call = "incoming"
    await env.until(lambda: env.calls.status()["call"] == "incoming")
    await env.devtools.emit("Runtime.bindingCalled", {"name": scripts.BINDING, "payload": '{"type":"answer"}'})
    await env.until(lambda: env.page.call == "in_call")
    env.page.ask = "hangup"  # (the same, picked up from the page when the binding didn't come)
    await env.until(lambda: not env.calls.active)
    assert env.page.call == "none" and env.last(CLOSED).data == {"reason": "hung up on the screen"}


async def test_the_covers_close_button(env: Any) -> None:
    await _ready(env)
    await env.devtools.emit("Runtime.bindingCalled", {"name": scripts.BINDING, "payload": '{"type":"close"}'})
    await env.until(lambda: not env.calls.active)
    assert env.last(CLOSED).data == {"reason": "closed on the screen"}


# ---------------------------------------------------------------- ending
async def test_hang_up_presses_end_then_shuts_everything_down(env: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="littlebot.calls")
    await env.open(contact="Dad")
    await env.until(lambda: env.calls.in_call)
    assert env.vision.lent == {"calls"}
    env.memory[0] = 1400
    proc = env.launcher.proc

    def freed() -> int:  # Chromium's memory comes back once it is gone
        return 1900 if proc.returncode is not None else 1400

    env.calls._memory = freed
    out = await env.calls.hang_up("asked in the voice chat")
    assert out == {"ok": True, "was_call": True, "closed": True}
    assert env.page.clicks[-1] == (POINTS["end_call"]["x"], POINTS["end_call"]["y"])
    assert proc.terminated == 1 and proc.returncode is not None  # the whole process group was signalled
    assert env.vision.log == [("lend", "calls"), ("return", "calls")] and not env.vision.lent
    assert not env.calls.active and env.ctx.arbiter.holder(Resource.SCREEN) is None
    last = env.calls.status()["last"]
    assert last["reason"] == "asked in the voice chat" and last["had_call"] is True
    assert last["free_before_mb"] == 1400 and last["free_mb"] == 1900 and last["freed_mb"] == 500
    infos = [r.getMessage() for r in caplog.records if r.name == "littlebot.calls"]
    assert any("WhatsApp is off (asked in the voice chat)" in m and "1400 MB of memory free before, 1900 MB after "
               "(+500 MB)" in m for m in infos)


async def test_hang_up_with_nothing_open(env: Any) -> None:
    assert await env.calls.hang_up() == {"ok": True, "was_call": False, "closed": False}


async def test_it_closes_when_the_call_is_over(env: Any) -> None:
    await env.open(contact="Dad")
    await env.until(lambda: env.calls.in_call)
    env.page.call = "none"  # the other side hung up
    await env.until(lambda: env.calls.status()["call"] == "none")
    assert env.calls.active  # (a moment's grace: WhatsApp redraws)
    env.clock.advance(service_mod.CALL_END_GRACE_S + 0.1)
    await env.until(lambda: not env.calls.active)
    await env.settle()
    assert env.last(CLOSED).data == {"reason": "the call ended"}
    assert not env.vision.lent and env.launcher.proc.returncode is not None


async def test_idle_closes_it_but_never_during_a_call(env: Any) -> None:
    await _ready(env)
    idle = env.ctx.config.calls.idle_seconds
    assert idle == 120
    env.clock.advance(idle - 5)
    await env.settle()
    assert env.calls.active
    await env.devtools.emit("Runtime.bindingCalled", {"name": scripts.BINDING, "payload": '{"type":"touch"}'})
    env.clock.advance(idle - 5)  # a touch started the wait again
    await env.settle()
    assert env.calls.active
    env.page.opened, env.page.call = "Ali Khan", "in_call"
    await env.until(lambda: env.calls.in_call)
    env.clock.advance(idle * 10)
    await env.settle()
    assert env.calls.active
    env.page.call = "none"
    await env.until(lambda: env.calls.status()["call"] == "none")
    env.clock.advance(service_mod.CALL_END_GRACE_S + 0.1)
    await env.until(lambda: not env.calls.active)


async def test_idle_with_no_call(env: Any) -> None:
    await _ready(env)
    env.clock.advance(env.ctx.config.calls.idle_seconds + 1)
    await env.until(lambda: not env.calls.active)
    assert env.last(CLOSED).data == {"reason": "nobody used it"}


@pytest.mark.parametrize("what,reason", [
    (lambda env: env.ctx.bus.publish(Topics.STOP_ALL, {}), "stop all"),
    (lambda env: env.ctx.bus.publish(Topics.MEDIA_STARTED, {}), "a video started"),
    (lambda env: env.ctx.bus.publish(Topics.ACTIVITY_STARTED, {}), "an activity started"),
    (lambda env: env.ctx.bus.publish("game.started", {}), "a game started"),
    (lambda env: env.ctx.bus.publish(Topics.FACE_SCREEN, {"on": False, "reason": "quiet"}), "the screen went dark (quiet)"),
    (lambda env: env.ctx.state.transition(RobotState.SLEEPING, "test"), "LittleBot went to sleep"),
    (lambda env: env.ctx.settings.set("calls.enabled", False), "switched off"),
    (lambda env: env.ctx.privacy.set(cloud_enabled=False), "cloud communication switched off"),
    (lambda env: env.ctx.privacy.set(microphone_enabled=False), "the microphone was switched off in Privacy"),
    (lambda env: env.ctx.privacy.set(camera_enabled=False), "the camera was switched off in Privacy"),
    (lambda env: env.ctx.arbiter.acquire("android", [Resource.SCREEN], Priority.MEDIA), "android took the screen"),
    (lambda env: env.ctx.arbiter.acquire("mirror", [Resource.SCREEN], Priority.MEDIA), "mirror took the screen"),
    (lambda env: env.ctx.arbiter.acquire("games", [Resource.SCREEN], Priority.ACTIVITY), "games took the screen"),
    (lambda env: env.launcher.proc.exit(0), "its window was closed"),
    (lambda env: env.memory.__setitem__(0, 100), "memory ran low (100 MB free)"),
])
async def test_what_shuts_it_down(env: Any, what: Any, reason: str) -> None:
    await env.open(contact="Dad")
    await env.until(lambda: env.calls.in_call)
    what(env)
    await env.until(lambda: not env.calls.active)
    await env.settle()
    assert env.last(CLOSED).data == {"reason": reason}
    assert env.launcher.proc.returncode is not None and not env.vision.lent
    assert env.ctx.arbiter.holder(Resource.SCREEN) is None or env.ctx.arbiter.holder(Resource.SCREEN).owner != "calls"


async def test_the_window_closed(env: Any) -> None:
    await _ready(env)
    await env.devtools.emit("Target.targetDestroyed", {"targetId": PAGE_TARGET}, session=None)
    await env.until(lambda: not env.calls.active)
    assert env.last(CLOSED).data == {"reason": "its window was closed"}


async def test_another_window_in_front_closes_it(env: Any) -> None:
    await _ready(env)
    await env.settle()  # (seen in front)
    env.windows[0] = [WHATSAPP_BEHIND, OTHER_FRONT]
    await env.settle()
    assert env.calls.active
    env.clock.advance(env.ctx.config.calls.off_screen_seconds + 1)
    await env.until(lambda: not env.calls.active)
    assert env.last(CLOSED).data == {"reason": "another window is in front of it"}


async def test_a_desktop_that_cannot_say_changes_nothing(env: Any) -> None:
    env.windows[0] = None
    await _ready(env)
    env.clock.advance(60)
    await env.settle()
    assert env.calls.active


async def test_stopping_littlebot_closes_it(env: Any) -> None:
    await _ready(env)
    await env.calls.stop()
    assert env.launcher.proc.returncode is not None and not env.vision.lent


async def test_chromium_that_never_answers(make_env: Any) -> None:
    env = await make_env(launcher_kw={"write_port": False}, calls={"start_timeout_seconds": 2})
    env.clock.tick = 0.5
    out = await env.open()
    assert out["ok"] is False and out["error"] == "failed"
    assert env.launcher.proc.returncode is not None and not env.calls.active
    assert env.vision.log == [("lend", "calls"), ("return", "calls")]


async def test_it_keeps_littlebot_awake_while_open(env: Any) -> None:
    await _ready(env)
    n = len([e for e in env.events if e.topic == USED])
    env.clock.advance(service_mod.KEEP_AWAKE_EVERY_S + 1)
    await env.until(lambda: len([e for e in env.events if e.topic == USED]) > n)


# ---------------------------------------------------------------- where the window may go
async def _paused(env: Any, url: str, frame: str = PAGE_TARGET) -> None:
    await env.devtools.emit("Fetch.requestPaused", {"requestId": f"R-{url}", "frameId": frame,
                                                    "resourceType": "Document", "request": {"url": url}})


async def test_only_whatsapp_pages_load(env: Any) -> None:
    await env.open()
    dt = env.devtools
    await _paused(env, "https://web.whatsapp.com/")
    await env.until(lambda: dt.count("Fetch.continueRequest") == 1)
    await _paused(env, "https://example.com/a-link-from-a-chat")
    await env.until(lambda: dt.count("Fetch.failRequest") == 1)
    assert dt.last("Fetch.failRequest")["errorReason"] == "BlockedByClient"
    await _paused(env, "https://static.whatsapp.net/frame.html", frame="F-2")  # WhatsApp's own, in a frame
    await env.until(lambda: dt.count("Fetch.continueRequest") == 2)
    await _paused(env, "https://ads.example.net/x", frame="F-2")
    await env.until(lambda: dt.count("Fetch.failRequest") == 2)


async def test_a_page_that_left_whatsapp_goes_back(env: Any) -> None:
    await env.open()
    await env.devtools.emit("Page.frameNavigated", {"frame": {"id": PAGE_TARGET, "url": "https://example.com/"}})
    await env.until(lambda: _navigations(env) == ["https://web.whatsapp.com/"] * 2)


async def test_other_windows_are_closed_unless_they_are_whatsapps(env: Any) -> None:
    await env.open()
    dt = env.devtools
    await dt.emit("Target.targetCreated", {"targetInfo": {"targetId": "T-AD", "type": "page",
                                                          "url": "https://example.com/"}}, session=None)
    await env.until(lambda: dt.last("Target.closeTarget") == {"targetId": "T-AD"})
    await dt.emit("Target.targetCreated", {"targetInfo": {"targetId": "T-CALL", "type": "page",
                                                          "url": "about:blank"}}, session=None)
    await env.until(lambda: {"targetId": "T-CALL", "flatten": True} in dt.params("Target.attachToTarget"))
    assert dt.count("Target.closeTarget") == 1
    await dt.emit("Target.targetInfoChanged", {"targetInfo": {"targetId": "T-CALL", "type": "page",
                                                              "url": "https://evil.example/"}}, session=None)
    await env.until(lambda: dt.last("Target.closeTarget") == {"targetId": "T-CALL"})


# ---------------------------------------------------------------- the list, signing out, status
async def test_contacts_are_kept_and_checked(env: Any) -> None:
    kept = env.calls.set_contacts([{"name": " Grandma ", "whatsapp": "Rose  Miller"}, {"name": "Dad", "whatsapp": ""}])
    assert kept == {"Grandma": "Rose Miller", "Dad": "Dad"} == env.calls.contacts()
    assert env.calls.find_contact("my grandma") == ("Grandma", "Rose Miller")
    with pytest.raises(ValueError):
        env.calls.set_contacts([{"name": "Dad", "whatsapp": "a"}, {"name": "dad", "whatsapp": "b"}])
    assert env.calls.contacts() == kept


async def test_sign_out_deletes_the_profile(env: Any) -> None:
    assert env.calls.status()["profile"] is False
    await _ready(env)
    (env.profile / "Default").mkdir()
    assert env.calls.status()["profile"] is True
    assert await env.calls.forget() is True
    assert not env.profile.exists() and not env.calls.active
    assert env.launcher.proc.returncode is not None
    assert await env.calls.forget() is False


async def test_status_never_holds_whatsapps_names(env: Any) -> None:
    await env.open(contact="Dad")
    await env.until(lambda: env.calls.in_call)
    st = env.calls.status()
    assert st["active"] and st["linked"] is True and st["camera_lent"] is True and st["hide_chats"] is True
    assert st["contacts"] == [{"name": "Dad", "whatsapp": "Ali Khan"}, {"name": "Mum", "whatsapp": "Sara"}]
    rest = {k: v for k, v in st.items() if k != "contacts"}
    assert "Ali" not in str(rest)


async def test_leftover_chromium_is_found_and_killed(make_env: Any, tmp_path: Any, monkeypatch: Any) -> None:
    """Anything still running on WhatsApp's profile is killed (and only that)."""
    import os

    proc = tmp_path / "proc"
    env = await make_env(calls_kw={"proc_root": str(proc)})
    mine = f"--user-data-dir={env.profile}".encode()
    for pid, args in ((111, [b"/usr/bin/chromium", b"--type=renderer", mine]),
                      (222, [b"/usr/bin/chromium", b"--user-data-dir=/somewhere/else"]),
                      (333, [b"/usr/bin/python3", b"littlebot"])):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "cmdline").write_bytes(b"\0".join(args) + b"\0")
    killed: list[int] = []

    def kill(pid: int, sig: int) -> None:
        killed.append(pid)
        (proc / str(pid) / "cmdline").unlink()

    monkeypatch.setattr(os, "kill", kill)
    assert env.calls._leftovers() == [111]
    assert env.calls._kill_leftovers("in a test") == 1
    assert killed == [111] and env.calls._leftovers() == []
