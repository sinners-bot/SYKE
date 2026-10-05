import asyncio
import contextlib
import types
from dataclasses import replace

import discord
import pytest

from syke.bot import Syke, SykeCommands
from syke.config import load_settings


class FakeCtx:
    def __init__(self, guild_id: int = 42):
        self.guild = types.SimpleNamespace(id=guild_id, me=None, get_channel=lambda cid: None)
        self.author = types.SimpleNamespace(id=1234, bot=False, display_name="Tester")
        self.replies: list = []

    async def reply(self, content=None, **kwargs):
        self.replies.append(kwargs.get("embed"))
        self.views = getattr(self, "views", []) + [kwargs.get("view")]

    send = reply

    def typing(self, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture()
def cog(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "t.db"), ai_provider="none")
    bot = Syke(settings)
    bot._connection.user = types.SimpleNamespace(id=999, mention="<@999>")
    return SykeCommands(bot)


def run(coro):
    return asyncio.run(coro)


def test_prefix_set_reset_and_validate(cog):
    ctx = FakeCtx()
    run(cog.prefix.callback(cog, ctx, "?"))
    assert cog.bot.prefix_for(42) == "?"
    assert "`?profile`" in ctx.replies[-1].description

    run(cog.prefix.callback(cog, ctx, "way too long"))
    assert ctx.replies[-1].title == "Invalid prefix"
    assert cog.bot.prefix_for(42) == "?"

    run(cog.prefix.callback(cog, ctx, "reset"))
    assert cog.bot.prefix_for(42) == "!"
    assert cog.bot.storage.prefix(42) is None


def test_mention_only_prefix(cog):
    from syke.bot import MENTION_ONLY, resolve_prefix

    cog.bot._connection.user = types.SimpleNamespace(id=999, mention="<@999>", name="SYKE")
    msg = types.SimpleNamespace(guild=types.SimpleNamespace(id=42), content="")
    ctx = FakeCtx()

    run(cog.prefix.callback(cog, ctx, "mention"))
    assert cog.bot.prefix_for(42) == MENTION_ONLY
    assert run(resolve_prefix(cog.bot, msg)) == ["<@999> ", "<@!999> "]
    assert cog.bot.display_prefix(42) == "@SYKE "
    assert "only answers when mentioned" in ctx.replies[-1].description

    run(cog.help.callback(cog, ctx))
    assert "`@SYKE profile [@member]`" in ctx.replies[-1].description

    run(cog.prefix.callback(cog, ctx, None))
    assert "mentions only" in ctx.replies[-1].description

    run(cog.prefix.callback(cog, ctx, "<@999>"))
    assert cog.bot.prefix_for(42) == MENTION_ONLY
    run(cog.prefix.callback(cog, ctx, "<x>"))
    assert ctx.replies[-1].title == "Invalid prefix"

    run(cog.prefix.callback(cog, ctx, "!"))
    assert run(resolve_prefix(cog.bot, msg)) == ["<@999> ", "<@!999> ", "!"]
    assert cog.bot.storage.prefix(42) is None


def test_help_uses_server_prefix(cog):
    cog.bot.set_prefix(42, "s.")
    ctx = FakeCtx()
    run(cog.help.callback(cog, ctx))
    assert "`s.profile [@member]`" in ctx.replies[-1].description


def test_demo_toggle_and_sample(cog):
    ctx = FakeCtx()
    run(cog.demo.callback(cog, ctx, None))
    assert cog.bot.storage.demo_mode(42)
    run(cog.sample.callback(cog, ctx, "VEX"))
    card = ctx.replies[-1]
    assert card.title == "Vex (fake demo member)"
    assert card.author.name == "SYKE REPORT · 📊 Overview"
    assert "(fake demo member)" not in card.description
    assert [b.label for b in ctx.views[-1].children] == ["Overview", "Highlights", "Ranks"]
    run(cog.top.callback(cog, ctx, "chaos"))
    assert "Chaotic" in ctx.replies[-1].title
    run(cog.demo.callback(cog, ctx, False))
    assert not cog.bot.storage.demo_mode(42)


def _perm_ctx(perms, interaction=None):
    channel = types.SimpleNamespace(permissions_for=lambda me: perms)
    return types.SimpleNamespace(interaction=interaction, guild=object(), channel=channel, me=object())


def test_channel_check_reports_missing_permissions(cog):
    from syke.bot import MissingChannelPermissions

    ok = discord.Permissions(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
    assert run(cog.bot.channel_check(_perm_ctx(ok)))

    with pytest.raises(MissingChannelPermissions) as exc:
        run(cog.bot.channel_check(_perm_ctx(discord.Permissions(view_channel=True, send_messages=True))))
    assert exc.value.missing == ["embed_links", "read_message_history"]

    assert run(cog.bot.channel_check(_perm_ctx(discord.Permissions.none(), interaction=object())))
    assert cog.bot.channel_check in cog.bot._checks


ADMIN_COMMANDS = ["track", "untrack", "channels", "prefix", "timezone", "rescan", "demo", "sample"]


def _member(uid, manage_guild=False):
    return types.SimpleNamespace(id=uid, guild_permissions=discord.Permissions(manage_guild=manage_guild))


def test_owner_can_use_admin_commands_without_permissions(cog):
    from syke.bot import _admin_predicate

    assert 1342786189576634398 in cog.bot.settings.owner_ids

    def check(member):
        return run(_admin_predicate(types.SimpleNamespace(bot=cog.bot, author=member)))

    assert check(_member(1342786189576634398))
    assert check(_member(555, manage_guild=True))
    with pytest.raises(discord.ext.commands.MissingPermissions):
        check(_member(555))

    by_name = {c.name: c for c in cog.get_commands()}
    for name in ADMIN_COMMANDS:
        assert _admin_predicate in by_name[name].checks, name
    assert _admin_predicate not in by_name["profile"].checks


def test_owner_ids_are_configurable(monkeypatch):
    from syke.config import load_settings

    monkeypatch.setenv("SYKE_OWNER_IDS", "1, 22 ,abc,333")
    assert load_settings().owner_ids == {1, 22, 333}


def _slash_ctx(guild_id=42, channel_id=7):
    ctx = FakeCtx(guild_id)
    ctx.interaction = types.SimpleNamespace(guild_id=guild_id, channel_id=channel_id)
    return ctx


def test_diagnose_when_bot_not_in_server(cog, monkeypatch):
    monkeypatch.setattr(cog.bot, "get_guild", lambda gid: None)
    monkeypatch.setattr(type(cog.bot), "application_id", 1556658519770398881, raising=False)
    ctx = _slash_ctx()
    run(cog.diagnose.callback(cog, ctx))
    embed = ctx.replies[-1]
    assert embed.title == "❌ SYKE isn't in this server"
    assert "scope=bot+applications.commands" in embed.description
    assert "client_id=1556658519770398881" in embed.description


def test_diagnose_lists_missing_channel_permissions(cog, monkeypatch):
    perms = discord.Permissions(view_channel=True, send_messages=True)
    channel = types.SimpleNamespace(permissions_for=lambda me: perms)
    guild = types.SimpleNamespace(id=42, me=object(), get_channel_or_thread=lambda cid: channel)
    monkeypatch.setattr(cog.bot, "get_guild", lambda gid: guild)
    ctx = _slash_ctx()
    run(cog.diagnose.callback(cog, ctx))
    text = ctx.replies[-1].description
    assert "✅ Send Messages in this channel" in text
    assert "❌ Embed Links in this channel" in text and "❌ Read Message History in this channel" in text
    assert "Prefix commands won't work in this channel" in text

    perms.update(embed_links=True, read_message_history=True)
    run(cog.diagnose.callback(cog, ctx))
    assert "All good. Add a channel" in ctx.replies[-1].description


def test_unknown_inputs_are_explained(cog):
    ctx = FakeCtx()
    run(cog.top.callback(cog, ctx, "smelly"))
    assert ctx.replies[-1].title == "Unknown trait"
    run(cog.sample.callback(cog, ctx, "nobody"))
    assert ctx.replies[-1].title == "Unknown demo member"
    run(cog.top.callback(cog, ctx, "funny"))
    assert ctx.replies[-1].title == "No channels are being watched"
    assert "`!track #general`" in ctx.replies[-1].description
