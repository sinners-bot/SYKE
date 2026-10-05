"""SYKE Discord bot. Every command works both as a slash command and with the server prefix."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import discord
from discord import app_commands
from discord.ext import commands

from .ai import write_roast
from .cards import ProfileView, build_pages
from .config import Settings, load_settings
from .demo import PERSONAS, build_demo_server
from .models import Msg
from .profile import Profile, ServerAnalysis, analyze_server, build_profile
from .render import render_leaderboard
from .scanner import Scanner
from .storage import Storage
from .traits import TRAIT_META, TRAITS

log = logging.getLogger("syke")

BRAND = discord.Colour.from_str("#8B5CF6")
WARN = discord.Colour.from_str("#F59E0B")
ALL_TIMEZONES = sorted(available_timezones())
DEMO_MESSAGES = build_demo_server(headline_volume=400)
MAX_PREFIX_LENGTH = 5

TRAIT_ALIASES: dict[str, str] = {
    **{t: t for t in TRAITS},
    "funniest": "funny", "freak": "freaky", "chaos": "chaotic", "smart": "serious",
    "active": "active", "messages": "active", "yap": "active", "yapper": "active",
}
TRAIT_TITLES: dict[str, str] = {
    **{t: f"{TRAIT_META[t][0]} {TRAIT_META[t][1]}" for t in TRAITS},
    "active": "💬 Most active",
}
PERSONA_BY_NAME: dict[str, int] = {name.lower(): uid for uid, (name, _, _) in PERSONAS.items()}

F = TypeVar("F", bound=Callable)


def notice(title: str, body: str, colour: discord.Colour = WARN) -> discord.Embed:
    return discord.Embed(title=title, description=body, colour=colour)


def public() -> Callable[[F], F]:
    def decorator(func: F) -> F:
        func = app_commands.guild_only()(func)
        return commands.guild_only()(func)
    return decorator


def admin() -> Callable[[F], F]:
    """Manage Server only, enforced for prefix use and hidden from others in the slash menu."""
    def decorator(func: F) -> F:
        func = app_commands.default_permissions(manage_guild=True)(func)
        func = commands.has_guild_permissions(manage_guild=True)(func)
        return public()(func)
    return decorator


async def resolve_prefix(bot: Syke, message: discord.Message) -> list[str]:
    prefix = bot.prefix_for(message.guild.id) if message.guild else bot.settings.default_prefix
    return commands.when_mentioned_or(prefix)(bot, message)


class Syke(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix=resolve_prefix,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.storage = Storage(settings.db_path)
        self.scanner = Scanner(settings.scan_limit, settings.cache_minutes)
        self._prefixes: dict[int, str] = {}

    async def setup_hook(self) -> None:
        await self.add_cog(SykeCommands(self))
        if self.settings.dev_guild_id:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to dev guild %s", len(synced), guild.id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands (may take a while to appear)", len(synced))

    async def on_ready(self) -> None:
        log.info("SYKE online as %s in %d servers (AI: %s)", self.user, len(self.guilds), self.settings.ai_provider)
        activity = discord.Activity(type=discord.ActivityType.watching, name=f"you 👁️ | {self.settings.default_prefix}help")
        await self.change_presence(activity=activity)

    def prefix_for(self, guild_id: int) -> str:
        if guild_id not in self._prefixes:
            self._prefixes[guild_id] = self.storage.prefix(guild_id) or self.settings.default_prefix
        return self._prefixes[guild_id]

    def set_prefix(self, guild_id: int, prefix: str | None) -> str:
        self.storage.set_prefix(guild_id, prefix)
        self._prefixes.pop(guild_id, None)
        return self.prefix_for(guild_id)

    def tz_for(self, guild_id: int) -> ZoneInfo:
        name = self.storage.timezone(guild_id) or self.settings.default_timezone
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    async def analyze(self, guild: discord.Guild) -> tuple[ServerAnalysis, list[int]] | None:
        tracked = self.storage.tracked(guild.id)
        demo = self.storage.demo_mode(guild.id)
        if not tracked and not demo:
            return None
        messages: list[Msg] = []
        skipped: list[int] = []
        if tracked:
            scan = await self.scanner.scan(guild, tracked)
            optouts = self.storage.optouts(guild.id)
            messages = [m for m in scan.messages if m.author_id not in optouts]
            skipped = scan.skipped_channels
        if demo:
            messages = messages + DEMO_MESSAGES
        server = await asyncio.to_thread(analyze_server, messages, self.tz_for(guild.id), self.settings.min_messages)
        return server, skipped

    async def send_report(
        self, ctx: commands.Context, profile: Profile, avatar_url: str | None, name_suffix: str = ""
    ) -> None:
        await write_roast(profile, self.settings)
        profile.name += name_suffix
        pages = build_pages(profile, avatar_url, demo=self.storage.demo_mode(ctx.guild.id))
        view = ProfileView(pages, owner_id=ctx.author.id)
        view.message = await ctx.reply(embed=pages["overview"], view=view)

    def no_channels(self, guild_id: int) -> discord.Embed:
        p = self.prefix_for(guild_id)
        return notice(
            "No channels are being watched",
            f"An admin needs to pick which channels SYKE can read:\n`{p}track #general`",
        )

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        err: BaseException = error
        while isinstance(err, (commands.HybridCommandError, commands.CommandInvokeError,
                               app_commands.CommandInvokeError)) and getattr(err, "original", None):
            err = err.original

        p = self.prefix_for(ctx.guild.id) if ctx.guild else self.settings.default_prefix
        usage = ""
        if ctx.command:
            signature = f" {ctx.command.signature}" if ctx.command.signature else ""
            usage = f"`{p}{ctx.command.qualified_name}{signature}`"
        if isinstance(err, (commands.CommandOnCooldown, app_commands.CommandOnCooldown)):
            embed = notice("Easy there 🧪", f"The lab is still recovering. Try again in {err.retry_after:.0f}s.")
        elif isinstance(err, (commands.MissingPermissions, app_commands.MissingPermissions)):
            embed = notice("Not so fast", "You need **Manage Server** to do that.")
        elif isinstance(err, (commands.NoPrivateMessage, app_commands.NoPrivateMessage)):
            embed = notice("Servers only", "SYKE judges people in servers, not in DMs.")
        elif isinstance(err, commands.MemberNotFound):
            embed = notice("Who?", f"I couldn't find a member called `{err.argument}`. Try mentioning them.")
        elif isinstance(err, commands.ChannelNotFound):
            embed = notice("Which channel?", f"I couldn't find `{err.argument}`. Mention it like `#general`.")
        elif isinstance(err, (commands.MissingRequiredArgument, commands.BadArgument)):
            embed = notice("Not quite", f"Usage: {usage}")
        else:
            log.error("Command %s failed", ctx.command, exc_info=err)
            embed = notice("Experiment failed 💥", "Something went wrong while judging. Try again in a moment.")
        await ctx.send(embed=embed, ephemeral=True)


class SykeCommands(commands.Cog, name="SYKE"):
    def __init__(self, bot: Syke) -> None:
        self.bot = bot

    def p(self, ctx: commands.Context) -> str:
        return self.bot.prefix_for(ctx.guild.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild or not self.bot.user:
            return
        if message.content.strip() in {f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"}:
            p = self.bot.prefix_for(message.guild.id)
            await message.reply(embed=notice(
                "👁️ SYKE is watching",
                f"My prefix here is `{p}`. Try `{p}profile` or `{p}help`. Slash commands work too.",
                BRAND,
            ), mention_author=False)

    # Everyone

    @commands.hybrid_command(name="profile", aliases=["judge", "p", "me"], description="Get a personality report for someone, or yourself")
    @app_commands.describe(member="Who to judge. Leave empty to judge yourself.")
    @commands.cooldown(1, 20, commands.BucketType.member)
    @public()
    async def profile(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        target = member or ctx.author
        assert ctx.guild is not None
        if target.bot:
            await ctx.reply(embed=notice("Nice try", "Bots have no personality. We checked."), ephemeral=True)
            return
        if target.id in self.bot.storage.optouts(ctx.guild.id):
            await ctx.reply(embed=notice("Off limits", f"{target.display_name} has opted out of being judged."), ephemeral=True)
            return

        async with ctx.typing():
            result = await self.bot.analyze(ctx.guild)
            if result is None:
                await ctx.reply(embed=self.bot.no_channels(ctx.guild.id))
                return
            server, _ = result
            if target.id not in server.users:
                have = server.below_threshold.get(target.id, 0)
                need = self.bot.settings.min_messages
                await ctx.reply(embed=notice(
                    "Insufficient evidence 🔬",
                    f"{target.display_name} has only **{have}** message{'s' if have != 1 else ''} in the watched "
                    f"channels. SYKE needs at least **{need}** before it can pass judgement.",
                ))
                return
            profile = build_profile(server, target.id)
            profile.name = target.display_name
            await self.bot.send_report(ctx, profile, target.display_avatar.url)

    @commands.hybrid_command(name="top", aliases=["leaderboard", "lb"], description="Server leaderboard for a trait")
    @app_commands.describe(trait="funny, toxic, cringe, freaky, serious, chaotic or active")
    @commands.cooldown(1, 15, commands.BucketType.member)
    @public()
    async def top(self, ctx: commands.Context, trait: str = "funny") -> None:
        assert ctx.guild is not None
        key = TRAIT_ALIASES.get(trait.lower().strip())
        if key is None:
            options = ", ".join(f"`{t}`" for t in [*TRAITS, "active"])
            await ctx.reply(embed=notice("Unknown trait", f"Pick one of: {options}"), ephemeral=True)
            return
        async with ctx.typing():
            result = await self.bot.analyze(ctx.guild)
        if result is None:
            await ctx.reply(embed=self.bot.no_channels(ctx.guild.id))
            return
        server, _ = result
        if not server.users:
            await ctx.reply(embed=notice(
                "Nobody qualifies yet",
                f"Members need {self.bot.settings.min_messages}+ messages in watched channels to be ranked.",
            ))
            return
        embed = discord.Embed(
            title=f"{TRAIT_TITLES[key]} — server leaderboard",
            description=f"```\n{render_leaderboard(server, key)}\n```",
            colour=BRAND,
        )
        demo = " • includes fake demo members" if self.bot.storage.demo_mode(ctx.guild.id) else ""
        embed.set_footer(text=f"{len(server.users)} members judged{demo}")
        await ctx.reply(embed=embed)

    @top.autocomplete("trait")
    async def top_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=title, value=key)
            for key, title in TRAIT_TITLES.items()
            if current.lower() in key
        ]

    @commands.hybrid_command(name="optout", description="Stop SYKE from reading or judging your messages here")
    @public()
    async def optout(self, ctx: commands.Context) -> None:
        self.bot.storage.set_optout(ctx.guild.id, ctx.author.id, True)
        await ctx.reply(embed=notice(
            "You're off the record 🫥",
            f"SYKE will ignore your messages and refuse to judge you here. Use `{self.p(ctx)}optin` to come back.",
            BRAND,
        ), ephemeral=True)

    @commands.hybrid_command(name="optin", description="Let SYKE judge you again")
    @public()
    async def optin(self, ctx: commands.Context) -> None:
        self.bot.storage.set_optout(ctx.guild.id, ctx.author.id, False)
        await ctx.reply(embed=notice("Welcome back to the lab 🧪", "Your messages count again. Brace yourself.", BRAND), ephemeral=True)

    @commands.hybrid_command(name="help", aliases=["commands"], description="How to use SYKE")
    @public()
    async def help(self, ctx: commands.Context) -> None:
        p = self.p(ctx)
        embed = discord.Embed(
            title="🧠 SYKE — personality judgement, scientifically questionable",
            colour=BRAND,
            description=(
                f"Prefix here: `{p}` • slash commands work too.\n\n"
                "**Everyone**\n"
                f"`{p}profile [@member]` — the full personality report\n"
                f"`{p}top [trait]` — leaderboard: funny, toxic, cringe, freaky, serious, chaotic, active\n"
                f"`{p}optout` / `{p}optin` — control whether you're judged\n\n"
                "**Admins (Manage Server)**\n"
                f"`{p}track #channel` / `{p}untrack #channel` — choose what SYKE reads\n"
                f"`{p}channels` — show settings and watched channels\n"
                f"`{p}prefix <new>` — change the prefix (`{p}prefix reset` for `{self.bot.settings.default_prefix}`)\n"
                f"`{p}timezone <zone>` — e.g. `{p}timezone Europe/London`\n"
                f"`{p}rescan` — re-read channels now\n"
                f"`{p}demo on|off` — add fake members to test alone\n"
                f"`{p}sample [name]` — report for a fake member\n\n"
                "SYKE doesn't store message content. Reports are for fun only."
            ),
        )
        await ctx.reply(embed=embed, ephemeral=True)

    # Admins

    @commands.hybrid_command(name="track", description="Let SYKE read a channel")
    @admin()
    async def track(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.read_message_history):
            await ctx.reply(embed=notice(
                "I can't see in there 👀",
                f"Give SYKE **View Channel** and **Read Message History** in {channel.mention}, then try again.",
            ), ephemeral=True)
            return
        added = self.bot.storage.track(ctx.guild.id, channel.id)
        self.bot.scanner.invalidate(ctx.guild.id)
        body = f"Now watching {channel.mention}." if added else f"{channel.mention} was already being watched."
        await ctx.reply(embed=notice("Channel tracked ✅", body, BRAND), ephemeral=True)

    @commands.hybrid_command(name="untrack", description="Stop SYKE from reading a channel")
    @admin()
    async def untrack(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        removed = self.bot.storage.untrack(ctx.guild.id, channel.id)
        self.bot.scanner.invalidate(ctx.guild.id)
        body = f"Stopped watching {channel.mention}." if removed else f"{channel.mention} wasn't being watched."
        await ctx.reply(embed=notice("Channel untracked", body, BRAND), ephemeral=True)

    @commands.hybrid_command(name="channels", aliases=["settings", "config"], description="Show SYKE's settings and watched channels")
    @admin()
    async def channels(self, ctx: commands.Context) -> None:
        tracked = self.bot.storage.tracked(ctx.guild.id)
        lines = []
        for cid in tracked:
            ch = ctx.guild.get_channel(cid)
            lines.append(f"• {ch.mention}" if ch else f"• ~~deleted channel~~ `{cid}`")
        tz = self.bot.storage.timezone(ctx.guild.id) or self.bot.settings.default_timezone
        demo = "on" if self.bot.storage.demo_mode(ctx.guild.id) else "off"
        body = "\n".join(lines) or f"None yet. Add one with `{self.p(ctx)}track #general`."
        embed = notice("SYKE settings ⚙️", f"**Watched channels**\n{body}", BRAND)
        embed.add_field(name="Prefix", value=f"`{self.p(ctx)}`")
        embed.add_field(name="Timezone", value=tz)
        embed.add_field(name="Demo mode", value=demo)
        await ctx.reply(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="prefix", description="Change SYKE's prefix for this server")
    @app_commands.describe(new="The new prefix (up to 5 characters), or 'reset'")
    @admin()
    async def prefix(self, ctx: commands.Context, new: str | None = None) -> None:
        current = self.p(ctx)
        default = self.bot.settings.default_prefix
        if new is None:
            await ctx.reply(embed=notice(
                "Current prefix", f"The prefix here is `{current}`. Change it with `{current}prefix ?`.", BRAND
            ), ephemeral=True)
            return
        new = new.strip()
        if new.lower() == "reset":
            new = default
        if not new or len(new) > MAX_PREFIX_LENGTH or any(c.isspace() for c in new) or "`" in new:
            await ctx.reply(embed=notice(
                "Invalid prefix", f"Use 1-{MAX_PREFIX_LENGTH} characters with no spaces or backticks."
            ), ephemeral=True)
            return
        applied = self.bot.set_prefix(ctx.guild.id, None if new == default else new)
        await ctx.reply(embed=notice(
            "Prefix updated ✅",
            f"Commands now start with `{applied}`, for example `{applied}profile`. "
            f"Mentioning {self.bot.user.mention} always works if you forget it.",
            BRAND,
        ))

    @commands.hybrid_command(name="timezone", aliases=["tz"], description="Set the timezone used for 'most active' hours")
    @app_commands.describe(zone="An IANA timezone, e.g. Europe/London or America/New_York")
    @admin()
    async def timezone(self, ctx: commands.Context, zone: str) -> None:
        match = next((tz for tz in ALL_TIMEZONES if tz.lower() == zone.strip().lower()), None)
        if match is None:
            await ctx.reply(embed=notice(
                "Unknown timezone", f"`{zone}` isn't a timezone I know. Try something like `Europe/Berlin`."
            ), ephemeral=True)
            return
        self.bot.storage.set_timezone(ctx.guild.id, match)
        await ctx.reply(embed=notice("Timezone set 🕐", f"Activity hours now use **{match}**.", BRAND), ephemeral=True)

    @timezone.autocomplete("zone")
    async def timezone_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        needle = current.lower()
        return [app_commands.Choice(name=tz, value=tz) for tz in ALL_TIMEZONES if needle in tz.lower()][:25]

    @commands.hybrid_command(name="rescan", description="Re-read the watched channels now")
    @admin()
    async def rescan(self, ctx: commands.Context) -> None:
        self.bot.scanner.invalidate(ctx.guild.id)
        async with ctx.typing(ephemeral=True):
            result = await self.bot.analyze(ctx.guild)
        if result is None:
            await ctx.reply(embed=self.bot.no_channels(ctx.guild.id), ephemeral=True)
            return
        server, skipped = result
        body = f"Judged **{len(server.users)}** members; **{len(server.below_threshold)}** need more messages."
        if skipped:
            body += f"\n⚠️ Couldn't read {len(skipped)} channel(s). Check SYKE's permissions."
        await ctx.reply(embed=notice("Rescan complete 🔬", body, BRAND), ephemeral=True)

    @commands.hybrid_command(name="demo", description="Add fake members so you can test SYKE on your own")
    @app_commands.describe(enabled="On: fake members join rankings. Off: real members only.")
    @admin()
    async def demo(self, ctx: commands.Context, enabled: bool | None = None) -> None:
        if enabled is None:
            enabled = not self.bot.storage.demo_mode(ctx.guild.id)
        self.bot.storage.set_demo_mode(ctx.guild.id, enabled)
        p = self.p(ctx)
        if enabled:
            names = ", ".join(name for name, _, _ in PERSONAS.values())
            body = (
                f"Fake members **{names}** now count towards server comparisons and leaderboards.\n\n"
                f"Send at least **{self.bot.settings.min_messages}** messages in a watched channel, then run "
                f"`{p}profile`. Try `{p}sample zyro` to see a fake member's report.\n\n"
                f"Turn this off with `{p}demo off` before real members start using SYKE."
            )
            await ctx.reply(embed=notice("Demo mode on 🧪", body, BRAND), ephemeral=True)
        else:
            await ctx.reply(embed=notice("Demo mode off", "Fake members removed. Rankings use real members only.", BRAND), ephemeral=True)

    @commands.hybrid_command(name="sample", description="Show the report for one of SYKE's fake demo members")
    @app_commands.describe(name="Zyro, Mira, bubbles, Dex or Vex")
    @commands.cooldown(1, 10, commands.BucketType.member)
    @admin()
    async def sample(self, ctx: commands.Context, name: str = "Zyro") -> None:
        persona_id = PERSONA_BY_NAME.get(name.lower().strip())
        if persona_id is None:
            names = ", ".join(f"`{n}`" for n, _, _ in PERSONAS.values())
            await ctx.reply(embed=notice("Unknown demo member", f"Pick one of: {names}"), ephemeral=True)
            return
        async with ctx.typing():
            result = await self.bot.analyze(ctx.guild)
            server = result[0] if result else None
            if server is None or persona_id not in server.users:
                server = await asyncio.to_thread(
                    analyze_server, DEMO_MESSAGES, self.bot.tz_for(ctx.guild.id), self.bot.settings.min_messages
                )
            profile = build_profile(server, persona_id)
            await self.bot.send_report(ctx, profile, None, name_suffix=" (fake demo member)")

    @sample.autocomplete("name")
    async def sample_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=n, value=n)
            for n, _, _ in PERSONAS.values()
            if current.lower() in n.lower()
        ]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if not settings.discord_token:
        sys.exit("DISCORD_TOKEN is missing. Copy .env.example to .env and paste your bot token.")
    Syke(settings).run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
