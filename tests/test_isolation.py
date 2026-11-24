"""Isolation: a role that hides every channel and every member, with roles saved and given back."""

import time
import types
from functools import total_ordering

import discord
import pytest

from syke.isolation import ROLE_NAME, Isolation, split_details

GID = 42


@total_ordering
class Role:
    def __init__(self, rid, name, position, perms=None, managed=False, default=False):
        self.id, self.name, self.position, self.managed, self._default = rid, name, position, managed, default
        self.permissions = types.SimpleNamespace(**{**{p: False for p in (
            "administrator", "manage_guild", "manage_roles", "moderate_members", "kick_members", "ban_members")},
            **(perms or {})})
        self.mention = f"<@&{rid}>"

    def is_default(self):
        return self._default

    def __eq__(self, other):
        return isinstance(other, Role) and other.id == self.id

    def __lt__(self, other):
        return self.position < other.position

    def __hash__(self):
        return self.id


class Channel:
    type = discord.ChannelType.text

    def __init__(self, cid, name, category=None, synced=False):
        self.id, self.name, self.category, self.permissions_synced = cid, name, category, synced
        self.overwrites = {}
        self.sent = []
        self.mention = f"<#{cid}>"

    def overwrites_for(self, target):
        return self.overwrites.get(getattr(target, "id", target), discord.PermissionOverwrite())

    async def set_permissions(self, target, overwrite=None, reason=None):
        self.overwrites[target.id] = overwrite

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


class Category(Channel):
    type = discord.ChannelType.category


class Guild:
    def __init__(self):
        self.id, self.owner_id = GID, 1
        self.default_role = Role(GID, "@everyone", 0, default=True)
        self.bot_role = Role(900, "SYKE", 50)
        self.mod = Role(800, "Mods", 40, {"moderate_members": True})
        self.vip = Role(700, "VIP", 10)
        self.booster = Role(600, "Booster", 9, managed=True)
        self.boss = Role(950, "Boss", 60)
        self.roles = [self.default_role, self.booster, self.vip, self.mod, self.bot_role, self.boss]
        self.cat = Category(1, "Main")
        self.general = Channel(2, "general", self.cat, synced=True)
        self.secret = Channel(3, "vip-only", self.cat)
        self.voice = Channel(4, "voice")
        self.channels = [self.cat, self.general, self.secret, self.voice]
        self.me = type("Me", (), {})()
        self.me.id, self.me.top_role, self.me.mention = 999, self.bot_role, "<@999>"
        self.me.guild_permissions = types.SimpleNamespace(manage_roles=True, manage_channels=True, administrator=False)
        self.members = {}

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_channel(self, cid):
        return next((c for c in self.channels if c.id == cid), None)

    def get_member(self, uid):
        return self.members.get(uid)

    async def create_role(self, name, **kwargs):
        role = Role(500, name, 1)
        self.roles.append(role)
        return role

    async def create_text_channel(self, name, overwrites=None, **kwargs):
        channel = Channel(5, name)
        channel.overwrites = {t.id: o for t, o in (overwrites or {}).items()}
        self.channels.append(channel)
        return channel


class Member:
    def __init__(self, guild, uid, roles, admin=False):
        self.guild, self.id, self.bot = guild, uid, False
        self.roles = [guild.default_role, *roles]
        self.mention, self.display_name = f"<@{uid}>", f"user{uid}"
        self.voice = None
        self.guild_permissions = types.SimpleNamespace(administrator=admin, manage_roles=False)
        guild.members[uid] = self

    @property
    def top_role(self):
        return max(self.roles)

    async def edit(self, roles=None, reason=None):
        self.roles = [self.guild.default_role, *[r for r in roles if not r.is_default()]]

    async def add_roles(self, *roles, reason=None):
        self.roles += [r for r in roles if r not in self.roles]

    async def send(self, *a, **k):
        raise discord.HTTPException(types.SimpleNamespace(status=403, reason="dm closed"), "closed")


@pytest.fixture()
def iso(cog):
    bot = cog.bot
    bot.spawn = lambda coro: coro.close()
    return Isolation(bot)


def test_details_split_into_length_and_reason():
    assert split_details("2h spamming in vc") == (7200, "spamming in vc")
    assert split_details("being weird") == (None, "being weird")
    assert split_details("perm raiding") == (None, "raiding")
    assert split_details("") == (None, "")


async def test_isolation_hides_everything_and_gives_roles_back(iso):
    guild = Guild()
    member = Member(guild, 7, [guild.vip, guild.booster])
    taken, stuck = await iso.isolate_member(member, seconds=3600, reason="spam", by=None)
    role = iso.role(guild)
    holding = iso.holding(guild)
    assert role.name == ROLE_NAME == "SYKE · contaminated" and holding.name == "contamination"
    assert taken == [guild.vip] and stuck == [guild.booster]               # Discord-managed roles can't be taken
    assert set(member.roles) == {guild.default_role, guild.booster, role}
    # Every channel hidden; the synced channel inherits its category's rule; holding channel visible to them + mods
    assert guild.cat.overwrites_for(role).view_channel is False
    assert guild.secret.overwrites_for(role).view_channel is False and guild.voice.overwrites_for(role).connect is False
    assert role.id not in guild.general.overwrites                          # synced: the category covers it
    assert holding.overwrites_for(role).view_channel is True
    assert holding.overwrites_for(guild.default_role).view_channel is False
    assert holding.overwrites_for(guild.mod).view_channel is True and guild.vip.id not in holding.overwrites
    record = iso.storage.isolation(GID, 7)
    assert record["roles"] == [guild.vip.id] and record["expires_at"] > time.time()
    back = await iso.release_member(member)
    assert back == [guild.vip] and set(member.roles) == {guild.default_role, guild.booster, guild.vip}
    assert iso.storage.isolation(GID, 7) is None


async def test_role_given_or_removed_by_hand(iso):
    guild = Guild()
    role, _, _ = await iso.ensure_setup(guild)
    member = Member(guild, 8, [guild.vip])
    before = types.SimpleNamespace(roles=list(member.roles))
    member.roles.append(role)                                                # a mod ticks the role in Discord
    await iso.on_member_update(before, member)
    assert set(member.roles) == {guild.default_role, role} and iso.storage.isolation(GID, 8)["roles"] == [guild.vip.id]
    assert guild.secret.overwrites_for(role).view_channel is False         # and the channels got hidden
    assert "contaminated" in iso.holding(guild).sent[-1]
    before = types.SimpleNamespace(roles=list(member.roles))
    member.roles.remove(role)                                                # ...and unticks it
    await iso.on_member_update(before, member)
    assert set(member.roles) == {guild.default_role, guild.vip} and iso.storage.isolation(GID, 8) is None


async def test_rejoining_and_new_channels_dont_escape(iso):
    guild = Guild()
    member = Member(guild, 9, [guild.vip])
    await iso.isolate_member(member, seconds=None, reason="")
    role = iso.role(guild)
    rejoined = Member(guild, 9, [])
    await iso.on_member_join(rejoined)
    assert role in rejoined.roles
    fresh = Channel(10, "new-chat")
    fresh.guild = guild
    await iso.on_guild_channel_create(fresh)
    assert fresh.overwrites_for(role).view_channel is False
    synced = Channel(11, "new-in-main", guild.cat, synced=True)
    synced.guild = guild
    await iso.on_guild_channel_create(synced)
    assert role.id not in synced.overwrites                                   # its category already hides it


async def test_time_runs_out(iso, monkeypatch):
    guild = Guild()
    iso.bot.get_guild = lambda gid: guild if gid == GID else None
    member = Member(guild, 7, [guild.vip])
    await iso.isolate_member(member, seconds=60, reason="")
    gone = Member(guild, 6, [guild.vip])
    await iso.isolate_member(gone, seconds=60, reason="")
    del guild.members[6]                                                    # left the server
    monkeypatch.setattr("syke.isolation.time.time", lambda: time.time_ns() / 1e9 + 120)
    await Isolation.expire.coro(iso)
    assert guild.vip in member.roles and iso.storage.isolations(GID) == []


async def test_isolate_command_checks_rank_and_admins(iso):
    guild = Guild()
    said = []

    async def reply(content=None, **kwargs):
        said.append(kwargs["embed"].title)

    class _T:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    mod = Member(guild, 2, [guild.mod])
    mod.guild_permissions = types.SimpleNamespace(administrator=False, manage_roles=True)
    ctx = types.SimpleNamespace(guild=guild, author=mod, reply=reply, typing=lambda: _T())
    boss = Member(guild, 3, [guild.boss])
    await Isolation.isolate.callback(iso, ctx, boss, details="")
    assert said[-1] == "Can't contaminate them"                                  # above the mod and above SYKE
    target = Member(guild, 4, [guild.vip])
    await Isolation.isolate.callback(iso, ctx, target, details="30m spamming")
    assert said[-1] == "☣️ user4 is contaminated" and iso.storage.isolation(GID, 4)["reason"] == "spamming"
    assert iso.storage.cases(GID, 4)[0][2:4] == ("contaminate", "spamming")      # in their mod history
    await Isolation.unisolate.callback(iso, ctx, target, reason="")
    assert said[-1] == "🧼 user4 is decontaminated" and guild.vip in target.roles
    await Isolation.unisolate.callback(iso, ctx, target, reason="")
    assert said[-1] == "Not contaminated"


async def test_old_isolation_names_are_renamed(iso):
    guild = Guild()
    role, channel, _ = await iso.ensure_setup(guild)
    renamed = []

    async def edit(name=None, **kwargs):
        renamed.append(name)

    role.name, channel.name = "SYKE · isolated", "isolation"
    role.edit = channel.edit = edit
    await iso.ensure_setup(guild)
    assert renamed == ["SYKE · contaminated", "contamination"]


def test_command_names(cog):
    from syke.isolation import Isolation as Cog
    names = {c.name: c.aliases for c in Cog(cog.bot).get_commands()}
    assert "contimate" in names["contaminate"] and "isolate" in names["contaminate"]
    assert "unisolate" in names["decontaminate"] and "isolated" in names["contaminated"]
