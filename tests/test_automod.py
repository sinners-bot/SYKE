import time
import types

import discord
import pytest

from syke.automod import AutoMod, caps_ratio, domains_in, filter_hit, normalize
from test_commands import run
from test_moderation import Ctx, Guild, Member, Role


# Pure helpers

def test_normalize_and_filter_hit():
    assert filter_hit("you are a FUCK", ["fuck"]) == "fuck"
    assert filter_hit("b i t c h move", ["bitch"]) == "bitch"   # spaced (5+ chars)
    assert filter_hit("fvck this", ["fvck"]) == "fvck"
    assert filter_hit("sh1t happens", ["shit"]) == "shit"       # leetspeak
    assert filter_hit("class assignment", ["ass"]) is None     # word-boundary, not substring
    assert filter_hit("go away", ["fuck"]) is None
    assert filter_hit("hello", []) is None
    assert filter_hit("get out of here now", ["get out"]) == "get out"  # phrase


def test_domains_and_caps():
    assert domains_in("visit https://youtube.com/watch and http://evil.net/x") == ["youtube.com", "evil.net"]
    assert domains_in("no links here") == []
    assert caps_ratio("HELLO") == 1.0
    assert caps_ratio("Hello") == 0.2
    assert caps_ratio("12345") == 0.0


# Enforcement

@pytest.fixture()
def am(cog):
    return AutoMod(cog.bot)


def _message(guild, author, content, mentions=None, roles=0, mid=1):
    channel = types.SimpleNamespace(id=1, mention="#general", send=_record_send(guild))
    return types.SimpleNamespace(
        guild=guild, author=author, content=content, channel=channel, id=mid,
        mentions=mentions or [], role_mentions=[object()] * roles,
        created_at=None, delete=_record_delete(guild),
    )


def _record_delete(guild):
    async def delete():
        guild.deleted = getattr(guild, "deleted", 0) + 1
    return delete


def _record_send(guild):
    async def send(*a, **k):
        guild.notices = getattr(guild, "notices", [])
        guild.notices.append(a[0] if a else k.get("embed"))
    return send


def _setup(am):
    guild = Guild()
    guild.me = Member(999, position=50, manage_messages=True, moderate_members=True)
    guild.deleted = 0
    offender = Member(7, position=3)
    guild.members = {7: offender}
    am.bot.get_guild = lambda gid: guild if gid == guild.id else None
    return guild, offender


def test_word_filter_deletes_and_logs_case(am):
    guild, offender = _setup(am)
    am.bot.storage.update_config(guild.id, "automod", filter_on=True, words=["slur"])
    run(am.scan(_message(guild, offender, "you are a slur")))
    assert guild.deleted == 1
    cases = am.bot.storage.cases(guild.id, 7)
    assert cases and cases[0][2] == "automod (filter)"


def test_mods_are_exempt(am):
    guild, _ = _setup(am)
    mod = Member(8, position=10, manage_messages=True)
    guild.members[8] = mod
    am.bot.storage.update_config(guild.id, "automod", filter_on=True, words=["slur"])
    run(am.scan(_message(guild, mod, "slur")))
    assert guild.deleted == 0


def test_antiinvite_and_antilink(am):
    guild, offender = _setup(am)
    am.bot.storage.update_config(guild.id, "automod", antiinvite=True)
    run(am.scan(_message(guild, offender, "join discord.gg/abcd")))
    assert guild.deleted == 1

    guild.deleted = 0
    am.bot.storage.update_config(guild.id, "automod", antiinvite=False, antilink=True,
                                 allow_domains=["youtube.com"])
    run(am.scan(_message(guild, offender, "check https://evil.net/x")))
    assert guild.deleted == 1
    guild.deleted = 0
    run(am.scan(_message(guild, offender, "watch https://youtube.com/v")))  # allowlisted
    assert guild.deleted == 0


def test_antispam_flood_times_out(am):
    guild, offender = _setup(am)
    am.bot.storage.update_config(guild.id, "automod", antispam=True, flood_count=4, flood_seconds=10,
                                 spam_timeout=300)
    for i in range(4):
        run(am.scan(_message(guild, offender, f"spam {i}", mid=i)))
    assert offender.timed_out is not None and offender.timed_out != "unset"
    assert any(c[2] == "automod (spam)" for c in am.bot.storage.cases(guild.id, 7))


def test_antispam_mass_mentions(am):
    guild, offender = _setup(am)
    am.bot.storage.update_config(guild.id, "automod", antispam=True, mention_limit=5)
    run(am.scan(_message(guild, offender, "ping", mentions=[object(), object(), object()], roles=3)))
    assert guild.deleted == 1


def test_disabled_automod_does_nothing(am):
    guild, offender = _setup(am)
    run(am.scan(_message(guild, offender, "slur discord.gg/x https://evil.net")))
    assert guild.deleted == 0


def test_toxic_flagging_posts_to_review(am):
    guild, offender = _setup(am)
    review = types.SimpleNamespace(id=55, mention="#review", send=_record_send(guild))
    guild.channels.append(review)
    guild.get_channel = lambda cid: review if cid == 55 else None
    am.bot.storage.update_config(guild.id, "automod", tox_flag=True, review_channel=55, tox_threshold=0.0)
    guild.notices = []
    run(am.scan(_message(guild, offender, "you absolute worthless idiot, kill yourself")))
    assert guild.deleted == 0          # flagging never deletes
    assert guild.notices                # posted to review channel
