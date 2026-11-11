"""Photos and videos (littlebot/capture): the dashboard's Photos page, and the share links a phone opens.

* ``/api/capture/...`` (behind the login like every /api page): the gallery, a photo / a recording from the
  dashboard, delete, show on LittleBot's screen, a share link (with its QR code) or a phone notification, the
  parent's switches and "stop every share link"; "always recording" (littlebot/capture/buffer.py): its state, the
  auto / on / off choice and "Save the last 3 minutes".
* ``GET /share/{token}`` (+ ``/file``, ``/thumb``): NO login, on purpose: a phone opens it from a notification or
  a QR code. The 22-character random token is the password, for one photo or video, until it runs out
  (``capture.share_hours``). A wrong or old token gets the same plain 404 as any unknown page. The page is static
  HTML (no script) with the photo or video and a Save button; videos are served with byte ranges (iPhones need
  them). The ``Host`` allowlist (the app's RequestGuard) still applies.
"""

from __future__ import annotations

import base64
import html
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import Field

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body

router = APIRouter(tags=["capture"])

UNAVAILABLE = "Photos and videos aren't available right now."
MAX_ITEMS = 500
SHARE_HEADERS = {
    # (/share is outside the dashboard's own page headers: littlebot/dashboard/app.py EXTERNAL_PREFIXES)
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",  # the token is in the address
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "private, no-cache",
    "X-Content-Type-Options": "nosniff",
}


class ShareBody(Body):
    to: int | None = Field(default=None, ge=0)  # a Who's who person's phone; none = the family's phones
    push: bool = False  # also send it to the phone(s) as a notification
    screen: bool = False  # also show the QR code on LittleBot's screen


class ShowBody(Body):
    play: bool = False  # a video: play it full screen (else the gallery card on it)


class CaptureSettingsBody(Body):
    enabled: bool | None = None
    video_sound: bool | None = None
    always_recording: Literal["auto", "on", "off"] | None = None  # the last minutes kept in RAM (capture/buffer.py)


class SaveBufferBody(Body):
    seconds: float | None = Field(default=None, ge=5, le=600)  # none = everything the buffer holds


def _svc(dash: DashState) -> Any:
    svc = getattr(dash.ctx, "capture", None)
    if svc is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    return svc


def _item_view(item: Any) -> dict[str, Any]:
    d = item.public()
    d["file"] = f"/api/capture/items/{item.id}/file"
    d["thumb"] = f"/api/capture/items/{item.id}/thumb"
    d["download_name"] = f"littlebot-{item.filename}"
    return d


def _view(dash: DashState) -> dict[str, Any]:
    svc = getattr(dash.ctx, "capture", None)
    if svc is None:
        s = dash.ctx.settings
        return {"available": False, "detail": "not running", "enabled": bool(s.get("capture.enabled", True)),
                "video_sound": bool(s.get("capture.video_sound", True)), "items": [], "phones": [],
                "recording": None, "buffer": None}
    out = svc.status()
    out["items"] = [_item_view(it) for it in svc.gallery.items(limit=MAX_ITEMS)]
    out["phones"] = svc.phones()
    if not out["phones"]:  # why "Send to phone" isn't offered, and how to set one up
        from littlebot.capture.service import _PUSH_WORDS  # noqa: PLC0415

        out["phone_help"] = _PUSH_WORDS["not_set_up"]
    return out


def _item(svc: Any, item_id: str) -> Any:
    item = svc.gallery.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="There's no such photo or video.")
    return item


# ---------------------------------------------------------------- the dashboard (behind the login)
@router.get("/api/capture")
async def capture_status(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return _view(dash)


@router.post("/api/capture/photo")
async def capture_photo(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    res = await _svc(dash).take_photo(source="dashboard", countdown=0, show=True)
    if res.get("ok"):
        log.info("dashboard: photo taken")
    return {**res, "status": _view(dash)}


@router.post("/api/capture/video/start")
async def capture_video_start(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    res = await _svc(dash).start_video(source="dashboard")
    return {**res, "status": _view(dash)}


@router.post("/api/capture/video/stop")
async def capture_video_stop(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    res = await _svc(dash).stop_video("stopped on the dashboard")
    return {**res, "status": _view(dash)}


@router.post("/api/capture/buffer/save")
async def capture_buffer_save(body: SaveBufferBody | None = None,
                              dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """"Save the last 3 minutes": what the always-recording buffer holds becomes a video in the gallery (LittleBot
    says so out loud: the room hears that a clip was kept)."""
    res = await _svc(dash).save_buffer(body.seconds if body is not None else None, source="dashboard",
                                       announce=True)
    if res.get("ok"):
        log.info("dashboard: the last %.0f s saved from the always-recording buffer", res.get("seconds") or 0)
    return {**res, "status": _view(dash)}


@router.get("/api/capture/items/{item_id}/file")
async def capture_file(item_id: str, download: bool = False, dash: DashState = Depends(get_dash)) -> Response:
    svc = _svc(dash)
    item = _item(svc, item_id)
    return _file(svc.gallery.file(item), item.media_type, f"littlebot-{item.filename}", download=download)


@router.get("/api/capture/items/{item_id}/thumb")
async def capture_thumb(item_id: str, dash: DashState = Depends(get_dash)) -> Response:
    svc = _svc(dash)
    item = _item(svc, item_id)
    return _thumb(svc, item)


@router.delete("/api/capture/items/{item_id}")
async def capture_delete(item_id: str, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    svc = _svc(dash)
    _item(svc, item_id)
    svc.delete(item_id)
    log.info("dashboard: a photo or video was deleted")
    return {"ok": True, "status": _view(dash)}


@router.post("/api/capture/items/{item_id}/show")
async def capture_show(item_id: str, body: ShowBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    svc = _svc(dash)
    item = _item(svc, item_id)
    if body.play and item.kind == "video":
        res = await svc.play_video(item.id, source="dashboard")
    else:
        res = await svc.show_gallery(item.id, source="dashboard")
    return res


@router.post("/api/capture/items/{item_id}/share")
async def capture_share(item_id: str, body: ShareBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    from littlebot.capture.qr import svg  # noqa: PLC0415

    svc = _svc(dash)
    item = _item(svc, item_id)
    link = svc.share(item.id)
    if not link.get("ok"):
        return link
    out: dict[str, Any] = {"ok": True, "url": link["url"], "urls": link["urls"], "expires_at": link["expires_at"],
                           "reachable": link["reachable"], "lan_help": link["lan_help"],
                           "qr": "data:image/svg+xml;base64," + base64.b64encode(svg(link["url"]).encode()).decode()}
    if body.push:
        sent = await svc.send_to_phone(item.id, to=body.to)
        out.update({"pushed": bool(sent.get("ok")), "push_error": None if sent.get("ok") else sent.get("detail")})
    if body.screen:
        shown = await svc.show_share_code(item.id)
        out["on_screen"] = bool(shown.get("ok"))
    return out


@router.delete("/api/capture/shares")
async def capture_revoke(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    n = _svc(dash).revoke_shares()
    log.info("dashboard: every share link stopped (%d)", n)
    return {"ok": True, "revoked": n, "status": _view(dash)}


@router.put("/api/capture/settings")
async def capture_settings(body: CaptureSettingsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    if body.enabled is None and body.video_sound is None and body.always_recording is None:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    s = dash.ctx.settings
    if body.enabled is not None:
        s.set("capture.enabled", bool(body.enabled), source="dashboard")
        log.info("photos and videos switched %s from dashboard", "on" if body.enabled else "off")
    if body.video_sound is not None:
        s.set("capture.video_sound", bool(body.video_sound), source="dashboard")
        log.info("video sound switched %s from dashboard", "on" if body.video_sound else "off")
    if body.always_recording is not None:
        svc = getattr(dash.ctx, "capture", None)
        if svc is not None:  # (the buffer follows at once, and its new state is in the answer)
            await svc.set_always_recording(body.always_recording, source="dashboard")
        else:
            s.set("capture.always_recording", body.always_recording, source="dashboard")
        log.info("always recording set to %s from dashboard", body.always_recording)
    return {"ok": True, **_view(dash)}


# ---------------------------------------------------------------- the share links (no login: the token)
def _not_found() -> Response:
    return JSONResponse({"detail": "Not Found"}, status_code=404, headers={"Cache-Control": "no-store",
                                                                           "X-Content-Type-Options": "nosniff"})


def _shared(dash: DashState, token: str) -> tuple[Any, Any] | None:
    svc = getattr(dash.ctx, "capture", None)
    if svc is None:
        return None
    item = svc.share_item(token)
    return (svc, item) if item is not None else None


def _file(path: Path, media_type: str, name: str, *, download: bool = False,
          headers: dict[str, str] | None = None) -> Response:
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(path, media_type=media_type, filename=name,
                        content_disposition_type="attachment" if download else "inline", headers=headers)


def _thumb(svc: Any, item: Any, headers: dict[str, str] | None = None) -> Response:
    path = svc.gallery.thumb(item.id)
    if not path.is_file():
        if item.kind != "photo":
            raise HTTPException(status_code=404, detail="No preview.")
        path = svc.gallery.file(item)
    return FileResponse(path, media_type="image/jpeg", headers=headers)


@router.get("/share/{token}", include_in_schema=False)
async def share_page(token: str, dash: DashState = Depends(get_dash)) -> Response:
    found = _shared(dash, token)
    if found is None:
        return _not_found()
    _svc_, item = found
    from littlebot.capture.service import _when  # noqa: PLC0415

    when = html.escape(_when(item.taken_at))
    name = html.escape(f"littlebot-{item.filename}")
    base = f"/share/{html.escape(token)}"
    if item.kind == "photo":
        media = f'<img src="{base}/file" alt="A photo from LittleBot, taken {when}">'
        hint = ("<b>iPhone:</b> press and hold the photo, then <i>Save to Photos</i>. "
                "<b>Android:</b> tap Save; it goes to your Downloads (and Photos / Gallery).")
        what = "photo"
    else:
        media = (f'<video src="{base}/file" poster="{base}/thumb" controls playsinline preload="metadata">'
                 "</video>")
        hint = ("<b>iPhone:</b> tap Save, then open it from the Downloads arrow (or the Files app) and choose "
                "<i>Save Video</i>. <b>Android:</b> tap Save; it goes to your Downloads.")
        what = "video"
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>A {what} from LittleBot</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ margin: 0; font: 16px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; background: #fff4f8;
  color: #3b2238; }}
main {{ max-width: 720px; margin: 0 auto; padding: 16px; }}
h1 {{ font-size: 20px; margin: 4px 0 2px; }}
p {{ margin: 6px 0; }}
.when {{ color: #7a5a74; font-size: 14px; }}
img, video {{ display: block; width: 100%; height: auto; border-radius: 14px; background: #000; margin: 12px 0; }}
a.save {{ display: block; text-align: center; padding: 14px; border-radius: 12px; background: #b0306a; color: #fff;
  font-weight: 600; text-decoration: none; font-size: 17px; }}
.hint {{ font-size: 14px; color: #5b4057; }}
@media (prefers-color-scheme: dark) {{ body {{ background: #1d1520; color: #f3e6ef; }} .when, .hint {{ color: #c9b3c3; }} }}
</style></head>
<body><main>
<h1>A {what} from LittleBot</h1>
<p class="when">Taken {when}</p>
{media}
<a class="save" href="{base}/file?download=1" download="{name}">Save</a>
<p class="hint">{hint}</p>
<p class="hint">This link works on your home Wi-Fi only, and stops working after a while.</p>
</main></body></html>"""
    return HTMLResponse(page, headers=SHARE_HEADERS)


@router.get("/share/{token}/file", include_in_schema=False)
async def share_file(token: str, download: bool = False, dash: DashState = Depends(get_dash)) -> Response:
    found = _shared(dash, token)
    if found is None:
        return _not_found()
    svc, item = found
    return _file(svc.gallery.file(item), item.media_type, f"littlebot-{item.filename}", download=download,
                 headers=SHARE_HEADERS)


@router.get("/share/{token}/thumb", include_in_schema=False)
async def share_thumb(token: str, dash: DashState = Depends(get_dash)) -> Response:
    found = _shared(dash, token)
    if found is None:
        return _not_found()
    svc, item = found
    return _thumb(svc, item, headers=SHARE_HEADERS)


__all__ = ["router"]
