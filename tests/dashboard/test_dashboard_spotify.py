"""The Assistant page's Spotify routes (/api/assistant/spotify/...): the Client ID, connecting with the pasted
address, the status (never a token), the receiver, control, disconnecting, and where music plays from. Against
the fake Spotify of tests/skills/spotify_fakes.py (no network)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills"))
from spotify_fakes import ACCESS, CLIENT_ID, DEVICE, PHONE, REFRESH, FakeSpotify  # noqa: E402

from littlebot.skills.hub import SkillHub  # noqa: E402
from littlebot.skills.music import Music  # noqa: E402
from littlebot.skills.spotify import Spotify  # noqa: E402


@pytest.fixture
def fake() -> FakeSpotify:
    return FakeSpotify()


@pytest.fixture
def spotify(dctx, fake) -> Spotify:
    sp = Spotify(dctx, transport=fake.transport(), token_path=Path(dctx.extras["spotify_token_file"]))
    dctx.skills = SkillHub(dctx, [sp, Music(dctx)])
    dctx.privacy.set(cloud_enabled=True)
    return sp


def no_secrets(text: str, fake: FakeSpotify) -> None:
    for s in (ACCESS, REFRESH, fake.access, fake.refresh, "GOOD-CODE"):
        assert s not in text


async def test_without_the_skill(authed, dctx) -> None:
    dctx.skills = SkillHub(dctx, [])
    r = await authed.get("/api/assistant/spotify")
    assert r.status_code == 503


async def test_connect_from_the_dashboard(authed, dctx, spotify, fake) -> None:
    r = await authed.get("/api/assistant/spotify")
    body = r.json()
    assert r.status_code == 200 and body["connected"] is False and body["blocked"] == "no_client_id"
    assert body["redirect_uri"] == "http://127.0.0.1:8888/callback" and body["client_id"] is None

    r = await authed.post("/api/assistant/spotify/client_id", json={"client_id": "nope"})
    assert r.status_code == 400 and "Client ID" in r.json()["detail"]
    r = await authed.post("/api/assistant/spotify/client_id", json={"client_id": CLIENT_ID.upper()})
    assert r.status_code == 200 and r.json()["client_id"] == CLIENT_ID
    assert dctx.settings.get("spotify.client_id") == CLIENT_ID

    r = await authed.post("/api/assistant/spotify/connect", json={})
    assert r.status_code == 200
    url = r.json()["url"]
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith("https://accounts.spotify.com/authorize?") and q["client_id"] == CLIENT_ID
    assert q["code_challenge_method"] == "S256" and q["redirect_uri"] == "http://127.0.0.1:8888/callback"
    fake.challenge = q["code_challenge"]
    assert (await authed.get("/api/assistant/spotify")).json()["pending"] is True

    r = await authed.post("/api/assistant/spotify/finish", json={"url": f"http://127.0.0.1:8888/callback?code="
                                                                        f"GOOD-CODE&state=not-it"})
    assert r.status_code == 400 and "different sign-in" in r.json()["detail"]
    r = await authed.post("/api/assistant/spotify/finish",
                          json={"url": f"http://127.0.0.1:8888/callback?code=GOOD-CODE&state={q['state']}"})
    assert r.status_code == 200, r.text
    st = r.json()["status"]
    assert st["connected"] is True and st["available"] is True and st["device"]["visible"] is True
    no_secrets(r.text, fake)
    r = await authed.get("/api/assistant/spotify")
    no_secrets(r.text, fake)
    assert r.json()["device"] == {"visible": True, "active": False, "volume": 60}
    info = await authed.get("/api/assistant")
    no_secrets(info.text, fake)
    assert any(s["name"] == "spotify" and s["available"] for s in info.json()["skills"])

    r = await authed.post("/api/assistant/spotify/disconnect")
    assert r.json() == {"ok": True, "was_connected": True}
    assert not Path(dctx.extras["spotify_token_file"]).exists()
    assert (await authed.get("/api/assistant/spotify")).json()["connected"] is False


async def test_connecting_needs_cloud_and_a_client_id(authed, dctx, spotify) -> None:
    r = await authed.post("/api/assistant/spotify/connect", json={})
    assert r.status_code == 400 and "isn't set up yet" in r.json()["detail"]
    dctx.privacy.set(cloud_enabled=False)
    r = await authed.post("/api/assistant/spotify/connect", json={"client_id": CLIENT_ID})
    assert r.status_code == 403 and "Privacy" in r.json()["detail"]
    r = await authed.post("/api/assistant/spotify/finish", json={"url": "http://127.0.0.1:8888/callback?code=x"})
    assert r.status_code == 403
    r = await authed.post("/api/assistant/spotify/finish", json={"url": "x", "token": "y"})
    assert r.status_code == 422  # unknown fields refused


async def test_the_receiver_missing_and_control(authed, dctx, spotify, fake) -> None:
    from spotify_fakes import connected

    connected(Path(dctx.extras["spotify_token_file"]))
    fake.devices = [dict(PHONE)]
    body = (await authed.get("/api/assistant/spotify")).json()
    assert body["connected"] and body["device"] == {"visible": False, "active": False, "volume": None}
    r = await authed.post("/api/assistant/spotify/control", json={"action": "pause"})
    assert r.status_code == 409 and r.json()["detail"] == "Spotify isn't playing on me right now."
    fake.devices = [dict(DEVICE), dict(PHONE)]
    fake.playing_on(DEVICE)
    body = (await authed.get("/api/assistant/spotify")).json()
    assert body["now"]["title"] == "Bohemian Rhapsody" and body["now"]["playing"] is True
    r = await authed.post("/api/assistant/spotify/control", json={"action": "pause"})
    assert r.status_code == 200 and r.json() == {"ok": True, "said": "Paused."}
    assert fake.player["is_playing"] is False
    r = await authed.post("/api/assistant/spotify/control", json={"action": "explode"})
    assert r.status_code == 422


async def test_where_music_plays_from(authed, dctx, spotify) -> None:
    r = await authed.post("/api/assistant/music/provider", json={"provider": "youtube"})
    assert r.status_code == 200 and dctx.settings.get("music.provider") == "youtube"
    assert (await authed.get("/api/assistant/spotify")).json()["provider"] == "youtube"
    r = await authed.post("/api/assistant/music/provider", json={"provider": "tidal"})
    assert r.status_code == 422


async def test_needs_login(client, spotify) -> None:
    r = await client.get("/api/assistant/spotify")
    assert r.status_code in (401, 403)
    r: Any = await client.post("/api/assistant/spotify/disconnect")
    assert r.status_code in (401, 403)
