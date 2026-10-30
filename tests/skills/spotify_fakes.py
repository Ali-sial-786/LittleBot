"""A fake Spotify (accounts.spotify.com + the Web API) for the Spotify tests, through httpx.MockTransport.

``FakeSpotify`` keeps the account's state (devices, what plays, the library, search results), answers the requests
the skill makes, and records each one (method, path, query, JSON body, whether it carried a token).
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

from littlebot.skills.spotify import Spotify
from littlebot.skills.spotify_api import Tokens, TokenStore

CLIENT_ID = "0123456789abcdef0123456789abcdef"
ACCESS = "ACCESS-TOKEN-never-shown-1"
REFRESH = "REFRESH-TOKEN-never-shown-1"
DEVICE = {"id": "dev-littlebot", "name": "LittleBot", "type": "Speaker", "is_active": False,
          "is_restricted": False, "volume_percent": 60, "supports_volume": True}
PHONE = {"id": "dev-phone", "name": "Ali's Pixel", "type": "Smartphone", "is_active": True,
         "is_restricted": False, "volume_percent": 80, "supports_volume": True}


def img(n: int) -> list[dict[str, Any]]:
    return [{"url": f"https://i.scdn.co/image/{n}-640", "width": 640, "height": 640},
            {"url": f"https://i.scdn.co/image/{n}-300", "width": 300, "height": 300},
            {"url": f"https://i.scdn.co/image/{n}-64", "width": 64, "height": 64}]


def track(n: int, name: str, artist: str, *, explicit: bool = False, album: str | None = None,
          ms: int = 200_000) -> dict[str, Any]:
    return {"uri": f"spotify:track:t{n}", "name": name, "explicit": explicit, "duration_ms": ms, "is_playable": True,
            "artists": [{"name": artist, "uri": f"spotify:artist:{artist.replace(' ', '')}"}],
            "album": {"uri": f"spotify:album:a{n}", "name": album or f"{name} (Album)", "images": img(n)}}


QUEEN = [track(1, "Bohemian Rhapsody", "Queen", album="A Night at the Opera", ms=354_000),
         track(2, "Bohemian Rhapsody - Live Aid", "Queen", album="Live Aid"),
         track(3, "Bohemian Rhapsody", "Pentatonix")]
TAYLOR = {"artists": {"items": [{"uri": "spotify:artist:TaylorSwift", "name": "Taylor Swift", "images": img(9)}]},
          "tracks": {"items": [track(10, "Shake It Off", "Taylor Swift"), track(11, "Love Story", "Taylor Swift")]}}
JAZZ = {"playlists": {"items": [None, {"uri": "spotify:playlist:jazz1", "name": "Jazz Classics", "images": img(20),
                                       "owner": {"id": "someone", "display_name": "Jazz Fan"},
                                       "tracks": {"total": 80}},
                                {"uri": "spotify:playlist:jazz2", "name": "Late Night Jazz", "images": img(21),
                                 "owner": {"id": "spotify", "display_name": "Spotify"}, "tracks": {"total": 50}}]}}
MY_LISTS = [{"uri": "spotify:playlist:road", "name": "Road Trip 2026", "images": img(30),
             "owner": {"id": "ali", "display_name": "Ali"}, "tracks": {"total": 40}},
            {"uri": "spotify:playlist:chill", "name": "Sunday chill", "images": img(31),
             "owner": {"id": "ali", "display_name": "Ali"}, "tracks": {"total": 12}}]
LIKED = [track(40 + i, f"Liked {i}", "Band", explicit=(i == 2)) for i in range(5)]
TOP = [track(60 + i, f"Top {i}", "Singer") for i in range(4)]


def challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class FakeSpotify:
    def __init__(self) -> None:
        self.devices: list[dict[str, Any]] = [dict(DEVICE), dict(PHONE)]
        self.player: dict[str, Any] | None = None  # GET /me/player (None: 204)
        self.search: dict[str, dict[str, Any]] = {}  # a word in q -> the answer
        self.playlists = list(MY_LISTS)
        self.liked = list(LIKED)
        self.top = list(TOP)
        self.requests: list[dict[str, Any]] = []
        self.token_requests: list[dict[str, str]] = []
        self.challenge: str | None = None  # the code_challenge the authorize URL carried
        self.code = "GOOD-CODE"
        self.access, self.refresh = ACCESS, REFRESH
        self.refresh_n = 0
        self.refresh_error: str | None = None  # "invalid_grant": the refresh token is no good
        self.expire_next: bool = False  # the next API call answers 401 once
        self.fail: dict[str, tuple[int, dict[str, Any]]] = {}  # "PUT /me/player/play" -> (status, body), once
        self.premium = True

    # ------------------------------------------------------------ helpers for tests
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def calls(self, method: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.requests if (method is None or r["method"] == method)
                and (path is None or r["path"] == path)]

    def playing_on(self, device: dict[str, Any], t: dict[str, Any] | None = None, *, playing: bool = True,
                   progress: int = 30_000, volume: int | None = None) -> None:
        dev = dict(device, is_active=True)
        if volume is not None:
            dev["volume_percent"] = volume
            for d in self.devices:
                if d["id"] == dev["id"]:
                    d["volume_percent"] = volume
        self.player = {"device": dev, "is_playing": playing, "progress_ms": progress, "shuffle_state": False,
                       "item": t or QUEEN[0], "currently_playing_type": "track"}

    # ------------------------------------------------------------ the server
    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.spotify.com":
            return self._token(request)
        assert request.url.host == "api.spotify.com"
        path = request.url.path.removeprefix("/v1")
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        body = json.loads(request.content) if request.content else None
        auth = request.headers.get("Authorization", "")
        self.requests.append({"method": request.method, "path": path, "query": query, "json": body,
                              "token_ok": auth == f"Bearer {self.access}"})
        if auth != f"Bearer {self.access}" or self.expire_next:
            self.expire_next = False
            return httpx.Response(401, json={"error": {"status": 401, "message": "The access token expired"}})
        key = f"{request.method} {path}"
        if key in self.fail:
            code, err = self.fail.pop(key)
            return httpx.Response(code, json=err)
        if not self.premium and path.startswith("/me/player") and request.method != "GET":
            return httpx.Response(403, json={"error": {"status": 403, "message": "Premium required",
                                                       "reason": "PREMIUM_REQUIRED"}})
        return self._api(request.method, path, query, body)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_requests.append(form)
        if form.get("client_id") != CLIENT_ID:
            return httpx.Response(400, json={"error": "invalid_client"})
        if form.get("grant_type") == "authorization_code":
            if form.get("code") != self.code or form.get("redirect_uri") != "http://127.0.0.1:8888/callback":
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid code"})
            if self.challenge is None or challenge(form.get("code_verifier", "")) != self.challenge:
                return httpx.Response(400, json={"error": "invalid_grant",
                                                 "error_description": "code_verifier was incorrect"})
            return httpx.Response(200, json={"access_token": self.access, "token_type": "Bearer",
                                             "scope": "user-read-playback-state user-modify-playback-state "
                                                      "user-read-currently-playing playlist-read-private "
                                                      "user-library-read user-top-read",
                                             "expires_in": 3600, "refresh_token": self.refresh})
        if form.get("grant_type") == "refresh_token":
            if self.refresh_error or form.get("refresh_token") != self.refresh:
                return httpx.Response(400, json={"error": self.refresh_error or "invalid_grant",
                                                 "error_description": "Refresh token revoked"})
            self.refresh_n += 1
            self.access = f"ACCESS-TOKEN-never-shown-{self.refresh_n + 1}"
            self.refresh = f"REFRESH-TOKEN-never-shown-{self.refresh_n + 1}"
            return httpx.Response(200, json={"access_token": self.access, "token_type": "Bearer",
                                             "expires_in": 3600, "refresh_token": self.refresh})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _api(self, method: str, path: str, query: dict[str, str], body: Any) -> httpx.Response:
        if method == "GET" and path == "/me/player/devices":
            return httpx.Response(200, json={"devices": self.devices})
        if method == "GET" and path == "/me/player":
            return httpx.Response(204) if self.player is None else httpx.Response(200, json=self.player)
        if method == "GET" and path == "/search":
            for word, answer in self.search.items():
                if word.lower() in query.get("q", "").lower():
                    return httpx.Response(200, json=answer)
            return httpx.Response(200, json={t + "s": {"items": []} for t in query.get("type", "track").split(",")})
        if method == "GET" and path == "/me/playlists":
            off, lim = int(query.get("offset", 0)), int(query.get("limit", 20))
            page = self.playlists[off:off + lim]
            return httpx.Response(200, json={"items": page, "next": None if off + lim >= len(self.playlists) else "x"})
        if method == "GET" and path == "/me/tracks":
            return httpx.Response(200, json={"items": [{"track": t} for t in self.liked]})
        if method == "GET" and path == "/me/top/tracks":
            return httpx.Response(200, json={"items": self.top})
        dev_id = query.get("device_id")
        dev = next((d for d in self.devices if d["id"] == dev_id), None)
        if method == "PUT" and path == "/me/player/play":
            if dev_id and dev is None:
                return httpx.Response(404, json={"error": {"status": 404, "message": "Device not found"}})
            if dev is not None:
                t = QUEEN[0]
                if body and body.get("uris"):
                    t = next((x for x in self.liked + self.top + QUEEN if x["uri"] == body["uris"][0]), t)
                elif body and body.get("offset"):
                    t = next((x for x in QUEEN + TAYLOR["tracks"]["items"] if x["uri"] == body["offset"]["uri"]), t)
                if body or self.player is None:
                    self.playing_on(dev, t, progress=0)
                else:
                    self.player["is_playing"] = True
            elif self.player is None:
                return httpx.Response(404, json={"error": {"status": 404, "message": "No active device",
                                                           "reason": "NO_ACTIVE_DEVICE"}})
            return httpx.Response(204)
        if self.player is None and path.startswith("/me/player/"):
            return httpx.Response(404, json={"error": {"status": 404, "message": "Player command failed: No active "
                                                       "device found", "reason": "NO_ACTIVE_DEVICE"}})
        if method == "PUT" and path == "/me/player/pause":
            self.player["is_playing"] = False
            return httpx.Response(204)
        if method == "POST" and path in ("/me/player/next", "/me/player/previous"):
            self.player["progress_ms"] = 0
            return httpx.Response(204)
        if method == "PUT" and path == "/me/player/volume":
            self.player["device"]["volume_percent"] = int(query["volume_percent"])
            for d in self.devices:
                if d["id"] == self.player["device"]["id"]:
                    d["volume_percent"] = int(query["volume_percent"])
            return httpx.Response(204)
        if method == "PUT" and path == "/me/player/shuffle":
            self.player["shuffle_state"] = query.get("state") == "true"
            return httpx.Response(204)
        if method == "PUT" and path == "/me/player":
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"status": 404, "message": "Not found"}})


def connected(path: Path, *, expires_in: float = 3600.0, now: float | None = None) -> None:
    """Write a sign-in file as if the owner had connected Spotify."""
    import time  # noqa: PLC0415

    t = time.time() if now is None else now
    TokenStore(path).write(Tokens(client_id=CLIENT_ID, access_token=ACCESS, refresh_token=REFRESH,
                                  expires_at=t + expires_in, scope="user-read-playback-state", connected_at=t))


def spotify_skill(ctx: Any, fake: FakeSpotify, *, connect: bool = True, **kw: Any) -> Spotify:
    """A Spotify skill on ``fake``, connected (by default), with cloud communication on."""
    path = Path(ctx.extras["spotify_token_file"])
    if connect:
        connected(path)
    if not ctx.config.is_offline:
        ctx.privacy.set(cloud_enabled=True)
    return Spotify(ctx, transport=fake.transport(), token_path=path, **kw)
