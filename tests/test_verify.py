import types

import discord

from syke.bot import _admin_predicate, install_permissions
from syke.verify import VERIFY_BUTTON, VerifyView, unacceptable_role
from test_commands import FakeCtx, run


def test_install_asks_for_manage_roles():
    assert install_permissions().manage_roles


def test_unacceptable_roles():
    everyone = types.SimpleNamespace(id=1, name="@everyone", managed=False, position=0, mention="@everyone",
                                     is_default=lambda: True)
    managed = types.SimpleNamespace(id=2, name="Syke", managed=True, position=1, mention="<@&2>",
                                    is_default=lambda: False)
    high = types.SimpleNamespace(id=3, name="Admin", managed=False, position=20, mention="<@&3>",
                                 is_default=lambda: False)
    fine = types.SimpleNamespace(id=4, name="Verified", managed=False, position=2, mention="<@&4>",
                                 is_default=lambda: False)
    assert "everyone" in unacceptable_role(everyone, bot_position=10, other_id=None)
    assert "managed" in unacceptable_role(managed, bot_position=10, other_id=None).lower()
    assert "above" in unacceptable_role(high, bot_position=10, other_id=None)
    assert unacceptable_role(fine, bot_position=10, other_id=None) is None
    assert "other verification role" in unacceptable_role(fine, bot_position=10, other_id=4)


def test_verification_settings_roundtrip(cog):
    assert cog.bot.storage.verification(42) == (None, None, None, None, None, None)
    cog.bot.storage.update_verification(42, role_id=5, text="hello")
    cog.bot.storage.update_verification(42, channel_id=9)
    assert cog.bot.storage.verification(42) == (5, None, 9, "hello", None, None)
    assert cog.bot.storage.clear_verification(42)
    assert not cog.bot.storage.clear_verification(42)
    assert cog.bot.storage.verification(42)[0] is None


class Person:
    def __init__(self, uid=7, admin=False):
        self.id = uid
        self.display_name = "Ada"
        self.bot = False
        self.roles = []
        self.added = []
        self.removed = []
        self.guild_permissions = discord.Permissions(manage_guild=admin)

    async def add_roles(self, *roles, reason=None, atomic=True):
        self.added.append((roles, reason))
        for role in roles:
            if not any(existing.id == role.id for existing in self.roles):
                self.roles.append(role)

    async def remove_roles(self, *roles, reason=None, atomic=True):
        self.removed.append((roles, reason))
        gone = {role.id for role in roles}
        self.roles = [role for role in self.roles if role.id not in gone]


def _role(rid, name, position=1, managed=False, default=False):
    return types.SimpleNamespace(
        id=rid, name=name, position=position, managed=managed, mention=f"<@&{rid}>",
        is_default=lambda: default,
    )


class Chan:
    def __init__(self, cid=3):
        self.id = cid
        self.mention = f"<#{cid}>"
        self.sent = []
        self.by_id = {}

    def permissions_for(self, me):
        return discord.Permissions(send_messages=True, embed_links=True, view_channel=True)

    async def send(self, **kwargs):
        message = types.SimpleNamespace(id=100 + len(self.by_id))
        message.edit = self._edit(message)
        message.delete = self._delete(message)
        self.by_id[message.id] = message
        self.sent.append(kwargs)
        return message

    def _edit(self, message):
        async def edit(**kwargs):
            message.edits = getattr(message, "edits", []) + [kwargs]
        return edit

    def _delete(self, message):
        async def delete():
            message.deleted = True
        return delete

    async def fetch_message(self, mid):
        try:
            return self.by_id[mid]
        except KeyError:
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="missing"), "missing") from None


def _guild(roles, channel=None):
    by_id = {role.id: role for role in roles}

    def get_channel(cid):
        return channel if channel is not None and channel.id == cid else None

    return types.SimpleNamespace(
        id=42,
        me=types.SimpleNamespace(
            guild_permissions=discord.Permissions(manage_roles=True),
            top_role=types.SimpleNamespace(position=10),
        ),
        get_role=lambda rid: by_id.get(rid),
        get_channel=get_channel,
        get_channel_or_thread=get_channel,
    )


def _ctx(guild, person, channel):
    ctx = FakeCtx(guild.id)
    ctx.guild = guild
    ctx.author = person
    ctx.channel = channel
    return ctx


def test_member_verify_grants_and_removes(cog):
    verified = _role(5, "Verified")
    newbie = _role(6, "Newbie")
    channel = Chan()
    guild = _guild([verified, newbie], channel)
    person = Person()
    person.roles = [newbie]
    ctx = _ctx(guild, person, channel)

    run(cog.verify.callback(cog, ctx))
    assert ctx.replies[-1].title == "Verification isn't on"

    run(cog.verify_role.callback(cog, ctx, verified))
    assert "<@&5>" in ctx.replies[-1].description
    run(cog.verify_take.callback(cog, ctx, newbie))
    run(cog.verify_channel.callback(cog, ctx, channel))
    run(cog.verify.callback(cog, ctx))
    assert ctx.replies[-1].title == "You're in ✅"
    assert person.added[0][1] == "Verified with SYKE"
    assert person.added[0][0] == (verified,)
    assert person.removed[0][0] == (newbie,)
    assert [role.id for role in person.roles] == [5]

    run(cog.verify.callback(cog, ctx))
    assert ctx.replies[-1].title == "Already verified"


def test_verify_rejects_bad_roles_and_the_wrong_channel(cog):
    verified = _role(5, "Verified", position=1)
    high = _role(8, "Admin", position=50)
    channel = Chan()
    other = Chan(9)
    guild = _guild([verified, high], channel)
    ctx = _ctx(guild, Person(admin=True), other)

    run(cog.verify_role.callback(cog, ctx, high))
    assert ctx.replies[-1].title == "That role won't work"
    assert cog.bot.storage.verification(42)[0] is None

    run(cog.verify_role.callback(cog, ctx, verified))
    run(cog.verify_take.callback(cog, ctx, verified))
    assert "other verification role" in ctx.replies[-1].description
    assert cog.bot.storage.verification(42)[1] is None

    run(cog.verify_channel.callback(cog, ctx, channel))
    run(cog.verify.callback(cog, ctx))
    assert ctx.replies[-1].title == "Not here"
    assert "<#3>" in ctx.replies[-1].description

    run(cog.verify_anywhere.callback(cog, ctx))
    assert cog.bot.storage.verification(42)[2] is None
    run(cog.verify.callback(cog, ctx))
    assert ctx.replies[-1].title == "You're in ✅"


def test_setup_panel_message_and_off(cog):
    verified = _role(5, "Verified")
    channel = Chan()
    guild = _guild([verified], channel)
    ctx = _ctx(guild, Person(admin=True), channel)

    run(cog.verify_setup.callback(cog, ctx))
    assert ctx.replies[-1].title == "Set up verification 🚪"
    assert "`!verify role @Verified`" in ctx.replies[-1].description
    assert "`!verify @member @Role`" in ctx.replies[-1].description

    run(cog.verify_panel.callback(cog, ctx))
    assert ctx.replies[-1].title == "Set up verification 🚪"

    run(cog.verify_role.callback(cog, ctx, verified))
    run(cog.verify_message.callback(cog, ctx, text="come in"))
    run(cog.verify_panel.callback(cog, ctx))
    assert ctx.replies[-1].title == "Panel posted ✅"
    assert channel.sent[-1]["embed"].description == "come in"
    assert channel.sent[-1]["view"].is_persistent()
    assert channel.sent[-1]["view"].children[0].custom_id == VERIFY_BUTTON
    assert cog.bot.storage.verification(42)[4:] == (3, 100)

    run(cog.verify_message.callback(cog, ctx, text="welcome"))
    assert ctx.replies[-1].description == "The posted panel is updated."
    assert channel.by_id[100].edits[-1]["embed"].description == "welcome"
    assert len(channel.sent) == 1

    run(cog.verify_panel.callback(cog, ctx))
    assert ctx.replies[-1].title == "Panel updated ✅"
    assert len(channel.sent) == 1

    run(cog.verify_message.callback(cog, ctx, text="reset"))
    assert channel.by_id[100].edits[-1]["embed"].description.startswith("Press **Verify**")

    run(cog.verify_off.callback(cog, ctx))
    assert ctx.replies[-1].title == "Verification off"
    assert cog.bot.storage.verification(42)[0] is None
    run(cog.verify.callback(cog, ctx))
    assert ctx.replies[-1].title == "Set up verification 🚪"


def test_button_verifies(cog):
    verified = _role(5, "Verified")
    channel = Chan()
    guild = _guild([verified], channel)
    cog.bot.storage.update_verification(42, role_id=5)
    person = Person()
    followup = []

    class Response:
        async def defer(self, *, ephemeral=False):
            self.ephemeral = ephemeral

    interaction = types.SimpleNamespace(
        guild=guild, user=person, channel_id=channel.id, client=cog.bot,
        response=Response(), followup=types.SimpleNamespace(send=_send(followup)),
    )
    run(VerifyView().children[0].callback(interaction))
    assert followup[-1]["embed"].title == "You're in ✅"
    assert followup[-1]["ephemeral"] is True
    assert person.roles == [verified]


def test_missing_manage_roles_is_reported(cog, monkeypatch):
    perms = discord.Permissions(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
    channel = types.SimpleNamespace(permissions_for=lambda me: perms)
    guild = types.SimpleNamespace(
        id=42,
        me=types.SimpleNamespace(guild_permissions=discord.Permissions.none()),
        get_channel_or_thread=lambda cid: channel,
    )
    monkeypatch.setattr(cog.bot, "get_guild", lambda gid: guild)
    from test_commands import _slash_ctx

    ctx = _slash_ctx()
    run(cog.diagnose.callback(cog, ctx))
    text = ctx.replies[-1].description
    assert "⚠️ Manage Roles for verification" in text
    assert "!verify` needs **Manage Roles**" in text


def test_verify_commands_are_gated(cog):
    group = next(command for command in cog.get_commands() if command.name == "verify")
    assert _admin_predicate not in group.checks
    assert group.invoke_without_command
    assert [name for name in group.params] == ["member", "role"]
    assert group.params["member"].required is False
    assert group.params["role"].required is False
    slash = group.app_command.get_command("me")
    assert [param.name for param in slash.parameters] == ["member", "role"]
    assert all(not param.required for param in slash.parameters)
    by_name = {command.name: command for command in group.commands}
    for name in ("setup", "role", "take", "channel", "anywhere", "message", "panel", "off"):
        assert _admin_predicate in by_name[name].checks, name
    assert "remove" in by_name["take"].aliases
    assert "text" in by_name["message"].aliases


def test_admin_verifies_someone_and_can_choose_the_role(cog):
    verified = _role(5, "Verified")
    newbie = _role(6, "Newbie")
    trusted = _role(11, "Trusted")
    high = _role(8, "Admin", position=50)
    gate = Chan(3)
    staff = Chan(9)
    guild = _guild([verified, newbie, trusted, high], gate)
    admin = Person(uid=1, admin=True)
    member = Person(uid=8)
    member.mention = "<@8>"
    member.roles = [newbie]
    ctx = _ctx(guild, admin, staff)

    run(cog.verify.callback(cog, ctx, member))
    assert ctx.replies[-1].title == "Set up verification 🚪"
    assert member.added == [] and member.removed == []

    picked = Person(uid=20)
    picked.mention = "<@20>"
    picked.roles = [newbie]
    run(cog.verify.callback(cog, ctx, picked, trusted))
    assert ctx.replies[-1].title == "Verified ✅"
    assert "<@&11>" in ctx.replies[-1].description
    assert "Nothing is set to take away" in ctx.replies[-1].description
    assert [role.id for role in picked.roles] == [6, 11]
    assert cog.bot.storage.verification(42)[0] is None

    run(cog.verify_role.callback(cog, ctx, verified))
    run(cog.verify_take.callback(cog, ctx, newbie))
    run(cog.verify_channel.callback(cog, ctx, gate))
    run(cog.verify.callback(cog, ctx, member))
    assert ctx.replies[-1].title == "Verified ✅"
    assert "<@8>" in ctx.replies[-1].description
    assert "<@&5>" in ctx.replies[-1].description
    assert "<@&6> is gone" in ctx.replies[-1].description
    assert [role.id for role in member.roles] == [5]
    assert member.removed[0][0] == (newbie,)

    run(cog.verify.callback(cog, ctx, member))
    assert ctx.replies[-1].title == "Already verified"
    assert "already has" in ctx.replies[-1].description

    both = Person(uid=15)
    both.mention = "<@15>"
    both.roles = [verified, newbie]
    run(cog.verify.callback(cog, ctx, both))
    assert ctx.replies[-1].title == "Verified ✅"
    assert "<@&6> is gone" in ctx.replies[-1].description
    assert both.added == []
    assert [role.id for role in both.roles] == [5]

    other = Person(uid=9)
    other.mention = "<@9>"
    other.roles = [newbie]
    run(cog.verify.callback(cog, ctx, other, trusted))
    assert ctx.replies[-1].title == "Verified ✅"
    assert "<@&11>" in ctx.replies[-1].description
    assert "<@&6> is gone" in ctx.replies[-1].description
    assert [role.id for role in other.roles] == [11]
    assert cog.bot.storage.verification(42)[0] == 5

    stuck = Person(uid=10)
    stuck.roles = [newbie]
    run(cog.verify.callback(cog, ctx, stuck, high))
    assert ctx.replies[-1].title == "Role won't work"
    assert stuck.added == [] and stuck.removed == []
    run(cog.verify.callback(cog, ctx, stuck, newbie))
    assert "other verification role" in ctx.replies[-1].description
    assert [role.id for role in stuck.roles] == [6]

    plain = Person(uid=12)
    run(cog.verify_take.callback(cog, ctx, None))
    run(cog.verify.callback(cog, ctx, plain))
    assert ctx.replies[-1].title == "Verified ✅"
    assert "Nothing is set to take away" in ctx.replies[-1].description
    assert [role.id for role in plain.roles] == [5]

    robot = Person(uid=13)
    robot.bot = True
    run(cog.verify.callback(cog, ctx, robot, trusted))
    assert ctx.replies[-1].title == "Bots stay out"
    assert robot.added == []


def test_members_cannot_verify_someone_else_or_pick_a_role(cog):
    verified = _role(5, "Verified")
    guild = _guild([verified])
    person = Person(admin=False)
    other = Person(uid=8)
    ctx = _ctx(guild, person, Chan())
    cog.bot.storage.update_verification(42, role_id=5, remove_role_id=6)

    run(cog.verify.callback(cog, ctx, other))
    assert ctx.replies[-1].title == "Admins only"
    assert "another person" in ctx.replies[-1].description
    assert other.added == []

    run(cog.verify.callback(cog, ctx, None, verified))
    assert ctx.replies[-1].title == "Admins only"
    assert "choose the role" in ctx.replies[-1].description
    assert person.added == []

    run(cog.verify.callback(cog, ctx))
    assert ctx.replies[-1].title == "You're in ✅"
    assert [role.id for role in person.roles] == [5]


def test_channels_shows_verification(cog):
    verified = _role(5, "Verified")
    ctx = FakeCtx()
    ctx.guild.get_role = lambda rid: verified if rid == 5 else None
    run(cog.channels.callback(cog, ctx))
    assert {field.name: field.value for field in ctx.replies[-1].fields}["Verification"] == "off"
    cog.bot.storage.update_verification(42, role_id=5)
    run(cog.channels.callback(cog, ctx))
    assert {field.name: field.value for field in ctx.replies[-1].fields}["Verification"] == "<@&5>"


def _send(box):
    async def send(**kwargs):
        box.append(kwargs)
    return send
