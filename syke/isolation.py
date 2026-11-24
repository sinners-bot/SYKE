"""Contamination: a role that hides every channel and every member from whoever has it.

`contaminate @member [duration] [reason]` takes away their other roles (saved, given back on release), gives them
the `SYKE · contaminated` role, and that role is denied View Channel everywhere. The only thing they still see is a
holding channel where they can talk to the mods, and its member list shows just the mods.

Giving the role by hand in Discord works the same way: SYKE notices, saves their roles and takes them off.
Removing it by hand gives the roles back. Leaving and rejoining doesn't get anyone out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import discord
from discord.ext import commands, tasks

from .moderation import (DANGER, OK, action_embed, blocker, bot_lacks, embed, moderator, needs, parse_duration,
                         problem, until)

log = logging.getLogger("syke.isolation")

CONFIG_KEY = "isolation"
ROLE_NAME = "SYKE · contaminated"
CHANNEL_NAME = "contamination"
# Names used before the rename: renamed to the new ones when found.
OLD_NAMES = {"SYKE · isolated", "isolation"}
# What the role is denied everywhere except the holding channel.
HIDDEN = {"view_channel": False, "connect": False}
# Mods: anyone whose roles can do one of these sees the holding channel.
MOD_PERMS = ("administrator", "manage_guild", "manage_roles", "moderate_members", "kick_members", "ban_members")
# Asked in this order when the moderator's words are read: "isolate @x 2h spamming".
PERMANENT = {"perm", "permanent", "forever", "indefinite", "indefinitely"}


def split_details(text: str) -> tuple[int | None, str]:
    """('2h spamming' -> (7200, 'spamming')), ('spamming' -> (None, 'spamming'))."""
    words = (text or "").split()
    if words and words[0].lower() in PERMANENT:
        return None, " ".join(words[1:])
    if words:
        seconds = parse_duration(words[0])
        if seconds:
            return seconds, " ".join(words[1:])
    return None, " ".join(words)


def takeable(role, me_top) -> bool:
    """A role SYKE can take off someone (and put back later)."""
    return not (role.is_default() or role.managed or role >= me_top)


class Isolation(commands.Cog, name="Isolation"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.storage = bot.storage
        self._busy: set[tuple[int, int]] = set()    # members SYKE is changing right now (ignore their role events)
        self._sync_jobs: dict[int, asyncio.Task] = {}

    async def cog_load(self) -> None:
        if not self.expire.is_running():
            self.expire.start()

    async def cog_unload(self) -> None:
        self.expire.cancel()

    def p(self, ctx) -> str:
        return self.bot.display_prefix(ctx.guild.id)

    # The role and the holding channel

    def role(self, guild) -> discord.Role | None:
        role_id = self.storage.get_config(guild.id, CONFIG_KEY).get("role")
        return guild.get_role(role_id) if role_id else None

    def holding(self, guild):
        channel_id = self.storage.get_config(guild.id, CONFIG_KEY).get("channel")
        return guild.get_channel(channel_id) if channel_id else None

    def mod_roles(self, guild) -> list[discord.Role]:
        return [r for r in guild.roles if not r.is_default() and any(getattr(r.permissions, p) for p in MOD_PERMS)]

    async def ensure_setup(self, guild) -> tuple[discord.Role, object, bool]:
        """The isolation role and holding channel, created if missing. Third value: anything was created."""
        created = False
        role = self.role(guild)
        if role is None:
            role = await guild.create_role(name=ROLE_NAME, permissions=discord.Permissions.none(), mentionable=False,
                                           colour=discord.Colour.dark_grey(), reason="SYKE contamination role")
            self.storage.update_config(guild.id, CONFIG_KEY, role=role.id)
            created = True
        if role.name in OLD_NAMES:
            with contextlib.suppress(discord.HTTPException):
                await role.edit(name=ROLE_NAME, reason="SYKE: isolation is now contamination")
        channel = self.holding(guild)
        if channel is not None and channel.name in OLD_NAMES:
            with contextlib.suppress(discord.HTTPException):
                await channel.edit(name=CHANNEL_NAME, topic="Contaminated members can only see this channel. "
                                   "Talk to the mods here.", reason="SYKE: isolation is now contamination")
        if channel is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                  add_reactions=False, attach_files=False, embed_links=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True,
                                                      read_message_history=True, embed_links=True),
            }
            for mod in self.mod_roles(guild):
                overwrites[mod] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                              read_message_history=True)
            channel = await guild.create_text_channel(
                CHANNEL_NAME, overwrites=overwrites, position=0, reason="SYKE contamination holding channel",
                topic="Contaminated members can only see this channel. Talk to the mods here.")
            self.storage.update_config(guild.id, CONFIG_KEY, channel=channel.id)
            created = True
        return role, channel, created

    async def hide_everything(self, guild, role) -> tuple[int, int]:
        """Deny the role View Channel (and voice) everywhere but the holding channel. (changed, refused)"""
        holding = self.holding(guild)
        changed = refused = 0
        channels = sorted(guild.channels, key=lambda c: getattr(c, "type", None) != discord.ChannelType.category)
        for channel in channels:
            if holding is not None and channel.id == holding.id:
                continue
            if self.already_hidden(channel, role):
                continue
            try:
                await channel.set_permissions(role, overwrite=self.hidden_overwrite(channel, role),
                                              reason="SYKE contamination")
                changed += 1
            except discord.HTTPException:
                refused += 1
        if refused:
            log.warning("Isolation: %d channel(s) refused the overwrite in %s", refused, guild.id)
        return changed, refused

    @staticmethod
    def hidden_overwrite(channel, role) -> discord.PermissionOverwrite:
        overwrite = channel.overwrites_for(role)
        for perm, value in HIDDEN.items():
            setattr(overwrite, perm, value)
        return overwrite

    @staticmethod
    def already_hidden(channel, role) -> bool:
        if channel.overwrites_for(role).view_channel is False:
            return True
        category = getattr(channel, "category", None)
        # A channel synced to its category takes the category's overwrite: nothing to add (and syncing stays intact).
        return bool(category is not None and getattr(channel, "permissions_synced", False)
                    and category.overwrites_for(role).view_channel is False)

    def queue_sync(self, guild, role) -> None:
        job = self._sync_jobs.get(guild.id)
        if job is None or job.done():
            self._sync_jobs[guild.id] = self.bot.spawn(self.hide_everything(guild, role))

    # Isolating and releasing

    async def isolate_member(self, member, *, seconds: int | None, reason: str, by=None) -> tuple[list, list]:
        """Swap their roles for the isolation role. Returns (roles taken, roles SYKE couldn't take)."""
        guild = member.guild
        role, _, created = await self.ensure_setup(guild)
        if created or not self.storage.get_config(guild.id, CONFIG_KEY).get("synced"):
            await self.hide_everything(guild, role)        # first time: hide everything before it matters
            self.storage.update_config(guild.id, CONFIG_KEY, synced=True)
        me_top = guild.me.top_role
        current = [r for r in member.roles if not r.is_default() and r != role]
        taken = [r for r in current if takeable(r, me_top)]
        stuck = [r for r in current if not takeable(r, me_top)]
        expires_at = time.time() + seconds if seconds else None
        self.storage.isolate(guild.id, member.id, [r.id for r in taken], expires_at, reason,
                             getattr(by, "id", 0) or 0)
        key = (guild.id, member.id)
        self._busy.add(key)
        try:
            await member.edit(roles=[*stuck, role], reason=f"SYKE contaminate by {by or 'role given'}: {reason or 'no reason'}")
        except discord.HTTPException:
            self.storage.release(guild.id, member.id)   # nothing changed on Discord: don't keep a record
            raise
        finally:
            self._busy.discard(key)
        if getattr(member, "voice", None) and member.voice.channel is not None:
            with contextlib.suppress(discord.HTTPException):
                await member.move_to(None, reason="SYKE contamination")
        return taken, stuck

    async def release_member(self, member, *, reason: str = "", by=None) -> list:
        """Give back the saved roles and take the isolation role. Returns the roles given back."""
        guild = member.guild
        record = self.storage.isolation(guild.id, member.id)
        role = self.role(guild)
        me_top = guild.me.top_role
        back = []
        for role_id in (record or {}).get("roles", []):
            saved = guild.get_role(role_id)
            if saved is not None and takeable(saved, me_top):
                back.append(saved)
        keep = [r for r in member.roles if not r.is_default() and (role is None or r != role)]
        roles = list({r.id: r for r in [*keep, *back]}.values())
        key = (guild.id, member.id)
        self._busy.add(key)
        try:
            await member.edit(roles=roles, reason=f"SYKE release by {by or 'role removed'}: {reason or 'no reason'}")
        finally:
            self._busy.discard(key)
        self.storage.release(guild.id, member.id)
        return back

    # Commands

    @commands.command(name="contaminate", aliases=["contimate", "isolate", "quarantine"],
                      usage="@member [duration] [reason]",
                      help="Hide every channel and member from someone (their roles come back on release)")
    @commands.guild_only()
    @moderator("manage_roles")
    async def isolate(self, ctx: commands.Context, member: discord.Member, *, details: str = "") -> None:
        guild = ctx.guild
        if member.bot:
            await ctx.reply(embed=problem("Not for bots", "Change the bot's role permissions instead."))
            return
        why_not = blocker(guild, ctx.author, member, owner_ids=self.bot.settings.owner_ids)
        if why_not:
            await ctx.reply(embed=problem("Can't contaminate them", why_not))
            return
        missing = bot_lacks(guild, "manage_roles", "manage_channels")
        if missing:
            await ctx.reply(embed=needs(missing))
            return
        if member.guild_permissions.administrator and not any(
                r.permissions.administrator for r in member.roles if takeable(r, guild.me.top_role)):
            await ctx.reply(embed=problem("They'd still see everything",
                                          "Their Administrator permission comes from a role SYKE can't take off "
                                          "(above SYKE's role, or a bot/booster role)."))
            return
        seconds, reason = split_details(details)
        async with ctx.typing():
            try:
                taken, stuck = await self.isolate_member(member, seconds=seconds, reason=reason, by=ctx.author)
            except discord.Forbidden:
                await ctx.reply(embed=problem("Discord said no", "SYKE needs **Manage Roles** and **Manage Channels**, "
                                              f"and its role must be above `{ROLE_NAME}` and their roles."))
                return
            length = until(seconds)
            case = self.storage.add_case(guild.id, member.id, ctx.author.id, "contaminate", reason, seconds)
            mod = self.bot.get_cog("Moderation")
            if mod is not None:
                await mod.tell(member, guild, "contaminated: you can't see any channels or members for now",
                               reason=reason, duration=length)
            await self.greet(member, reason, length)
        notes = [f"Took **{len(taken)}** role{'s' * (len(taken) != 1)}; they come back on release."]
        if stuck:
            notes.append("Couldn't take " + ", ".join(r.mention for r in stuck[:6]) +
                         " (above SYKE or managed by Discord). If those roles are allowed into channels, "
                         "those channels stay visible.")
        holding = self.holding(guild)
        if holding is not None:
            notes.append(f"They can only see {holding.mention}.")
        result = action_embed(f"☣️ {member.display_name} is contaminated", member, ctx.author, reason=reason,
                              duration=length, case=case, colour=DANGER, extra="\n".join(notes))
        await ctx.reply(embed=result)
        if mod is not None:
            await mod.log(guild, result.copy())

    @commands.command(name="decontaminate", aliases=["uncontaminate", "unisolate", "unquarantine", "release"],
                      usage="@member [reason]", help="End someone's contamination and give their roles back")
    @commands.guild_only()
    @moderator("manage_roles")
    async def unisolate(self, ctx: commands.Context, member: discord.Member, *, reason: str = "") -> None:
        guild = ctx.guild
        role = self.role(guild)
        if not self.storage.isolation(guild.id, member.id) and (role is None or role not in member.roles):
            await ctx.reply(embed=problem("Not contaminated", f"{member.mention} isn't contaminated."))
            return
        try:
            back = await self.release_member(member, reason=reason, by=ctx.author)
        except discord.Forbidden:
            await ctx.reply(embed=problem("Discord said no", "SYKE's role must be above their roles to give them back."))
            return
        case = self.storage.add_case(guild.id, member.id, ctx.author.id, "decontaminate", reason)
        result = action_embed(f"🧼 {member.display_name} is decontaminated", member, ctx.author, reason=reason, case=case,
                              colour=OK, extra=f"Gave back **{len(back)}** role{'s' * (len(back) != 1)}.")
        await ctx.reply(embed=result)
        mod = self.bot.get_cog("Moderation")
        if mod is not None:
            await mod.log(guild, result.copy())
            await mod.tell(member, guild, "decontaminated: you can see the server again")

    @commands.command(name="contaminated", aliases=["contaminations", "isolated", "quarantined"], usage="[sync]",
                      help="Who's contaminated; 'contaminated sync' re-hides every channel from the role")
    @commands.guild_only()
    @moderator("manage_roles")
    async def isolated(self, ctx: commands.Context, action: str = "") -> None:
        guild = ctx.guild
        p = self.p(ctx)
        if action.lower() in ("sync", "setup", "fix"):
            missing = bot_lacks(guild, "manage_roles", "manage_channels")
            if missing:
                await ctx.reply(embed=needs(missing))
                return
            async with ctx.typing():
                role, channel, _ = await self.ensure_setup(guild)
                changed, refused = await self.hide_everything(guild, role)
                self.storage.update_config(guild.id, CONFIG_KEY, synced=True)
            body = (f"{role.mention} can't see any channel now except {channel.mention}. "
                    f"Updated {changed} channel{'s' * (changed != 1)}.")
            if refused:
                body += f"\n⚠️ {refused} channel(s) refused: give SYKE **Manage Channels** there (or Administrator)."
            await ctx.reply(embed=embed("☣️ Contamination is set up", body))
            return
        rows = self.storage.isolations(guild.id)
        role, holding = self.role(guild), self.holding(guild)
        lines = []
        for _, user_id, expires_at in rows[:20]:
            ends = f"<t:{int(expires_at)}:R>" if expires_at else "until released"
            lines.append(f"• <@{user_id}> · {ends}")
        body = "\n".join(lines) or "Nobody is contaminated."
        setup = (f"\n\nRole: {role.mention if role else 'not made yet'} · Holding channel: "
                 f"{holding.mention if holding else 'not made yet'}\n"
                 f"`{p}contaminate @member [2h] [reason]` · `{p}decontaminate @member` · `{p}contaminated sync` "
                 "re-hides every channel. Giving the role by hand works too.")
        await ctx.reply(embed=embed(f"☣️ Contaminated ({len(rows)})", body + setup))

    async def greet(self, member, reason: str, length: str | None) -> None:
        holding = self.holding(member.guild)
        if holding is None:
            return
        text = (f"{member.mention} you've been contaminated, so this is the only channel you can see. "
                f"You can talk to the mods here.")
        if reason:
            text += f"\n**Reason:** {reason}"
        if length:
            text += f"\n**Length:** {length}"
        with contextlib.suppress(discord.HTTPException):
            await holding.send(text, allowed_mentions=discord.AllowedMentions(users=[member]))

    # Keeping it airtight

    @commands.Cog.listener()
    async def on_member_update(self, before, after) -> None:
        guild = after.guild
        role = self.role(guild)
        if role is None or (guild.id, after.id) in self._busy:
            return
        had, has = role in before.roles, role in after.roles
        if had == has:
            return
        record = self.storage.isolation(guild.id, after.id)
        try:
            if has and not had and record is None:      # a mod gave the role by hand
                await self.isolate_member(after, seconds=None, reason="role given by hand")
                await self.greet(after, "", None)
            elif had and not has and record is not None:  # a mod took it off by hand
                await self.release_member(after, reason="role removed by hand")
        except discord.HTTPException:
            log.warning("Isolation role change for %s in %s couldn't be applied", after.id, guild.id)

    @commands.Cog.listener()
    async def on_member_join(self, member) -> None:
        """Leaving and rejoining doesn't end an isolation."""
        if not self.storage.isolation(member.guild.id, member.id):
            return
        role = self.role(member.guild)
        if role is None:
            return
        self._busy.add((member.guild.id, member.id))
        try:
            with contextlib.suppress(discord.HTTPException):
                await member.add_roles(role, reason="SYKE: still contaminated")
        finally:
            self._busy.discard((member.guild.id, member.id))

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel) -> None:
        role = self.role(channel.guild)
        holding = self.holding(channel.guild)
        if role is None or (holding is not None and channel.id == holding.id) or self.already_hidden(channel, role):
            return
        with contextlib.suppress(discord.HTTPException):
            await channel.set_permissions(role, overwrite=self.hidden_overwrite(channel, role),
                                          reason="SYKE contamination: new channel")

    @tasks.loop(seconds=30)
    async def expire(self) -> None:
        now = time.time()
        for guild_id, user_id, expires_at in self.storage.isolations():
            if expires_at is None or expires_at > now:
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            member = guild.get_member(user_id)
            if member is None:          # they left and their time ran out: nothing to give back on Discord
                self.storage.release(guild_id, user_id)
                continue
            try:
                await self.release_member(member, reason="time's up")
            except discord.HTTPException:
                log.warning("Couldn't end isolation of %s in %s", user_id, guild_id)
                continue
            mod = self.bot.get_cog("Moderation")
            if mod is not None:
                await mod.log(guild, embed("🧼 Contamination ended", f"{member.mention}'s contamination ran out; "
                                                                  "their roles are back.", OK))

    @expire.before_loop
    async def _before_expire(self) -> None:
        await self.bot.wait_until_ready()
