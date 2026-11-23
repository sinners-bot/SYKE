"""Badges unlock on their own, show progress, and get announced once."""

import time
import types
from dataclasses import replace

import pytest

from syke.achievements import Achievements, BADGES
from syke.bot import Syke
from syke.config import load_settings
from syke.music_liked import Library
from syke.music_sources import Track
from syke.persona import Persona

NS = types.SimpleNamespace
GID = 42


class Channel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(kw.get("embed"))


@pytest.fixture()
def world(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "a.db"), ai_provider="none", web_enabled=False,
                       owner_ids=frozenset({1}))
    bot = Syke(settings)
    ach, persona = Achievements(bot), Persona(bot)
    bot.get_cog = lambda name: {"Achievements": ach, "Persona": persona}.get(name)
    guild = NS(id=GID)
    return bot, ach, persona, guild


def member(uid, name="Mo"):
    return NS(id=uid, display_name=name, mention=f"<@{uid}>", bot=False)


def test_messages_unlock_chatterbox_and_night_owl(world):
    bot, ach, _, guild = world
    night = time.mktime((2026, 11, 20, 3, 0, 0, 0, 0, -1))
    rows = [(i, 10, 7, "hello there", night + i, "Mo", 0, 0) for i in range(120)]
    bot.storage.add_corpus(GID, rows)
    bot.tz_for = lambda gid: __import__("zoneinfo").ZoneInfo("UTC")
    bot.storage._write("UPDATE corpus SET created_at = ?", (1763607600.0,))   # 03:00 UTC
    done = ach.progress(GID, 7)
    assert done["night_owl"] == 120 and done["chatterbox"] == 120
    new = {b.key for b in ach.evaluate(GID, 7)}
    assert "night_owl" in new and "chatterbox" not in new
    assert ach.evaluate(GID, 7) == []                                      # only once


async def test_events_unlock_badges_quietly(world):
    bot, ach, _, guild = world
    channel = Channel()
    for _ in range(9):
        await ach.award_event(guild, member(7), channel, "radio_starts")
    assert "radio_dj" not in ach.unlocked(GID, 7)
    await ach.award_event(guild, member(7), channel, "radio_starts")
    await ach.award_event(guild, member(7), channel, "best_blend", 91, maximum=True)
    await ach.award_event(guild, member(7), channel, "best_blend", 40, maximum=True)
    assert ach.counter(GID, 7, "best_blend") == 91
    assert {"radio_dj", "soulmate"} <= set(ach.unlocked(GID, 7))
    assert channel.sent == []                                              # never announced in chat
    assert "📻 Radio DJ" in ach.badge_line(GID, 7)                          # shown in Wrapped


def test_feelings_promises_money_and_the_father(world):
    bot, ach, persona, guild = world
    persona.change_bond(GID, 7, -50, "insult", "called you trash")
    assert ach.counter(GID, 7, "grudge_low") == 1 and "survived_grudge" not in {b.key for b in ach.evaluate(GID, 7)}
    persona.change_bond(GID, 7, 55, "kind", "apologised")
    assert "survived_grudge" in {b.key for b in ach.evaluate(GID, 7)}
    persona.change_bond(GID, 8, 80, "kind", "was nice")
    for i in range(10):
        bot.storage._write("INSERT INTO persona_promises (guild_id, channel_id, user_id, text, made_at, due, status) "
                           "VALUES (?, 1, 8, ?, 0, 0, 'kept')", (GID, f"p{i}"))
    bot.storage.econ_touch(GID, 8, 0)
    bot.storage.econ_adjust(GID, 8, cash=150_000)
    lib = Library(bot.storage)
    for i in range(50):
        lib.add(8, Track(title=f"S{i}", url=f"https://youtu.be/{i}"))
    got = {b.key for b in ach.evaluate(GID, 8)}
    assert {"syke_fave", "promise_keeper", "high_roller", "tastemaker"} <= got
    assert {b.key for b in ach.evaluate(GID, 1)} >= {"father"}             # Echo
    assert "👑 SYKE's Dad" in ach.badge_line(GID, 1)


async def test_badges_command_shows_progress(world):
    bot, ach, _, guild = world
    for _ in range(4):
        ach.bump(GID, 7, "radio_starts")
    replies = []

    class Ctx:
        prefix = clean_prefix = "$"

        async def reply(self, content=None, **kw):
            replies.append(kw["embed"])
    ctx = Ctx()
    ctx.guild, ctx.author = guild, member(7)
    await ach.achievements_cmd.callback(ach, ctx, None)
    card = replies[0]
    todo = [f.value for f in card.fields if f.name == "Still to unlock"][0]
    assert "Radio DJ" in todo and "4/10" in todo and "SYKE's Dad" not in todo   # the father badge stays secret
    assert card.title.endswith(f"0/{len(BADGES) - 1}")


def test_wrapped_ignores_gif_links_and_markup():
    from zoneinfo import ZoneInfo
    from syke.social import wrapped_stats
    rows = [(1, 1, "https://klipy.com/gifs/funny-cat-gif-123 <:pog:999> <@5>", 1.7e9, 0, 0)] * 6 + \
           [(2, 1, "pizza tonight pizza", 1.7e9, 0, 0)] * 2
    stats = wrapped_stats(rows, ZoneInfo("UTC"))
    assert "klipy" not in stats["top_words"] and "gif" not in stats["top_words"] and "gifs" not in stats["top_words"]
    assert stats["top_words"][0] == "pizza"
