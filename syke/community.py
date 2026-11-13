"""Community features: weekly awards, the !guess game, and welcome messages after verification."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import welcome as welcome_mod
from .ai import write_welcome_roast
from .awards import (
    DAYS,
    DEFAULT_DAY,
    DEFAULT_HOUR,
    WEEK,
    awards_embed,
    compute_awards,
    due_at,
    parse_day,
    parse_hour,
    schedule_text,
)
from .models import Msg
from .moderation import moderator
from .verify import unacceptable_role

log = logging.getLogger("syke.community")

BRAND = discord.Colour.from_str("#8B5CF6")
WARN = discord.Colour.from_str("#F59E0B")
OK = discord.Colour.from_str("#22C55E")
AWARDS_KEY = "awards"
AWARDS_CHECK_MINUTES = 5
AWARDS_GRACE = timedelta(hours=12)  # a missed slot (bot offline) still posts if it's this recent
GUESS_SECONDS = 30
GUESS_OPTIONS = 4
GUESS_POOL = 15
GUESS_LEADERS = 10
OFF_WORDS = {"off", "none", "reset", "clear", "disable", "no", "false", "0"}
ON_WORDS = {"on", "yes", "true", "enable", "1"}


def embed(title: str, body: str = "", colour: discord.Colour = BRAND) -> discord.Embed:
    return discord.Embed(title=title, description=body, colour=colour)


def problem(title: str, body: str) -> discord.Embed:
    return embed(title, body, WARN)


def role_problem(guild, role) -> str | None:
    position = getattr(getattr(getattr(guild, "me", None), "top_role", None), "position", None)
    if not isinstance(position, int):
        return "SYKE can't see its own role yet. Try again in a moment."
    return unacceptable_role(role, bot_position=position, other_id=None)


def unescape(text: str) -> str:
    """Slash commands can't hold line breaks, so `\\n` stands in for one."""
    return (text or "").replace("\\n", "\n").strip()


# !guess

class GuessView(discord.ui.View):
    def __init__(self, cog: "Community", guild_id: int, author_id: int, author_name: str,
                 options: list[tuple[int, str]], link: str | None) -> None:
        super().__init__(timeout=GUESS_SECONDS)
        self.cog = cog
        self.guild_id = guild_id
        self.author_id = author_id
        self.author_name = author_name
        self.link = link
        self.guessed: set[int] = set()
        self.winner: int | None = None
        self.message: discord.Message | None = None
        self.lock = asyncio.Lock()
        for index, (uid, name) in enumerate(options):
            button = discord.ui.Button(label=name[:80] or "someone", style=discord.ButtonStyle.secondary,
                                       custom_id=f"syke:guess:{index}:{uid}:{random.random()}")
            button.callback = self._make_callback(uid)
            self.add_item(button)

    def _make_callback(self, picked: int):
        async def callback(interaction: discord.Interaction) -> None:
            await self.pick(interaction, picked)
        return callback

    def reveal(self, base: discord.Embed, text: str, colour: discord.Colour) -> discord.Embed:
        result = base.copy()
        result.colour = colour
        answer = f"It was **{self.author_name}** (<@{self.author_id}>)."
        if self.link:
            answer += f" [Original message]({self.link})"
        result.add_field(name=text, value=answer, inline=False)
        result.set_footer(text=f"{len(self.guessed)} guess(es) · !guess to play again")
        return result

    def finish_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
                uid = int(item.custom_id.split(":")[3]) if item.custom_id else 0
                if uid == self.author_id:
                    item.style = discord.ButtonStyle.success

    async def pick(self, interaction: discord.Interaction, picked: int) -> None:
        user = interaction.user
        async with self.lock:
            if self.winner is not None:
                await interaction.response.send_message("Too slow, it's already been guessed.", ephemeral=True)
                return
            if user.id == self.author_id:
                await interaction.response.send_message("You wrote it. No fair. 🫵", ephemeral=True)
                return
            if user.id in self.guessed:
                await interaction.response.send_message("One guess per round.", ephemeral=True)
                return
            self.guessed.add(user.id)
            if picked != self.author_id:
                self.cog.bot.storage.add_guess_score(self.guild_id, user.id, 0)
                await interaction.response.send_message("Nope. ❌ You're out for this round.", ephemeral=True)
                return
            self.winner = user.id
            total = self.cog.bot.storage.add_guess_score(self.guild_id, user.id, 1)
            self.finish_buttons()
            self.stop()
            base = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else embed("")
            await interaction.response.edit_message(
                embed=self.reveal(base, f"✅ {user.display_name} got it! ({total} point{'s' if total != 1 else ''})",
                                  OK),
                view=self,
            )
        self.cog.release_channel(interaction.channel_id)

    async def on_timeout(self) -> None:
        if self.winner is not None or self.message is None:
            return
        self.finish_buttons()
        base = self.message.embeds[0] if self.message.embeds else embed("")
        with contextlib.suppress(discord.HTTPException):
            await self.message.edit(embed=self.reveal(base, "⏰ Time's up, nobody got it", WARN), view=self)
        self.cog.release_channel(self.message.channel.id)


def guess_round(candidates: list[tuple], authors: list[tuple[int, str, int]], optouts: set[int],
                is_command, rng: random.Random | None = None):
    """Pick a message and answer options: `(row, [(user_id, name), ...])`, or None if there aren't enough people."""
    rng = rng or random.Random()
    pool = [(uid, name) for uid, name, _ in authors if uid not in optouts and name][:GUESS_POOL]
    if len(pool) < 2:
        return None
    for row in candidates:
        _, _, author_id, author_name, content = row
        if author_id in optouts or is_command(content) or len(content.split()) < 3:
            continue
        others = [p for p in pool if p[0] != author_id]
        if not others:
            continue
        decoys = rng.sample(others, min(GUESS_OPTIONS - 1, len(others)))
        options = decoys + [(author_id, author_name)]
        rng.shuffle(options)
        return row, options
    return None


class Community(commands.Cog, name="Community"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._guessing: set[int] = set()

    async def cog_load(self) -> None:
        if not self.awards_loop.is_running():
            self.awards_loop.start()

    async def cog_unload(self) -> None:
        self.awards_loop.cancel()

    def p(self, ctx: commands.Context) -> str:
        return self.bot.display_prefix(ctx.guild.id)

    def release_channel(self, channel_id: int | None) -> None:
        self._guessing.discard(channel_id or 0)

    # Weekly awards

    def week_messages(self, guild_id: int, now: float | None = None) -> list[Msg]:
        now = now or time.time()
        tracked = self.bot.storage.tracked(guild_id)
        optouts = self.bot.storage.optouts(guild_id)
        since = now - WEEK.total_seconds()
        found = []
        for mid, cid, author, name, content, created, laughs, total in self.bot.storage.history(guild_id, tracked):
            if created is None or created < since or author in optouts:
                continue
            if self.bot.is_command_text(guild_id, content):
                continue
            found.append(Msg(author, name, content, datetime.fromtimestamp(created, timezone.utc),
                             cid, laughs, total, mid))
        return found

    async def build_awards(self, guild: discord.Guild):
        with contextlib.suppress(Exception):
            await self.bot.analyze(guild)  # Refreshes reaction counts on recent messages.
        messages = await asyncio.to_thread(self.week_messages, guild.id)
        labels = await asyncio.to_thread(self.bot.storage.labels, guild.id)
        return await asyncio.to_thread(compute_awards, messages, self.bot.tz_for(guild.id), labels)

    async def post_awards(self, guild: discord.Guild, config: dict) -> tuple[bool, str]:
        """Post this week's awards in the configured channel and move the winner role. (posted, note)."""
        channel = guild.get_channel(config.get("channel") or 0)
        if channel is None:
            return False, "The awards channel is gone. Set one with `awards channel #channel`."
        result = await self.build_awards(guild)
        role = guild.get_role(config.get("role") or 0) if config.get("role") else None
        note = ""
        if role is not None and role_problem(guild, role):
            note = f"Couldn't hand out {role.mention}: {role_problem(guild, role)}"
            role = None
        try:
            await channel.send(embed=awards_embed(result, guild.name, role_mention=role.mention if role else None),
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return False, f"SYKE couldn't post in {channel.mention}. Check Send Messages and Embed Links."
        if role is not None:
            winners = set(result.winners)
            for uid in config.get("holders", []):
                if uid in winners:
                    continue
                member = guild.get_member(uid)
                if member is None:
                    with contextlib.suppress(discord.HTTPException):
                        member = await guild.fetch_member(uid)
                if member is not None and role in member.roles:
                    with contextlib.suppress(discord.HTTPException):
                        await member.remove_roles(role, reason="SYKE weekly awards: new week")
            given = []
            for uid in winners:
                member = guild.get_member(uid)
                if member is None:
                    with contextlib.suppress(discord.HTTPException):
                        member = await guild.fetch_member(uid)
                if member is None:
                    continue
                try:
                    if role not in member.roles:
                        await member.add_roles(role, reason="SYKE weekly awards winner")
                    given.append(uid)
                except discord.HTTPException:
                    note = f"SYKE couldn't give {role.mention}. Give it **Manage Roles** and drag its role above."
            self.bot.storage.update_config(guild.id, AWARDS_KEY, holders=given)
        return True, note

    @tasks.loop(minutes=AWARDS_CHECK_MINUTES)
    async def awards_loop(self) -> None:
        for guild_id, config in self.bot.storage.configs(AWARDS_KEY).items():
            if not config.get("channel"):
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            now = datetime.now(self.bot.tz_for(guild_id))
            due = due_at(now, config.get("day", DEFAULT_DAY), config.get("hour", DEFAULT_HOUR))
            if config.get("last", 0) >= due.timestamp() or now - due > AWARDS_GRACE:
                continue
            self.bot.storage.update_config(guild_id, AWARDS_KEY, last=due.timestamp())
            try:
                posted, note = await self.post_awards(guild, config)
                if not posted or note:
                    log.warning("Weekly awards in %s: %s", guild_id, note)
            except Exception:
                log.exception("Weekly awards failed in %s", guild_id)

    @awards_loop.before_loop
    async def _before_awards(self) -> None:
        await self.bot.wait_until_ready()

    def awards_settings(self, guild: discord.Guild, prefix: str) -> discord.Embed:
        config = self.bot.storage.get_config(guild.id, AWARDS_KEY)
        channel = guild.get_channel(config.get("channel") or 0) if config.get("channel") else None
        role = guild.get_role(config.get("role") or 0) if config.get("role") else None
        status = channel.mention if channel else ("missing channel" if config.get("channel") else "off")
        body = (
            "Every week SYKE hands out awards from the last 7 days of chat in watched channels: "
            "Biggest Yapper, Funniest, Most Laughs, Most Unhinged, Most Toxic, Freakiest, Cringe Lord, "
            "Most Serious, Night Owl and the Message of the Week.\n\n"
            f"**Channel:** {status}\n"
            f"**When:** {schedule_text(config)} ({self.bot.tz_for(guild.id)})\n"
            f"**Winner role:** {role.mention if role else 'none'} (winners keep it for a week)\n\n"
            f"`{prefix}awards channel #channel` · `{prefix}awards when sunday 18:00` · "
            f"`{prefix}awards role @Role` · `{prefix}awards preview` · `{prefix}awards now` · `{prefix}awards off`"
        )
        return embed("🏆 Weekly awards", body)

    @commands.hybrid_group(name="awards", fallback="settings", case_insensitive=True,
                           description="Weekly awards: who carried, ruined and haunted the chat")
    @moderator("manage_guild")
    async def awards(self, ctx: commands.Context) -> None:
        await ctx.reply(embed=self.awards_settings(ctx.guild, self.p(ctx)), ephemeral=True)

    @awards.command(name="channel", description="Where the weekly awards get posted (turns them on)")
    @app_commands.describe(channel="The channel for the awards")
    @moderator("manage_guild")
    async def awards_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await ctx.reply(embed=problem("I can't post there", f"Give SYKE **Send Messages** and **Embed Links** "
                                                                f"in {channel.mention}."), ephemeral=True)
            return
        config = self.bot.storage.update_config(ctx.guild.id, AWARDS_KEY, channel=channel.id, last=time.time())
        await ctx.reply(embed=embed("Weekly awards on 🏆", f"They'll be posted in {channel.mention}, "
                                                          f"{schedule_text(config)}. "
                                                          f"Try `{self.p(ctx)}awards preview` to see this week's."),
                        ephemeral=True)

    @awards.command(name="when", aliases=["schedule", "time"], description="Which day and hour the awards go out")
    @app_commands.describe(day="Day of the week, e.g. sunday", hour="Hour in the server's timezone, e.g. 18 or 6pm")
    @moderator("manage_guild")
    async def awards_when(self, ctx: commands.Context, day: str, hour: str = str(DEFAULT_HOUR)) -> None:
        day_index, hour_value = parse_day(day), parse_hour(hour)
        if day_index is None or hour_value is None:
            await ctx.reply(embed=problem("When?", f"Try `{self.p(ctx)}awards when sunday 18:00` or "
                                                   f"`{self.p(ctx)}awards when fri 9pm`."), ephemeral=True)
            return
        config = self.bot.storage.update_config(ctx.guild.id, AWARDS_KEY, day=day_index, hour=hour_value,
                                                last=time.time())
        await ctx.reply(embed=embed("Schedule set 🗓️", f"Awards go out {schedule_text(config)} "
                                                       f"({self.bot.tz_for(ctx.guild.id)}). "
                                                       f"Change the timezone with `{self.p(ctx)}timezone`."),
                        ephemeral=True)

    @awards.command(name="role", description="A role every winner gets until next week's awards")
    @app_commands.describe(role="The winner role. Leave empty to stop giving one.")
    @moderator("manage_guild")
    async def awards_role(self, ctx: commands.Context, role: Optional[discord.Role] = None) -> None:
        if role is None:
            self.bot.storage.update_config(ctx.guild.id, AWARDS_KEY, role=None)
            await ctx.reply(embed=embed("No winner role", "Winners just get the glory."), ephemeral=True)
            return
        issue = role_problem(ctx.guild, role)
        if issue:
            await ctx.reply(embed=problem("That role won't work", issue), ephemeral=True)
            return
        self.bot.storage.update_config(ctx.guild.id, AWARDS_KEY, role=role.id, holders=[])
        await ctx.reply(embed=embed("Winner role set ✅", f"Every award winner gets {role.mention} for a week."),
                        ephemeral=True)

    @awards.command(name="preview", description="See this week's awards so far, just for you")
    @moderator("manage_guild")
    async def awards_preview(self, ctx: commands.Context) -> None:
        async with ctx.typing(ephemeral=True):
            result = await self.build_awards(ctx.guild)
        await ctx.reply(embed=awards_embed(result, ctx.guild.name, preview=True), ephemeral=True)

    @awards.command(name="now", aliases=["post"], description="Post the awards right now (and move the winner role)")
    @commands.cooldown(1, 60, commands.BucketType.guild)
    @moderator("manage_guild")
    async def awards_now(self, ctx: commands.Context) -> None:
        config = self.bot.storage.get_config(ctx.guild.id, AWARDS_KEY)
        if not config.get("channel"):
            await ctx.reply(embed=problem("Where to?", f"Set a channel first: `{self.p(ctx)}awards channel #channel`."),
                            ephemeral=True)
            return
        async with ctx.typing(ephemeral=True):
            posted, note = await self.post_awards(ctx.guild, config)
        if not posted:
            await ctx.reply(embed=problem("Didn't post", note), ephemeral=True)
            return
        await ctx.reply(embed=embed("Awards posted 🏆", note or f"Check <#{config['channel']}>."), ephemeral=True)

    @awards.command(name="off", description="Stop posting weekly awards")
    @moderator("manage_guild")
    async def awards_off(self, ctx: commands.Context) -> None:
        self.bot.storage.update_config(ctx.guild.id, AWARDS_KEY, channel=None)
        await ctx.reply(embed=embed("Weekly awards off", "No more trophies. The chat will have to validate itself."),
                        ephemeral=True)

    # !guess

    @commands.hybrid_group(name="guess", fallback="play", aliases=["whosaid", "wsi"], case_insensitive=True,
                           description="Who said it? Guess which member wrote a real message")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    @commands.guild_only()
    @app_commands.guild_only()
    async def guess(self, ctx: commands.Context) -> None:
        channel_id = getattr(ctx.channel, "id", 0)
        if channel_id in self._guessing:
            await ctx.reply(embed=problem("One at a time", "There's already a round going in this channel."),
                            ephemeral=True)
            return
        storage = self.bot.storage
        candidates, authors = await asyncio.gather(
            asyncio.to_thread(storage.guess_candidates, ctx.guild.id),
            asyncio.to_thread(storage.top_authors, ctx.guild.id),
        )
        picked = guess_round(candidates, authors, storage.optouts(ctx.guild.id),
                             lambda text: self.bot.is_command_text(ctx.guild.id, text))
        if picked is None:
            await ctx.reply(embed=problem(
                "Not enough to go on",
                f"SYKE needs stored messages from at least 2 people. Watch a channel with `{self.p(ctx)}track`, "
                f"or read history with `{self.p(ctx)}collect`.",
            ), ephemeral=True)
            return
        (message_id, msg_channel, author_id, author_name, content), options = picked
        link = f"https://discord.com/channels/{ctx.guild.id}/{msg_channel}/{message_id}" if message_id else None
        quote = " ".join(content.split()).replace("`", "'")
        question = embed("🕵️ Who said it?", f"> {quote}\n\nFirst correct guess wins. One guess each. "
                                           f"{GUESS_SECONDS} seconds.")
        view = GuessView(self, ctx.guild.id, author_id, author_name, options, link)
        self._guessing.add(channel_id)
        try:
            view.message = await ctx.reply(embed=question, view=view)
        except discord.HTTPException:
            self.release_channel(channel_id)
            raise

    @guess.command(name="top", aliases=["leaderboard", "lb", "scores"], description="Best guessers in this server")
    @commands.guild_only()
    async def guess_top(self, ctx: commands.Context) -> None:
        leaders = self.bot.storage.guess_leaders(ctx.guild.id, GUESS_LEADERS)
        if not leaders:
            await ctx.reply(embed=embed("🕵️ No detectives yet", f"Play a round with `{self.p(ctx)}guess`."))
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> — **{points}** pt{'s' if points != 1 else ''} "
                 f"({played} played)" for i, (uid, points, played) in enumerate(leaders)]
        await ctx.reply(embed=embed("🕵️ Best guessers", "\n".join(lines)))

    # Welcome messages

    def welcome_config(self, guild_id: int) -> dict:
        return self.bot.storage.get_config(guild_id, welcome_mod.CONFIG_KEY)

    async def send_welcome(self, member, config: dict, channel=None) -> bool:
        guild = member.guild
        channel = channel or guild.get_channel(config.get("channel") or 0)
        if channel is None:
            return False
        roast = ""
        templates = (config.get("title") or welcome_mod.DEFAULT_TITLE) + (config.get("text") or welcome_mod.DEFAULT_TEXT) \
            + (config.get("footer") or "")
        if "{roast}" in templates:
            name = getattr(member, "display_name", "") or getattr(member, "name", "")
            username = getattr(member, "name", "") or name
            roast = await write_welcome_roast(self.bot.settings, name, username, guild.name,
                                              welcome_mod.avatar_for_ai(member))
            roast = roast or welcome_mod.offline_roast(name, username, welcome_mod.has_custom_avatar(member))
        content, card = welcome_mod.render(config, member, guild, roast)
        try:
            await channel.send(content=content, embed=card,
                               allowed_mentions=discord.AllowedMentions(users=[member], everyone=False, roles=False))
        except discord.HTTPException:
            log.warning("Couldn't send a welcome in %s", guild.id, exc_info=True)
            return False
        return True

    @commands.Cog.listener()
    async def on_syke_verified(self, member) -> None:
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        config = self.welcome_config(guild.id)
        if config.get("channel"):
            await self.send_welcome(member, config)

    @commands.hybrid_group(name="welcome", fallback="settings", case_insensitive=True,
                           description="The welcome message SYKE sends when someone gets verified")
    @moderator("manage_guild")
    async def welcome(self, ctx: commands.Context) -> None:
        await ctx.reply(embed=welcome_mod.describe(self.welcome_config(ctx.guild.id), ctx.guild, self.p(ctx)),
                        ephemeral=True)

    @welcome.command(name="channel", description="Where welcome messages go (turns them on)")
    @app_commands.describe(channel="The welcome channel")
    @moderator("manage_guild")
    async def welcome_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await ctx.reply(embed=problem("I can't post there", f"Give SYKE **Send Messages** and **Embed Links** "
                                                                f"in {channel.mention}."), ephemeral=True)
            return
        self.bot.storage.update_config(ctx.guild.id, welcome_mod.CONFIG_KEY, channel=channel.id)
        body = f"New members get welcomed in {channel.mention} once they're verified."
        if not self.bot.storage.verification(ctx.guild.id)[0]:
            body += f"\n⚠️ Verification isn't set up yet. Start with `{self.p(ctx)}verify setup`."
        await ctx.reply(embed=embed("Welcome messages on 👋", body + f"\nPreview it with `{self.p(ctx)}welcome test`."),
                        ephemeral=True)

    async def _welcome_text(self, ctx: commands.Context, field: str, text: str, limit: int, label: str) -> None:
        text = unescape(text)
        if text.lower() in OFF_WORDS or not text:
            self.bot.storage.update_config(ctx.guild.id, welcome_mod.CONFIG_KEY, **{field: None})
            await ctx.reply(embed=embed(f"{label} reset", "Back to SYKE's default."), ephemeral=True)
            return
        if len(text) > limit:
            await ctx.reply(embed=problem("Too long", f"Keep it under {limit} characters."), ephemeral=True)
            return
        self.bot.storage.update_config(ctx.guild.id, welcome_mod.CONFIG_KEY, **{field: text})
        await ctx.reply(embed=embed(f"{label} saved ✅", f"See it with `{self.p(ctx)}welcome test`."), ephemeral=True)

    @welcome.command(name="title", description="The welcome embed's title. {name}, {server} and friends work")
    @app_commands.describe(text="The title, or 'reset'")
    @moderator("manage_guild")
    async def welcome_title(self, ctx: commands.Context, *, text: str) -> None:
        await self._welcome_text(ctx, "title", text, welcome_mod.TITLE_LIMIT, "Title")

    @welcome.command(name="message", aliases=["text", "body"],
                     description="The welcome text. Use {roast}, {mention}, {name}, {server}, {count}; \\n for a new line")
    @app_commands.describe(text="The message, or 'reset'")
    @moderator("manage_guild")
    async def welcome_message(self, ctx: commands.Context, *, text: str) -> None:
        await self._welcome_text(ctx, "text", text, welcome_mod.TEXT_LIMIT, "Message")

    @welcome.command(name="footer", description="Small text under the welcome embed")
    @app_commands.describe(text="The footer, or 'off'")
    @moderator("manage_guild")
    async def welcome_footer(self, ctx: commands.Context, *, text: str) -> None:
        await self._welcome_text(ctx, "footer", text, 300, "Footer")

    @welcome.command(name="colour", aliases=["color"], description="The welcome embed's colour, e.g. #ff8800")
    @app_commands.describe(colour="A hex colour like #ff8800, or 'reset'")
    @moderator("manage_guild")
    async def welcome_colour(self, ctx: commands.Context, colour: str) -> None:
        if colour.strip().lower() in OFF_WORDS:
            self.bot.storage.update_config(ctx.guild.id, welcome_mod.CONFIG_KEY, colour=None)
            await ctx.reply(embed=embed("Colour reset", "Back to SYKE purple."), ephemeral=True)
            return
        value = welcome_mod.parse_colour(colour)
        if value is None:
            await ctx.reply(embed=problem("Which colour?", "Use a hex code like `#ff8800`."), ephemeral=True)
            return
        self.bot.storage.update_config(ctx.guild.id, welcome_mod.CONFIG_KEY, colour=value)
        await ctx.reply(embed=embed("Colour saved ✅", f"`#{value:06X}`", discord.Colour(value)), ephemeral=True)

    @welcome.command(name="image", aliases=["banner"], description="A big image or GIF under the welcome text")
    @app_commands.describe(url="An image link (https://...), or 'off'")
    @moderator("manage_guild")
    async def welcome_image(self, ctx: commands.Context, url: str) -> None:
        url = url.strip().strip("<>")
        if url.lower() in OFF_WORDS:
            self.bot.storage.update_config(ctx.guild.id, welcome_mod.CONFIG_KEY, image=None)
            await ctx.reply(embed=embed("Image removed", "Welcomes are text only now."), ephemeral=True)
            return
        if not welcome_mod.URL.fullmatch(url):
            await ctx.reply(embed=problem("That's not a link", "Paste an image link starting with https://."),
                            ephemeral=True)
            return
        self.bot.storage.update_config(ctx.guild.id, welcome_mod.CONFIG_KEY, image=url)
        await ctx.reply(embed=embed("Image saved ✅", f"See it with `{self.p(ctx)}welcome test`."), ephemeral=True)

    @welcome.command(name="ping", description="Whether the welcome pings the new member")
    @app_commands.describe(enabled="On: the message @mentions them. Off: no ping.")
    @moderator("manage_guild")
    async def welcome_ping(self, ctx: commands.Context, enabled: bool) -> None:
        self.bot.storage.update_config(ctx.guild.id, welcome_mod.CONFIG_KEY, ping=enabled)
        await ctx.reply(embed=embed("Saved ✅", "New members get pinged." if enabled else "No pings."), ephemeral=True)

    @welcome.command(name="thumbnail", aliases=["avatar"], description="Whether the welcome shows their avatar")
    @app_commands.describe(enabled="On: their avatar sits in the corner.")
    @moderator("manage_guild")
    async def welcome_thumbnail(self, ctx: commands.Context, enabled: bool) -> None:
        self.bot.storage.update_config(ctx.guild.id, welcome_mod.CONFIG_KEY, thumbnail=enabled)
        await ctx.reply(embed=embed("Saved ✅", "Avatar on." if enabled else "Avatar off."), ephemeral=True)

    @welcome.command(name="test", aliases=["preview"], description="Send a test welcome for yourself, here")
    @commands.cooldown(1, 10, commands.BucketType.member)
    @moderator("manage_guild")
    async def welcome_test(self, ctx: commands.Context) -> None:
        config = self.welcome_config(ctx.guild.id)
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        sent = await self.send_welcome(ctx.author, {**config, "ping": False}, channel=ctx.channel)
        if not sent:
            await ctx.reply(embed=problem("Couldn't send it", "Check SYKE can post embeds here."), ephemeral=True)
        elif ctx.interaction is not None:
            await ctx.reply(embed=embed("Test sent ✅", "That's what new members will see."), ephemeral=True)

    @welcome.command(name="off", description="Stop sending welcome messages")
    @moderator("manage_guild")
    async def welcome_off(self, ctx: commands.Context) -> None:
        self.bot.storage.update_config(ctx.guild.id, welcome_mod.CONFIG_KEY, channel=None)
        await ctx.reply(embed=embed("Welcome messages off", "Your design is saved for when you turn it back on."),
                        ephemeral=True)


def help_lines(p: str) -> str:
    return (
        f"`{p}guess` — who said it? Race to name who wrote a real message · `{p}guess top`\n"
        f"`{p}awards` — weekly awards: channel, schedule, winner role, preview (admins)\n"
        f"`{p}welcome` — design the welcome sent after someone verifies (admins)\n"
        f"`{p}dashboard` — every setting in one place, with buttons (admins)"
    )
