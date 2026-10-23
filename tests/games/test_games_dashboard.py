"""Dashboard API for games: list, start (with length), stop, status, errors."""

from __future__ import annotations

import contextlib

import httpx
import pytest

from littlebot.core.arbiter import Priority, Resource
from littlebot.dashboard import create_app
from littlebot.games.service import GamesService
from littlebot.testing import make_test_context

PASSWORD = "correct horse battery"


@pytest.fixture
def dctx():
    ctx = make_test_context(dashboard={"allowed_hosts": ["testserver"]})
    ctx.games = GamesService(ctx, seed=1)
    yield ctx
    ctx.games.close()
    ctx.db.close()


@contextlib.asynccontextmanager
async def logged_in(ctx):
    """A dashboard client that finished first-run setup (logged in, CSRF header set)."""
    app = create_app(ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)),
                                 base_url="http://testserver") as client:
        code = app.state.auth.issue_setup_code()
        r = await client.post("/api/setup", json={"setup_code": code, "password": PASSWORD})
        assert r.status_code == 200, r.text
        client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        yield client


async def test_list_start_status_stop(dctx):
    async with logged_in(dctx) as c:
        r = await c.get("/api/games")
        assert r.status_code == 200
        data = r.json()
        assert data["available"] is True and len(data["items"]) == 8
        assert data["status"]["running"] is False
        assert {"id", "name", "description", "ages", "emoji", "audience"} <= set(data["items"][0])

        r = await c.post("/api/games/memory/start", json={"minutes": 2})
        assert r.status_code == 200, r.text
        assert r.json()["status"]["running"] is True and r.json()["status"]["limit_seconds"] == 120
        assert dctx.games.active and dctx.games.status()["source"] == "dashboard"

        r = await c.get("/api/games/status")
        assert r.json()["id"] == "memory"

        r = await c.post("/api/games/bubbles/start", json={})  # replaces; default length
        assert r.status_code == 200 and r.json()["status"]["limit_seconds"] == 180

        r = await c.post("/api/games/stop", json={})
        assert r.status_code == 200 and r.json()["stopped"] is True
        assert r.json()["status"]["running"] is False and r.json()["status"]["last"]["reason"] == "parent stop"
        r = await c.post("/api/games/stop", json={})
        assert r.json()["stopped"] is False


async def test_start_errors(dctx):
    async with logged_in(dctx) as c:
        assert (await c.post("/api/games/chess/start", json={})).status_code == 404
        assert (await c.post("/api/games/Not-A-Game!/start", json={})).status_code == 404
        r = await c.post("/api/games/memory/start", json={"minutes": 100})
        assert r.status_code == 422
        dctx.arbiter.acquire("media", [Resource.SCREEN, Resource.AUDIO_OUT, Resource.FACE], Priority.MEDIA)
        r = await c.post("/api/games/memory/start", json={})
        assert r.status_code == 409 and "video" in r.json()["detail"]
        assert not dctx.games.active


async def test_games_unavailable(dctx):
    games = dctx.games
    dctx.games = None
    try:
        async with logged_in(dctx) as c:
            r = await c.get("/api/games")
            assert r.status_code == 200 and r.json()["available"] is False and r.json()["items"] == []
            assert (await c.get("/api/games/status")).json()["running"] is False
            assert (await c.post("/api/games/memory/start", json={})).status_code == 503
            assert (await c.post("/api/games/stop", json={})).status_code == 503
    finally:
        dctx.games = games


async def test_games_api_needs_login(dctx):
    app = create_app(dctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        assert (await c.get("/api/games")).status_code == 401
        assert (await c.post("/api/games/memory/start", json={})).status_code == 401
    assert not dctx.games.active


async def test_page_and_nav_are_served(dctx):
    async with logged_in(dctx) as c:
        r = await c.get("/static/js/pages/games.js")
        assert r.status_code == 200 and "export default" in r.text
        app_js = (await c.get("/static/js/app.js")).text
        assert "import games from './pages/games.js'" in app_js and "id: 'games'" in app_js
