"""Fake RSS / Atom feeds for the briefing and news tests (served through httpx.MockTransport, no network)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

CBC_TOP = "https://www.cbc.ca/webfeed/rss/rss-topstories"
CBC_HAMILTON = "https://www.cbc.ca/webfeed/rss/rss-canada-hamiltonnews"
BBC_WORLD = "https://feeds.bbci.co.uk/news/world/rss.xml"
SCIENCE = "https://science.example.org/atom.xml"


def rss(channel: str, titles: list[str]) -> bytes:
    items = "".join(f"<item><title><![CDATA[{t}]]></title><link>https://x.example/{i}</link>"
                    f"<description>&lt;p&gt;Long text {i}&lt;/p&gt;</description></item>" for i, t in enumerate(titles))
    return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">'
            f"<channel><title>{channel}</title><link>https://x.example</link>{items}</channel></rss>").encode()


def atom(feed_title: str, titles: list[str]) -> bytes:
    entries = "".join(f'<entry><title type="html">{t}</title><id>urn:{i}</id></entry>' for i, t in enumerate(titles))
    return (f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>{feed_title}</title>'
            f"{entries}</feed>").encode()


FEEDS: dict[str, bytes] = {
    CBC_TOP: rss("CBC | Top Stories News", ["Ontario announces new transit funding",
                                           "Leafs win in overtime &amp; fans cheer", "Snowfall warning for the north"]),
    CBC_HAMILTON: rss("CBC | Hamilton News", ["Hamilton council approves LRT station designs",
                                             "New bakery opens on James Street North"]),
    BBC_WORLD: rss("BBC News - World", ["Election results due in Brazil", "Scientists spot a new comet",
                                        "Ontario announces new transit funding"]),
    SCIENCE: atom("Example Science - Daily", ["Comet &lt;b&gt;C/2026&lt;/b&gt; brightens", "Bees can count"]),
}


class FakeFeeds:
    """httpx handler: the feeds above; ``status`` per URL (e.g. 500); ``delay`` per URL in seconds."""

    def __init__(self, feeds: dict[str, bytes] | None = None, *, status: dict[str, int] | None = None,
                 delay: dict[str, float] | None = None) -> None:
        self.feeds = dict(FEEDS if feeds is None else feeds)
        self.status = status or {}
        self.delay = delay or {}
        self.requests: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if self.delay.get(url):
            await asyncio.sleep(self.delay[url])
        if url in self.status:
            return httpx.Response(self.status[url], text="nope")
        body = self.feeds.get(url)
        if body is None:
            return httpx.Response(404, text="not here")
        return httpx.Response(200, content=body, headers={"Content-Type": "application/rss+xml"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def online_context(feeds: list[str] | None = None, **overrides: Any) -> Any:
    from littlebot.testing import make_test_context

    skills = {"news_feeds": feeds if feeds is not None else [CBC_TOP, CBC_HAMILTON, BBC_WORLD]}
    skills.update(overrides.pop("skills", {}))
    ctx = make_test_context(skills=skills, **overrides)
    if not ctx.config.is_offline:
        ctx.privacy.set(cloud_enabled=True)
    return ctx
