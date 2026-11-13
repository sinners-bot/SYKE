"""Server security: anti-nuke and anti-raid.

**Anti-nuke** watches the audit log for a single actor mass-deleting channels or
roles, mass-banning/kicking members, or spraying webhooks in a short window. When
someone trips the limit, SYKE neutralises them — strips their roles (default,
reversible), kicks, or bans — unless they're the server owner, a SYKE owner, the
bot itself, or whitelisted.

**Anti-raid** watches joins. A burst of accounts joining at once flips the server
into raid mode and kicks or bans the wave (optionally only young accounts). Raid
mode can also be toggled by hand and auto-clears once things go quiet.

Both are OFF by default and share one per-server config blob (storage key
"security"). Every action is logged to the mod-log channel.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .moderation import (
    BRAND,
    DANGER,
    OK,
    WARN,
    bot_lacks,
    embed,
    human_duration,
    moderator,
)

log = logging.getLogger("syke.security")

CONFIG_KEY = "security"

DEFAULTS = {
    "antinuke": False,
    "an_action": "strip",     # strip | kick | ban
    "an_limit": 3,            # this many destructive actions...
    "an_window": 10,          # ...within this many seconds trips it
    "whitelist": [],          # user ids trusted to act in bulk
    "antiraid": False,
    "ar_action": "kick",      # kick | ban | alert
    "ar_count": 8,            # this many joins...
    "ar_window": 10,          # ...within this many seconds is a raid
    "ar_min_age_days": 0,     # only act on accounts younger than this (0 = all)
    "raid_mode": False,       # gate on new joiners (manual or auto)
}

AN_ACTIONS = ("strip", "kick", "ban")
AR_ACTIONS = ("kick", "ban", "alert")

# Destructive audit-log actions anti-nuke counts, with a friendly label.
WATCHED = {
    "channel_delete": ("deleting channels", discord.AuditLogAction.channel_delete),
    "channel_create": ("creating channels", discord.AuditLogAction.channel_create),
    "role_delete": ("deleting roles", discord.AuditLogAction.role_delete),
    "role_create": ("creating roles", discord.AuditLogAction.role_create),
    "ban": ("banning members", discord.AuditLogAction.ban),
    "kick": ("kicking members", discord.AuditLogAction.kick),
    "webhook_create": ("creating webhooks", discord.AuditLogAction.webhook_create),
}

# Perms that make a role worth stripping from a rogue actor.
DANGEROUS_PERMS = ("administrator", "manage_guild", "manage_channels", "manage_roles",
                   "ban_members", "kick_members", "manage_webhooks", "manage_emojis_and_stickers")

PUNISH_COOLDOWN = 30  # don't re-punish the same actor within this many seconds
RAID_CALM = 120       # auto-clear raid mode after this long with no joins


def cfg(config: dict, key: str):
    return config.get(key, DEFAULTS[key])


def trips(times: list[float], now: float, window: float, limit: int) -> bool:
    """True when at least `limit` of `times` fall within `window` seconds of `now`."""
    return sum(1 for t in times if now - t <= window) >= limit


class Security(commands.Cog, name="Security"):
    def __init__(self, bot) -> None:
        self.bot = bot
        # (guild_id, actor_id) -> kind -> timestamps
        self._nuke: dict[tuple[int, int], dict[str, deque]] = defaultdict(lambda: defaultdict(lambda: deque(maxlen=40)))
        self._punished: dict[tuple[int, int], float] = {}
        self._joins: dict[int, deque] = defaultdict(lambda: deque(maxlen=100))
        self._recent_members: dict[int, deque] = defaultdict(lambda: deque(maxlen=60))
        self._raid_auto: set[int] = set()

    async def cog_load(self) -> None:
        self.raid_watch.start()

    async def cog_unload(self) -> None:
        self.raid_watch.cancel()

    def p(self, ctx: commands.Context) -> str:
        return self.bot.display_prefix(ctx.guild.id)

    def config(self, guild_id: int) -> dict:
        return self.bot.storage.get_config(guild_id, CONFIG_KEY)

    def save(self, guild_id: int, **changes) -> dict:
        return self.bot.storage.update_config(guild_id, CONFIG_KEY, **changes)

    async def log_entry(self, guild: discord.Guild, entry: discord.Embed) -> None:
        channel_id = self.bot.storage.modlog_channel(guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is not None:
            entry.timestamp = datetime.now(timezone.utc)
            with contextlib.suppress(discord.HTTPException):
                await channel.send(embed=entry)

    # ------------------------------------------------------------------ anti-nuke

    def an_exempt(self, guild: discord.Guild, actor, config: dict) -> bool:
        """Who anti-nuke never touches: nobody we can't identify, the owner, us, SYKE owners, whitelist."""
        if actor is None:
            return True
        aid = getattr(actor, "id", None)
        if aid is None:
            return True
        if aid == getattr(getattr(self.bot, "user", None), "id", 0):
            return True
        if aid == getattr(guild, "owner_id", None):
            return True
        if aid in getattr(self.bot.settings, "owner_ids", ()):  # SYKE's own owners
            return True
        if aid in cfg(config, "whitelist"):
            return True
        return False

    def register_nuke(self, guild_id: int, actor_id: int, kind: str, config: dict, now: float | None = None) -> bool:
        """Record one destructive action; return True if the actor just crossed the limit."""
        now = time.monotonic() if now is None else now
        bucket = self._nuke[(guild_id, actor_id)][kind]
        bucket.append(now)
        return trips(list(bucket), now, cfg(config, "an_window"), cfg(config, "an_limit"))

    async def _actor_for(self, guild: discord.Guild, action, target_id=None, within: float = 25) -> discord.abc.User | None:
        """Find who performed a destructive action, from the audit log."""
        if bot_lacks(guild, "view_audit_log"):
            return None
        try:
            async for entry in guild.audit_logs(limit=6, action=action):
                age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
                if age > within:
                    continue
                if target_id is None or getattr(entry.target, "id", None) == target_id:
                    return entry.user
        except discord.HTTPException:
            return None
        return None

    async def guard(self, guild: discord.Guild, kind: str, target_id=None) -> None:
        """Entry point for every watched destructive event."""
        if guild is None:
            return
        config = self.config(guild.id)
        if not cfg(config, "antinuke"):
            return
        action = WATCHED[kind][1]
        actor = await self._actor_for(guild, action, target_id)
        if self.an_exempt(guild, actor, config):
            return
        if self.register_nuke(guild.id, actor.id, kind, config):
            await self.punish(guild, actor, kind, config)

    async def punish(self, guild: discord.Guild, actor, kind: str, config: dict) -> str:
        key = (guild.id, actor.id)
        now = time.monotonic()
        if now - self._punished.get(key, 0) < PUNISH_COOLDOWN:
            return "already handled"
        self._punished[key] = now
        action = cfg(config, "an_action")
        member = guild.get_member(actor.id)
        reason = f"SYKE anti-nuke: {WATCHED[kind][0]} too fast"
        done = "flagged (couldn't act)"
        try:
            if action == "ban":
                await guild.ban(discord.Object(id=actor.id), reason=reason, delete_message_seconds=0)
                done = "banned"
            elif action == "kick" and member is not None:
                await member.kick(reason=reason)
                done = "kicked"
            elif member is not None:
                removed = await self.strip_roles(guild, member, reason)
                done = f"stripped of {removed} role(s)" if removed else "had no removable roles"
        except discord.Forbidden:
            done = "flagged (missing permissions)"
        except discord.HTTPException:
            done = "flagged (action failed)"
        await self.log_entry(guild, embed(
            "🚨 Anti-nuke triggered",
            f"**Actor:** {getattr(actor, 'mention', actor)} (`{actor.id}`)\n"
            f"**Why:** {WATCHED[kind][0]} past the limit "
            f"({cfg(config, 'an_limit')} in {cfg(config, 'an_window')}s)\n"
            f"**Action taken:** {done}", DANGER))
        log.warning("anti-nuke: %s in guild %s -> %s", actor.id, guild.id, done)
        return done

    async def strip_roles(self, guild: discord.Guild, member: discord.Member, reason: str) -> int:
        """Remove every role from `member` that grants a dangerous permission and that SYKE can manage."""
        me = guild.me
        top = getattr(getattr(me, "top_role", None), "position", 0)
        drop = []
        for role in getattr(member, "roles", []):
            if role.is_default() or role.managed or role.position >= top:
                continue
            if any(getattr(role.permissions, perm, False) for perm in DANGEROUS_PERMS):
                drop.append(role)
        if not drop:
            return 0
        with contextlib.suppress(discord.HTTPException):
            await member.remove_roles(*drop, reason=reason)
        return len(drop)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel) -> None:
        await self.guard(getattr(channel, "guild", None), "channel_delete", getattr(channel, "id", None))

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel) -> None:
        await self.guard(getattr(channel, "guild", None), "channel_create", getattr(channel, "id", None))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role) -> None:
        await self.guard(getattr(role, "guild", None), "role_delete", getattr(role, "id", None))

    @commands.Cog.listener()
    async def on_guild_role_create(self, role) -> None:
        await self.guard(getattr(role, "guild", None), "role_create", getattr(role, "id", None))

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user) -> None:
        await self.guard(guild, "ban", getattr(user, "id", None))

    @commands.Cog.listener()
    async def on_member_remove(self, member) -> None:
        # Leaves also fire this, so only count it if the audit log shows a fresh kick.
        await self.guard(getattr(member, "guild", None), "kick", getattr(member, "id", None))

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel) -> None:
        await self.guard(getattr(channel, "guild", None), "webhook_create")

    # ------------------------------------------------------------------ anti-raid

    def is_young(self, member, config: dict) -> bool:
        days = cfg(config, "ar_min_age_days")
        if not days:
            return True  # age gate off -> everyone in the burst counts
        created = getattr(member, "created_at", None)
        if created is None:
            return True
        return (datetime.now(timezone.utc) - created) < timedelta(days=days)

    async def on_raid_join(self, member, config: dict) -> None:
        guild = member.guild
        now = time.monotonic()
        self._joins[guild.id].append(now)
        self._recent_members[guild.id].append((now, member))
        raiding = cfg(config, "raid_mode")
        just_tripped = False
        if not raiding and trips(list(self._joins[guild.id]), now, cfg(config, "ar_window"), cfg(config, "ar_count")):
            self.save(guild.id, raid_mode=True)
            self._raid_auto.add(guild.id)
            raiding = just_tripped = True
            await self.log_entry(guild, embed(
                "🚨 Raid detected",
                f"**{cfg(config, 'ar_count')}+ joins** in {cfg(config, 'ar_window')}s. Raid mode is **on** — "
                f"new joiners will be **{cfg(config, 'ar_action')}**'d. Turn it off with `raidmode off`.", DANGER))
        if not raiding:
            return
        if just_tripped:
            # Sweep the whole burst that just arrived.
            window = cfg(config, "ar_window")
            for t, m in list(self._recent_members[guild.id]):
                if now - t <= window:
                    await self.act_on_raider(m, config)
        else:
            await self.act_on_raider(member, config)

    async def act_on_raider(self, member, config: dict) -> None:
        if member.bot or not self.is_young(member, config):
            return
        if member.id in getattr(self.bot.settings, "owner_ids", ()):
            return
        action = cfg(config, "ar_action")
        reason = "SYKE anti-raid"
        with contextlib.suppress(discord.HTTPException, AttributeError):
            if action == "ban":
                await member.ban(reason=reason, delete_message_seconds=0)
            elif action == "kick":
                await member.kick(reason=reason)
            # "alert" just logs below

    @tasks.loop(seconds=30)
    async def raid_watch(self) -> None:
        """Auto-clear raid mode once joins go quiet (only if we turned it on)."""
        now = time.monotonic()
        for gid in list(self._raid_auto):
            joins = self._joins.get(gid)
            last = joins[-1] if joins else 0
            if now - last >= RAID_CALM:
                self._raid_auto.discard(gid)
                self.save(gid, raid_mode=False)
                guild = self.bot.get_guild(gid)
                if guild is not None:
                    await self.log_entry(guild, embed("✅ Raid mode cleared",
                                                       "Joins have gone quiet, so SYKE turned raid mode back off.", OK))

    @raid_watch.before_loop
    async def _before_raid_watch(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member) -> None:
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        config = self.config(guild.id)
        if cfg(config, "antiraid") or cfg(config, "raid_mode"):
            await self.on_raid_join(member, config)

    # ------------------------------------------------------------------ settings UI

    def overview(self, guild: discord.Guild, prefix: str) -> discord.Embed:
        c = self.config(guild.id)

        def onoff(k):
            return "🟢 on" if cfg(c, k) else "⚪ off"

        wl = ", ".join(f"<@{u}>" for u in cfg(c, "whitelist")) or "none"
        age = cfg(c, "ar_min_age_days")
        body = (
            f"**🛡️ Anti-nuke:** {onoff('antinuke')} — if one person does "
            f"**{cfg(c, 'an_limit')}** destructive actions in **{cfg(c, 'an_window')}s**, "
            f"SYKE will **{cfg(c, 'an_action')}** them.\n"
            f"Whitelisted: {wl}\n\n"
            f"**🚨 Anti-raid:** {onoff('antiraid')} — **{cfg(c, 'ar_count')}** joins in "
            f"**{cfg(c, 'ar_window')}s** trips raid mode; joiners get **{cfg(c, 'ar_action')}**'d"
            f"{f' (only accounts under {age}d old)' if age else ''}.\n"
            f"**Raid mode right now:** {'🔴 ON' if cfg(c, 'raid_mode') else '⚪ off'}\n\n"
            f"Set up: `{prefix}antinuke on` · `{prefix}antiraid on` · `{prefix}modlog #channel` for alerts.\n"
            f"`{prefix}help antinuke` or `{prefix}help antiraid` for everything."
        )
        e = embed("🚨 Server Security", body)
        e.set_footer(text="Both start OFF. Whitelist people you trust before turning anti-nuke on.")
        return e

    @commands.hybrid_group(name="antinuke", aliases=["an"], fallback="settings", case_insensitive=True,
                           description="Protection against a rogue admin nuking the server")
    @moderator("manage_guild")
    async def antinuke(self, ctx: commands.Context) -> None:
        await ctx.reply(embed=self.overview(ctx.guild, self.p(ctx)), ephemeral=True)

    @antinuke.command(name="on", description="Turn anti-nuke on")
    @moderator("manage_guild")
    async def antinuke_on(self, ctx: commands.Context) -> None:
        self.save(ctx.guild.id, antinuke=True)
        warn = "" if not bot_lacks(ctx.guild, "view_audit_log") else \
            "\n\n⚠️ I'm missing **View Audit Log** — I can't see who does what, so anti-nuke can't act. Grant it to me."
        await ctx.reply(embed=embed("Anti-nuke on 🛡️",
                                    "I'll watch for mass channel/role deletions and mass bans/kicks."
                                    f"{warn}", OK), ephemeral=True)

    @antinuke.command(name="off", description="Turn anti-nuke off")
    @moderator("manage_guild")
    async def antinuke_off(self, ctx: commands.Context) -> None:
        self.save(ctx.guild.id, antinuke=False)
        await ctx.reply(embed=embed("Anti-nuke off", "No longer watching for nukes."), ephemeral=True)

    @antinuke.command(name="action", description="What to do to a nuker: strip (roles), kick or ban")
    @app_commands.describe(action="strip, kick or ban")
    @moderator("manage_guild")
    async def antinuke_action(self, ctx: commands.Context, action: str) -> None:
        action = action.strip().lower()
        if action not in AN_ACTIONS:
            await ctx.reply(embed=embed("Pick one", f"Use `strip`, `kick` or `ban`."), ephemeral=True)
            return
        self.save(ctx.guild.id, an_action=action)
        await ctx.reply(embed=embed("Saved ✅", f"Nukers will now be **{action}**'d."), ephemeral=True)

    @antinuke.command(name="limit", description="Threshold: how many destructive actions in how many seconds")
    @app_commands.describe(count="Destructive actions", seconds="Within this many seconds (default 10)")
    @moderator("manage_guild")
    async def antinuke_limit(self, ctx: commands.Context, count: int, seconds: int = 10) -> None:
        count = max(2, min(count, 50))
        seconds = max(3, min(seconds, 120))
        self.save(ctx.guild.id, an_limit=count, an_window=seconds)
        await ctx.reply(embed=embed("Saved ✅", f"Trips at **{count}** actions within **{seconds}s**."), ephemeral=True)

    @antinuke.command(name="whitelist", aliases=["trust", "wl"],
                      description="Trust a user to act in bulk (toggles them on/off the list)")
    @app_commands.describe(user="The user to trust or un-trust")
    @moderator("manage_guild")
    async def antinuke_whitelist(self, ctx: commands.Context, user: discord.User) -> None:
        wl = list(cfg(self.config(ctx.guild.id), "whitelist"))
        if user.id in wl:
            wl.remove(user.id)
            msg = f"{user.mention} is no longer whitelisted."
        else:
            wl.append(user.id)
            msg = f"{user.mention} is now trusted and won't trip anti-nuke."
        self.save(ctx.guild.id, whitelist=wl)
        await ctx.reply(embed=embed("Saved ✅", msg), ephemeral=True)

    @commands.hybrid_group(name="antiraid", aliases=["ar"], fallback="settings", case_insensitive=True,
                           description="Protection against join-flood raids")
    @moderator("manage_guild")
    async def antiraid(self, ctx: commands.Context) -> None:
        await ctx.reply(embed=self.overview(ctx.guild, self.p(ctx)), ephemeral=True)

    @antiraid.command(name="on", description="Turn anti-raid on")
    @moderator("manage_guild")
    async def antiraid_on(self, ctx: commands.Context) -> None:
        self.save(ctx.guild.id, antiraid=True)
        await ctx.reply(embed=embed("Anti-raid on 🚨",
                                    "I'll watch for join floods and flip on raid mode automatically.", OK),
                        ephemeral=True)

    @antiraid.command(name="off", description="Turn anti-raid off")
    @moderator("manage_guild")
    async def antiraid_off(self, ctx: commands.Context) -> None:
        self.save(ctx.guild.id, antiraid=False)
        await ctx.reply(embed=embed("Anti-raid off", "No longer watching joins for raids."), ephemeral=True)

    @antiraid.command(name="action", description="What to do to raiders: kick, ban or just alert")
    @app_commands.describe(action="kick, ban or alert")
    @moderator("manage_guild")
    async def antiraid_action(self, ctx: commands.Context, action: str) -> None:
        action = action.strip().lower()
        if action not in AR_ACTIONS:
            await ctx.reply(embed=embed("Pick one", "Use `kick`, `ban` or `alert`."), ephemeral=True)
            return
        self.save(ctx.guild.id, ar_action=action)
        await ctx.reply(embed=embed("Saved ✅", f"Raiders will be **{action}**'d."), ephemeral=True)

    @antiraid.command(name="rate", description="Raid trigger: how many joins in how many seconds")
    @app_commands.describe(count="Number of joins", seconds="Within this many seconds (default 10)")
    @moderator("manage_guild")
    async def antiraid_rate(self, ctx: commands.Context, count: int, seconds: int = 10) -> None:
        count = max(3, min(count, 100))
        seconds = max(3, min(seconds, 120))
        self.save(ctx.guild.id, ar_count=count, ar_window=seconds)
        await ctx.reply(embed=embed("Saved ✅", f"A raid is **{count}+** joins within **{seconds}s**."), ephemeral=True)

    @antiraid.command(name="age", description="Only act on accounts younger than this many days (0 = all)")
    @app_commands.describe(days="Account age in days; 0 acts on everyone in the burst")
    @moderator("manage_guild")
    async def antiraid_age(self, ctx: commands.Context, days: int) -> None:
        days = max(0, min(days, 365))
        self.save(ctx.guild.id, ar_min_age_days=days)
        note = "acts on every joiner in a raid" if days == 0 else f"only acts on accounts under **{days}d** old"
        await ctx.reply(embed=embed("Saved ✅", f"During a raid, SYKE {note}."), ephemeral=True)

    @commands.hybrid_command(name="raidmode", usage="<on|off>",
                             description="Turn raid mode on or off right now")
    @app_commands.describe(state="on or off")
    @moderator("manage_guild")
    async def raidmode(self, ctx: commands.Context, state: str) -> None:
        state = state.strip().lower()
        if state in ("on", "enable", "true", "yes"):
            self.save(ctx.guild.id, raid_mode=True)
            self._raid_auto.discard(ctx.guild.id)  # manual: don't auto-clear
            c = self.config(ctx.guild.id)
            await ctx.reply(embed=embed("🔴 Raid mode ON",
                                        f"New joiners will be **{cfg(c, 'ar_action')}**'d until you run "
                                        f"`{self.p(ctx)}raidmode off`.", DANGER), ephemeral=True)
        elif state in ("off", "disable", "false", "no"):
            self.save(ctx.guild.id, raid_mode=False)
            self._raid_auto.discard(ctx.guild.id)
            await ctx.reply(embed=embed("✅ Raid mode off", "Back to normal.", OK), ephemeral=True)
        else:
            await ctx.reply(embed=embed("On or off?", "Use `raidmode on` or `raidmode off`."), ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Security(bot))
