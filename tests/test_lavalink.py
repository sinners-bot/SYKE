"""Lavalink: the voice handoff (with the channel id DAVE needs), events, lookups and the player on top."""

import asyncio
import types
from dataclasses import replace

import discord
import pytest

from syke import lavalink as ll, music
from syke.bot import Syke
from syke.config import load_settings
from syke.music_sources import Track

NS = types.SimpleNamespace


class FakeNode(ll.Node):
    def __init__(self):
        super().__init__(NS(), "http://lavalink:2333", "pw")
        self.session_id = "s1"
        self.ready.set()
        self.updates: list[tuple[int, dict]] = []
        self.loads: dict[str, dict] = {}
        self.loaded: list[str] = []

    async def update(self, guild_id, body, no_replace=False):
        self.updates.append((guild_id, body))
        return {}

    async def load(self, identifier):
        self.loaded.append(identifier)
        return self.loads.get(identifier, {"loadType": "empty", "data": {}})

    async def destroy(self, guild_id):
        self.updates.append((guild_id, {"destroyed": True}))


def lltrack(title, uri, source="youtube", length=200000, encoded=None):
    return {"encoded": encoded or f"enc-{title}", "info": {"title": title, "uri": uri, "author": "Artist",
                                                            "length": length, "isStream": False,
                                                            "sourceName": source, "artworkUrl": "https://art/x.jpg"}}


class Text:
    def __init__(self):
        self.sent = []

    async def send(self, *a, **k):
        msg = NS(kwargs=k, delete=_noop, edit=_noop)
        self.sent.append(msg)
        return msg


async def _noop(*a, **k):
    return None


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


def make_voice(node, guild):
    channel = NS(id=70, guild=guild, name="vc", members=[], mention="<#70>")
    client = NS(cogs={"Music": NS(node=node)}, _connection=NS(_remove_voice_client=lambda key: None))
    voice = ll.LavalinkVoice.__new__(ll.LavalinkVoice)
    discord.VoiceProtocol.__init__(voice, client, channel)
    ll.LavalinkVoice.__init__(voice, client, channel)
    return voice


async def test_voice_handoff_includes_the_channel_for_dave():
    node = FakeNode()
    guild = NS(id=1, get_channel=lambda i: None)
    voice = make_voice(node, guild)
    await voice.on_voice_state_update({"channel_id": "70", "session_id": "sess"})
    assert node.updates == []  # waits for the server details too
    await voice.on_voice_server_update({"token": "tok", "endpoint": "c-lhr.discord.media:443"})
    assert node.updates[-1] == (1, {"voice": {"token": "tok", "endpoint": "c-lhr.discord.media:443",
                                              "sessionId": "sess", "channelId": "70"}})
    assert voice.is_connected() and node.voices[1] is voice


async def test_track_events_drive_the_after_callback():
    node = FakeNode()
    voice = make_voice(node, NS(id=1, get_channel=lambda i: None))
    ended = []
    await voice.play_track("enc-a", position_ms=5000, volume=80, filters={"rotation": {"rotationHz": 0.2}},
                           after=lambda e: ended.append("a"))
    body = node.updates[-1][1]
    assert body["track"] == {"encoded": "enc-a"} and body["position"] == 5000 and body["volume"] == 80
    assert voice.is_playing()
    voice._event({"type": "TrackEndEvent", "reason": "replaced"})
    assert ended == [] and voice.is_playing()
    voice._event({"type": "TrackExceptionEvent", "exception": {"message": "This video requires login."}})
    voice._event({"type": "TrackEndEvent", "reason": "loadFailed"})
    assert ended == ["a"] and voice.last_end_reason == "loadFailed" and "login" in voice.last_exception
    voice._player_update({"position": 1234, "ping": 40, "connected": True})
    assert voice.position_ms == 1234 and voice.latency == 0.04


async def test_node_routes_messages():
    node = FakeNode()
    voice = make_voice(node, NS(id=1, get_channel=lambda i: None))
    node._handle_calls = []

    async def rest(*a, **k):
        return None

    node.rest = rest
    await node._handle({"op": "ready", "sessionId": "abc", "resumed": False})
    assert node.session_id == "abc" and node.available
    await node._handle({"op": "playerUpdate", "guildId": "1", "state": {"position": 999}})
    assert voice.position_ms == 999
    await node._handle({"op": "stats", "players": 3})
    assert node.stats["players"] == 3


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    settings = replace(load_settings(), db_path=str(tmp_path / "l.db"), ai_provider="none", web_enabled=False)
    bot = Syke(settings)
    cog = music.Music(bot)
    node = FakeNode()
    cog.node = node
    text = Text()
    guild = NS(id=1, get_channel=lambda i: None, get_channel_or_thread=lambda i: text, get_member=lambda i: None)
    voice = make_voice(node, guild)
    guild.voice_client = voice
    player = cog.player(guild)
    player.channel_id = 5
    yield NS(cog=cog, node=node, voice=voice, player=player, text=text)
    player._cancel_idle()


async def test_lookups_go_through_lavalink(rig):
    node, cog = rig.node, rig.cog
    node.loads["ytsearch:lofi beats"] = {"loadType": "search", "data": [lltrack("Lofi", "https://youtu.be/a"),
                                                                         lltrack("Other", "https://youtu.be/b")]}
    tracks, name = await cog.lookup("lofi beats", 7)
    assert name is None and len(tracks) == 1 and tracks[0].encoded == "enc-Lofi" and tracks[0].requester == 7
    node.loads["https://www.youtube.com/playlist?list=PL1"] = {
        "loadType": "playlist", "data": {"info": {"name": "Mix"}, "tracks": [lltrack(f"t{i}", f"u{i}") for i in range(3)]}}
    tracks, name = await cog.lookup("https://www.youtube.com/playlist?list=PL1", 7)
    assert name == "Mix" and len(tracks) == 3
    node.loads["scsearch:rare song"] = {"loadType": "search", "data": [lltrack("Rare", "https://sc/r", "soundcloud")]}
    node.loads["ytsearch:rare song"] = {"loadType": "error", "data": {"message": "Sign in to confirm you're not a bot"}}
    tracks, _ = await cog.lookup("rare song", 7)
    assert tracks[0].source == "soundcloud"
    with pytest.raises(LookupError):
        await cog.lookup("nothing at all", 7)


async def test_player_plays_through_lavalink_and_falls_back_to_soundcloud(rig):
    node, voice, p = rig.node, rig.voice, rig.player
    node.loads["ytsearch:Artist - Song"] = {"loadType": "search", "data": [lltrack("Song", "https://youtu.be/s")]}
    node.loads["scsearch:Artist - Song"] = {"loadType": "search",
                                            "data": [lltrack("Song SC", "https://sc/s", "soundcloud")]}
    p.queue.append(Track(title="Song — Artist", query="Artist - Song", source="Spotify"))
    p.queue.append(Track(title="Next", url="https://youtu.be/n", encoded="enc-next", duration=100))
    await p.start_or_queue()
    assert node.updates[-1][1]["track"] == {"encoded": "enc-Song"} and p.current.url == "https://youtu.be/s"
    voice._event({"type": "TrackExceptionEvent", "exception": {"message": "Please sign in"}})
    voice._event({"type": "TrackEndEvent", "reason": "loadFailed"})
    await settle()
    assert node.updates[-1][1]["track"] == {"encoded": "enc-Song SC"} and p.current.title == "Song — Artist"
    voice._event({"type": "TrackEndEvent", "reason": "finished"})
    await settle()
    assert p.current.title == "Next" and node.updates[-1][1]["track"] == {"encoded": "enc-next"}


async def test_effects_volume_and_seek_change_live(rig):
    node, p = rig.node, rig.player
    p.queue.append(Track(title="A", url="https://youtu.be/a", encoded="enc-a", duration=200))
    await p.start_or_queue()
    plays = len([u for u in node.updates if "track" in u[1]])
    p.filter, p.volume = "nightcore", 150
    p.restart(42)
    await settle()
    body = node.updates[-1][1]
    assert body["position"] == 42000 and body["volume"] == 150 and body["filters"]["timescale"]["speed"] == 1.2
    assert len([u for u in node.updates if "track" in u[1]]) == plays  # no restart of the song
    assert p.speed == 1.2
    p.skip()
    await settle()
    assert node.updates[-1][1] == {"track": {"encoded": None}}


async def test_song_cut_off_mid_play_is_reloaded_and_resumed_not_skipped(rig, monkeypatch):
    import time as _time
    node, voice, p = rig.node, rig.voice, rig.player

    async def no_wait(_seconds):
        return None
    monkeypatch.setattr(music, "resume_wait", no_wait)
    node.loads["ytsearch:Dr. Dre - Still D.R.E."] = {"loadType": "search",
                                                       "data": [lltrack("Still DRE", "https://youtu.be/d")]}
    node.loads["scsearch:Dr. Dre - Still D.R.E."] = {"loadType": "search",
                                                       "data": [lltrack("Still DRE SC", "https://sc/d", "soundcloud")]}
    p.queue.append(Track(title="Still D.R.E. — Dr. Dre", query="Dr. Dre - Still D.R.E.", duration=270))
    p.queue.append(Track(title="Next", url="https://youtu.be/n", encoded="enc-next", duration=100))
    await p.start_or_queue()
    for attempt in range(music.YT_RESUMES):
        p.base, p.t0 = 90.0, _time.monotonic()                        # 1:30 into the song
        voice._event({"type": "TrackExceptionEvent", "exception": {"message": "All clients failed to load the item."}})
        voice._event({"type": "TrackEndEvent", "reason": "loadFailed"})
        await settle()
        body = node.updates[-1][1]
        assert body["track"] == {"encoded": "enc-Still DRE"} and 88000 <= body["position"] <= 90000
        assert p.current.title == "Still D.R.E. — Dr. Dre"                # same song, same spot
    p.base, p.t0 = 95.0, _time.monotonic()
    voice._event({"type": "TrackEndEvent", "reason": "loadFailed"})       # YouTube keeps refusing: SoundCloud
    await settle()
    assert node.updates[-1][1]["track"] == {"encoded": "enc-Still DRE SC"}
