"""Player page + JSON bridge routes: token protection, state polling, event reporting."""

from __future__ import annotations

import httpx
import pytest
from conftest import VID_A, VID_B  # type: ignore[import-not-found]
from fastapi import FastAPI

from littlebot.core.events import Topics
from littlebot.core.state import RobotState
from littlebot.media import create_player_router


@pytest.fixture
async def client(env):
    app = FastAPI()
    app.include_router(create_player_router(env.media))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as c:
        yield c


def _h(env):
    return {"X-Player-Token": env.media.player_token}


async def test_page_requires_token(env, client):
    r = await client.get("/player")
    assert r.status_code == 401
    r = await client.get("/player", params={"token": "wrong"})
    assert r.status_code == 403
    r = await client.get("/player", params={"token": env.media.player_token[:-1]})
    assert r.status_code == 403
    r = await client.get("/player", params={"token": env.media.player_token})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="yt-host"' in r.text and "player/static/player.js" in r.text
    # Nothing may sit in front of / block the YouTube player (RMF, Developer Policies III.I.6).
    assert "shield" not in r.text and "<button" not in r.text
    assert env.media.player_token not in r.text  # the page reads it from its own URL


async def test_page_security_headers(env, client):
    r = await client.get("/player", params={"token": env.media.player_token})
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self' https://www.youtube.com" in csp
    assert "frame-src https://www.youtube-nocookie.com https://www.youtube.com" in csp
    assert "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"


async def test_static_assets_whitelisted(client):
    r = await client.get("/player/static/player.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "onYouTubeIframeAPIReady" in r.text and "https://www.youtube.com/iframe_api" in r.text
    r = await client.get("/player/static/player.css")
    assert r.status_code == 200 and "text/css" in r.headers["content-type"]
    for bad in ("player.html", "controller.py", "..%2Fcontroller.py", "nope.js"):
        assert (await client.get(f"/player/static/{bad}")).status_code == 404


@pytest.mark.parametrize("method, path", [("GET", "/player/api/state"), ("POST", "/player/api/event")])
async def test_api_requires_token(env, client, method, path):
    body = {"type": "heartbeat"}
    r = await client.request(method, path, json=body)
    assert r.status_code == 401
    r = await client.request(method, path, json=body, headers={"X-Player-Token": "nope"})
    assert r.status_code == 403
    r = await client.request(method, path, json=body, headers=_h(env))
    assert r.status_code == 200
    r = await client.request(method, path, json=body, params={"token": env.media.player_token})
    assert r.status_code == 200


async def test_rejected_requests_do_not_mark_connected(env, client):
    await client.get("/player/api/state", headers={"X-Player-Token": "nope"})
    assert env.media.status()["player"]["connected"] is False


async def test_state_polling_reflects_commands(env, client):
    r = await client.get("/player/api/state", headers=_h(env))
    idle = r.json()
    assert idle["action"] == "idle" and idle["item"] is None and r.headers["cache-control"] == "no-store"
    item = env.add(VID_A, "Twinkle")
    await env.media.play(item["id"])
    s = (await client.get("/player/api/state", headers=_h(env))).json()
    assert s["action"] == "play" and s["seq"] > idle["seq"] and s["load_id"]
    assert s["item"] == {"id": item["id"], "kind": "video", "video_id": VID_A, "list_id": None,
                         "start_seconds": 0, "title": "Twinkle"}
    assert s["volume"] == env.media.status()["volume"] and s["poll_ms"] == 500
    await env.media.pause()
    assert (await client.get("/player/api/state", headers=_h(env))).json()["action"] == "pause"
    await env.media.resume()
    assert (await client.get("/player/api/state", headers=_h(env))).json()["action"] == "play"
    env.media.set_volume(0.25)
    assert (await client.get("/player/api/state", headers=_h(env))).json()["volume"] == 0.25
    await env.media.stop()
    s = (await client.get("/player/api/state", headers=_h(env))).json()
    assert s["action"] == "stop" and s["reason"] == "stopped" and s["item"] is None


async def test_heartbeat_and_connected_status(env, client):
    assert env.media.status()["player"]["connected"] is False
    item = env.add()
    await env.media.play(item["id"])
    load_id = (await client.get("/player/api/state", headers=_h(env))).json()["load_id"]
    assert env.media.status()["player"]["connected"] is True
    r = await client.post("/player/api/event", headers=_h(env), json={
        "type": "heartbeat", "load_id": load_id, "player_state": 1, "current_time": 12.5, "duration": 95.0,
        "video_id": VID_A})
    assert r.json() == {"ok": True, "current": True}
    player = env.media.status()["player"]
    assert player["connected"] is True
    assert player["info"] == {"player_state": 1, "current_time": 12.5, "duration": 95.0, "video_id": VID_A}
    env.clock.advance(7)
    assert env.media.status()["player"]["connected"] is False
    await client.post("/player/api/event", headers=_h(env), json={"type": "ready", "load_id": load_id})
    player = env.media.status()["player"]
    assert player["connected"] is True and player["last_event"]["type"] == "ready"


async def test_playing_then_ended_event(env, client):
    item = env.add()
    await env.media.play(item["id"])
    load_id = (await client.get("/player/api/state", headers=_h(env))).json()["load_id"]
    await client.post("/player/api/event", headers=_h(env), json={"type": "playing", "load_id": load_id})
    assert env.media.status()["state"] == "playing"
    r = await client.post("/player/api/event", headers=_h(env), json={"type": "ended", "load_id": load_id})
    assert r.status_code == 200
    stopped = env.last(Topics.MEDIA_STOPPED)
    assert stopped is not None and stopped.data["reason"] == "ended"
    assert env.ctx.state.state == RobotState.IDLE
    assert env.all(Topics.FACE_VISIBLE)[-1].data["visible"] is True


async def test_error_150_event(env, client):
    item = env.add()
    await env.media.play(item["id"])
    load_id = (await client.get("/player/api/state", headers=_h(env))).json()["load_id"]
    r = await client.post("/player/api/event", headers=_h(env), json={"type": "error", "code": 150,
                                                                       "load_id": load_id})
    assert r.status_code == 200
    err = env.last(Topics.MEDIA_ERROR)
    assert err is not None and "Unsupported video" in err.data["error"] and err.data["code"] == 150
    assert env.last(Topics.MEDIA_STOPPED).data["reason"] == "error"
    assert "Unsupported video" in env.media.get_item(item["id"])["last_error"]
    assert env.ctx.state.state == RobotState.IDLE


async def test_events_for_old_loads_are_ignored(env, client):
    a, _b = env.add(VID_A, "A"), env.add(VID_B, "B")
    await env.media.play(a["id"])
    old = (await client.get("/player/api/state", headers=_h(env))).json()["load_id"]
    await env.media.skip()
    r = await client.post("/player/api/event", headers=_h(env), json={"type": "ended", "load_id": old})
    assert r.json()["current"] is False
    assert env.ctx.state.state == RobotState.PLAYING_MEDIA


@pytest.mark.parametrize("kind", ["hidden", "navigated_away"])
async def test_hidden_or_navigated_page_stops_playback(env, client, kind):
    item = env.add()
    await env.media.play(item["id"])
    load_id = (await client.get("/player/api/state", headers=_h(env))).json()["load_id"]
    await client.post("/player/api/event", headers=_h(env), json={"type": "playing", "load_id": load_id})
    await client.post("/player/api/event", headers=_h(env), json={"type": kind, "load_id": load_id})
    assert env.last(Topics.MEDIA_STOPPED).data["reason"] == "error"
    assert "hidden or left" in env.last(Topics.MEDIA_ERROR).data["error"]
    assert env.ctx.state.state == RobotState.IDLE
    await env.settle()
    assert env.backend.closed == 1  # the kiosk (and any popup it opened) is closed


@pytest.mark.parametrize("body", [
    {"type": "explode"},
    {"type": "error", "code": "x" * 100},
    {"type": "heartbeat", "player_state": 42},
    {"type": "heartbeat", "load_id": "x" * 500},
    {"no": "type"},
])
async def test_invalid_events_rejected(env, client, body):
    r = await client.post("/player/api/event", headers=_h(env), json=body)
    assert r.status_code == 422
