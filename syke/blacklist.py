"""The blacklist: people SYKE ignores completely, in every server and in DMs.

A blacklisted user can't use any command (prefix, slash, buttons, menus or forms), and SYKE never replies to,
reacts to, yaps about, repeats or answers them: their messages never reach his listeners, and they're left out of
every chat he reads for context. Moderation still sees them, so the word filter, spam protection, raid
protection and mod logs keep working on them, and so does the owner dashboard: its chat view, live feed and DM
list show everything they say. Owners can't be blacklisted.

`blacklist add <user>`, `blacklist remove <user>` and `blacklist` (the list) are for SYKE's owners only.
"""

from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

from .owners import is_owner

log = logging.getLogger("syke.blacklist")

GLOBAL = 0              # guild_kv row that isn't about one server
KEY = "blacklist"

_BLOCKED: set[int] = set()

# Cogs that still see a blacklisted user's messages and joins: moderation is not acknowledging someone.
MODERATION_COGS = {"Moderation", "Automod", "Security", "Isolation", "Music"}

# Events about a person, and how to find who: event name -> function(args) -> user id or None.
_WHO = {
    "message": lambda a: _id(getattr(a[0], "author", None)),
    "message_edit": lambda a: _id(getattr(a[1], "author", None)) if len(a) > 1 else None,
    "raw_message_edit": lambda a: _raw_edit_author(a[0]),
    "reaction_add": lambda a: _id(a[1]) if len(a) > 1 else None,
    "reaction_remove": lambda a: _id(a[1]) if len(a) > 1 else None,
    "raw_reaction_add": lambda a: getattr(a[0], "user_id", None),
    "raw_reaction_remove": lambda a: getattr(a[0], "user_id", None),
    "typing": lambda a: _id(a[1]) if len(a) > 1 else None,
    "member_join": lambda a: _id(a[0]),
    "member_update": lambda a: _id(a[1]) if len(a) > 1 else None,
    "voice_state_update": lambda a: _id(a[0]),
}


def _id(user) -> int | None:
    uid = getattr(user, "id", None)
    return uid if isinstance(uid, int) else None


def _raw_edit_author(payload) -> int | None:
    message = getattr(payload, "message", None)
    if message is not None:
        return _id(getattr(message, "author", None))
    author = (getattr(payload, "data", None) or {}).get("author") or {}
    return int(author["id"]) if str(author.get("id", "")).isdigit() else None


def is_blocked(user) -> bool:
    uid = getattr(user, "id", user)
    return isinstance(uid, int) and uid in _BLOCKED and not is_owner(uid)


def blocked_ids() -> set[int]:
    return {uid for uid in _BLOCKED if not is_owner(uid)}


def load(storage, seed=()) -> None:
    """Read the list from storage; `seed` ids are added once (and stay unless removed)."""
    data = storage.get_config(GLOBAL, KEY) or {}
    ids = {int(i) for i in data.get("ids", []) if str(i).isdigit()}
    seeded = {int(i) for i in data.get("seeded", []) if str(i).isdigit()}
    new = {int(i) for i in seed} - seeded
    if new:
        ids |= new
        storage.set_config(GLOBAL, KEY, {"ids": sorted(ids), "seeded": sorted(seeded | new)})
    _BLOCKED.clear()
    _BLOCKED.update(ids)


def _save(storage) -> None:
    data = storage.get_config(GLOBAL, KEY) or {}
    storage.set_config(GLOBAL, KEY, {**data, "ids": sorted(_BLOCKED)})


def event_user(event: str, args: tuple) -> int | None:
    pick = _WHO.get(event)
    if pick is None:
        return None
    try:
        return pick(args)
    except (IndexError, AttributeError, TypeError, ValueError, KeyError):
        return None


def moderation_only(listener) -> bool:
    """Listeners that still get a blacklisted user's events: moderation, and watchers that only observe for the
    owner (the dashboard's live feed marks itself with `sees_blacklisted = True`)."""
    owner = getattr(listener, "__self__", None)
    return getattr(owner, "qualified_name", None) in MODERATION_COGS or getattr(owner, "sees_blacklisted", False)


def interaction_user(data: dict) -> int | None:
    user = (data.get("member") or {}).get("user") or data.get("user") or {}
    return int(user["id"]) if str(user.get("id", "")).isdigit() else None


def guard_interactions(bot) -> None:
    """Drop interactions from blacklisted users before discord.py sees them: no command, button, menu or form
    runs, and SYKE sends nothing back."""
    parsers = getattr(getattr(bot, "_connection", None), "parsers", None)
    if not isinstance(parsers, dict) or "INTERACTION_CREATE" not in parsers:
        return
    original = parsers["INTERACTION_CREATE"]
    if getattr(original, "_blacklist_guard", False):
        return

    def parse(data):
        if is_blocked(interaction_user(data or {})):
            return None
        return original(data)

    parse._blacklist_guard = True
    parsers["INTERACTION_CREATE"] = parse


def _user_id(text: str) -> int | None:
    match = re.search(r"\d{15,21}", text or "")
    return int(match.group()) if match else None


class Blacklist(commands.Cog, name="Blacklist"):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        return is_owner(ctx.author)

    async def _name(self, uid: int) -> str:
        user = self.bot.get_user(uid)
        if user is None:
            try:
                user = await self.bot.fetch_user(uid)
            except discord.HTTPException:
                user = None
        return f"**{discord.utils.escape_markdown(user.name)}** (`{uid}`)" if user else f"`{uid}`"

    @commands.group(name="blacklist", aliases=["bl"], invoke_without_command=True,
                    help="SYKE owners: people SYKE ignores completely, everywhere")
    async def blacklist(self, ctx: commands.Context) -> None:
        ids = sorted(blocked_ids())
        lines = [f"• {await self._name(uid)}" for uid in ids[:40]]
        card = discord.Embed(title=f"🚫 Blacklist · {len(ids)}", colour=discord.Colour.dark_red(),
                             description="\n".join(lines) or "Nobody.")
        card.set_footer(text="blacklist add <user or ID> · blacklist remove <user or ID>")
        await ctx.reply(embed=card, mention_author=False)

    @blacklist.command(name="add")
    async def add(self, ctx: commands.Context, *, who: str) -> None:
        uid = _user_id(who)
        if uid is None:
            await ctx.reply("Give a user ID or mention.", mention_author=False)
            return
        if is_owner(uid) or uid == getattr(self.bot.user, "id", None):
            await ctx.reply("SYKE's owners (and SYKE) can't be blacklisted.", mention_author=False)
            return
        _BLOCKED.add(uid)
        _save(self.bot.storage)
        log.info("Blacklisted %s (by %s)", uid, ctx.author.id)
        await ctx.reply(f"🚫 {await self._name(uid)} is blacklisted. SYKE ignores them everywhere now.",
                        mention_author=False)

    @blacklist.command(name="remove", aliases=["rm", "delete", "del"])
    async def remove(self, ctx: commands.Context, *, who: str) -> None:
        uid = _user_id(who)
        if uid is None or uid not in _BLOCKED:
            await ctx.reply("They aren't on the blacklist.", mention_author=False)
            return
        _BLOCKED.discard(uid)
        _save(self.bot.storage)
        log.info("Unblacklisted %s (by %s)", uid, ctx.author.id)
        await ctx.reply(f"✅ {await self._name(uid)} is off the blacklist.", mention_author=False)
