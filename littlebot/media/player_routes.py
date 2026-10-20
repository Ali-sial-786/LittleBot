"""HTTP routes for the on-robot YouTube player page and its JSON bridge.

``create_player_router(media)`` returns a FastAPI ``APIRouter`` that the dashboard mounts
at the root **without** parent authentication. Every route that reveals or changes state
requires the per-process player token (``?token=`` or ``X-Player-Token`` header), compared
in constant time: missing -> 401, wrong -> 403.

Routes
------
GET  /player?token=...        the page (strict Content-Security-Policy for the IFrame API)
GET  /player/static/<file>    player.js / player.css (no secrets, whitelisted names only)
GET  /player/api/state        desired state: {seq, action, load_id, item, volume, owner, ...}
                              (``?page_id=..&visible=1|0``: the first visible page claims a new load)
POST /player/api/event        {type: ready|playing|paused|buffering|ended|error|heartbeat|hidden|..., load_id, code, ...}
                              (``video_id`` + for playlists ``playlist_ids``: checked by the controller)

Request bodies under ``/player`` are capped at ``MAX_BODY_BYTES`` by the dashboard's outermost
middleware *before* anything is read (``Content-Length`` -> 413, chunked bodies cut off at the cap).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

STATIC_DIR = Path(__file__).resolve().parent / "static"
_ASSETS = {"player.js": "application/javascript; charset=utf-8", "player.css": "text/css; charset=utf-8"}

# Only what the official IFrame Player API needs: its loader + widget script from www.youtube.com,
# the player iframe from youtube-nocookie.com (or youtube.com), and our own same-origin bridge.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'none'",
    "script-src 'self' https://www.youtube.com https://s.ytimg.com",
    "frame-src https://www.youtube-nocookie.com https://www.youtube.com",
    "connect-src 'self'",
    "img-src 'self' data:",
    "style-src 'self'",
    "font-src 'none'",
    "media-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
])

PAGE_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    # YouTube embeds need a Referer (error 153 without one); this sends only our origin, never the token.
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), usb=(), payment=()",
}
API_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

# Largest accepted request body for any /player route (enforced by the dashboard middleware).
# The biggest legitimate one is an event with 500 ``playlist_ids`` (~7 KB of JSON).
MAX_BODY_BYTES = 16 * 1024


class PlayerMedia(Protocol):
    """What the router needs from the media service (MediaController implements it)."""

    def check_player_token(self, token: str | None) -> bool: ...

    def player_state(self, page_id: str | None = None, visible: bool = True) -> dict[str, Any]: ...

    async def handle_player_event(self, event: dict[str, Any]) -> dict[str, Any]: ...


ShortStr = Annotated[str, StringConstraints(max_length=64)]
_PAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
EventType = Literal["ready", "playing", "paused", "buffering", "cued", "ended", "track_ended", "error",
                    "heartbeat", "autoplay_blocked", "hidden", "navigated_away"]


class PlayerEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: EventType
    load_id: ShortStr | None = None
    code: int | Annotated[str, StringConstraints(max_length=40)] | None = None
    player_state: int | None = Field(default=None, ge=-1, le=5)
    current_time: float | None = Field(default=None, ge=0, le=1e7)
    duration: float | None = Field(default=None, ge=0, le=1e7)
    video_id: ShortStr | None = None
    playlist_ids: list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{11}$")]] | None = Field(
        default=None, max_length=500)
    page_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,64}$")] | None = None
    detail: Annotated[str, StringConstraints(max_length=200)] | None = None


def create_player_router(media: PlayerMedia) -> APIRouter:
    router = APIRouter(tags=["player"])
    page_html = (STATIC_DIR / "player.html").read_text(encoding="utf-8")
    assets = {name: (STATIC_DIR / name).read_bytes() for name in _ASSETS}

    def require_token(request: Request) -> None:
        token = request.headers.get("x-player-token") or request.query_params.get("token")
        if not token:
            raise HTTPException(status_code=401, detail="player token required")
        if not media.check_player_token(token):
            raise HTTPException(status_code=403, detail="invalid player token")

    @router.get("/player", include_in_schema=False, dependencies=[Depends(require_token)])
    async def player_page() -> HTMLResponse:
        return HTMLResponse(page_html, headers=PAGE_HEADERS)

    @router.get("/player/static/{name}", include_in_schema=False)
    async def player_asset(name: str) -> Response:
        if name not in assets:
            raise HTTPException(status_code=404, detail="not found")
        return Response(assets[name], media_type=_ASSETS[name],
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    @router.get("/player/api/state", dependencies=[Depends(require_token)])
    async def player_state(request: Request) -> JSONResponse:
        # ?page_id=<the page's random id>&visible=1|0: the first visible page claims a new load.
        page_id = request.query_params.get("page_id")
        if not (page_id and _PAGE_ID_RE.match(page_id)):
            page_id = None
        visible = request.query_params.get("visible") != "0"
        return JSONResponse(media.player_state(page_id=page_id, visible=visible), headers=API_HEADERS)

    @router.post("/player/api/event", dependencies=[Depends(require_token)])
    async def player_event(event: PlayerEvent) -> JSONResponse:
        result = await media.handle_player_event(event.model_dump())
        return JSONResponse(result, headers=API_HEADERS)

    return router
