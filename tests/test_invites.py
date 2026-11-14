import asyncio
import types
from datetime import datetime, timezone

import discord

from syke.bot import install_permissions
from syke.invites import InviteUse, join_lines, remember_invite, settle_invites


class FakeCtx:
    def __init__(self, guild_id: int = 42):
        self.guild = types.SimpleNamespace(id=guild_id, me=None, get_channel=lambda cid: None)
        self.author = types.SimpleNamespace(id=1234, bot=False, display_name="Tester")
        self.replies: list = []

    async def reply(self, content=None, **kwargs):
        self.replies.append(kwargs["embed"] if kwargs.get("embed") is not None else content)
        return types.SimpleNamespace(id=len(self.replies))


def run(coro):
    return asyncio.run(coro)


def _use(code="abc", uses=1, max_uses=0, inviter_id=55, name="Bob", vanity=False) -> InviteUse:
    return InviteUse(code, uses, max_uses, inviter_id, name, vanity)


def test_install_asks_for_manage_server():
    # SYKE is now invited with Administrator, which covers Manage Server.
    assert install_permissions().administrator


def test_one_new_use_names_that_invite():
    before = {"abc": _use(uses=2)}
    chosen, cache = settle_invites(before, [_use(uses=3)])
    assert chosen is not None and chosen.inviter_id == 55 and chosen.code == "abc"
    assert cache["abc"].uses == 3


def test_extra_uses_stay_pending_for_the_next_join():
    before = {"abc": _use(uses=2)}
    after = [_use(uses=4)]
    chosen, cache = settle_invites(before, after)
    assert chosen is not None and chosen.inviter_id == 55
    assert cache["abc"].uses == 3
    chosen_again, cache = settle_invites(cache, after)
    assert chosen_again is not None and chosen_again.code == "abc"
    assert cache["abc"].uses == 4


def test_two_invites_growing_is_a_tie():
    before = {"abc": _use("abc", 1, inviter_id=55), "xyz": _use("xyz", 4, inviter_id=66, name="Cara")}
    after = [_use("abc", 2, inviter_id=55), _use("xyz", 5, inviter_id=66, name="Cara")]
    chosen, cache = settle_invites(before, after)
    assert chosen is None
    assert cache["abc"].uses == 2 and cache["xyz"].uses == 5


def test_brand_new_invite_used_once():
    chosen, cache = settle_invites({}, [_use(uses=1)])
    assert chosen is not None and chosen.code == "abc"
    assert cache["abc"].uses == 1


def test_used_up_invite_that_vanished():
    before = {"once": _use("once", uses=0, max_uses=1, inviter_id=9, name="Dee")}
    chosen, cache = settle_invites(before, [])
    assert chosen is not None and chosen.code == "once" and chosen.inviter_id == 9
    assert cache == {}


def test_deleting_an_unlimited_invite_blames_nobody():
    chosen, cache = settle_invites({"keep": _use("keep", uses=4, max_uses=0)}, [])
    assert chosen is None
    assert cache == {}


def test_vanity_growth_has_no_person():
    before = {"cool": _use("cool", uses=10, inviter_id=None, name="", vanity=True)}
    chosen, _ = settle_invites(before, [_use("cool", uses=11, inviter_id=None, name="", vanity=True)])
    assert chosen is not None and chosen.vanity and chosen.inviter_id is None


def test_stale_create_event_does_not_rewind_uses():
    cache = {"abc": _use(uses=3)}
    remember_invite(cache, _use(uses=0, name="Bobby"))
    assert cache["abc"].uses == 3
    assert cache["abc"].inviter_name == "Bob"
    remember_invite(cache, _use(uses=4, name="Bobby"))
    assert cache["abc"].uses == 4
    assert cache["abc"].inviter_name == "Bobby"


def test_join_lines_cover_each_answer():
    title, body, found = join_lines("Ada", self_lookup=False, owner=False, row=(55, "Bob", "abc", "user", 1_700_000_000))
    assert found and title == "Invited by 📨"
    assert "**Ada** was invited by **Bob** with `discord.gg/abc`" in body
    assert "<t:1700000000:R>" in body

    title, body, found = join_lines("Ada", self_lookup=True, owner=False, row=(55, "Bob", "abc", "user", 1_700_000_000))
    assert found and body.startswith("You were invited by **Bob**")

    title, body, found = join_lines("Ada", self_lookup=False, owner=False, row=(None, "", "cool", "vanity", 10))
    assert found and title == "Vanity invite 📨" and "vanity invite `discord.gg/cool`" in body

    title, body, found = join_lines("Ada", self_lookup=False, owner=False, row=(None, "", "abc", "user", 10))
    assert found and "doesn't say who created it" in body and "`discord.gg/abc`" in body

    title, body, found = join_lines("Ada", self_lookup=False, owner=False, row=(None, "", "", "unknown", 10))
    assert not found and "couldn't tell which invite" in body

    title, body, found = join_lines("Ada", self_lookup=False, owner=True, row=None)
    assert not found and "owns this server" in body

    title, body, found = join_lines("Ada", self_lookup=True, owner=False, row=None)
    assert not found and "didn't see you join" in body


def _member(uid, name, joined_at=None):
    return types.SimpleNamespace(id=uid, display_name=name, guild=None, joined_at=joined_at,
                                 display_avatar=types.SimpleNamespace(url="https://cdn.example/a.png"))


def test_inviter_command_reports_the_stored_person(cog):
    cog.bot.storage.remember_join(
        42, 77, inviter_id=55, inviter_name="Bob", code="abc", kind="user", joined_at=1_700_000_000,
    )
    ctx = FakeCtx()
    member = _member(77, "Ada")
    run(cog.inviter.callback(cog, ctx, member))
    embed = ctx.replies[-1]
    assert embed.title == "Invited by 📨"
    assert "Ada" in embed.description and "Bob" in embed.description
    assert embed.author.name == "Ada"
    assert embed.colour == discord.Colour.from_str("#8B5CF6")

    ctx.author.id = 77
    ctx.author.display_name = "Ada"
    run(cog.inviter.callback(cog, ctx, None))
    assert ctx.replies[-1].description.startswith("You were invited by **Bob**")


def test_inviter_command_shrugs_when_the_join_was_missed(cog):
    ctx = FakeCtx()
    run(cog.inviter.callback(cog, ctx, _member(77, "Ada")))
    assert ctx.replies[-1].title == "No witnesses 🔬"
    assert "didn't see **Ada** join" in ctx.replies[-1].description

    ctx.guild.owner_id = 77
    run(cog.inviter.callback(cog, ctx, _member(77, "Ada")))
    assert "owns this server" in ctx.replies[-1].description


def test_rejoin_replaces_the_stored_inviter(cog):
    cog.bot.storage.remember_join(1, 2, inviter_id=3, inviter_name="A", code="a", kind="user", joined_at=1)
    cog.bot.storage.remember_join(1, 2, inviter_id=4, inviter_name="B", code="b", kind="user", joined_at=2)
    assert cog.bot.storage.join_of(1, 2) == (4, "B", "b", "user", 2)
    assert cog.bot.storage.join_of(1, 9) is None


def test_join_records_the_invite_that_grew(cog):
    bot = cog.bot
    bot._invite_settle = 0
    bot._invite_cache[42] = {"abc": _use(uses=2)}

    async def fetch(_guild):
        return [_use(uses=3)]

    bot._fetch_invite_uses = fetch
    member = _member(77, "Ada", datetime(2024, 1, 2, tzinfo=timezone.utc))
    member.guild = types.SimpleNamespace(id=42, name="Lab")
    run(bot.on_member_join(member))
    assert bot.storage.join_of(42, 77)[:4] == (55, "Bob", "abc", "user")
    assert bot._invite_cache[42]["abc"].uses == 3


def test_two_joins_on_one_invite_are_both_recorded(cog):
    bot = cog.bot
    bot._invite_settle = 0
    bot._invite_cache[1] = {"abc": _use(uses=0)}

    async def fetch(_guild):
        return [_use(uses=2)]

    bot._fetch_invite_uses = fetch

    async def both():
        for uid in (10, 11):
            member = _member(uid, "Ada")
            member.guild = types.SimpleNamespace(id=1)
            await bot.on_member_join(member)

    run(both())
    assert bot.storage.join_of(1, 10)[0] == 55
    assert bot.storage.join_of(1, 11)[0] == 55
    assert bot._invite_cache[1]["abc"].uses == 2


def test_join_without_permission_is_not_recorded(cog):
    bot = cog.bot
    bot._invite_settle = 0

    async def fetch(_guild):
        return None

    bot._fetch_invite_uses = fetch
    member = _member(10, "Ada")
    member.guild = types.SimpleNamespace(id=4, name="Lab")
    run(bot.on_member_join(member))
    assert bot.storage.join_of(4, 10) is None
    assert 4 not in bot._invite_cache


def test_cold_cache_does_not_guess(cog):
    bot = cog.bot
    bot._invite_settle = 0

    async def fetch(_guild):
        return [_use(uses=40)]

    bot._fetch_invite_uses = fetch
    member = _member(10, "Ada")
    member.guild = types.SimpleNamespace(id=3)
    run(bot.on_member_join(member))
    assert bot.storage.join_of(3, 10) is None
    assert bot._invite_cache[3]["abc"].uses == 40


def test_single_use_invite_deleted_on_join_is_recorded(cog):
    bot = cog.bot
    bot._invite_settle = 0
    bot._invite_cache[5] = {"once": _use("once", uses=0, max_uses=1)}

    async def fetch(_guild):
        return []

    bot._fetch_invite_uses = fetch
    invite = types.SimpleNamespace(code="once", guild=types.SimpleNamespace(id=5))
    member = _member(12, "Ada")
    member.guild = types.SimpleNamespace(id=5)

    async def go():
        await bot.on_invite_delete(invite)
        await bot.on_member_join(member)

    run(go())
    assert bot.storage.join_of(5, 12)[:4] == (55, "Bob", "once", "user")
    assert "once" not in bot._invite_cache[5]


def test_invite_create_updates_a_loaded_cache_only(cog):
    bot = cog.bot
    bot._invite_cache[5] = {}
    invite = types.SimpleNamespace(
        code="new", uses=0, max_uses=0,
        inviter=types.SimpleNamespace(id=55, display_name="Bob", name="bob"),
        guild=types.SimpleNamespace(id=5),
    )
    run(bot.on_invite_create(invite))
    assert bot._invite_cache[5]["new"].inviter_id == 55

    bot._invite_cache.pop(5)
    bot._invite_locks.pop(5, None)
    run(bot.on_invite_create(invite))
    assert 5 not in bot._invite_cache


def test_fetch_includes_vanity_and_refuses_without_permission(cog):
    async def invites():
        return [
            types.SimpleNamespace(code="abc", uses=2, max_uses=0,
                                  inviter=types.SimpleNamespace(id=5, name="Bob", display_name="Bob")),
            types.SimpleNamespace(code="cool", uses=1, max_uses=0,
                                  inviter=types.SimpleNamespace(id=9, name="Nope", display_name="Nope")),
        ]

    async def vanity():
        return types.SimpleNamespace(code="cool", uses=9, max_uses=0, inviter=None)

    guild = types.SimpleNamespace(
        id=1,
        me=types.SimpleNamespace(guild_permissions=discord.Permissions(manage_guild=True)),
        features=["VANITY_URL"],
        invites=invites,
        vanity_invite=vanity,
    )
    found = {inv.code: inv for inv in run(cog.bot._fetch_invite_uses(guild))}
    assert found["abc"].inviter_id == 5
    assert found["cool"].vanity and found["cool"].uses == 9 and found["cool"].inviter_id is None

    guild.me = types.SimpleNamespace(guild_permissions=discord.Permissions.none())
    assert run(cog.bot._fetch_invite_uses(guild)) is None


def test_ambiguous_join_is_stored_as_unknown(cog):
    bot = cog.bot
    bot._invite_settle = 0
    bot._invite_cache[8] = {"abc": _use("abc", 1), "xyz": _use("xyz", 1, inviter_id=66, name="Cara")}

    async def fetch(_guild):
        return [_use("abc", 2), _use("xyz", 2, inviter_id=66, name="Cara")]

    bot._fetch_invite_uses = fetch
    member = _member(12, "Ada")
    member.guild = types.SimpleNamespace(id=8)
    run(bot.on_member_join(member))
    assert bot.storage.join_of(8, 12)[3] == "unknown"

    ctx = FakeCtx(8)
    run(cog.inviter.callback(cog, ctx, _member(12, "Ada")))
    assert ctx.replies[-1].title == "No witnesses 🔬"
    assert "couldn't tell which invite" in ctx.replies[-1].description
