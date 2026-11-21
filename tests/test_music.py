"""Music: link parsing, song-name lookups for Spotify/Apple/Deezer, FFmpeg settings, and the queue engine."""

import asyncio
import json
import time
import types
from dataclasses import replace

import discord
import pytest

from syke import music, music_sources as src
from syke.bot import Syke
from syke.config import load_settings
from syke.music_sources import Track

NS = types.SimpleNamespace


# Links and lookups

@pytest.mark.parametrize("query,kind", [
    ("https://open.spotify.com/intl-de/track/4uLU6hMCjMI75M1A2tKUQC?si=x", "spotify"),
    ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "spotify"),
    ("https://spotify.link/abc123", "short"),
    ("https://music.apple.com/us/album/x/123?i=456", "apple"),
    ("https://www.deezer.com/de/track/3135556", "deezer"),
    ("<https://www.youtube.com/watch?v=dQw4w9WgXcQ>", "url"),
    ("https://soundcloud.com/artist/song", "url"),
    ("never gonna give you up", "search"),
])
def test_classify(query, kind):
    assert src.classify(query)[0] == kind


def test_spotify_playlist_and_track_pages():
    def page(entity):
        data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
        return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'

    name, tracks = src.spotify_tracks(page({"name": "Late Night", "trackList": [
        {"title": "Blinding Lights", "subtitle": "The Weeknd", "duration": 200040},
        {"title": "Nights", "subtitle": "Frank Ocean", "duration": 307000}]}), "playlist")
    assert name == "Late Night" and len(tracks) == 2
    assert tracks[0].query == "The Weeknd - Blinding Lights" and tracks[0].duration == pytest.approx(200.04)
    assert tracks[0].label == "Blinding Lights — The Weeknd" and not tracks[0].url
    _, (one,) = src.spotify_tracks(page({"name": "Song", "artists": [{"name": "A"}, {"name": "B"}],
                                         "duration": 1000}), "track")
    assert one.query == "A, B - Song"
    assert src.spotify_tracks("<html></html>", "track") == ("", [])


def test_apple_music_schema_data():
    ld = {"@type": "MusicAlbum", "name": "Album", "byArtist": {"name": "Band"},
          "tracks": [{"@type": "MusicRecording", "name": "One", "duration": "PT3M5S"},
                     {"@type": "MusicRecording", "name": "Two", "byArtist": [{"name": "Guest"}]}]}
    page = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    name, tracks = src.apple_tracks(page)
    assert name == "Album" and [t.query for t in tracks] == ["Band - One", "Guest - Two"]
    assert tracks[0].duration == 185
    _, (fallback,) = src.apple_tracks('<meta property="og:title" content="Song by Singer">')
    assert fallback.query == "Song - Singer"


def test_deezer_api_shapes():
    name, (track,) = src.deezer_tracks({"title": "Hey", "artist": {"name": "Jude"}, "duration": 200}, "track")
    assert track.query == "Jude - Hey"
    name, tracks = src.deezer_tracks({"title": "List", "tracks": {"data": [
        {"title": "a", "artist": {"name": "x"}, "duration": 1}] * 3}}, "playlist")
    assert name == "List" and len(tracks) == 3


def test_times():
    assert src.parse_time("1:30") == 90 and src.parse_time("90") == 90 and src.parse_time("1:02:03") == 3723
    assert src.parse_time("1m30s") == 90 and src.parse_time("abc") is None
    assert src.duration_text(65) == "1:05" and src.duration_text(3725) == "1:02:05" and src.duration_text(None) == "LIVE"
    assert music.progress_bar(50, 100, 10).index("●") == 5 and music.progress_bar(0, None) == "🔴 LIVE"


def test_stream_details_and_errors():
    info = {"_type": "playlist", "entries": [{"url": "https://cdn/a.webm", "acodec": "opus", "title": "Song",
                                              "webpage_url": "https://youtu.be/x", "duration": 100,
                                              "http_headers": {"User-Agent": "UA", "Accept": "*/*"}}]}
    track = src.fill_stream(Track(title="", query="artist - song"), info)
    assert track.stream == "https://cdn/a.webm" and track.acodec == "opus" and track.title == "Song"
    assert track.headers == {"User-Agent": "UA"} and track.url == "https://youtu.be/x"
    assert not track.fresh()  # YouTube addresses are never streamed: the song gets downloaded first
    with pytest.raises(LookupError):
        src.fill_stream(Track(title="x"), {"_type": "playlist", "entries": []})
    assert "cookies" in src.clean_error(Exception("ERROR: Sign in to confirm you're not a bot"))
    assert Track(title="x", url="https://www.youtube.com/watch?v=abc&t=3").video_id() == "abc"
    assert Track(title="x", url="https://youtu.be/xyz").video_id() == "xyz"


def test_ffmpeg_passthrough_and_effects():
    track = Track(title="x", stream="https://cdn/a.webm", acodec="opus", headers={"User-Agent": "UA"})
    before, options, codec = src.ffmpeg_args(track)
    assert codec == "opus" and options == "-vn"  # Opus straight through: no re-encode
    assert "-reconnect 1" in before and "-ss" not in before and "User-Agent: UA" in before
    before, options, codec = src.ffmpeg_args(track, position=30, volume=50, filters=src.FILTERS["bassboost"])
    assert codec is None and "-ss 30.00" in before and "bass=g=8" in options and "volume=0.50" in options
    live = Track(title="x", stream="https://radio/stream", acodec="aac", is_live=True)
    before, _, codec = src.ffmpeg_args(live, position=30)
    assert "-ss" not in before and codec is None


# The queue engine

class FakeSource:
    def __init__(self, stream, *, before_options="", options="", codec=None, bitrate=None):
        self.stream, self.before, self.options, self.codec = stream, before_options, options, codec


class FakeVC:
    def __init__(self, channel):
        self.channel = channel
        self.playing = self.paused = False
        self.after = None
        self.sources = []

    def play(self, source, after):
        self.sources.append(source)
        self.playing, self.paused, self.after = True, False, after

    def stop(self):
        if self.playing or self.paused:
            self.playing = self.paused = False
            self.after(None)

    def finish(self):  # the song ends on its own
        self.playing = False
        self.after(None)

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return self.paused

    def pause(self):
        self.playing, self.paused = False, True

    def resume(self):
        self.playing, self.paused = True, False


class TextChannel:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        msg = NS(kwargs=kwargs, delete=_noop, edit=_noop)
        self.sent.append(msg)
        return msg


async def _noop(*args, **kwargs):
    return None


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    settings = replace(load_settings(), db_path=str(tmp_path / "m.db"), ai_provider="none", web_enabled=False)
    bot = Syke(settings)
    cog = music.Music(bot)
    text = TextChannel()
    voice = NS(id=70, bitrate=96000, members=[], mention="<#70>")
    vc = FakeVC(voice)
    guild = NS(id=1, voice_client=vc, get_channel_or_thread=lambda i: text)
    failing: set[str] = set()

    async def resolve(track):
        if track.title in failing:
            raise LookupError("gone")
        track.stream, track.acodec, track.resolved_at = f"https://cdn/{track.title}", "opus", time.time()
        return track

    monkeypatch.setattr(src, "resolve_stream", resolve)
    monkeypatch.setattr(discord, "FFmpegOpusAudio", FakeSource)
    player = cog.player(guild)
    player.channel_id = 1
    yield NS(cog=cog, player=player, vc=vc, text=text, failing=failing)
    player._cancel_idle()


def songs(*names):
    return [Track(title=n, url=f"https://youtu.be/{n}", duration=200) for n in names]


async def test_queue_plays_in_order_and_goes_idle(rig):
    p, vc = rig.player, rig.vc
    p.queue += songs("a", "b", "c")
    await p.start_or_queue()
    assert p.current.title == "a" and vc.sources[-1].codec == "opus"
    p.t0 -= 200  # pretend the whole song played
    vc.finish()
    await settle()
    assert p.current.title == "b"
    p.skip()
    await settle()
    assert p.current.title == "c" and [t.title for t in p.history] == ["a", "b"]
    p.t0 -= 200
    vc.finish()
    await settle()
    assert p.current is None and p.idle_task is not None


async def test_loop_track_repeats_but_skip_moves_on(rig):
    p, vc = rig.player, rig.vc
    p.queue += songs("a", "b")
    await p.start_or_queue()
    p.loop = "track"
    p.t0 -= 200
    vc.finish()
    await settle()
    assert p.current.title == "a" and len(vc.sources) == 2
    p.skip()
    await settle()
    assert p.current.title == "b"


async def test_loop_queue_recycles(rig):
    p, vc = rig.player, rig.vc
    p.queue += songs("a", "b")
    await p.start_or_queue()
    p.loop = "queue"
    p.t0 -= 200
    vc.finish()
    await settle()
    assert p.current.title == "b" and [t.title for t in p.queue] == ["a"]


async def test_unplayable_songs_are_skipped_with_a_note(rig):
    p = rig.player
    rig.failing.update({"a", "b"})
    p.queue += songs("a", "b", "c")
    await p.start_or_queue()
    assert p.current.title == "c"
    notes = [m.kwargs["embed"].description for m in rig.text.sent if m.kwargs.get("embed") and
             m.kwargs["embed"].title == "Skipped a song"]
    assert len(notes) == 2 and "gone" in notes[0]


async def test_seek_volume_and_dropped_streams_resume_in_place(rig):
    p, vc = rig.player, rig.vc
    p.queue += songs("a")
    await p.start_or_queue()
    p.restart(30)
    await settle()
    assert p.current.title == "a" and "-ss 30.00" in vc.sources[-1].before
    p.volume = 50
    p.restart()
    await settle()
    assert "volume=0.50" in vc.sources[-1].options and vc.sources[-1].codec is None
    vc.finish()  # dies at ~30s of a 200s song: look it up again and carry on
    await settle()
    assert p.current.title == "a" and "-ss 30" in vc.sources[-1].before and p.retries == 1


async def test_stop_clears_everything(rig):
    p, vc = rig.player, rig.vc
    p.queue += songs("a", "b")
    await p.start_or_queue()
    p.stop()
    await settle()
    assert p.current is None and not p.queue and len(vc.sources) == 1


async def test_pause_freezes_the_clock(rig):
    p = rig.player
    p.queue += songs("a")
    await p.start_or_queue()
    p.t0 -= 10
    assert p.pause()
    frozen = p.position()
    p.paused_at -= 5  # time passes while paused
    p.t0 -= 0
    assert p.resume()
    assert abs(p.position() - frozen) < 6


def test_dj_rules(rig):
    cog, vc = rig.cog, rig.vc
    guild = NS(id=1, voice_client=vc)
    listener = NS(id=5, voice=NS(channel=vc.channel), roles=[], guild_permissions=discord.Permissions.none(), bot=False)
    elsewhere = NS(id=6, voice=None, roles=[], guild_permissions=discord.Permissions.none(), bot=False)
    vc.channel.members = [listener, NS(bot=False), NS(bot=True)]
    assert cog.control_problem(guild, listener) is None
    assert "Join" in cog.control_problem(guild, elsewhere)
    cog.bot.storage.update_config(1, music.CONFIG, dj=77)
    assert "<@&77>" in cog.control_problem(guild, listener)
    listener.roles = [NS(id=77)]
    assert cog.control_problem(guild, listener) is None


async def test_lookup_routes_each_kind(rig, monkeypatch):
    cog = rig.cog
    seen = []

    async def fake_text(url):
        seen.append(url)
        data = {"props": {"pageProps": {"state": {"data": {"entity": {
            "name": "Mix", "trackList": [{"title": "Song", "subtitle": "Artist", "duration": 1000}]}}}}}}
        return url, f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'

    extracted = []

    async def fake_extract(target, flat=False):
        extracted.append((target, flat))
        if target.startswith("ytsearch1:"):
            assert flat  # a light search: no player request
            return {"_type": "playlist", "entries": [{"title": "Found", "url": "https://www.youtube.com/watch?v=f",
                                                      "ie_key": "Youtube", "id": "f", "duration": 5}]}
        if "list=PL" in target:
            return {"_type": "playlist", "title": "My list", "entries": [
                {"_type": "url", "ie_key": "Youtube", "id": "v1", "url": "https://www.youtube.com/watch?v=v1",
                 "title": "One", "duration": 10}, None]}
        return {"title": "Single", "url": "https://cdn/s", "acodec": "opus", "webpage_url": target}

    async def fake_json(url):
        assert "oembed" in url
        return {"title": "Single", "author_name": "Rick"}

    monkeypatch.setattr(cog, "fetch_text", fake_text)
    monkeypatch.setattr(cog, "fetch_json", fake_json)
    monkeypatch.setattr(src, "extract", fake_extract)
    tracks, name = await cog.lookup("https://open.spotify.com/playlist/abc", 7)
    assert name == "Mix" and tracks[0].query == "Artist - Song" and tracks[0].requester == 7
    assert seen[-1] == "https://open.spotify.com/embed/playlist/abc"
    tracks, name = await cog.lookup("https://www.youtube.com/watch?v=xyz&list=RDxyz&index=2", 7)
    assert name is None and tracks[0].title == "Single" and tracks[0].url.endswith("v=xyz") and not tracks[0].stream
    tracks, name = await cog.lookup("https://www.youtube.com/playlist?list=PL123", 7)
    assert name == "My list" and [t.url for t in tracks] == ["https://www.youtube.com/watch?v=v1"]
    tracks, name = await cog.lookup("lofi beats", 7)
    assert tracks[0].title == "Found" and tracks[0].url.endswith("v=f")


async def test_youtube_songs_download_and_fall_back_to_soundcloud(monkeypatch, tmp_path):
    song = tmp_path / "abc.webm"
    song.write_bytes(b"x")
    calls = []

    def fake_download(target):
        calls.append(target)
        if "blocked" in target:
            raise Exception("ERROR: Sign in to confirm you're not a bot")
        return {"_local_path": str(song), "acodec": "opus", "title": "Real Title", "url": "https://gv/x",
                "webpage_url": "https://www.youtube.com/watch?v=abc", "duration": 200}

    async def fake_extract(target, flat=False):
        calls.append(target)
        return {"_type": "playlist", "entries": [{"url": "https://sc/stream.mp3", "acodec": "mp3", "title": "SC"}]}

    monkeypatch.setattr(src, "_download", fake_download)
    monkeypatch.setattr(src, "extract", fake_extract)
    track = await src.resolve_stream(Track(title="https://www.youtube.com/watch?v=abc", url="https://www.youtube.com/watch?v=abc"))
    assert track.local and track.stream == str(song) and track.fresh() and track.title == "Real Title"
    before, options, codec = src.ffmpeg_args(track)
    assert "-reconnect" not in before and codec == "opus"
    spotify = await src.resolve_stream(Track(title="Song — Artist", query="Artist - Song"))
    assert spotify.local and calls[-1] == "ytsearch1:Artist - Song"
    blocked = await src.resolve_stream(Track(title="Blocked Song", url="https://www.youtube.com/watch?v=blocked"))
    assert not blocked.local and blocked.stream == "https://sc/stream.mp3" and calls[-1] == "scsearch1:Blocked Song"


def test_cards_link_and_colour_by_source(rig):
    p = rig.player
    p.guild.get_member = lambda i: None
    track = Track(title="Blinding Lights — The Weeknd", query="The Weeknd - Blinding Lights", url="https://youtu.be/x",
                  origin="https://open.spotify.com/track/abc", source="Spotify", duration=200, requester=5)
    p.current = track
    p.queue = [Track(title="Next [Remix]", url="https://soundcloud.com/a/b", source="soundcloud", duration=90)]
    card = p.now_card()
    assert card.url == "https://youtu.be/x" and card.colour == discord.Colour.from_str("#1DB954")
    fields = {f.name: f.value for f in card.fields}
    assert "(https://open.spotify.com/track/abc)" in fields["Source"]
    assert fields["Up next"].startswith("[Next (Remix)](https://soundcloud.com/a/b)")
    view = music.Controls(rig.cog, p.guild.id)
    assert any(getattr(b, "url", None) == "https://open.spotify.com/track/abc" for b in view.children)
    queue = rig.cog.queue_card(p, 0)
    assert "[Next (Remix)](https://soundcloud.com/a/b)" in queue.description
    assert music.style(Track(title="x", source="Youtube"))[0] == "YouTube"
    assert music.link(Track(title="plain")) == "**plain**"
    p.current = None
