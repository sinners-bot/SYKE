"""The blacklist: SYKE ignores the person completely; only moderation still sees them."""

import asyncio
import types
from dataclasses import replace

import pytest
from discord.ext import commands

from syke import blacklist as bl
from syke.bot import BLACKLIST_SEED, Syke
from syke.config import load_settings

NS = types.SimpleNamespace
BAD = 1413573321798455386
OWNER = 987654321098765432
OK = 123456789012345678


@pytest.fixture
def bot(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "bl.db"), ai_provider="none", web_enabled=False,
                       owner_ids=frozenset({OWNER}))
    yield Syke(settings)
    bl._BLOCKED.clear()


def test_the_requested_user_is_blacklisted_from_the_start_and_removal_sticks(bot):
    assert BAD in BLACKLIST_SEED and bl.is_blocked(BAD) and bl.is_blocked(NS(id=BAD))
    assert not bl.is_blocked(OK) and not bl.is_blocked(None)
    bl._BLOCKED.discard(BAD)
    bl._save(bot.storage)
    bl.load(bot.storage, seed=BLACKLIST_SEED)           # a restart doesn't put them back once lifted
    assert not bl.is_blocked(BAD)


def test_owners_can_never_be_blocked(bot):
    bl._BLOCKED.add(OWNER)
    assert not bl.is_blocked(OWNER) and OWNER not in bl.blocked_ids()


async def test_their_events_reach_only_moderation(bot):
    await bot._async_setup_hook()                        # gives the bot its event loop, as login does
    heard = []

    class Fun(commands.Cog, name="Social"):
        @commands.Cog.listener()
        async def on_message(self, message):
            heard.append(("social", message.author.id))

    class Mod(commands.Cog, name="Automod"):
        @commands.Cog.listener()
        async def on_message(self, message):
            heard.append(("automod", message.author.id))

        @commands.Cog.listener()
        async def on_raw_reaction_add(self, payload):
            heard.append(("automod-react", payload.user_id))

    await bot.add_cog(Fun())
    await bot.add_cog(Mod())
    called = []

    async def on_message(message):
        called.append(message.author.id)
    bot.on_message = on_message                          # SYKE's own handler: commands, replies, yaps
    bot.dispatch("message", NS(author=NS(id=BAD)))
    bot.dispatch("message", NS(author=NS(id=OK)))
    bot.dispatch("raw_reaction_add", NS(user_id=BAD))
    await asyncio.sleep(0.05)
    assert ("automod", BAD) in heard and ("social", BAD) not in heard and ("automod-react", BAD) in heard
    assert ("social", OK) in heard and ("automod", OK) in heard
    assert called == [OK]


def test_their_interactions_are_dropped_before_anything_runs(bot):
    parsers = bot._connection.parsers
    seen = []
    original = parsers["INTERACTION_CREATE"]
    try:
        parsers["INTERACTION_CREATE"] = lambda data: seen.append(data)
        bl.guard_interactions(bot)
        parsers["INTERACTION_CREATE"]({"type": 2, "member": {"user": {"id": str(BAD)}}})
        parsers["INTERACTION_CREATE"]({"type": 3, "user": {"id": str(BAD)}})           # a button in DMs
        parsers["INTERACTION_CREATE"]({"type": 2, "member": {"user": {"id": str(OK)}}})
        assert [bl.interaction_user(d) for d in seen] == [OK]
    finally:
        parsers["INTERACTION_CREATE"] = original
    assert getattr(original, "_blacklist_guard", False)                               # wired at startup


def test_left_out_of_what_syke_reads_and_off_limits_to_commands(bot):
    assert Syke.turn_text(NS(author=NS(id=BAD), content="yo syke", attachments=[], stickers=[], embeds=[])) == ""
    assert BAD in bot.storage.optouts(42)        # yaps, wrapped, profiles, roasts skip people who opted out


async def test_only_owners_can_manage_it(bot):
    cog = bl.Blacklist(bot)
    replies = []

    async def reply(text=None, **kw):
        replies.append(text or kw.get("embed").description)
    owner = NS(author=NS(id=OWNER), reply=reply)
    assert await cog.cog_check(owner) and not await cog.cog_check(NS(author=NS(id=OK)))
    bot.get_user = lambda uid: NS(name=f"user{uid}")
    await cog.add.callback(cog, owner, who=f"<@{OK}>")
    assert bl.is_blocked(OK) and "blacklisted" in replies[-1]
    await cog.add.callback(cog, owner, who=str(OWNER))
    assert "can't be blacklisted" in replies[-1] and not bl.is_blocked(OWNER)
    await cog.remove.callback(cog, owner, who=str(OK))
    assert not bl.is_blocked(OK) and bot.storage.get_config(0, "blacklist")["ids"] == [BAD]
