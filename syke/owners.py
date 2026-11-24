"""SYKE's owners (SYKE_OWNER_IDS) can use every command in every server, manager or not.

`is_owner` works anywhere (no bot object needed), and `bypass_checks` lets owners through every permission,
role or manager check on every command. Checks that aren't about who you are still apply: a server-only
command still needs a server, and SYKE still needs his own permissions to act.
"""

from __future__ import annotations

from discord.ext import commands

_OWNERS: set[int] = set()

# Failures an owner is let past; everything else (no DM, SYKE lacking permissions, cooldowns) still applies.
_BYPASS = (commands.MissingPermissions, commands.MissingRole, commands.MissingAnyRole, commands.NotOwner,
           commands.CheckAnyFailure)
_KEEP = (commands.NoPrivateMessage, commands.PrivateMessageOnly, commands.BotMissingPermissions,
         commands.BotMissingRole, commands.BotMissingAnyRole, commands.NSFWChannelRequired)


def set_owners(ids) -> None:
    _OWNERS.clear()
    _OWNERS.update(int(i) for i in ids or ())


def is_owner(user) -> bool:
    """`user` is a member/user, or an id."""
    uid = getattr(user, "id", user)
    return isinstance(uid, int) and uid in _OWNERS


def _owner_proof(predicate):
    if getattr(predicate, "_owner_proof", False):
        return predicate

    async def check(ctx) -> bool:
        try:
            result = predicate(ctx)
            if hasattr(result, "__await__"):
                result = await result
        except _KEEP:
            raise
        except (*_BYPASS, commands.CheckFailure):
            if is_owner(ctx.author):
                return True
            raise
        if not result and is_owner(ctx.author):
            return True
        return result

    check._owner_proof = True
    for attr in ("permission", "__name__", "__qualname__"):   # keep what help pages read off the predicate
        if hasattr(predicate, attr):
            with_value = getattr(predicate, attr)
            try:
                setattr(check, attr, with_value)
            except (AttributeError, TypeError):
                pass
    check.__wrapped__ = predicate
    return check


def bypass_checks(bot) -> int:
    """Make every prefix/hybrid command's checks let owners through. Returns how many commands were touched."""
    touched = 0
    for command in bot.walk_commands():
        if command.checks:
            command.checks = [_owner_proof(c) for c in command.checks]
            touched += 1
    return touched
