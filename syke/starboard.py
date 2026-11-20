"""Starboard: messages that get enough ⭐ reactions are reposted to a hall-of-fame channel.

The post shows the message, its first image, the author and a jump link, and its count updates
live as stars come and go. Members can't star their own messages, bots never count, and stars
in the starboard channel itself are ignored. If the count drops below the threshold the post is
removed again. Posts are remembered per server (the newest few thousand), so edits survive restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .moderation import OK, WARN, embed, moderator, problem

log = logging.getLogger("syke.starboard")

CONFIG = "starboard"
POSTS = "starboard_posts"
DEFAULT_EMOJI = "⭐"
DEFAULT_THRESHOLD = 3
KEEP_POSTS = 3000
GOLD = discord.Colour.from_str("#F5B83D")
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def star_tier(count: int) -> str:
    if count >= 25:
        return "💫"
    if count >= 10:
        return "🌟"
    return "⭐"


def emoji_key(emoji) -> str:
    """A reaction's emoji as stored in config: the character, or the custom emoji's id."""
    if isinstance(emoji, str):
        return emoji
    if getattr(emoji, "id", None):
        return str(emoji.id)
    return str(getattr(emoji, "name", emoji))


def emoji_label(key: str, guild) -> str:
    if key.isdigit():
        found = guild.get_emoji(int(key)) if guild else None
        return str(found) if found else "a custom emoji"
    return key


def first_image(message) -> str | None:
    for a in getattr(message, "attachments", []) or []:
        if (a.content_type or "").startswith("image/") or a.filename.lower().endswith(IMAGE_TYPES):
            return a.url
    for e in getattr(message, "embeds", []) or []:
        if e.image and e.image.url:
            return e.image.url
        if e.type in ("image", "gifv") and e.thumbnail and e.thumbnail.url:
            return e.thumbnail.url
    return None


def star_card(message, count: int) -> discord.Embed:
    card = discord.Embed(description=(message.content or "")[:4000] or None, colour=GOLD,
                         timestamp=message.created_at)
    author = message.author
    card.set_author(name=getattr(author, "display_name", None) or author.name,
                    icon_url=getattr(getattr(author, "display_avatar", None), "url", None))
    image = first_image(message)
    if image:
        card.set_image(url=image)
    elif message.attachments:
        card.add_field(name="Attachment", value=f"[{message.attachments[0].filename}]({message.attachments[0].url})")
    ref = getattr(message, "reference", None)
    if ref is not None and isinstance(ref.resolved, discord.Message) and ref.resolved.content:
        replied = ref.resolved
        card.add_field(name=f"Replying to {replied.author.display_name}", value=replied.content[:300], inline=False)
    card.set_footer(text=f"#{getattr(message.channel, 'name', 'channel')}")
    return card


def header(count: int, emoji_text: str, message) -> str:
    shown = star_tier(count) if emoji_text == DEFAULT_EMOJI else emoji_text
    return f"{shown} **{count}** · {message.channel.mention}"


def jump_view(message) -> discord.ui.View:
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Jump to message", url=message.jump_url))
    return view


class Starboard(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}

    def config(self, guild_id: int) -> dict:
        return self.bot.storage.get_config(guild_id, CONFIG)

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    def posts(self, guild_id: int) -> dict:
        return self.bot.storage.get_config(guild_id, POSTS)

    def remember(self, guild_id: int, message_id: int, post_id: int | None) -> None:
        posts = self.posts(guild_id)
        if post_id is None:
            posts.pop(str(message_id), None)
        else:
            posts[str(message_id)] = post_id
        if len(posts) > KEEP_POSTS:
            for old in sorted(posts, key=int)[: len(posts) - KEEP_POSTS]:
                posts.pop(old, None)
        self.bot.storage.set_config(guild_id, POSTS, posts)

    async def count_stars(self, message, key: str, config: dict) -> int:
        for reaction in message.reactions:
            if emoji_key(reaction.emoji) != key:
                continue
            count = 0
            async for user in reaction.users(limit=None):
                if user.bot or (user.id == message.author.id and not config.get("self")):
                    continue
                count += 1
            return count
        return 0

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload) -> None:
        await self.check(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload) -> None:
        await self.check(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload) -> None:
        await self.check(payload, cleared=True)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload) -> None:
        if payload.guild_id is None:
            return
        post_id = self.posts(payload.guild_id).get(str(payload.message_id))
        if post_id is None:
            return
        config = self.config(payload.guild_id)
        guild = self.bot.get_guild(payload.guild_id)
        board = guild.get_channel(config.get("channel") or 0) if guild else None
        if board is not None:
            with contextlib.suppress(discord.HTTPException):
                await board.get_partial_message(post_id).delete()
        self.remember(payload.guild_id, payload.message_id, None)

    async def check(self, payload, cleared: bool = False) -> None:
        guild_id = getattr(payload, "guild_id", None)
        if guild_id is None:
            return
        config = self.config(guild_id)
        board_id = config.get("channel")
        if not board_id or payload.channel_id == board_id:
            return
        key = config.get("emoji") or DEFAULT_EMOJI
        if not cleared and emoji_key(getattr(payload, "emoji", None)) != key:
            return
        if payload.channel_id in (config.get("ignore") or []):
            return
        guild = self.bot.get_guild(guild_id)
        board = guild.get_channel(board_id) if guild else None
        channel = guild.get_channel_or_thread(payload.channel_id) if guild else None
        if board is None or channel is None:
            return
        if getattr(channel, "is_nsfw", lambda: False)() and not board.is_nsfw():
            return
        lock = self._locks.setdefault(payload.message_id, asyncio.Lock())
        async with lock:
            try:
                await self._update(guild, board, channel, payload.message_id, key, config)
            finally:
                if not lock._waiters:  # type: ignore[attr-defined]
                    self._locks.pop(payload.message_id, None)

    async def _update(self, guild, board, channel, message_id: int, key: str, config: dict) -> None:
        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException:
            return
        if message.author.bot and not config.get("bots"):
            return
        count = await self.count_stars(message, key, config)
        threshold = int(config.get("threshold") or DEFAULT_THRESHOLD)
        post_id = self.posts(guild.id).get(str(message_id))
        emoji_text = emoji_label(key, guild)
        if count >= threshold:
            text = header(count, emoji_text, message)
            if post_id:
                try:
                    await board.get_partial_message(post_id).edit(content=text)
                    return
                except discord.NotFound:
                    post_id = None
                except discord.HTTPException:
                    return
            try:
                post = await board.send(text, embed=star_card(message, count), view=jump_view(message),
                                        allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.warning("Starboard post failed in %s", guild.id, exc_info=True)
                return
            self.remember(guild.id, message_id, post.id)
        elif post_id:
            with contextlib.suppress(discord.HTTPException):
                await board.get_partial_message(post_id).delete()
            self.remember(guild.id, message_id, None)

    # Commands

    @commands.hybrid_group(name="starboard", aliases=["sb"], fallback="settings", case_insensitive=True,
                           description="Pin the best messages to a hall-of-fame channel by reactions")
    @moderator("manage_guild")
    async def starboard(self, ctx: commands.Context) -> None:
        config = self.config(ctx.guild.id)
        p = self.p(ctx)
        if not config.get("channel"):
            await ctx.reply(embed=embed("⭐ Starboard is off", f"Turn it on with `{p}starboard channel #hall-of-fame`."),
                            ephemeral=True)
            return
        key = config.get("emoji") or DEFAULT_EMOJI
        ignored = ", ".join(f"<#{c}>" for c in config.get("ignore") or []) or "none"
        lines = [f"**Channel:** <#{config['channel']}>",
                 f"**Needs:** {config.get('threshold') or DEFAULT_THRESHOLD} × {emoji_label(key, ctx.guild)}",
                 f"**Self-stars count:** {'yes' if config.get('self') else 'no'}",
                 f"**Ignored channels:** {ignored}",
                 "",
                 f"`{p}starboard threshold 5` · `{p}starboard emoji 🔥` · `{p}starboard selfstar on` · "
                 f"`{p}starboard ignore #channel` · `{p}starboard off`"]
        await ctx.reply(embed=embed("⭐ Starboard", "\n".join(lines)), ephemeral=True)

    @starboard.command(name="channel", description="Where starred messages go (turns the starboard on)")
    @app_commands.describe(channel="The hall-of-fame channel")
    @moderator("manage_guild")
    async def starboard_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links and perms.read_message_history):
            await ctx.reply(embed=problem("I can't post there", f"Give SYKE **Send Messages**, **Embed Links** and "
                                                                f"**Read Message History** in {channel.mention}."),
                            ephemeral=True)
            return
        config = self.bot.storage.update_config(ctx.guild.id, CONFIG, channel=channel.id)
        key = config.get("emoji") or DEFAULT_EMOJI
        await ctx.reply(embed=embed("Starboard on ⭐", f"Messages with **{config.get('threshold') or DEFAULT_THRESHOLD}** "
                                                       f"{emoji_label(key, ctx.guild)} reactions get posted in "
                                                       f"{channel.mention}.", OK), ephemeral=True)

    @starboard.command(name="threshold", aliases=["min", "stars"], description="How many reactions a message needs")
    @app_commands.describe(count="Reactions needed (1 to 100)")
    @moderator("manage_guild")
    async def starboard_threshold(self, ctx: commands.Context, count: app_commands.Range[int, 1, 100]) -> None:
        self.bot.storage.update_config(ctx.guild.id, CONFIG, threshold=int(count))
        await ctx.reply(embed=embed("Threshold set", f"Messages now need **{count}** reactions.", OK), ephemeral=True)

    @starboard.command(name="emoji", description="Which reaction counts (default ⭐)")
    @app_commands.describe(emoji="A Unicode emoji or one of this server's emojis")
    @moderator("manage_guild")
    async def starboard_emoji(self, ctx: commands.Context, emoji: str) -> None:
        emoji = emoji.strip()
        try:
            partial = discord.PartialEmoji.from_str(emoji)
        except Exception:
            partial = None
        if partial is not None and partial.id:
            if ctx.guild.get_emoji(partial.id) is None:
                await ctx.reply(embed=problem("Not one of ours", "Use a Unicode emoji or one from this server."),
                                ephemeral=True)
                return
            key = str(partial.id)
        elif emoji and len(emoji) <= 16 and not emoji.isalnum():
            key = emoji
        else:
            await ctx.reply(embed=problem("That's not an emoji", "Try `⭐`, `🔥` or one of this server's emojis."),
                            ephemeral=True)
            return
        self.bot.storage.update_config(ctx.guild.id, CONFIG, emoji=key)
        await ctx.reply(embed=embed("Emoji set", f"{emoji_label(key, ctx.guild)} reactions now count.", OK), ephemeral=True)

    @starboard.command(name="selfstar", description="Whether people can star their own messages")
    @app_commands.describe(enabled="On: your own reaction counts")
    @moderator("manage_guild")
    async def starboard_self(self, ctx: commands.Context, enabled: bool) -> None:
        self.bot.storage.update_config(ctx.guild.id, CONFIG, self=enabled or None)
        await ctx.reply(embed=embed("Saved", "Self-stars count now." if enabled else "Self-stars don't count.", OK),
                        ephemeral=True)

    @starboard.command(name="ignore", description="Ignore (or stop ignoring) a channel")
    @app_commands.describe(channel="The channel to toggle")
    @moderator("manage_guild")
    async def starboard_ignore(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        ignored = list(self.config(ctx.guild.id).get("ignore") or [])
        if channel.id in ignored:
            ignored.remove(channel.id)
            text = f"Stars in {channel.mention} count again."
        else:
            ignored.append(channel.id)
            text = f"Stars in {channel.mention} are ignored."
        self.bot.storage.update_config(ctx.guild.id, CONFIG, ignore=ignored or None)
        await ctx.reply(embed=embed("Saved", text, OK), ephemeral=True)

    @starboard.command(name="off", description="Turn the starboard off")
    @moderator("manage_guild")
    async def starboard_off(self, ctx: commands.Context) -> None:
        self.bot.storage.update_config(ctx.guild.id, CONFIG, channel=None)
        await ctx.reply(embed=embed("Starboard off", "Existing posts stay where they are.", WARN), ephemeral=True)
