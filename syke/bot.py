"""SYKE Discord bot. Every command works both as a slash command and with the server prefix."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import discord
from discord import app_commands
from discord.ext import commands

from .ai import pick_comeback, write_roast
from .cards import MESSAGE_VIEW_TIMEOUT, ProfileView, build_pages, iq_page
from .config import Settings, load_settings
from .demo import PERSONAS, build_demo_server
from .markov import build_chain
from .models import Msg
from .profile import Profile, ServerAnalysis, analyze_server, build_profile, refine_scores
from .render import render_leaderboard
from .scanner import Scanner, to_msg
from .storage import Storage
from .traits import TRAIT_META, TRAITS

log = logging.getLogger("syke")

BRAND = discord.Colour.from_str("#8B5CF6")
WARN = discord.Colour.from_str("#F59E0B")
ALL_TIMEZONES = sorted(available_timezones())
DEMO_MESSAGES = build_demo_server(headline_volume=400)
MAX_PREFIX_LENGTH = 5
MENTION_ONLY = "<mention>"
MENTION_WORDS = {"mention", "@", "ping", "@mention"}
VERSION = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "dev")[:7]
NEEDED_PERMISSIONS = ("view_channel", "send_messages", "embed_links", "read_message_history")
MIN_MIMIC_MESSAGES = 10
COLLECT_DEFAULT = 5000
COLLECT_MAX = 20000
YAP_REPLY_COOLDOWN = 4
STOPWORDS = {
    "the", "and", "you", "your", "are", "was", "but", "not", "that", "this", "with", "have", "just",
    "like", "what", "for", "its", "it's", "i'm", "im", "dont", "don't", "can", "get", "got", "all",
    "how", "why", "who", "when", "then", "than", "they", "them", "she", "her", "his", "him", "out",
}

TRAIT_ALIASES: dict[str, str] = {
    **{t: t for t in TRAITS},
    "funniest": "funny", "freak": "freaky", "chaos": "chaotic", "smart": "serious",
    "active": "active", "messages": "active", "yap": "active", "yapper": "active",
    "iq": "iq", "smartest": "iq", "brain": "iq", "brains": "iq",
}
TRAIT_TITLES: dict[str, str] = {
    **{t: f"{TRAIT_META[t][0]} {TRAIT_META[t][1]}" for t in TRAITS},
    "active": "💬 Most active",
    "iq": "🧠 Highest IQ",
}
PERSONA_BY_NAME: dict[str, int] = {name.lower(): uid for uid, (name, _, _) in PERSONAS.items()}

F = TypeVar("F", bound=Callable)


def notice(title: str, body: str, colour: discord.Colour = WARN) -> discord.Embed:
    return discord.Embed(title=title, description=body, colour=colour)


def corpus_row(message: discord.Message) -> tuple:
    m = to_msg(message)
    return (m.message_id, m.channel_id, m.author_id, m.content, m.created_at.timestamp(),
            m.author_name, m.laugh_reactions, m.total_reactions)


def msg_row(m: Msg) -> tuple:
    return (m.message_id, m.channel_id, m.author_id, m.content, m.created_at.timestamp(),
            m.author_name, m.laugh_reactions, m.total_reactions)


def server_emojis(guild: discord.Guild) -> dict[str, str]:
    """Custom emojis SYKE can post here, by name."""
    return {e.name: str(e) for e in getattr(guild, "emojis", ()) if getattr(e, "available", True)}


def public() -> Callable[[F], F]:
    def decorator(func: F) -> F:
        func = app_commands.guild_only()(func)
        return commands.guild_only()(func)
    return decorator


def is_admin(bot: Syke, member: discord.abc.User) -> bool:
    if member.id in bot.settings.owner_ids:
        return True
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


async def _admin_predicate(ctx: commands.Context) -> bool:
    if is_admin(ctx.bot, ctx.author):
        return True
    raise commands.MissingPermissions(["manage_guild"])


def admin() -> Callable[[F], F]:
    """Manage Server or a SYKE owner (SYKE_OWNER_IDS).

    The slash versions stay hidden from members without Manage Server; Discord enforces that
    itself, so owners without the permission use the prefix or @mention form instead.
    """
    def decorator(func: F) -> F:
        func = app_commands.default_permissions(manage_guild=True)(func)
        func = commands.check(_admin_predicate)(func)
        return public()(func)
    return decorator


async def resolve_prefix(bot: Syke, message: discord.Message) -> list[str]:
    prefix = bot.prefix_for(message.guild.id) if message.guild else bot.settings.default_prefix
    if prefix == MENTION_ONLY:
        return commands.when_mentioned(bot, message)
    return commands.when_mentioned_or(prefix)(bot, message)


class MissingChannelPermissions(commands.CheckFailure):
    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"missing {', '.join(missing)}")
        self.missing = missing


class SykeContext(commands.Context):
    async def reply(self, content=None, **kwargs):
        """Fall back to a plain message when Discord refuses a reply (e.g. no Read Message History)."""
        try:
            return await super().reply(content, **kwargs)
        except discord.HTTPException:
            if self.interaction is not None:
                raise
            log.warning("Reply failed in #%s; retrying without a message reference", getattr(self.channel, "name", "?"))
            kwargs.pop("mention_author", None)
            return await self.send(content, **kwargs)


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
        self._analyses: dict[int, tuple[tuple, ServerAnalysis]] = {}
        self._ingested: dict[int, float] = {}
        self._background: set[asyncio.Task] = set()
        self._warmed = False
        self._last_yap: dict[int, float] = {}
        self._last_comeback: dict[tuple[int, int], float] = {}
        self._history_version: dict[int, int] = {}
        self._last_summary: dict[tuple[int, int], str] = {}
        self._deep_scans: set[tuple[int, int]] = set()
        self.add_check(self.channel_check)

    async def setup_hook(self) -> None:
        await self.add_cog(SykeCommands(self))
        self.add_view(ProfileView(self.load_card))
        log.info("SYKE %s starting: %d prefix commands, default prefix %r",
                 VERSION, len(self.commands), self.settings.default_prefix)
        try:
            if self.settings.dev_guild_id:
                guild = discord.Object(id=self.settings.dev_guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d slash commands to dev guild %s", len(synced), guild.id)
            else:
                synced = await self.tree.sync()
                log.info("Synced %d global slash commands (may take a while to appear)", len(synced))
        except discord.HTTPException:
            log.exception("Slash command sync failed; prefix commands still work")

    async def get_context(self, origin, *, cls=None):
        return await super().get_context(origin, cls=cls or SykeContext)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Joined %r (%s)", guild.name, guild.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("Removed from %r (%s)", guild.name, guild.id)

    def invite_url(self) -> str:
        return discord.utils.oauth_url(
            self.application_id or (self.user.id if self.user else 0),
            permissions=discord.Permissions(**{p: True for p in NEEDED_PERMISSIONS}),
            scopes=("bot", "applications.commands"),
        )

    async def on_command(self, ctx: commands.Context) -> None:
        where = f"{ctx.guild.id}/#{getattr(ctx.channel, 'name', ctx.channel.id)}" if ctx.guild else "DM"
        style = "slash" if ctx.interaction else f"prefix {ctx.prefix!r}"
        override = ""
        perms = getattr(ctx.author, "guild_permissions", None)
        if ctx.author.id in self.settings.owner_ids and not (perms and perms.manage_guild):
            override = " [owner override]"
        log.info("%s ran %s (%s) in %s%s", ctx.author, ctx.command.qualified_name, style, where, override)

    async def on_ready(self) -> None:
        log.info("SYKE %s online as %s in %d servers (AI: %s)", VERSION, self.user, len(self.guilds), self.settings.ai_provider)
        if not self.intents.message_content:
            log.warning("Message Content intent is off: prefix commands will not work")
        for guild in self.guilds:
            log.info("  member of %r (%s), prefix %r", guild.name, guild.id, self.display_prefix(guild.id))
        if self.settings.owner_ids:
            log.info("Owners with admin access everywhere: %s", ", ".join(map(str, sorted(self.settings.owner_ids))))
        activity = discord.Activity(type=discord.ActivityType.watching, name=f"you 👁️ | {self.settings.default_prefix}help")
        await self.change_presence(activity=activity)
        if not self._warmed:
            self._warmed = True
            for guild in self.guilds:
                if self.storage.tracked(guild.id):
                    self.spawn(self._warm(guild))

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _warm(self, guild: discord.Guild) -> None:
        """Read watched channels after startup so the first !profile doesn't wait on a full scan."""
        started = time.monotonic()
        try:
            await self.analyze(guild)
            log.info("Pre-scanned %r in %.1fs", guild.name, time.monotonic() - started)
        except Exception:
            log.exception("Pre-scan of %r failed", guild.name)

    async def on_message(self, message: discord.Message) -> None:
        await self.process_commands(message)
        if message.author.bot or not message.guild or not message.content:
            return
        if self.is_command_text(message.guild.id, message.content):
            return
        if message.channel.id in self.storage.tracked(message.guild.id) \
                and message.author.id not in self.storage.optouts(message.guild.id):
            self.storage.add_corpus(message.guild.id, [corpus_row(message)])
        said = await self.replied_yap(message)
        if said is not None:
            await self.answer_yap_reply(message, said)
        else:
            await self.maybe_yap(message)

    async def replied_yap(self, message: discord.Message) -> str | None:
        """The text of SYKE's yap that `message` replies to, if it is one.

        Yaps are the only plain-text messages SYKE sends (everything else is an embed), so this
        also recognises yaps sent before a restart.
        """
        ref = message.reference
        if ref is None or ref.message_id is None or self.user is None:
            return None
        if message.channel.id not in self.storage.yap_channels(message.guild.id):
            return None
        target = ref.resolved if isinstance(ref.resolved, discord.Message) else ref.cached_message
        if target is None:
            try:
                target = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return None
        if target.author.id != self.user.id or target.embeds or not target.content:
            return None
        return target.content

    def comeback_candidates(self, guild_id: int, reply: str, exclude: set[str]) -> list[str]:
        words = sorted({w for w in re.findall(r"[a-z0-9']+", reply.lower())
                        if len(w) >= 3 and w not in STOPWORDS}, key=len, reverse=True)[:6]
        pool = self.storage.search_messages(guild_id, words, 30) + self.storage.random_messages(guild_id, 30)
        seen = {e.strip().lower() for e in exclude}
        candidates = []
        for text in pool:
            key = text.strip().lower()
            if key in seen or len(text.split()) < 2 or self.is_command_text(guild_id, text):
                continue
            seen.add(key)
            candidates.append(text)
        return candidates

    async def answer_yap_reply(self, message: discord.Message, said: str) -> None:
        """Someone answered a yap: reply with the stored message that best fits, picked by AI."""
        key = (message.channel.id, message.author.id)
        now = time.monotonic()
        if now - self._last_comeback.get(key, -1e9) < YAP_REPLY_COOLDOWN:
            return
        self._last_comeback[key] = now
        candidates = self.comeback_candidates(message.guild.id, message.content, {said, message.content})
        if not candidates:
            return
        async with message.channel.typing():
            pick = await pick_comeback(self.settings, said, message.content, candidates)
        if pick is None:
            return
        with contextlib.suppress(discord.HTTPException):
            await message.reply(candidates[pick], mention_author=False)

    def is_command_text(self, guild_id: int, text: str) -> bool:
        text = text.lstrip()
        starts = {self.settings.default_prefix}
        if self.prefix_for(guild_id) != MENTION_ONLY:
            starts.add(self.prefix_for(guild_id))
        if self.user:
            starts |= {f"<@{self.user.id}>", f"<@!{self.user.id}>"}
        return any(text.startswith(s) for s in starts)

    def pick_yap(self, guild_id: int, exclude_id: int = 0) -> str | None:
        for text in self.storage.random_messages(guild_id, 15, exclude_id):
            if len(text.split()) >= 3 and not self.is_command_text(guild_id, text):
                return text
        return None

    async def maybe_yap(self, message: discord.Message) -> None:
        """In yap channels, now and then answer chat with something a member said in the past."""
        if message.channel.id not in self.storage.yap_channels(message.guild.id):
            return
        now = time.monotonic()
        if now - self._last_yap.get(message.channel.id, -1e9) < self.settings.yap_cooldown:
            return
        if random.random() >= self.settings.yap_chance:
            return
        line = self.pick_yap(message.guild.id, exclude_id=message.id)
        if line is None:
            return
        self._last_yap[message.channel.id] = now
        with contextlib.suppress(discord.HTTPException):
            await message.channel.send(line)

    def prefix_for(self, guild_id: int) -> str:
        if guild_id not in self._prefixes:
            self._prefixes[guild_id] = self.storage.prefix(guild_id) or self.settings.default_prefix
        return self._prefixes[guild_id]

    def set_prefix(self, guild_id: int, prefix: str | None) -> str:
        self.storage.set_prefix(guild_id, prefix)
        self._prefixes.pop(guild_id, None)
        return self.prefix_for(guild_id)

    def display_prefix(self, guild_id: int) -> str:
        """What users should type before a command, e.g. '!' or '@SYKE '."""
        prefix = self.prefix_for(guild_id)
        if prefix == MENTION_ONLY:
            return f"@{self.user.name if self.user else 'SYKE'} "
        return prefix

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
        scanned_at = None
        optouts = self.storage.optouts(guild.id)
        if tracked:
            scan = await self.scanner.scan(guild, tracked)
            messages = [m for m in scan.messages if m.author_id not in optouts]
            skipped = scan.skipped_channels
            scanned_at = scan.finished_at
            if self._ingested.get(guild.id) != scanned_at:
                self._ingested[guild.id] = scanned_at
                rows = [msg_row(m) for m in messages
                        if m.message_id and not self.is_command_text(guild.id, m.content)]
                await asyncio.to_thread(self.storage.add_corpus, guild.id, rows)
        tz = self.tz_for(guild.id)
        key = (scanned_at, demo, str(tz), frozenset(optouts), self.settings.min_messages,
               self._history_version.get(guild.id, 0))
        cached = self._analyses.get(guild.id)
        if cached and cached[0] == key:
            return cached[1], skipped
        if tracked:
            messages = messages + await asyncio.to_thread(self.older_messages, guild.id, tracked, messages, optouts)
        if demo:
            messages = messages + DEMO_MESSAGES
        labels = await asyncio.to_thread(self.storage.labels, guild.id)
        server = await asyncio.to_thread(analyze_server, messages, tz, self.settings.min_messages, labels)
        self._analyses[guild.id] = (key, server)
        return server, skipped

    def older_messages(self, guild_id: int, tracked: list[int], scanned: list[Msg], optouts: set[int]) -> list[Msg]:
        """Stored messages the live scan didn't reach (e.g. from `scanme` or `collect`)."""
        seen = {m.message_id for m in scanned}
        extra = []
        for mid, cid, author, name, content, created, laughs, total in self.storage.history(guild_id, tracked):
            if mid in seen or author in optouts or self.is_command_text(guild_id, content):
                continue
            extra.append(Msg(author, name, content, datetime.fromtimestamp(created, timezone.utc),
                             cid, laughs, total, mid))
        return extra

    def bump_history(self, guild_id: int) -> None:
        self._history_version[guild_id] = self._history_version.get(guild_id, 0) + 1

    async def send_report(
        self, ctx: commands.Context, profile: Profile, avatar_url: str | None, name_suffix: str = "",
        server: ServerAnalysis | None = None,
    ) -> None:
        key = (ctx.guild.id, profile.user_id)
        await write_roast(profile, self.settings, server_emojis(ctx.guild), avoid=self._last_summary.get(key))
        if profile.ai_used:
            self._last_summary[key] = profile.summary
        if profile.ai_labels:
            await asyncio.to_thread(self.storage.save_labels, ctx.guild.id, profile.ai_labels)
            self.bump_history(ctx.guild.id)
        if server is not None and (profile.ai_labels or profile.ai_scores):
            labels = await asyncio.to_thread(self.storage.labels, ctx.guild.id)
            refine_scores(profile, server, self.tz_for(ctx.guild.id), labels)
        profile.name += name_suffix
        pages = build_pages(profile, avatar_url, demo=self.storage.demo_mode(ctx.guild.id))
        view = ProfileView(self.load_card, timeout=MESSAGE_VIEW_TIMEOUT)
        message = await ctx.reply(embed=pages["overview"], view=view)
        if message is not None:
            self.storage.save_card(message.id, ctx.author.id, {k: e.to_dict() for k, e in pages.items()})

    def load_card(self, message_id: int) -> tuple[int, dict[str, discord.Embed]] | None:
        card = self.storage.card(message_id)
        if card is None:
            return None
        owner_id, pages = card
        return owner_id, {k: discord.Embed.from_dict(v) for k, v in pages.items()}

    def no_channels(self, guild_id: int) -> discord.Embed:
        p = self.display_prefix(guild_id)
        return notice(
            "No channels are being watched",
            f"An admin needs to pick which channels SYKE can read:\n`{p}track #general`",
        )

    async def channel_check(self, ctx: commands.Context) -> bool:
        if ctx.interaction is not None or ctx.guild is None:
            return True
        perms = ctx.channel.permissions_for(ctx.me)
        missing = [p for p in NEEDED_PERMISSIONS if not getattr(perms, p)]
        if missing:
            raise MissingChannelPermissions(missing)
        return True

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, MissingChannelPermissions):
            pretty = ", ".join(p.replace("_", " ").title() for p in error.missing)
            channel = getattr(ctx.channel, "mention", "this channel")
            log.warning("Can't respond in #%s (guild %s): missing %s",
                        getattr(ctx.channel, "name", "?"), ctx.guild.id, pretty)
            with contextlib.suppress(discord.HTTPException):
                await ctx.author.send(embed=notice(
                    "I can't reply there 🙊",
                    f"You ran `{ctx.message.content[:50]}` in {channel}, but SYKE is missing **{pretty}** there. "
                    "Ask an admin to allow those for SYKE's role in that channel, or use the slash command instead.",
                ))
            return
        err: BaseException = error
        while isinstance(err, (commands.HybridCommandError, commands.CommandInvokeError,
                               app_commands.CommandInvokeError)) and getattr(err, "original", None):
            err = err.original

        p = self.display_prefix(ctx.guild.id) if ctx.guild else self.settings.default_prefix
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
        try:
            await ctx.send(embed=embed, ephemeral=True)
        except discord.HTTPException:
            log.warning("Could not report an error for %s", ctx.command, exc_info=True)


class SykeCommands(commands.Cog, name="SYKE"):
    def __init__(self, bot: Syke) -> None:
        self.bot = bot

    def p(self, ctx: commands.Context) -> str:
        return self.bot.display_prefix(ctx.guild.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild or not self.bot.user:
            return
        if message.content.strip() in {f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"}:
            p = self.bot.display_prefix(message.guild.id)
            if self.bot.prefix_for(message.guild.id) == MENTION_ONLY:
                how = f"This server uses mentions as the prefix. Try `{p}profile` or `{p}help`."
            else:
                how = f"My prefix here is `{p}`. Try `{p}profile` or `{p}help`."
            await message.reply(embed=notice("👁️ SYKE is watching", f"{how} Slash commands work too.", BRAND),
                                mention_author=False)

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
                await ctx.reply(embed=self.not_enough(ctx, target, server))
                return
            profile = build_profile(server, target.id)
            profile.name = target.display_name
            await self.bot.send_report(ctx, profile, target.display_avatar.url, server=server)

    @commands.hybrid_command(name="iq", aliases=["brain", "smarts", "iqtest"], description="SYKE's (very unscientific) IQ estimate for someone")
    @app_commands.describe(member="Whose IQ to measure. Leave empty for your own.")
    @commands.cooldown(1, 10, commands.BucketType.member)
    @public()
    async def iq(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        target = member or ctx.author
        if target.bot:
            await ctx.reply(embed=notice("Nice try", "Bots are artificially intelligent. It's in the name."), ephemeral=True)
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
        user = server.users.get(target.id)
        if user is None or user.iq is None:
            await ctx.reply(embed=self.not_enough(ctx, target, server))
            return
        others = [u.iq.iq for uid, u in server.users.items() if uid != target.id and u.iq]
        smarter_than = round(sum(v < user.iq.iq for v in others) / len(others) * 100) if len(others) >= 2 else None
        await ctx.reply(embed=iq_page(target.display_name, user.iq, smarter_than, target.display_avatar.url,
                                      demo=self.bot.storage.demo_mode(ctx.guild.id)))

    def not_enough(self, ctx: commands.Context, target: discord.abc.User, server: ServerAnalysis) -> discord.Embed:
        have = server.below_threshold.get(target.id, 0)
        need = self.bot.settings.min_messages
        return notice(
            "Insufficient evidence 🔬",
            f"{target.display_name} has only **{have}** message{'s' if have != 1 else ''} in the watched "
            f"channels. SYKE needs at least **{need}** before it can pass judgement."
            + (f" Run `{self.p(ctx)}scanme` to dig up older ones." if target.id == ctx.author.id else ""),
        )

    @commands.hybrid_command(name="top", aliases=["leaderboard", "lb"], description="Server leaderboard for a trait")
    @app_commands.describe(trait="funny, toxic, cringe, freaky, serious, chaotic, active or iq")
    @commands.cooldown(1, 15, commands.BucketType.member)
    @public()
    async def top(self, ctx: commands.Context, trait: str = "funny") -> None:
        assert ctx.guild is not None
        key = TRAIT_ALIASES.get(trait.lower().strip())
        if key is None:
            options = ", ".join(f"`{t}`" for t in [*TRAITS, "active", "iq"])
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

    async def _deep_read(self, guild_id: int, channel, author_id: int, limit: int) -> list[tuple]:
        rows = []
        async for message in channel.history(limit=limit):
            if message.author.id == author_id and message.content \
                    and not self.bot.is_command_text(guild_id, message.content):
                rows.append(corpus_row(message))
        return rows

    @commands.hybrid_command(name="scanme", aliases=["scan", "addme", "deepscan"],
                             description="Dig through older history so all your messages count towards your profile")
    @commands.cooldown(1, 600, commands.BucketType.member)
    @public()
    async def scanme(self, ctx: commands.Context) -> None:
        guild, author = ctx.guild, ctx.author
        if author.id in self.bot.storage.optouts(guild.id):
            await ctx.reply(embed=notice("You're opted out", f"Run `{self.p(ctx)}optin` first."), ephemeral=True)
            return
        channels = [ch for cid in self.bot.storage.tracked(guild.id) if (ch := guild.get_channel(cid))
                    and ch.permissions_for(guild.me).read_message_history]
        if not channels:
            await ctx.reply(embed=self.bot.no_channels(guild.id), ephemeral=True)
            return
        key = (guild.id, author.id)
        if key in self.bot._deep_scans:
            await ctx.reply(embed=notice("Already digging ⛏️", "Your scan is still running. Hang tight."), ephemeral=True)
            return
        self.bot._deep_scans.add(key)
        limit = self.bot.settings.deep_scan_limit
        started = time.monotonic()
        try:
            async with ctx.typing():
                results = await asyncio.gather(
                    *(self._deep_read(guild.id, ch, author.id, limit) for ch in channels), return_exceptions=True
                )
                rows = [row for r in results if not isinstance(r, BaseException) for row in r]
                for ch, r in zip(channels, results):
                    if isinstance(r, BaseException):
                        log.warning("Deep scan failed in #%s: %s", ch.name, r)
                added = await asyncio.to_thread(self.bot.storage.add_corpus, guild.id, rows)
                self.bot.bump_history(guild.id)
                result = await self.bot.analyze(guild)
        finally:
            self.bot._deep_scans.discard(key)
        log.info("Deep scan for %s in %s: %d messages (%d new) in %.1fs",
                 author, guild.id, len(rows), added, time.monotonic() - started)

        server = result[0] if result else None
        need = self.bot.settings.min_messages
        if server and author.id in server.users:
            total = server.users[author.id].stats.message_count
            status = f"Your profile now draws on **{total:,}** messages. Run `{self.p(ctx)}profile` to see it."
        else:
            total = server.below_threshold.get(author.id, 0) if server else 0
            status = f"That's **{total}** in total; SYKE needs **{need}** before it can judge you."
        body = (f"Read back up to **{limit:,}** messages in {len(channels)} watched channel"
                f"{'s' if len(channels) != 1 else ''} and found **{len(rows):,}** of yours "
                f"(**{added:,}** SYKE hadn't seen).\n{status}")
        await ctx.reply(embed=notice("Scan complete ⛏️", body, BRAND))

    async def corpus(self, guild: discord.Guild, author_id: int) -> list[str] | None:
        """Texts to learn from; a scan of watched channels tops up a thin corpus. None if nothing is set up."""
        rows = self.bot.storage.corpus(guild.id, author_id)
        if len(rows) < MIN_MIMIC_MESSAGES and self.bot.storage.tracked(guild.id):
            await self.bot.analyze(guild)
            rows = self.bot.storage.corpus(guild.id, author_id)
        if not rows and not self.bot.storage.tracked(guild.id):
            return None
        optouts = self.bot.storage.optouts(guild.id)
        return [text for uid, text in rows if uid not in optouts]

    async def babble(self, ctx: commands.Context, author_id: int) -> tuple[str | None, int] | None:
        async with ctx.typing():
            texts = await self.corpus(ctx.guild, author_id)
            if texts is None:
                return None
            if len(texts) < MIN_MIMIC_MESSAGES:
                return None, len(texts)
            chain = await asyncio.to_thread(build_chain, texts)
            return chain.generate(random.Random()), len(texts)

    @commands.hybrid_command(name="mimic", aliases=["impersonate", "copy"], description="Make SYKE talk like someone, using a Markov chain of their messages")
    @app_commands.describe(member="Who to imitate. Leave empty to imitate yourself.")
    @commands.cooldown(1, 5, commands.BucketType.member)
    @public()
    async def mimic(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        target = member or ctx.author
        if target.bot:
            await ctx.reply(embed=notice("Nice try", "Bots already talk like bots."), ephemeral=True)
            return
        if target.id in self.bot.storage.optouts(ctx.guild.id):
            await ctx.reply(embed=notice("Off limits", f"{target.display_name} has opted out of SYKE."), ephemeral=True)
            return
        result = await self.babble(ctx, target.id)
        if result is None:
            await ctx.reply(embed=self.bot.no_channels(ctx.guild.id))
            return
        line, count = result
        if line is None:
            await ctx.reply(embed=notice(
                "Not enough to go on 🔬",
                f"SYKE has only **{count}** message{'s' if count != 1 else ''} from {target.display_name} and needs "
                f"at least **{MIN_MIMIC_MESSAGES}**. An admin can run `{self.p(ctx)}collect` to read more history.",
            ))
            return
        embed = discord.Embed(description=line, colour=BRAND)
        embed.set_author(name=f"{target.display_name} (probably)", icon_url=target.display_avatar.url)
        embed.set_footer(text=f"Markov chain of {count} messages • not a real quote")
        await ctx.reply(embed=embed)

    @commands.hybrid_command(name="diagnose", aliases=["check"], description="Check why SYKE's prefix commands might not work here")
    @app_commands.guild_only()
    async def diagnose(self, ctx: commands.Context) -> None:
        guild_id = ctx.interaction.guild_id if ctx.interaction else (ctx.guild.id if ctx.guild else None)
        if guild_id is None:
            await ctx.reply(embed=notice("Servers only", "Run this inside a server."), ephemeral=True)
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await ctx.reply(embed=notice(
                "❌ SYKE isn't in this server",
                "Only SYKE's slash commands were added here, not the bot itself, so it can't read "
                "messages, use a prefix or scan channels.\n\n"
                f"**Fix:** [invite SYKE as a bot]({self.bot.invite_url()}) and pick this server. "
                "Your settings stay as they are.",
            ), ephemeral=True)
            return

        channel_id = ctx.interaction.channel_id if ctx.interaction else ctx.channel.id
        channel = guild.get_channel_or_thread(channel_id)
        rows = ["✅ SYKE is a member of this server",
                f"{'✅' if self.bot.intents.message_content else '❌'} Message Content intent"]
        missing: list[str] = []
        if channel is None:
            rows.append("❌ SYKE can't see this channel at all")
            missing = list(NEEDED_PERMISSIONS)
        else:
            perms = channel.permissions_for(guild.me)
            for perm in NEEDED_PERMISSIONS:
                ok = getattr(perms, perm)
                rows.append(f"{'✅' if ok else '❌'} {perm.replace('_', ' ').title()} in this channel")
                if not ok:
                    missing.append(perm)

        p = self.bot.display_prefix(guild.id)
        tracked = self.bot.storage.tracked(guild.id)
        unreadable = [
            cid for cid in tracked
            if (ch := guild.get_channel_or_thread(cid)) is None
            or not (ch.permissions_for(guild.me).view_channel and ch.permissions_for(guild.me).read_message_history)
        ]
        rows.append(f"ℹ️ Prefix here: `{p}` (mentioning SYKE always works)")
        rows.append(f"{'✅' if tracked and not unreadable else '⚠️'} Watched channels: {len(tracked)}"
                    + (f", {len(unreadable)} unreadable" if unreadable else ""))

        if missing:
            verdict = ("Prefix commands won't work in this channel. In the channel settings → **Permissions**, "
                       "allow the ❌ items for SYKE's role.")
        elif not tracked:
            verdict = f"All good. Add a channel to analyse with `{p}track #channel`."
        else:
            verdict = f"All good here. Try `{p}help`."
        embed = notice("🩺 SYKE diagnostics", "\n".join(rows) + f"\n\n{verdict}", WARN if missing else BRAND)
        embed.set_footer(text=f"SYKE version {VERSION}")
        await ctx.reply(embed=embed, ephemeral=True)

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
                f"`{p}iq [@member]` — SYKE's very unscientific IQ estimate\n"
                f"`{p}top [trait]` — leaderboard: funny, toxic, cringe, freaky, serious, chaotic, active, iq\n"
                f"`{p}mimic [@member]` — SYKE talks like them (Markov chain)\n"
                f"`{p}scanme` — dig up your older messages so they count towards your profile\n"
                f"`{p}optout` / `{p}optin` — control whether you're judged\n"
                "`/diagnose` — check why prefix commands might not work here\n\n"
                "**Admins (Manage Server)**\n"
                f"`{p}track #channel` / `{p}untrack #channel` — choose what SYKE reads\n"
                f"`{p}channels` — show settings and watched channels\n"
                f"`{p}prefix <new|mention|reset>` — custom prefix, @mention only, or back to `{self.bot.settings.default_prefix}`\n"
                f"`{p}timezone <zone>` — for late-night detection, e.g. `{p}timezone Europe/London`\n"
                f"`{p}rescan` — re-read channels now\n"
                f"`{p}collect [#channel] [limit]` — read more history for mimic/yap\n"
                f"`{p}yap on|off` — SYKE chimes in with old messages here; reply to it and it answers back\n"
                f"`{p}demo on|off` — add fake members to test alone\n"
                f"`{p}sample [name]` — report for a fake member\n\n"
                "SYKE keeps text from watched channels for mimic/yap; "
                f"`{p}optout` deletes yours. Reports are for fun only."
            ),
        )
        embed.set_footer(text=f"SYKE version {VERSION}")
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

    @commands.hybrid_command(name="prefix", description="Choose SYKE's prefix: a custom one, mentions only, or reset")
    @app_commands.describe(new="A prefix of up to 5 characters, 'mention' to use @SYKE only, or 'reset'")
    @admin()
    async def prefix(self, ctx: commands.Context, new: str | None = None) -> None:
        default = self.bot.settings.default_prefix
        mention = self.bot.user.mention if self.bot.user else "@SYKE"
        if new is None:
            p = self.p(ctx)
            mode = "mentions only" if self.bot.prefix_for(ctx.guild.id) == MENTION_ONLY else f"`{p}`"
            embed = notice("Prefix settings ⚙️", f"Current prefix: **{mode}**", BRAND)
            embed.add_field(name="Use a custom prefix", value=f"`{p}prefix ?` (1-{MAX_PREFIX_LENGTH} characters)", inline=False)
            embed.add_field(name="Use mentions only", value=f"`{p}prefix mention` → commands become `{mention} profile`", inline=False)
            embed.add_field(name="Back to default", value=f"`{p}prefix reset` → `{default}`", inline=False)
            embed.set_footer(text="Mentioning SYKE always works, whatever the prefix.")
            await ctx.reply(embed=embed, ephemeral=True)
            return

        new = new.strip()
        bot_mentions = {f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"} if self.bot.user else set()
        if new.lower() in MENTION_WORDS or new in bot_mentions:
            self.bot.set_prefix(ctx.guild.id, MENTION_ONLY)
            await ctx.reply(embed=notice(
                "Prefix updated ✅",
                f"SYKE now only answers when mentioned, e.g. {mention} `profile` or {mention} `help`.\n"
                f"Switch back any time with {mention} `prefix {default}`.",
                BRAND,
            ))
            return

        if new.lower() == "reset":
            new = default
        if not new or len(new) > MAX_PREFIX_LENGTH or any(c.isspace() for c in new) or "`" in new or new.startswith("<"):
            await ctx.reply(embed=notice(
                "Invalid prefix",
                f"Use 1-{MAX_PREFIX_LENGTH} characters with no spaces or backticks, or `mention` for mentions only.",
            ), ephemeral=True)
            return
        applied = self.bot.set_prefix(ctx.guild.id, None if new == default else new)
        await ctx.reply(embed=notice(
            "Prefix updated ✅",
            f"Commands now start with `{applied}`, for example `{applied}profile`. "
            f"Mentioning {mention} always works if you forget it.",
            BRAND,
        ))

    @commands.hybrid_command(name="timezone", aliases=["tz"], description="Set the timezone SYKE uses to spot late-night posting")
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
        await ctx.reply(embed=notice(
            "Timezone set 🕐",
            f"Late-night posting is now judged in **{match}**. Peak hours on cards always show in each "
            "reader's own timezone.", BRAND), ephemeral=True)

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

    async def _collect_channel(self, guild_id: int, channel, limit: int, optouts: set[int]) -> list[tuple[int, int, int, str]]:
        rows = []
        async for message in channel.history(limit=limit):
            if not message.author.bot and message.content and message.author.id not in optouts \
                    and not self.bot.is_command_text(guild_id, message.content):
                rows.append(corpus_row(message))
        return rows

    @commands.hybrid_command(name="collect", description="Read channel history so mimic and yap have more to work with")
    @app_commands.describe(channel="Channel to read. Leave empty for all watched channels.",
                           limit=f"Messages to read per channel, up to {COLLECT_MAX}")
    @commands.cooldown(1, 30, commands.BucketType.guild)
    @admin()
    async def collect(self, ctx: commands.Context, channel: discord.TextChannel | None = None,
                      limit: int = COLLECT_DEFAULT) -> None:
        limit = max(1, min(limit, COLLECT_MAX))
        if channel is not None:
            channels = [channel]
        else:
            channels = [ch for cid in self.bot.storage.tracked(ctx.guild.id) if (ch := ctx.guild.get_channel(cid))]
            if not channels:
                await ctx.reply(embed=self.bot.no_channels(ctx.guild.id), ephemeral=True)
                return
        readable = [ch for ch in channels
                    if ch.permissions_for(ctx.guild.me).view_channel
                    and ch.permissions_for(ctx.guild.me).read_message_history]
        unreadable = [ch for ch in channels if ch not in readable]
        if not readable:
            await ctx.reply(embed=notice(
                "I can't see in there 👀",
                f"Give SYKE **View Channel** and **Read Message History** in {', '.join(ch.mention for ch in unreadable)}.",
            ), ephemeral=True)
            return

        optouts = self.bot.storage.optouts(ctx.guild.id)
        started = time.monotonic()
        async with ctx.typing():
            results = await asyncio.gather(
                *(self._collect_channel(ctx.guild.id, ch, limit, optouts) for ch in readable), return_exceptions=True
            )
            rows: list[tuple[int, int, int, str]] = []
            for ch, result in zip(readable, results):
                if isinstance(result, BaseException):
                    log.warning("Collect failed in #%s: %s", ch.name, result)
                    unreadable.append(ch)
                else:
                    rows.extend(result)
            added = await asyncio.to_thread(self.bot.storage.add_corpus, ctx.guild.id, rows)
            self.bot.bump_history(ctx.guild.id)
        total, authors = self.bot.storage.corpus_size(ctx.guild.id)
        log.info("Collected %d messages (%d new) in %s in %.1fs", len(rows), added, ctx.guild.id, time.monotonic() - started)
        body = (f"Read **{len(rows)}** messages from {', '.join(ch.mention for ch in readable if ch not in unreadable)} "
                f"(**{added}** new).\nSYKE now knows **{total}** messages from **{authors}** members. "
                f"Try `{self.p(ctx)}mimic @someone` or `{self.p(ctx)}yap on`.")
        if unreadable:
            body += f"\n⚠️ Couldn't read {', '.join(ch.mention for ch in unreadable)}."
        await ctx.reply(embed=notice("Collection complete 🧫", body, BRAND))

    @commands.hybrid_command(name="yap", description="Let SYKE chime in with random things members have said before")
    @app_commands.describe(enabled="On: SYKE yaps in this channel now and then. Off: it stops.",
                           channel="Channel to change. Leave empty for this one.")
    @admin()
    async def yap(self, ctx: commands.Context, enabled: bool | None = None,
                  channel: discord.TextChannel | None = None) -> None:
        channel = channel or ctx.channel
        p = self.p(ctx)
        if enabled is None:
            active = [ch.mention for cid in self.bot.storage.yap_channels(ctx.guild.id)
                      if (ch := ctx.guild.get_channel(cid))]
            body = (f"SYKE is yapping in {', '.join(active)}." if active else "SYKE isn't yapping anywhere.")
            body += f"\nUse `{p}yap on` or `{p}yap off` in a channel to change it."
            await ctx.reply(embed=notice("Yap settings 🗣️", body, BRAND), ephemeral=True)
            return

        self.bot.storage.set_yap(ctx.guild.id, channel.id, enabled)
        if not enabled:
            await ctx.reply(embed=notice("Yap off 🤐", f"SYKE will keep quiet in {channel.mention}.", BRAND))
            return
        body = (f"Every so often, when people chat in {channel.mention}, SYKE will drop something a member "
                f"said in the past. Reply to one and SYKE answers with another message that fits. "
                f"Turn it off with `{p}yap off`.")
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages:
            body += f"\n⚠️ SYKE can't send messages in {channel.mention} yet."
        if self.bot.pick_yap(ctx.guild.id) is None:
            body += (f"\n⚠️ SYKE hasn't stored any messages yet. Watch a channel with `{p}track #channel` "
                     f"or read history with `{p}collect`.")
        await ctx.reply(embed=notice("Yap on 🗣️", body, BRAND))

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
            await self.bot.send_report(ctx, profile, None, name_suffix=" (fake demo member)", server=server)

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
