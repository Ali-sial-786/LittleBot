"""Optional title lookup via YouTube's public oEmbed endpoint.

Off by default (``media.oembed_titles``) and never used in offline mode. It is a single
GET to https://www.youtube.com/oembed - the same service playback talks to anyway - and
never blocks ``add_item``: the controller runs it in the background and only replaces a
default title. Its HTTP status also gives an early warning to the parent: 401 means the
owner disabled embedding (the video can't play on LittleBot), 404 means not found/private.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("littlebot.media.oembed")

OEMBED_ENDPOINT = "https://www.youtube.com/oembed"


@dataclass(frozen=True)
class OEmbedResult:
    status_code: int | None  # None = network error
    title: str | None = None

    @property
    def problem(self) -> str | None:
        if self.status_code == 401:
            return "Unsupported video: its owner doesn't allow it to be played outside YouTube (embedding disabled)."
        if self.status_code in (403, 404):
            return "YouTube says this video or playlist was not found or is private."
        return None


async def lookup_oembed(url: str, *, timeout: float = 5.0) -> OEmbedResult:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.get(OEMBED_ENDPOINT, params={"url": url, "format": "json"})
    except httpx.HTTPError as e:
        log.info("oEmbed lookup failed: %s", type(e).__name__)
        return OEmbedResult(None)
    if resp.status_code != 200:
        return OEmbedResult(resp.status_code)
    try:
        data = resp.json()
    except ValueError:
        return OEmbedResult(resp.status_code)
    title = data.get("title") if isinstance(data, dict) else None
    return OEmbedResult(200, title if isinstance(title, str) else None)
