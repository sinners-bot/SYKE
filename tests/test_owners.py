"""SYKE's owners can use every command in every server, manager or not."""

import types

import pytest
from discord.ext import commands

from syke import owners
from syke.polls import can_manage
from syke.tickets import is_staff
from test_help import bot  # noqa: F401  (every cog loaded)

OWNER = 1342786189576634398


def _ctx(bot, author_id, guild=True, manage=False):
    perms = types.SimpleNamespace(manage_guild=manage, administrator=False, manage_messages=manage,
                                  kick_members=manage, ban_members=manage, moderate_members=manage,
                                  manage_channels=manage, manage_roles=manage, manage_threads=manage,
                                  manage_nicknames=manage, manage_emojis=manage, manage_expressions=manage)
    author = types.SimpleNamespace(id=author_id, guild_permissions=perms, roles=[], bot=False)
    g = types.SimpleNamespace(id=1, owner_id=5, me=None) if guild else None
    return types.SimpleNamespace(bot=bot, author=author, guild=g, interaction=None, me=None,
                                 channel=types.SimpleNamespace(id=2), message=types.SimpleNamespace(author=author))


async def _passes(command, ctx) -> bool:
    for check in command.checks:
        try:
            result = check(ctx)
            if hasattr(result, "__await__"):
                result = await result
        except commands.CheckFailure:
            return False
        if not result:
            return False
    return True


async def test_owner_passes_every_permission_check(bot):
    owners.set_owners(bot.settings.owner_ids)
    beef = bot.get_command("beefcomments")
    assert not await _passes(beef, _ctx(bot, OWNER))   # before: Discord's own check knows nothing of owners
    assert owners.bypass_checks(bot) > 30               # what SYKE does at startup
    assert owners.bypass_checks(bot) > 30 and len(beef.checks) == len(beef.checks)   # safe to run twice
    assert OWNER in bot.settings.owner_ids
    gated = [c for c in bot.walk_commands() if c.checks]
    assert len(gated) > 30
    blocked_for_members = 0
    for command in gated:
        assert await _passes(command, _ctx(bot, OWNER)), f"owner blocked from {command.qualified_name}"
        blocked_for_members += not await _passes(command, _ctx(bot, 123))
    assert blocked_for_members > 20            # normal members are still kept out of manager commands
    for name in ("beefcomments", "persona", "ban", "purge", "servers"):
        assert bot.get_command(name) is not None


async def test_server_only_still_needs_a_server(bot):
    owners.set_owners(bot.settings.owner_ids)
    owners.bypass_checks(bot)
    ban = bot.get_command("ban")
    with pytest.raises(commands.NoPrivateMessage):
        for check in ban.checks:
            result = check(_ctx(bot, OWNER, guild=False))
            if hasattr(result, "__await__"):
                await result


def test_inline_manager_checks_know_owners(bot):
    owners.set_owners(bot.settings.owner_ids)
    owner = types.SimpleNamespace(id=OWNER, guild_permissions=None, roles=[])
    member = types.SimpleNamespace(id=5, guild_permissions=None, roles=[])
    assert can_manage(owner) and not can_manage(member)
    assert is_staff(owner, {}) and not is_staff(member, {})
    assert owners.is_owner(OWNER) and not owners.is_owner(member)
