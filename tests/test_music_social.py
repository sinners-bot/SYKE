"""Blend, listening parties and rivalries."""

import random
import time
import types
from dataclasses import replace

import pytest

from test_lavalink import NS, rig  # noqa: F401  (fixture)

from syke import music
from syke.music_liked import LikedSongs
from syke.music_social import MusicSocial, Party, blend_order, recap_stats, taste_match, artist_of
from syke.music_sources import Track
from syke.rivalry import Rivalry, match_name


def t(title, artist, n):
    return Track(title=f"{title} — {artist}", url=f"https://youtu.be/{n}", query=f"{artist} - {title}",
                 uploader=artist, duration=200)


@pytest.fixture()
def world(rig):
    bot = rig.cog.bot
    liked, social, rival = LikedSongs(bot), MusicSocial(bot), Rivalry(bot)
    cogs = {"Music": rig.cog, "LikedSongs": liked, "MusicSocial": social, "Rivalry": rival}
    bot.get_cog = lambda name: cogs.get(name)
    return NS(rig=rig, bot=bot, liked=liked, social=social, rival=rival)


def test_taste_match_rewards_shared_songs_and_artists(world):
    lib = world.liked.library
    for i, (song, artist) in enumerate([("A", "Dave"), ("B", "Central Cee"), ("C", "Stormzy"), ("D", "Dave")]):
        lib.add(1, t(song, artist, i))
    for i, (song, artist) in enumerate([("A", "Dave"), ("E", "Central Cee"), ("F", "Drake")]):
        lib.add(2, t(song, artist, 10 + i if song != "A" else 0))
    for i, (song, artist) in enumerate([("X", "Mozart"), ("Y", "Bach")]):
        lib.add(3, t(song, artist, 20 + i))
    close, shared_songs, shared_artists = taste_match(lib.songs(1), lib.songs(2))
    far, _, none = taste_match(lib.songs(1), lib.songs(3))
    assert close > 50 > far and far == 0 and none == []
    assert len(shared_songs) == 1 and shared_artists[0] == "dave"
    assert artist_of((0, "Song", "", "", "Dave - Topic", 0, "", "", "")) == "dave"


def test_blend_starts_with_shared_then_takes_turns(world):
    lib = world.liked.library
    lib.add(1, t("Shared", "Dave", 0))
    for i in range(3):
        lib.add(1, t(f"Mine {i}", "X", 100 + i))
        lib.add(2, t(f"Yours {i}", "Y", 200 + i))
    lib.add(2, t("Shared", "Dave", 0))
    order = blend_order(lib.songs(1), lib.songs(2), rng=random.Random(1))
    assert order[0][1] == "both" and [w for _, w in order[1:]] == ["a", "b", "a", "b", "a", "b"]


def recap_party():
    party = Party(host=1, voice_channel_id=9, text_channel_id=5, started=time.time() - 600, members=[1, 2])
    from syke.music_social import Pick
    party.picks = [Pick("k1", "Banger — Dave", 1, likes={2, 3}), Pick("k2", "Mid — Y", 2, skipped_by=1),
                   Pick("k3", "Fine — Z", 2, likes={1})]
    party.skips[1] += 1
    return party


def test_party_recap_finds_the_best_and_the_skipper():
    stats = recap_stats(recap_party())
    assert stats["songs"] == 3 and stats["most_liked"].title == "Banger — Dave"
    assert stats["best_picker"] == 1 and stats["skipper"] == 1 and stats["skip_count"] == 1


async def test_party_takes_turns_and_counts_likes_and_skips(world):
    rig, lib = world.rig, world.liked.library
    for i in range(3):
        lib.add(1, t(f"One {i}", "A", 300 + i))
        lib.add(2, t(f"Two {i}", "B", 400 + i))
    voice = {1: NS(channel=NS(id=9)), 2: NS(channel=NS(id=9))}
    rig.player.guild = NS(id=1, get_member=lambda uid: NS(id=uid, voice=voice.get(uid), display_name=f"u{uid}"),
                          get_channel=lambda cid: None)
    rig.player.party = Party(1, 9, 5, time.time(), [1, 2])
    await world.social.party_fill(rig.player, 4)
    assert [x.requester for x in rig.player.queue] == [1, 2, 1, 2]                 # turns
    rig.player.current = rig.player.queue.pop(0)
    world.social.note_like(rig.player, 2)
    world.social.note_skip(rig.player, 2)
    pick = rig.player.party.picks[0]
    assert pick.likes == {2} and pick.skipped_by == 2 and rig.player.party.skips[2] == 1
    voice.pop(2)                                                                    # left voice: skipped in rotation
    rig.player.queue.clear()
    await world.social.party_fill(rig.player, 2)
    assert [x.requester for x in rig.player.queue] == [1, 1]


async def test_rivalry_rounds_scores_and_comments(world, monkeypatch):
    rival, bot = world.rival, world.bot
    window = [(time.time(), 10, "Mo", "nah you're wrong", 7), (time.time(), 10, "Zara", "cope", 8)]
    said = []

    async def comment(*args):
        said.append(args)
    monkeypatch.setattr(rival, "maybe_comment", comment)
    await rival.record(1, [{"a": "mo", "b": "Zara", "winner": "Zara", "topic": "pineapple on pizza", "heat": 2}],
                       window)
    row = rival.get(1, 7, 8)
    assert row[2] == 1 and (row[3], row[4]) == (0, 1)
    await rival.record(1, [{"a": "Mo", "b": "Zara", "winner": "Mo", "topic": "pizza", "heat": 2}], window)
    row = rival.get(1, 7, 8)
    assert row[2] == 1 and (row[3], row[4]) == (1, 0)           # same argument: latest verdict replaces the old
    assert rival.note_round(1, 7, 8, 8, "messi vs ronaldo", 3, 10, now=time.time() + 3600) == 2
    row = rival.get(1, 7, 8)
    assert row[2] == 2 and (row[3], row[4]) == (1, 1)
    await rival.record(1, [{"a": "Kai", "b": "Mo", "winner": "none"}], window)   # Kai wasn't in the chat
    assert rival.get(1, 7, 99) is None
    assert said[0][3] == 1                                                       # new round reported


async def test_syke_comments_only_in_yap_channels_and_not_too_often(world, monkeypatch):
    rival, bot = world.rival, world.bot
    sent = []
    channel = NS(id=10)

    async def send(text, **kw):
        sent.append(text)
        return NS(content=text)
    channel.send = send
    guild = NS(id=1, get_channel=lambda cid: channel if cid == 10 else None,
               get_member=lambda uid: NS(display_name={7: "Mo", 8: "Zara"}.get(uid, "x")))
    bot.get_guild = lambda gid: guild
    monkeypatch.setattr(bot.storage, "yap_channels", lambda gid: {10})
    rival.note_round(1, 7, 8, 7, "pizza", 1, 10)
    await rival.maybe_comment(1, 7, 8, 1, 10, "pizza", 7)
    assert sent == []                                                            # round 1: quiet
    await rival.maybe_comment(1, 7, 8, 2, 10, "pizza", 7)
    await rival.maybe_comment(1, 7, 8, 3, 10, "pizza", 7)
    assert len(sent) == 1 and ("Mo" in sent[0] and "Zara" in sent[0])
    monkeypatch.setattr(bot.storage, "yap_channels", lambda gid: set())
    rival._said_pair.clear(), rival._said_channel.clear()
    await rival.maybe_comment(1, 7, 8, 4, 10, "pizza", 7)
    assert len(sent) == 1


def test_names_from_the_ai_match_speakers():
    speakers = {"Mo": 7, "Zara_x": 8}
    assert match_name("mo", speakers) == 7 and match_name("zara", speakers) == 8 and match_name("Kai", speakers) is None
