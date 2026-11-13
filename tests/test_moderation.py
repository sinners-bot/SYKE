import contextlib
import time
import types

import discord
import pytest

from syke.moderation import (
    DEFAULT_KINDS,
    KINDS,
    Moderation,
    blocker,
    help_lines,
    human_duration,
    parse_duration,
    parse_kinds,
    parse_restrict,
    violation,
)
from test_commands import run


@pytest.fixture()
def mod(cog):
    return Moderation(cog.bot)


# Parsing

def test_parse_duration():
    assert parse_duration("10m") == 600
    assert parse_duration("1h30m") == 5400
    assert parse_duration("2d") == 172800
    assert parse_duration("1w") == 604800
    assert parse_duration("45s") == 45
    assert parse_duration("15") == 900  # bare number = minutes
    assert parse_duration("3hours") == 10800
    assert parse_duration("spam") is None
    assert parse_duration("10x") is None
    assert parse_duration("") is None


def test_human_duration():
    assert human_duration(None) == "permanently"
    assert human_duration(90) == "1m 30s"
    assert human_duration(86400 + 3600 + 60) == "1d 1h"
    assert human_duration(604800) == "1w"


def test_parse_restrict_defaults_to_images_and_links():
    assert parse_restrict("") == (DEFAULT_KINDS, None, "")
    assert parse_restrict("being annoying") == (DEFAULT_KINDS, None, "being annoying")


def test_parse_restrict_kinds_duration_and_reason_in_any_order():
    kinds, seconds, reason = parse_restrict("images, links 1h posting gore")
    assert kinds == ("images", "links") and seconds == 3600 and reason == "posting gore"
    kinds, seconds, reason = parse_restrict("2d reactions+stickers spam reacting")
    assert kinds == ("reactions", "stickers") and seconds == 172800 and reason == "spam reacting"
    kinds, seconds, reason = parse_restrict("all perm raid")
    assert kinds == tuple(KINDS) and seconds is None and reason == "raid"
    kinds, _, _ = parse_restrict("media")
    assert kinds == ("images", "links", "stickers")
    kinds, _, reason = parse_restrict("pics and gifs for 30m stop")
    assert kinds == ("images", "links") and reason == "stop"


def test_reason_words_that_look_like_kinds_later_stay_in_the_reason():
    kinds, _, reason = parse_restrict("images 1h kept posting links")
    assert kinds == ("images",)
    assert reason == "kept posting links"


def test_parse_kinds_stops_at_other_words():
    assert parse_kinds(["images", "hello"]) == (("images",), ["hello"])
    assert parse_kinds(["hello"]) == ((), ["hello"])


# Enforcement

def _msg(content="", attachments=(), embeds=(), stickers=(), snapshots=()):
    return types.SimpleNamespace(content=content, attachments=list(attachments), embeds=list(embeds),
                                 stickers=list(stickers), message_snapshots=list(snapshots))


def test_violation_spots_each_kind():
    assert violation(_msg(attachments=[object()]), {"images"}, set()) == KINDS["images"][0]
    assert violation(_msg("look https://x.com/a"), {"links"}, set()) == KINDS["links"][0]
    assert violation(_msg("join discord.gg/abc"), {"links"}, set()) == KINDS["links"][0]
    assert violation(_msg(embeds=[object()]), {"links"}, set()) == KINDS["links"][0]
    assert violation(_msg(stickers=[object()]), {"stickers"}, set()) == KINDS["stickers"][0]
    assert violation(_msg("<:pog:123456789012345678>"), {"emojis"}, set()) == KINDS["emojis"][0]
    assert violation(_msg("<:pog:123456789012345678>"), {"emojis"}, {123456789012345678}) is None


def test_violation_ignores_what_isnt_restricted():
    assert violation(_msg("hi https://x.com"), {"images"}, set()) is None
    assert violation(_msg(attachments=[object()]), {"links"}, set()) is None
    assert violation(_msg("plain text"), set(KINDS), set()) is None


def test_violation_catches_forwards():
    forwarded = types.SimpleNamespace(content="", attachments=[object()], embeds=[], stickers=[])
    assert violation(_msg(snapshots=[forwarded]), {"images"}, set()) == KINDS["images"][0]


# Fakes

class Role:
    def __init__(self, rid, name="role", position=1):
        self.id = rid
        self.name = name
        self.position = position
        self.mention = f"<@&{rid}>"

    def __eq__(self, other):
        return getattr(other, "id", None) == self.id

    def __hash__(self):
        return hash(self.id)


class Member:
    def __init__(self, uid, position=1, admin=False, **perms):
        self.id = uid
        self.mention = f"<@{uid}>"
        self.display_name = f"user{uid}"
        self.bot = False
        self.top_role = Role(1000 + uid, position=position)
        self.roles = []
        self.guild_permissions = discord.Permissions(administrator=admin, **perms)
        self.dms = []
        self.timed_out = None

    async def add_roles(self, *roles, reason=None):
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]

    async def send(self, **kwargs):
        self.dms.append(kwargs.get("embed"))

    async def timeout(self, until, reason=None):
        self.timed_out = until

    def is_timed_out(self):
        return self.timed_out is not None


class Channel:
    def __init__(self, cid):
        self.id = cid
        self.mention = f"<#{cid}>"
        self.overwrites = {}
        self.sent = []

    def overwrites_for(self, target):
        return self.overwrites.get(target.id, discord.PermissionOverwrite())

    async def set_permissions(self, target, *, overwrite, reason=None):
        self.overwrites[target.id] = overwrite

    def permissions_for(self, member):
        return discord.Permissions.all()

    async def send(self, *args, **kwargs):
        self.sent.append(kwargs.get("embed") or (args[0] if args else None))


class Guild:
    def __init__(self, gid=42):
        self.id = gid
        self.name = "Lab"
        self.owner_id = 1
        self.me = Member(999, position=50, manage_roles=True, manage_messages=True, moderate_members=True)
        self.default_role = Role(gid, "@everyone", position=0)
        self.channels = [Channel(10), Channel(11)]
        self.roles = {}
        self.members = {}
        self.emojis = []

    def get_role(self, rid):
        return self.roles.get(rid)

    def get_channel(self, cid):
        return next((c for c in self.channels if c.id == cid), None)

    def get_member(self, uid):
        return self.members.get(uid)

    async def create_role(self, *, name, permissions, mentionable, reason):
        role = Role(500 + len(self.roles), name)
        self.roles[role.id] = role
        return role


class Ctx:
    def __init__(self, guild, author):
        self.guild = guild
        self.author = author
        self.channel = guild.channels[0]
        self.interaction = None
        self.message = None
        self.replies = []

    async def reply(self, content=None, **kwargs):
        self.replies.append(kwargs.get("embed") or content)

    send = reply

    def typing(self):
        return contextlib.nullcontext()


def _setup(mod):
    guild = Guild()
    boss = Member(2, position=20, manage_roles=True, moderate_members=True)
    target = Member(3, position=5)
    guild.members = {2: boss, 3: target}
    mod.bot.get_guild = lambda gid: guild if gid == guild.id else None
    return guild, boss, target


# Who can act on whom

def test_blocker_respects_role_order():
    guild, boss, target = Guild(), Member(2, position=20), Member(3, position=5)
    assert blocker(guild, boss, target) is None
    assert "yourself" in blocker(guild, boss, boss)
    assert "owner" in blocker(guild, boss, Member(1, position=1))
    assert "at or above yours" in blocker(guild, target, boss)
    high = Member(4, position=60)
    assert "Drag SYKE's role" in blocker(guild, Member(1, position=100), high)
    assert blocker(guild, Member(1, position=100), high, bot_above=False) is None
    # SYKE owners skip the "above you" rule, but SYKE's own role still matters.
    assert blocker(guild, Member(7, position=1), Member(8, position=10), owner_ids={7}) is None


# Commands

def test_restrict_adds_roles_overwrites_and_lifts(mod):
    guild, boss, target = _setup(mod)

    async def flow():
        ctx = Ctx(guild, boss)
        await mod.restrict.callback(mod, ctx, target, details="images links 1h spam")
        assert "No images & files and links & embeds" in ctx.replies[-1].title
        assert "spam" in ctx.replies[-1].description
        assert {r.name for r in target.roles} == {"SYKE · no images", "SYKE · no links"}
        assert target.dms and "restricted" in target.dms[-1].description
        assert mod.active(guild.id, target.id) == {"images", "links"}
        for task in list(mod._overwrite_jobs.values()):
            await task
        images_role = guild.get_role(mod.bot.storage.restrict_role(guild.id, "images"))
        for channel in guild.channels:
            assert channel.overwrites_for(images_role).attach_files is False

        await mod.unrestrict.callback(mod, ctx, target, what="images")
        assert mod.active(guild.id, target.id) == {"links"}
        assert [r.name for r in target.roles] == ["SYKE · no links"]
        await mod.unrestrict.callback(mod, ctx, target, what="")
        assert mod.active(guild.id, target.id) == set()
        assert target.roles == []
        cases = mod.bot.storage.cases(guild.id, target.id)
        assert [c[2] for c in cases] == ["unrestrict (links)", "unrestrict (images)", "restrict (images, links)"]

    run(flow())


def test_restrict_refuses_higher_members_and_bots(mod):
    guild, boss, target = _setup(mod)
    ctx = Ctx(guild, target)
    run(mod.restrict.callback(mod, ctx, boss, details=""))
    assert ctx.replies[-1].title == "Can't restrict them"
    bot_member = Member(5)
    bot_member.bot = True
    run(mod.restrict.callback(mod, Ctx(guild, boss), bot_member, details=""))
    assert mod.bot.storage.restrictions(guild.id) == []


def test_enforcement_deletes_and_expiry_lifts(mod):
    guild, boss, target = _setup(mod)

    async def flow():
        await mod.restrict.callback(mod, Ctx(guild, boss), target, details="links 10m")
        deleted = []

        async def delete():
            deleted.append(True)

        channel = guild.channels[0]
        message = types.SimpleNamespace(guild=guild, author=target, channel=channel, content="https://evil.example",
                                        attachments=[], embeds=[], stickers=[], message_snapshots=[], delete=delete)
        await mod.enforce(message)
        assert deleted == [True]
        assert channel.sent and "restricted" in channel.sent[-1]
        fine = types.SimpleNamespace(**{**vars(message), "content": "just words"})
        await mod.enforce(fine)
        assert deleted == [True]

        mod.bot.storage.restrict(guild.id, target.id, "links", expires_at=time.time() - 1,
                                 reason="", moderator_id=boss.id)
        mod.forget_cache(guild.id)
        await mod.expire.coro(mod)
        assert mod.bot.storage.restrictions(guild.id) == []
        assert target.roles == []
        await mod.enforce(message)
        assert deleted == [True]

    run(flow())


def test_warnings_history_and_delete(mod):
    guild, boss, target = _setup(mod)
    ctx = Ctx(guild, boss)
    run(mod.warn.callback(mod, ctx, target, reason="being rude"))
    run(mod.warn.callback(mod, ctx, target, reason="again"))
    assert "**Warnings:** 2" in ctx.replies[-1].description
    run(mod.warnings.callback(mod, ctx, target))
    assert "2 warning(s)" in ctx.replies[-1].title and "being rude" in ctx.replies[-1].description
    first = mod.bot.storage.cases(guild.id, target.id, "warn")[-1][0]
    run(mod.delwarn.callback(mod, ctx, first))
    assert len(mod.bot.storage.cases(guild.id, target.id, "warn")) == 1
    run(mod.clearwarns.callback(mod, ctx, target))
    assert mod.bot.storage.cases(guild.id, target.id, "warn") == []


def test_timeout_caps_and_applies(mod):
    guild, boss, target = _setup(mod)
    ctx = Ctx(guild, boss)
    run(mod.timeout.callback(mod, ctx, target, "30d", reason=""))
    assert ctx.replies[-1].title == "Too long"
    run(mod.timeout.callback(mod, ctx, target, "nope", reason=""))
    assert ctx.replies[-1].title == "How long?"
    run(mod.timeout.callback(mod, ctx, target, "10m", reason="cool off"))
    assert target.timed_out.total_seconds() == 600
    run(mod.untimeout.callback(mod, ctx, target, reason=""))
    assert target.timed_out is None


def test_lock_and_unlock_restore_previous_overwrites(mod):
    guild, boss, _ = _setup(mod)
    channel = guild.channels[0]
    channel.overwrites[guild.default_role.id] = discord.PermissionOverwrite(send_messages=True)
    ctx = Ctx(guild, boss)
    run(mod.lock.callback(mod, ctx, channel, reason="raid"))
    assert channel.overwrites_for(guild.default_role).send_messages is False
    assert channel.overwrites_for(guild.me).send_messages is True
    run(mod.lock.callback(mod, ctx, channel, reason=""))
    assert ctx.replies[-1].title == "Already locked"
    run(mod.unlock.callback(mod, ctx, channel))
    assert channel.overwrites_for(guild.default_role).send_messages is True
    run(mod.unlock.callback(mod, ctx, channel))
    assert ctx.replies[-1].title == "Not locked"


def test_modlog_receives_actions(mod):
    guild, boss, target = _setup(mod)
    log_channel = guild.channels[1]
    ctx = Ctx(guild, boss)
    run(mod.modlog.callback(mod, ctx, log_channel, ""))
    run(mod.warn.callback(mod, ctx, target, reason="logged"))
    assert log_channel.sent and "logged" in log_channel.sent[-1].description
    run(mod.modlog.callback(mod, ctx, None, "off"))
    assert mod.bot.storage.modlog_channel(guild.id) is None


def test_moderation_cog_loads_alongside_syke(cog):
    async def load():
        await cog.bot.add_cog(cog)
        mod = Moderation(cog.bot)
        await cog.bot.add_cog(mod)
        mod.expire.cancel()
        top_level = [c for c in cog.bot.tree.get_commands()]
        assert len(top_level) <= 100  # Discord's limit for global slash commands
        names = {c.name for c in cog.bot.commands}
        for name in ("restrict", "unrestrict", "restrictions", "timeout", "warn", "purge", "lock", "ban"):
            assert name in names
        needed = {getattr(check, "permission", None) for check in mod.restrict.checks}
        assert "manage_roles" in needed
        assert "ban_members" in {getattr(check, "permission", None) for check in mod.ban.checks}

    run(load())


def test_help_lists_moderation(cog):
    from test_commands import FakeCtx

    ctx = FakeCtx()
    run(cog.help.callback(cog, ctx))
    field = ctx.replies[-1].fields[-1]
    assert field.name == "Moderators" and "restrict" in field.value
    assert len(field.value) <= 1024
    assert len(help_lines("!")) <= 1024


def test_install_asks_for_moderation_permissions():
    from syke.bot import install_permissions

    perms = install_permissions()
    for name in ("manage_messages", "moderate_members", "kick_members", "ban_members", "attach_files"):
        assert getattr(perms, name), name
