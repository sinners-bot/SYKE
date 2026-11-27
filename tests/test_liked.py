"""Liked songs: like what's playing, view and edit your library, play it back."""

import types

import discord
import pytest

from test_lavalink import NS, lltrack, rig, settle  # noqa: F401  (fixture)

from syke import music
from syke.music_liked import LIBRARY_MAX, LikeButton, LibraryView, LikedSongs, row_to_track
from syke.music_sources import Track


def song(i, **kw):
    return Track(title=f"Song {i}", url=f"https://youtu.be/v{i}", uploader=f"Artist {i}", duration=180 + i, **kw)


class Resp:
    def __init__(self):
        self.sent, self.done, self.modal, self.edited = [], False, None, []

    async def send_message(self, content=None, **kw):
        self.sent.append((content, kw))
        self.done = True

    async def edit_message(self, **kw):
        self.edited.append(kw)
        self.done = True

    async def send_modal(self, modal):
        self.modal = modal

    async def defer(self, **kw):
        self.done = True

    def is_done(self):
        return self.done


def interaction(user, guild=None, custom_id=""):
    it = NS(user=user, guild=guild, channel=None, client=None, data={"custom_id": custom_id}, response=Resp())
    it.followups = []

    async def follow(content=None, **kw):
        it.followups.append((content, kw))
    it.followup = NS(send=follow)
    return it


@pytest.fixture()
def liked(rig):
    cog = LikedSongs(rig.cog.bot)
    rig.cog.bot.get_cog = lambda name: {"Music": rig.cog, "LikedSongs": cog}.get(name)
    return cog


def test_library_adds_once_edits_and_orders(liked):
    lib = liked.library
    for i in range(1, 4):
        assert lib.add(7, song(i)) == "added"
    assert lib.add(7, song(2)) == "already" and lib.count(7) == 3
    assert [r[1] for r in lib.songs(7)] == ["Song 3", "Song 2", "Song 1"]          # newest first
    assert lib.move(7, 3, 1) and [r[1] for r in lib.songs(7)] == ["Song 1", "Song 3", "Song 2"]
    lib.to_top(7, [lib.songs(7)[2][0]])
    assert lib.songs(7)[0][1] == "Song 2"
    assert lib.remove_number(7, 1)[1] == "Song 2" and lib.count(7) == 2
    assert lib.count(8) == 0                                                         # libraries are per person
    assert lib.clear(7) == 2 and lib.count(7) == 0


def test_library_has_a_limit(liked):
    lib = liked.library
    for i in range(LIBRARY_MAX):
        lib.add(7, song(i))
    assert lib.add(7, song(99999)) == "full"


async def test_anyone_can_like_whats_playing_from_the_card(rig, liked):
    rig.player.current = song(1, radio="pop", query="NewJeans - Super Shy")
    view = music.Controls(rig.cog, 1)
    like = [c for c in view.children if isinstance(c, LikeButton)]
    assert like and like[0].item.custom_id == "syke:like:1"
    press = interaction(NS(id=7), custom_id="syke:like:1")
    assert await view.interaction_check(press)            # no DJ rights needed to like
    await liked.like_now(press, 1)
    assert "Added **Song 1**" in press.response.sent[0][0] and liked.library.count(7) == 1
    saved = liked.library.songs(7)[0]
    assert saved[2] == "https://youtu.be/v1" and saved[3] == "NewJeans - Super Shy"
    again = interaction(NS(id=7))
    await liked.like_now(again, 1)
    content, kw = again.response.sent[0]
    assert "already" in content and kw["view"] is not None                         # offers to remove it
    rig.player.current = None
    nothing = interaction(NS(id=7))
    await liked.like_now(nothing, 1)
    assert "Nothing's playing" in nothing.response.sent[0][0]


def test_library_card_and_who_can_edit(liked):
    for i in range(1, 13):
        liked.library.add(7, song(i))
    owner = NS(id=7, display_name="Echo")
    mine = LibraryView(liked, owner, 7)
    card = mine.card()
    assert card.title == "❤️ Your Liked songs" and "`10.`" in card.description and "Page 1/2" in card.footer.text
    labels = [getattr(c, "label", None) for c in mine.children]
    assert {"Play", "Shuffle", "Add", "Edit", "Clear"} <= set(labels)
    theirs = LibraryView(liked, owner, 8)
    assert theirs.card().title == "❤️ Echo's Liked songs"
    labels = [getattr(c, "label", None) for c in theirs.children]
    assert "Edit" not in labels and "Clear" not in labels and "Play" in labels       # others can listen, not edit


async def test_playing_a_library_queues_it(rig, liked, monkeypatch):
    for i in range(1, 4):
        liked.library.add(7, song(i))
    queued = []

    async def enqueue(ctx, tracks, name, **kw):
        queued.append((tracks, name))

    async def ready(ctx):
        return True
    monkeypatch.setattr(rig.cog, "enqueue", enqueue)
    monkeypatch.setattr(rig.cog, "ready_or_explain", ready)
    replies = []

    class Ctx:
        author = NS(id=7, display_name="Echo", voice=NS(channel=object()))

        async def reply(self, content=None, **kw):
            replies.append(content)

        async def defer(self, **kw):
            pass
    await liked.play_library(Ctx(), Ctx.author, shuffle=False)
    tracks, name = queued[0]
    assert [t.title for t in tracks] == ["Song 3", "Song 2", "Song 1"] and name == "your Liked songs"
    assert all(t.requester == 7 for t in tracks)
    Ctx.author = NS(id=9, display_name="Mo", voice=None)
    await liked.play_library(Ctx(), NS(id=7, display_name="Echo"), shuffle=True)
    assert replies[-1] == "Join a voice channel first."


async def test_saved_links_play_the_exact_upload(rig):
    rig.node.loads["https://youtu.be/v1"] = {"loadType": "track", "data": lltrack("Song 1", "https://youtu.be/v1",
                                                                               encoded="enc-v1")}
    rig.node.loads["https://youtu.be/v1"]["data"] = [rig.node.loads["https://youtu.be/v1"]["data"]]
    rig.node.loads["https://youtu.be/v1"]["loadType"] = "search"
    track = row_to_track((1, "Song 1", "https://youtu.be/v1", "", "Artist", 180.0, "", "youtube", ""), 7)
    assert await rig.cog.ll_resolve(track) == "enc-v1"
    assert rig.node.loaded[0] == "https://youtu.be/v1"


async def test_add_by_name_or_link(rig, liked):
    rig.node.loads["ytsearch:still dre"] = {"loadType": "search",
                                            "data": [lltrack("Still D.R.E.", "https://youtu.be/d")]}
    assert "Added **Still D.R.E.**" in await liked.add_by_query(7, "still dre")
    assert "already" in await liked.add_by_query(7, "still dre")
    assert "Couldn't find" in await liked.add_by_query(7, "nothing at all")
