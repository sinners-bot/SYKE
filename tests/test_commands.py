import asyncio
import contextlib
import types

import discord
import pytest

class FakeCtx:
    def __init__(self, guild_id: int = 42):
        self.guild = types.SimpleNamespace(id=guild_id, me=None, get_channel=lambda cid: None)
        self.author = types.SimpleNamespace(id=1234, bot=False, display_name="Tester")
        self.replies: list = []

    async def reply(self, content=None, **kwargs):
        self.replies.append(kwargs.get("embed"))
        self.views = getattr(self, "views", []) + [kwargs.get("view")]
        return types.SimpleNamespace(id=len(self.replies))

    send = reply

    def typing(self, **kwargs):
        return contextlib.nullcontext()


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
    assert [b.label for b in ctx.views[-1].children] == ["Overview", "Full report", "Highlights", "Ranks"]
    owner_id, pages = cog.bot.load_card(len(ctx.replies))
    assert owner_id == 1234 and pages["ranks"].title == "Vex (fake demo member)'s Achievements"
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


def _fake_guilds(monkeypatch, bot, sizes):
    guilds = [types.SimpleNamespace(id=i, name=f"Server {i}", member_count=n, me=None) for i, n in enumerate(sizes, 1)]
    monkeypatch.setattr(type(bot), "guilds", property(lambda self: guilds))


def test_about_shows_server_count_and_invite(cog, monkeypatch):
    _fake_guilds(monkeypatch, cog.bot, [120, 30, 5])
    ctx = FakeCtx()
    run(cog.about.callback(cog, ctx))
    fields = {f.name: f.value for f in ctx.replies[-1].fields}
    assert fields["Servers"] == "**3**"
    assert fields["Members watched"] == "**155**"
    button = ctx.views[-1].children[0]
    assert "scope=bot+applications.commands" in button.url


def test_servers_is_owner_only(cog, monkeypatch):
    _fake_guilds(monkeypatch, cog.bot, [10, 900])
    ctx = FakeCtx()
    run(cog.servers.callback(cog, ctx))
    assert ctx.replies[-1].title == "Owners only"

    ctx.author.id = next(iter(cog.bot.settings.owner_ids))
    run(cog.servers.callback(cog, ctx))
    embed = ctx.replies[-1]
    assert embed.title == "🌐 SYKE is in 2 servers"
    assert embed.description.index("Server 2") < embed.description.index("Server 1")


def test_showcase_pages_have_examples(cog):
    from syke.showcase import TOUR, ShowcaseView, tour_page

    sent = {}

    class Ctx(FakeCtx):
        async def reply(self, content=None, **kwargs):
            sent.update(kwargs)

    run(cog.showcase.callback(cog, Ctx()))
    assert isinstance(sent["view"], ShowcaseView) and sent["view"].is_persistent()
    assert "Personality reports" in sent["embeds"][0].description
    for key, *_ in TOUR:
        embeds = tour_page(key, "?")
        assert all(len(e) < 6000 for e in embeds)
    assert len(tour_page("profile", "?")) == 2
    assert "`?iq [@member]`" in tour_page("iq", "?")[0].description


def test_install_settings_are_configured_once(cog):
    from syke.bot import INSTALL_SCOPES, install_permissions

    edits = []

    def app(params):
        config = types.SimpleNamespace(oauth2_install_params=params) if params else None
        async def edit(**kwargs):
            edits.append(kwargs)
        return types.SimpleNamespace(bot_public=True, custom_install_url=None,
                                     guild_integration_config=config, edit=edit)

    async def info(current):
        return current

    cog.bot.application_info = lambda: info(app(None))
    run(cog.bot._configure_install())
    assert edits[-1]["guild_install_scopes"] == INSTALL_SCOPES
    assert edits[-1]["guild_install_permissions"] == install_permissions()

    done = types.SimpleNamespace(scopes=list(INSTALL_SCOPES), permissions=install_permissions())
    cog.bot.application_info = lambda: info(app(done))
    run(cog.bot._configure_install())
    assert len(edits) == 1


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _ask_message(bot, content, mid=1, reference=None, guild=True, author_id=777):
    replies = []

    async def reply(content=None, **kwargs):
        sent = types.SimpleNamespace(id=10_000 + mid, embeds=[kwargs.get("embed")], content="",
                                     author=types.SimpleNamespace(id=999))
        replies.append(sent)
        return sent

    g = types.SimpleNamespace(id=42, name="Lab") if guild else None
    channel = types.SimpleNamespace(id=1, name="general", guild=g, typing=lambda: _Typing())
    return types.SimpleNamespace(
        id=mid, content=content, guild=g, channel=channel, reply=reply, mentions=[],
        author=types.SimpleNamespace(id=author_id, bot=False, display_name="Tester", display_avatar=None),
        reference=reference,
    ), replies


def test_ask_by_mention_dm_and_follow_up(cog, monkeypatch):
    bot = cog.bot
    run(bot.add_cog(cog))
    bot._connection.user = types.SimpleNamespace(id=999, mention="<@999>", name="SYKE")
    calls = []

    async def fake_ask(settings, question, history, asker, place):
        calls.append((question, list(history), place))
        return f"answer {len(calls)}"

    monkeypatch.setattr("syke.bot.ask", fake_ask)
    monkeypatch.setattr("syke.bot.discord.Message", types.SimpleNamespace)

    assert bot.mention_question(_ask_message(bot, "<@999> profile")[0]) is None, "commands aren't questions"
    assert bot.mention_question(_ask_message(bot, "<@999>")[0]) is None

    message, replies = _ask_message(bot, "<@999> why is the sky blue?")
    assert run(bot.maybe_answer(message))
    assert calls[-1][0] == "why is the sky blue?" and "#general" in calls[-1][2]
    first = replies[-1]
    assert first.embeds[0].description == "answer 1"
    assert first.embeds[0].footer.text.endswith("SYKE AI")

    ref = types.SimpleNamespace(message_id=first.id, resolved=first, cached_message=None)
    follow, replies = _ask_message(bot, "and at night?", mid=2, reference=ref, author_id=778)
    assert run(bot.maybe_answer(follow))
    assert calls[-1][1] == [{"role": "user", "content": "why is the sky blue?"},
                            {"role": "assistant", "content": "answer 1"}]

    bot._asks.clear()
    again, _ = _ask_message(bot, "really?", mid=3, reference=ref, author_id=779)
    run(bot.maybe_answer(again))
    assert calls[-1][1][0]["content"] == "why is the sky blue?", "rebuilt from the embed after a restart"

    dm, replies = _ask_message(bot, "hello there", mid=4, guild=False, author_id=780)
    assert run(bot.maybe_answer(dm)) and calls[-1][2] == "a private DM"
    assert not run(bot.maybe_answer(_ask_message(bot, "just chatting", mid=5, author_id=781)[0]))


def test_ask_without_ai_key_explains(cog):
    bot = cog.bot
    bot._connection.user = types.SimpleNamespace(id=999, mention="<@999>", name="SYKE")
    message, replies = _ask_message(bot, "<@999> what's 2+2")
    run(bot.maybe_answer(message))
    assert replies[-1].embeds[0].title == "No brain installed 🧠"
