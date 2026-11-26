"""Stories (LibriVox audiobooks) and doing what people ask for in conversation."""

import asyncio
import random
import time
import types
from dataclasses import replace

import pytest

from syke import ai
from syke import stories as st
from syke.music import Music
from syke.music_sources import Track

import httpx

NS = types.SimpleNamespace
REAL_CLIENT = httpx.AsyncClient
GID = 42

API_BOOK = {"books": [
    {"id": "1", "title": "The Tell-Tale Heart", "language": "English", "totaltimesecs": 1200,
     "url_librivox": "https://librivox.org/the-tell-tale-heart/", "url_rss": "",
     "authors": [{"first_name": "Edgar Allan", "last_name": "Poe"}],
     "sections": [{"section_number": "2", "title": "Part two", "listen_url": "https://archive.org/b.mp3",
                   "playtime": "600", "readers": [{"display_name": "Ann"}]},
                  {"section_number": "1", "title": "Part one", "listen_url": "https://archive.org/a.mp3",
                   "playtime": "10:00", "readers": [{"display_name": "Ann"}]}]},
    {"id": "2", "title": "War and Peace", "language": "English", "totaltimesecs": 220000, "authors": [],
     "sections": [{"section_number": "1", "listen_url": "https://archive.org/w.mp3", "playtime": "3600"}]},
    {"id": "3", "title": "Der Sandmann", "language": "German", "totaltimesecs": 1500, "authors": [],
     "sections": [{"section_number": "1", "listen_url": "https://archive.org/s.mp3", "playtime": "1500"}]},
]}


def test_books_chapters_and_picking():
    books = st.parse_books(API_BOOK)
    poe = books[0]
    assert poe.authors == "Edgar Allan Poe" and [c["url"] for c in poe.chapters] == ["https://archive.org/a.mp3",
                                                                                     "https://archive.org/b.mp3"]
    assert poe.chapters[0]["seconds"] == 600
    for seed in range(10):
        assert st.pick_story(books, random.Random(seed)).title == "The Tell-Tale Heart"   # English, story-sized
    tracks = st.tracks_for(poe, 7)
    assert tracks[0].title == "The Tell-Tale Heart · Part one" and tracks[0].source == "librivox"
    assert tracks[0].uploader == "read by Ann" and tracks[0].requester == 7


def test_rss_chapters_and_genre_names():
    xml = ("<rss><item><title><![CDATA[01 - The Raven]]></title><enclosure url=\"https://archive.org/r.mp3\" "
           "type=\"audio/mpeg\"/><itunes:duration>00:08:30</itunes:duration></item></rss>")
    assert st.rss_chapters(xml) == [{"n": 1, "title": "01 - The Raven", "url": "https://archive.org/r.mp3",
                                     "seconds": 510, "reader": ""}]
    assert st.genre_key("scary story") == "horror" and st.genre_key("sci-fi") == "scifi"
    assert st.genre_key("bedtime") == "kids" and st.genre_key("the raven") is None


IA_SEARCH = {"response": {"numFound": 2, "docs": [
    {"identifier": "tell_tale_heart_librivox", "title": "The Tell-Tale Heart", "creator": "Edgar Allan Poe",
     "runtime": "00:20:00", "language": "eng"},
    {"identifier": "empty_item", "title": "Ghost Stories", "creator": ["A", "B"], "runtime": "00:30:00",
     "language": "English"}]}}
IA_META = {"files": [
    {"name": "heart_02_poe_64kb.mp3", "format": "64Kbps MP3", "track": "02", "title": "Part two", "length": "600.5",
     "artist": "Ann"},
    {"name": "heart_01_poe_64kb.mp3", "format": "64Kbps MP3", "track": "01/02", "title": "Part one",
     "length": "10:00", "artist": "Ann"},
    {"name": "heart_01_poe.mp3", "format": "128Kbps MP3", "track": "01", "title": "Part one"},
    {"name": "heart.png", "format": "PNG"}]}


def test_archive_org_search_and_chapters():
    books = st.parse_ia_search(IA_SEARCH)
    assert books[0].ia == "tell_tale_heart_librivox" and books[0].seconds == 1200 and books[0].language == "English"
    assert books[1].authors == "A, B"
    chapters = st.ia_chapters("tell_tale_heart_librivox", IA_META)
    assert [c["url"] for c in chapters] == [                      # one bitrate, in track order
        "https://archive.org/download/tell_tale_heart_librivox/heart_01_poe_64kb.mp3",
        "https://archive.org/download/tell_tale_heart_librivox/heart_02_poe_64kb.mp3"]
    assert [c["seconds"] for c in chapters] == [600, 600] and chapters[0]["reader"] == "Ann"
    q = st.ia_query("horror")
    assert "collection:(librivoxaudio)" in q and '"ghost stories"' in q
    assert 'title:("the" AND "raven")' in st.ia_query(title='the "raven"!')


def _web(monkeypatch, *, archive_up=True, librivox_up=False):
    """Fake internet: archive.org and LibriVox answer from the fixtures above, or with a server error."""
    import httpx
    seen = []

    def handle(request):
        url = str(request.url)
        seen.append(url)
        if "archive.org" in url and not archive_up or "librivox.org" in url and not librivox_up:
            return httpx.Response(500, text="Internal Server Error")
        if "advancedsearch" in url:
            return httpx.Response(200, json=IA_SEARCH)
        if "/metadata/empty_item" in url:
            return httpx.Response(200, json={"files": []})
        if "/metadata/" in url:
            return httpx.Response(200, json=IA_META)
        if "librivox.org/api" in url:
            return httpx.Response(200, json=API_BOOK)
        return httpx.Response(404)

    monkeypatch.setattr(st.httpx, "AsyncClient",
                        lambda **k: REAL_CLIENT(transport=httpx.MockTransport(handle), **k))
    return seen


def _story_ctx(cog, monkeypatch):
    queued, replies = [], []

    class FakeMusic:
        async def enqueue(self, ctx, tracks, name, source_link=""):
            queued.append((name, [t.url for t in tracks], source_link))

    monkeypatch.setattr(cog.bot, "get_cog", lambda name: FakeMusic() if name == "Music" else None)

    async def reply(*a, **k):
        replies.append(a[0] if a else k.get("embed"))

    ctx = NS(author=NS(id=7, voice=NS(channel=NS(id=1))), guild=NS(id=GID), reply=reply, clean_prefix="$")
    return ctx, queued, replies


async def test_story_by_genre_works_while_librivox_is_down(cog, monkeypatch):
    seen = _web(monkeypatch, librivox_up=False)
    ctx, queued, replies = _story_ctx(cog, monkeypatch)
    stories = st.Stories(cog.bot)
    for _ in range(4):                                  # whichever item is picked first, a playable one is queued
        stories._cache.clear()
        queued.clear()
        assert await stories.play_story(ctx, "scary story")
        name, urls, link = queued[0]
        assert name == "The Tell-Tale Heart by Edgar Allan Poe" and len(urls) == 2
        assert link == "https://archive.org/details/tell_tale_heart_librivox"
    assert not any("librivox.org/api" in u for u in seen)    # genres never touch LibriVox's broken filter
    no_voice = NS(author=NS(id=8, voice=None), guild=NS(id=GID), reply=ctx.reply, clean_prefix="$")
    assert not await stories.play_story(no_voice, "horror") and "voice channel" in replies[-1]


async def test_story_title_falls_back_to_librivox_and_says_so_only_when_all_fail(cog, monkeypatch):
    ctx, queued, replies = _story_ctx(cog, monkeypatch)
    stories = st.Stories(cog.bot)
    seen = _web(monkeypatch, archive_up=False, librivox_up=True)
    assert await stories.play_story(ctx, "the tell-tale heart")
    assert queued[-1][1] == ["https://archive.org/a.mp3", "https://archive.org/b.mp3"]
    assert any("title=%5Ethe+tell-tale+heart" in u or "title=%5Ethe%20tell-tale%20heart" in u for u in seen)
    _web(monkeypatch, archive_up=False, librivox_up=False)
    assert not await stories.play_story(ctx, "the tell-tale heart")
    assert "isn't answering" in replies[-1]


async def test_story_chapters_never_fall_back_to_youtube():
    music = Music.__new__(Music)
    loads = []

    async def ll_load(identifier):
        loads.append(identifier)
        return [], None, "not found"

    music.ll_load = ll_load
    with pytest.raises(LookupError):
        await music.ll_resolve(Track(title="Book · Part 1", url="https://archive.org/a.mp3", source="librivox"))
    assert loads == ["https://archive.org/a.mp3"]                          # no ytsearch:/scsearch: guesses


# Actions from conversation

class FakeMusic:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("do_") or name == "play_query":
            async def call(ctx, *args):
                self.calls.append((name, args))
            return call
        raise AttributeError(name)


def _message(content, guild=True):
    sent = []

    async def send(text=None, **k):
        sent.append(text)

    async def reply(text=None, **k):
        sent.append(("reply", text))

    author = NS(id=7, send=send, display_name="Mo", name="mo")
    return NS(content=content, author=author, guild=NS(id=GID) if guild else None, reply=reply, sent=sent)


@pytest.fixture()
def acting(cog, monkeypatch):
    bot = cog.bot
    music = FakeMusic()
    monkeypatch.setattr(bot, "get_cog", lambda name: music if name == "Music" else None)

    async def get_context(message):
        return NS(author=message.author, guild=message.guild)

    monkeypatch.setattr(bot, "get_context", get_context)
    return bot, music


async def test_actions_run_only_when_asked(acting):
    bot, music = acting
    assert await bot.run_action(_message("syke hop in vc"), {"do": "join"}, "syke hop in vc") == "join"
    assert await bot.run_action(_message("x"), {"do": "play", "what": "drake"}, "syke play some drake") == "play"
    assert await bot.run_action(_message("x"), {"do": "skip"}, "skip this pls") == "skip"
    assert music.calls == [("do_join", ()), ("play_query", ("drake",)), ("do_skip", ())]
    # The AI decides whether someone is really asking; the word check is only a safety net for when it's wrong:
    # it blocks actions with nothing in the message that could ask for them.
    assert await bot.run_action(_message("x"), {"do": "join"}, "lmao that's crazy") is None
    assert await bot.run_action(_message("x"), {"do": "leave"}, "i love this song") is None
    assert await bot.run_action(_message("x"), {"do": "explode"}, "explode") is None
    assert await bot.run_action(_message("x"), None, "play drake") is None
    assert len(music.calls) == 3


async def test_dm_me_goes_only_to_the_person_who_asked(acting):
    bot, music = acting
    m = _message("syke dm me the song name")
    assert await bot.run_action(m, {"do": "dm", "what": "it was take turns by zayn 🎧"}, m.content) == "dm"
    assert m.sent == ["it was take turns by zayn 🎧"]
    assert await bot.run_action(m, {"do": "dm", "what": "again"}, m.content) is None   # not again so soon
    closed = _message("dm me")

    async def refuse(*a, **k):
        import discord
        raise discord.Forbidden(NS(status=403, reason="closed"), "Cannot send messages to this user")

    closed.author.send = refuse
    closed.author.id = 8
    assert await bot.run_action(closed, {"do": "dm", "what": "hey"}, "dm me") is None
    assert closed.sent == [("reply", "your dms are closed 💀")]


async def test_reply_prompt_offers_actions(cog, monkeypatch):
    bot = cog.bot
    bot.settings = replace(bot.settings, ai_provider="openai", openai_api_key="k")
    bot._voices[GID] = (time.monotonic(), __import__("syke.bot", fromlist=["Voice"]).Voice(chain=None, emojis=[],
                                                                                           gifs=[]))
    bot.storage.add_corpus(GID, [(1, 10, 100, "who up rn"), (2, 10, 101, "this chat is cooked")])
    seen = {}

    async def fake(s, system, user, temperature=1.0, timeout=30, model=None, images=None):
        seen["system"] = system
        return '{"reply": "bet, hopping in", "action": {"do": "join", "what": ""}}'

    monkeypatch.setattr(ai, "_call_openai", fake)

    class Ch:
        id = 10

        def history(self, limit=None, before=None):
            async def gen():
                if False:
                    yield None
            return gen()

    m = NS(id=5, content="syke join vc", channel=Ch(), guild=NS(id=GID, emojis=[], stickers=[]),
           author=NS(id=7, display_name="Mo", name="mo", bot=False), reference=None, attachments=[], stickers=[],
           embeds=[], mentions=[])
    turn = await bot.build_yap(m, target="join vc")
    assert turn["action"] == {"do": "join", "what": ""} and "ACTIONS" in seen["system"]
