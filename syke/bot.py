"""SYKE Discord bot: slash commands and wiring."""

from __future__ import annotations

import asyncio
import logging
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import discord
from discord import app_commands

from .ai import write_roast
from .config import Settings, load_settings
from .demo import PERSONAS, build_demo_server
from .models import Msg
from .profile import Profile, ServerAnalysis, analyze_server, build_profile
from .render import render_leaderboard, render_report
from .scanner import Scanner
from .storage import Storage
from .traits import TRAIT_META, TRAITS

log = logging.getLogger("syke")

BRAND = discord.Colour.from_str("#8B5CF6")
WARN = discord.Colour.from_str("#F59E0B")
ALL_TIMEZONES = sorted(available_timezones())
DEMO_MESSAGES = build_demo_server(headline_volume=400)
PERSONA_CHOICES = [app_commands.Choice(name=name, value=uid) for uid, (name, _, _) in PERSONAS.items()]

LEADERBOARD_CHOICES = [
    app_commands.Choice(name=f"{TRAIT_META[t][0]} {TRAIT_META[t][1]}", value=t) for t in TRAITS
] + [app_commands.Choice(name="💬 Most active", value="active")]


def notice(title: str, body: str, colour: discord.Colour = WARN) -> discord.Embed:
    return discord.Embed(title=title, description=body, colour=colour)


class Syke(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.storage = Storage(settings.db_path)
        self.scanner = Scanner(settings.scan_limit, settings.cache_minutes)
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(PublicCommands(self))
        self.tree.add_command(AdminCommands(self))
        self.tree.on_error = self.on_tree_error

    async def setup_hook(self) -> None:
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
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="your messages 👁️"))

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

    async def report_embed(self, guild_id: int, profile: Profile, avatar_url: str | None) -> discord.Embed:
        await write_roast(profile, self.settings)
        embed = discord.Embed(
            title=f"🧠 SYKE REPORT — {profile.name}",
            description=f"```\n{render_report(profile)}\n```"[:4096],
            colour=BRAND,
        )
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        engine = "AI analysis" if profile.ai_used else "offline analysis"
        demo = " • demo mode: fake members included" if self.storage.demo_mode(guild_id) else ""
        embed.set_footer(text=f"SYKE • {engine} • for entertainment purposes only{demo}")
        return embed

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            embed = notice("Easy there 🧪", f"The lab is still recovering. Try again in {error.retry_after:.0f}s.")
        elif isinstance(error, app_commands.MissingPermissions):
            embed = notice("Not so fast", "You need **Manage Server** to configure SYKE.")
        elif isinstance(error, app_commands.NoPrivateMessage):
            embed = notice("Servers only", "SYKE judges people in servers, not in DMs.")
        else:
            log.exception("Command failed", exc_info=error)
            embed = notice("Experiment failed 💥", "Something went wrong while judging. Try again in a moment.")
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


NO_CHANNELS = notice(
    "No channels are being watched",
    "An admin needs to pick which channels SYKE can read:\n`/syke-admin track channel:#general`",
)


@app_commands.guild_only()
class PublicCommands(app_commands.Group, name="syke", description="Get judged by SYKE"):
    def __init__(self, bot: Syke) -> None:
        super().__init__()
        self.bot = bot

    @app_commands.command(name="profile", description="Generate a personality report for someone (or yourself)")
    @app_commands.describe(member="Who to judge. Leave empty to judge yourself.")
    @app_commands.checks.cooldown(1, 20, key=lambda i: (i.guild_id, i.user.id))
    async def profile(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        target = member or interaction.user
        assert interaction.guild is not None
        if target.bot:
            await interaction.response.send_message(
                embed=notice("Nice try", "Bots have no personality. We checked."), ephemeral=True
            )
            return
        if target.id in self.bot.storage.optouts(interaction.guild.id):
            await interaction.response.send_message(
                embed=notice("Off limits", f"{target.display_name} has opted out of being judged."), ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        result = await self.bot.analyze(interaction.guild)
        if result is None:
            await interaction.followup.send(embed=NO_CHANNELS)
            return
        server, _ = result

        if target.id not in server.users:
            have = server.below_threshold.get(target.id, 0)
            need = self.bot.settings.min_messages
            await interaction.followup.send(embed=notice(
                "Insufficient evidence 🔬",
                f"{target.display_name} has only **{have}** message{'s' if have != 1 else ''} in the watched "
                f"channels. SYKE needs at least **{need}** before it can pass judgement.",
            ))
            return

        profile = build_profile(server, target.id)
        profile.name = target.display_name
        embed = await self.bot.report_embed(interaction.guild.id, profile, target.display_avatar.url)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="leaderboard", description="See who tops the server for a trait")
    @app_commands.describe(trait="Which trait to rank by")
    @app_commands.choices(trait=LEADERBOARD_CHOICES)
    @app_commands.checks.cooldown(1, 15, key=lambda i: (i.guild_id, i.user.id))
    async def leaderboard(self, interaction: discord.Interaction, trait: app_commands.Choice[str]) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(thinking=True)
        result = await self.bot.analyze(interaction.guild)
        if result is None:
            await interaction.followup.send(embed=NO_CHANNELS)
            return
        server, _ = result
        if not server.users:
            await interaction.followup.send(embed=notice(
                "Nobody qualifies yet",
                f"Members need {self.bot.settings.min_messages}+ messages in watched channels to be ranked.",
            ))
            return
        embed = discord.Embed(
            title=f"{trait.name} — server leaderboard",
            description=f"```\n{render_leaderboard(server, trait.value)}\n```",
            colour=BRAND,
        )
        demo = " • includes fake demo members" if self.bot.storage.demo_mode(interaction.guild.id) else ""
        embed.set_footer(text=f"{len(server.users)} members judged{demo}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="optout", description="Stop SYKE from reading or judging your messages here")
    async def optout(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        self.bot.storage.set_optout(interaction.guild.id, interaction.user.id, True)
        await interaction.response.send_message(embed=notice(
            "You're off the record 🫥",
            "SYKE will ignore your messages and refuse to judge you in this server. Use `/syke optin` to come back.",
            BRAND,
        ), ephemeral=True)

    @app_commands.command(name="optin", description="Let SYKE judge you again")
    async def optin(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        self.bot.storage.set_optout(interaction.guild.id, interaction.user.id, False)
        await interaction.response.send_message(embed=notice(
            "Welcome back to the lab 🧪", "Your messages count again. Brace yourself.", BRAND
        ), ephemeral=True)

    @app_commands.command(name="help", description="How SYKE works")
    async def help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🧠 SYKE — personality judgement, scientifically questionable",
            colour=BRAND,
            description=(
                "SYKE reads the channels your admins chose, crunches the numbers, "
                "and writes a brutally honest personality report.\n\n"
                "**Everyone**\n"
                "`/syke profile [member]` — get the full report\n"
                "`/syke leaderboard <trait>` — who's the funniest, most toxic, most chaotic…\n"
                "`/syke optout` / `/syke optin` — control whether you're judged\n\n"
                "**Admins (Manage Server)**\n"
                "`/syke-admin track` / `untrack` — choose watched channels\n"
                "`/syke-admin channels` — see what's watched\n"
                "`/syke-admin timezone` — set the server timezone for activity hours\n"
                "`/syke-admin rescan` — force a fresh read of the channels\n"
                "`/syke-admin demo` — add fake members so you can test SYKE alone\n"
                "`/syke-admin sample` — see a report for one of the fake members\n\n"
                "SYKE doesn't store message content. Reports are for fun only."
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class AdminCommands(app_commands.Group, name="syke-admin", description="Configure SYKE for this server"):
    def __init__(self, bot: Syke) -> None:
        super().__init__()
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None or not perms.manage_guild:
            raise app_commands.MissingPermissions(["manage_guild"])
        return True

    @app_commands.command(name="track", description="Let SYKE read a channel for personality analysis")
    async def track(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild is not None
        me = interaction.guild.me
        perms = channel.permissions_for(me)
        if not (perms.view_channel and perms.read_message_history):
            await interaction.response.send_message(embed=notice(
                "I can't see in there 👀",
                f"Give SYKE **View Channel** and **Read Message History** in {channel.mention}, then try again.",
            ), ephemeral=True)
            return
        added = self.bot.storage.track(interaction.guild.id, channel.id)
        self.bot.scanner.invalidate(interaction.guild.id)
        body = f"Now watching {channel.mention}." if added else f"{channel.mention} was already being watched."
        await interaction.response.send_message(embed=notice("Channel tracked ✅", body, BRAND), ephemeral=True)

    @app_commands.command(name="untrack", description="Stop SYKE from reading a channel")
    async def untrack(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild is not None
        removed = self.bot.storage.untrack(interaction.guild.id, channel.id)
        self.bot.scanner.invalidate(interaction.guild.id)
        body = f"Stopped watching {channel.mention}." if removed else f"{channel.mention} wasn't being watched."
        await interaction.response.send_message(embed=notice("Channel untracked", body, BRAND), ephemeral=True)

    @app_commands.command(name="channels", description="List the channels SYKE is watching")
    async def channels(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        tracked = self.bot.storage.tracked(interaction.guild.id)
        if not tracked:
            await interaction.response.send_message(embed=NO_CHANNELS, ephemeral=True)
            return
        lines = []
        for cid in tracked:
            ch = interaction.guild.get_channel(cid)
            lines.append(f"• {ch.mention}" if ch else f"• ~~deleted channel~~ `{cid}`")
        tz = self.bot.storage.timezone(interaction.guild.id) or self.bot.settings.default_timezone
        embed = notice("Watched channels 👁️", "\n".join(lines), BRAND)
        embed.set_footer(text=f"Timezone: {tz} • reads up to {self.bot.settings.scan_limit:,} messages per channel")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="timezone", description="Set the timezone used for 'most active' hours")
    @app_commands.describe(name="An IANA timezone, e.g. Europe/London or America/New_York")
    async def timezone(self, interaction: discord.Interaction, name: str) -> None:
        assert interaction.guild is not None
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            await interaction.response.send_message(embed=notice(
                "Unknown timezone", f"`{name}` isn't a timezone I know. Try something like `Europe/Berlin`."
            ), ephemeral=True)
            return
        self.bot.storage.set_timezone(interaction.guild.id, name)
        await interaction.response.send_message(embed=notice("Timezone set 🕐", f"Activity hours now use **{name}**.", BRAND), ephemeral=True)

    @timezone.autocomplete("name")
    async def timezone_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        needle = current.lower()
        matches = [tz for tz in ALL_TIMEZONES if needle in tz.lower()][:25]
        return [app_commands.Choice(name=tz, value=tz) for tz in matches]

    @app_commands.command(name="rescan", description="Throw away cached data and re-read the watched channels")
    async def rescan(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        self.bot.scanner.invalidate(interaction.guild.id)
        await interaction.response.defer(thinking=True, ephemeral=True)
        result = await self.bot.analyze(interaction.guild)
        if result is None:
            await interaction.followup.send(embed=NO_CHANNELS, ephemeral=True)
            return
        server, skipped = result
        body = f"Judged **{len(server.users)}** members; **{len(server.below_threshold)}** need more messages."
        if skipped:
            body += f"\n⚠️ Couldn't read {len(skipped)} channel(s). Check SYKE's permissions."
        await interaction.followup.send(embed=notice("Rescan complete 🔬", body, BRAND), ephemeral=True)

    @app_commands.command(name="demo", description="Add fake members so you can test SYKE on your own")
    @app_commands.describe(enabled="On: fake members join rankings and leaderboards. Off: real members only.")
    async def demo(self, interaction: discord.Interaction, enabled: bool) -> None:
        assert interaction.guild is not None
        self.bot.storage.set_demo_mode(interaction.guild.id, enabled)
        names = ", ".join(name for name, _, _ in PERSONAS.values())
        if enabled:
            body = (
                f"Fake members **{names}** now count towards server comparisons and leaderboards.\n\n"
                f"Send at least **{self.bot.settings.min_messages}** messages in a watched channel, then run "
                "`/syke profile`. Use `/syke-admin sample` to see a fake member's report.\n\n"
                "Turn this off before real members start using SYKE."
            )
            await interaction.response.send_message(embed=notice("Demo mode on 🧪", body, BRAND), ephemeral=True)
        else:
            body = "Fake members removed. Rankings now use real members only."
            await interaction.response.send_message(embed=notice("Demo mode off", body, BRAND), ephemeral=True)

    @app_commands.command(name="sample", description="Show the report for one of SYKE's fake demo members")
    @app_commands.describe(persona="Which fake member to judge")
    @app_commands.choices(persona=PERSONA_CHOICES)
    @app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
    async def sample(self, interaction: discord.Interaction, persona: app_commands.Choice[int]) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(thinking=True)
        result = await self.bot.analyze(interaction.guild)
        server = result[0] if result else None
        if server is None or persona.value not in server.users:
            server = await asyncio.to_thread(
                analyze_server, DEMO_MESSAGES, self.bot.tz_for(interaction.guild.id), self.bot.settings.min_messages
            )
        profile = build_profile(server, persona.value)
        embed = await self.bot.report_embed(interaction.guild.id, profile, None)
        embed.title = f"🧠 SYKE REPORT — {profile.name} (fake demo member)"
        await interaction.followup.send(embed=embed)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if not settings.discord_token:
        sys.exit("DISCORD_TOKEN is missing. Copy .env.example to .env and paste your bot token.")
    Syke(settings).run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
