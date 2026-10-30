"""News headlines for the briefing (littlebot/skills/news.py): RSS / Atom parsing, sources, mixing, the cache and
the time limit, the privacy gates. No network: httpx.MockTransport."""

from __future__ import annotations

import asyncio

import pytest
from news_fakes import BBC_WORLD, CBC_HAMILTON, CBC_TOP, FEEDS, SCIENCE, FakeFeeds, online_context, rss

from littlebot.skills import news
from littlebot.skills.news import Headline, NewsError, NewsFeeds, mix, parse_feed, source_name, topic_words


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def ctx():
    c = online_context()
    yield c
    c.db.close()


def feeds(ctx, web: FakeFeeds, **kw) -> NewsFeeds:
    return NewsFeeds(ctx, transport=web.transport(), **kw)


# ------------------------------------------------------------------ parsing
def test_rss_titles_are_clean_and_have_their_source():
    found = parse_feed(FEEDS[CBC_TOP], CBC_TOP)
    assert [h.title for h in found] == ["Ontario announces new transit funding", "Leafs win in overtime & fans cheer",
                                        "Snowfall warning for the north"]
    assert {h.source for h in found} == {"CBC"}
    assert parse_feed(FEEDS[CBC_HAMILTON], CBC_HAMILTON)[0].source == "CBC Hamilton"
    assert parse_feed(FEEDS[BBC_WORLD], BBC_WORLD)[0].source == "BBC World"


def test_atom_and_rss_1_feeds():
    found = parse_feed(FEEDS[SCIENCE], SCIENCE)
    assert [h.title for h in found] == ["Comet C/2026 brightens", "Bees can count"]  # markup in a title dropped
    assert found[0].source == "Example Science"
    rdf = (b'<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
           b'xmlns="http://purl.org/rss/1.0/"><channel><title>Old Times</title></channel>'
           b'<item><title>First old story</title></item><item><title>Second old story</title></item></rdf:RDF>')
    assert [(h.title, h.source) for h in parse_feed(rdf, "https://old.example.com/rss")] == [
        ("First old story", "Old Times"), ("Second old story", "Old Times")]


def test_bad_feeds_give_nothing():
    assert parse_feed(b"", "u") == []
    assert parse_feed(b"<html><body>not a feed", "u") == []
    assert parse_feed(b"{\"json\": true}", "u") == []
    bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
            b"<rss><channel><item><title>&lol2;</title></item></channel></rss>")
    assert parse_feed(bomb, "u") == []  # entity declarations are refused outright
    long = rss("X", ["word " * 80, "", "Same", "same!"])
    titles = [h.title for h in parse_feed(long, "https://x.example")]
    assert len(titles[0]) <= news.MAX_TITLE + 3 and titles[0].endswith("...") and titles[1:] == ["Same"]


def test_source_names():
    assert source_name("https://www.theguardian.com/world/rss", "World news | The Guardian") == "The Guardian"
    assert source_name("https://www.theguardian.com/world/rss", "The Guardian | Sport") == "The Guardian Sport"
    assert source_name("https://www.cbc.ca/x", "CBC | Top Stories News") == "CBC"
    assert source_name("https://blog.example.net/feed", "") == "blog.example.net"
    assert source_name("not a url", "") == "the news"


def test_mix_takes_turns_and_drops_repeats():
    a = [Headline("A1", "A"), Headline("Same story", "A"), Headline("A3", "A")]
    b = [Headline("same story!", "B"), Headline("B2", "B")]
    assert [h.title for h in mix([a, b], 10)] == ["A1", "same story!", "B2", "A3"]
    assert [h.title for h in mix([a, b], 2)] == ["A1", "same story!"]
    assert mix([], 3) == []


def test_topic_words_and_matching():
    assert topic_words("the news about the Leafs") == ["leafs"]
    assert topic_words("2026 election") == ["2026", "election"]
    h = Headline("Elections called in Ontario", "CBC")
    assert news.about(h, ["election"]) and not news.about(h, ["lect"]) and news.about(h, ["ontario"])


# ------------------------------------------------------------------ fetching
async def test_headlines_are_mixed_across_feeds_and_cached(ctx):
    web = FakeFeeds()
    clock = Clock()
    svc = feeds(ctx, web, now=clock)
    got = await svc.headlines(5)
    assert [(h.source, h.title) for h in got] == [
        ("CBC", "Ontario announces new transit funding"), ("CBC Hamilton", "Hamilton council approves LRT station "
                                                                           "designs"),
        ("BBC World", "Election results due in Brazil"), ("CBC", "Leafs win in overtime & fans cheer"),
        ("CBC Hamilton", "New bakery opens on James Street North")]
    assert len(web.requests) == 3
    await svc.headlines(3)
    assert len(web.requests) == 3  # cached
    clock.t += news.CACHE_S + 1
    await svc.headlines(3)
    assert len(web.requests) == 6
    assert [h.title for h in svc.cached(2)] == [got[0].title, got[1].title]
    await svc.aclose()


async def test_topic_filter(ctx):
    svc = feeds(ctx, FakeFeeds())
    got = await svc.headlines(5, "comet")
    assert [h.title for h in got] == ["Scientists spot a new comet"]
    assert await svc.headlines(5, "cricket") == []
    await svc.aclose()


async def test_a_slow_feed_doesnt_hold_the_answer_and_is_cached_for_next_time(ctx):
    web = FakeFeeds(delay={BBC_WORLD: 0.3})
    svc = feeds(ctx, web, timeout=0.1)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    got = await svc.headlines(10)
    assert loop.time() - t0 < 0.25
    assert "BBC World" not in {h.source for h in got} and got
    await asyncio.sleep(0.35)
    got = await svc.headlines(10)
    assert "BBC World" in {h.source for h in got}
    await svc.aclose()


async def test_failures(ctx, monkeypatch):
    web = FakeFeeds(status={CBC_TOP: 500, CBC_HAMILTON: 503, BBC_WORLD: 403})
    clock = Clock()
    svc = feeds(ctx, web, now=clock)
    with pytest.raises(NewsError) as e:
        await svc.headlines(3)
    assert e.value.kind == "unavailable" and "couldn't be reached" in str(e.value)
    n = len(web.requests)
    with pytest.raises(NewsError):
        await svc.headlines(3)
    assert len(web.requests) == n  # failed feeds wait RETRY_S before they are asked again
    clock.t += news.RETRY_S + 1
    web.status = {}
    assert await svc.headlines(3)
    # a feed failing later: its last copy is used, however old
    clock.t += news.CACHE_S + 1
    web.status = {CBC_TOP: 500, CBC_HAMILTON: 500, BBC_WORLD: 500}
    assert [h.source for h in await svc.headlines(3)] == ["CBC", "CBC Hamilton", "BBC World"]
    await svc.aclose()
    # too big a feed is dropped
    monkeypatch.setattr(news, "MAX_BYTES", 200)
    big = feeds(ctx, FakeFeeds())
    with pytest.raises(NewsError):
        await big.headlines(3)
    await big.aclose()


async def test_gates():
    off = online_context(mode="offline")
    try:
        with pytest.raises(NewsError) as e:
            await NewsFeeds(off, transport=FakeFeeds().transport()).headlines()
        assert e.value.kind == "offline"
    finally:
        off.db.close()
    ctx = online_context()
    try:
        ctx.privacy.set(cloud_enabled=False)
        web = FakeFeeds()
        with pytest.raises(NewsError) as e:
            await NewsFeeds(ctx, transport=web.transport()).headlines()
        assert e.value.kind == "cloud_off" and web.requests == []
    finally:
        ctx.db.close()
    none = online_context(feeds=["ftp://nope", "", "javascript:alert(1)"])
    try:
        assert NewsFeeds(none).feeds() == []
        with pytest.raises(NewsError) as e:
            await NewsFeeds(none).headlines()
        assert e.value.kind == "no_feeds"
    finally:
        none.db.close()
