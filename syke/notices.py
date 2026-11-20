"""Sticky messages and scheduled announcements.

Sticky: `sticky set [#channel] <text>` keeps a message pinned to the bottom of a channel. A few
seconds after people stop talking, SYKE deletes its old copy and posts it again at the bottom.

Announcements: `announce add #channel <when> | <message>` posts a message later, once or on
repeat (hourly, daily, weekly, monthly). Times are in the server's timezone (`timezone`).
With the slash command a form pops up for the message, so it can span several lines.
A background loop checks every 20 seconds, so they survive restarts.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .moderation import BRAND, OK, WARN, embed, moderator, problem

log = logging.getLogger("syke.notices")

STICKY = "sticky"
ANNOUNCE = "announce"
STICKY_DELAY = 4.0
STICKY_MAX = 10
STICKY_FOOTER = "📌 Sticky message"
ANNOUNCE_MAX = 25
TEXT_MAX = 2000
REPEATS = ("once", "hourly", "daily", "weekly", "monthly")
REPEAT_WORDS = {"once": "once", "hourly": "hourly", "daily": "daily", "everyday": "daily", "weekly": "weekly",
                "monthly": "monthly", "every hour": "hourly", "every day": "daily", "every week": "weekly",
                "every month": "monthly"}
WEEKDAYS = {name: i for i, names in enumerate((
    ("monday", "mon"), ("tuesday", "tue", "tues"), ("wednesday", "wed"), ("thursday", "thu", "thur", "thurs"),
    ("friday", "fri"), ("saturday", "sat"), ("sunday", "sun"))) for name in names}
UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60,
         "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "d": 86400, "day": 86400,
         "days": 86400, "w": 604800, "week": 604800, "weeks": 604800}


# Time parsing

def parse_clock(text: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"(\d{1,2})(?:[:.h](\d{2}))?\s*(am|pm)?", text.strip().lower())
    if not m:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if m.group(2) is None and half is None:
        return None  # a bare number is too ambiguous
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def parse_when(text: str, now: datetime) -> datetime | None:
    """A time in `now`'s timezone from text like "in 2h", "18:00", "tomorrow 6pm", "fri 20:00", "24.12.2026 18:00"."""
    raw = re.sub(r"\s+", " ", (text or "").strip().lower().replace(" at ", " "))
    if not raw:
        return None
    if raw == "now":
        return now
    rel = re.fullmatch(r"(?:in )?((?:\d+ ?[a-z]+ ?)+)", raw)
    if rel:
        total = 0
        for amount, unit in re.findall(r"(\d+) ?([a-z]+)", rel.group(1)):
            if unit not in UNITS:
                total = -1
                break
            total += int(amount) * UNITS[unit]
        if total > 0:
            return now + timedelta(seconds=total)
    day = None
    rest = raw
    first, _, after = raw.partition(" ")
    if first in ("today", "tonight"):
        day, rest = now.date(), after
    elif first == "tomorrow":
        day, rest = (now + timedelta(days=1)).date(), after
    elif first in WEEKDAYS:
        ahead = (WEEKDAYS[first] - now.weekday()) % 7
        day, rest = (now + timedelta(days=ahead)).date(), after
    else:
        m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})(?: |$)|(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?: |$)", raw)
        if m:
            try:
                if m.group(1):
                    day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
                else:
                    year = int(m.group(6)) if m.group(6) else now.year
                    year += 2000 if year < 100 else 0
                    day = datetime(year, int(m.group(5)), int(m.group(4))).date()
                    if not m.group(6) and day < now.date():
                        day = day.replace(year=now.year + 1)
            except ValueError:
                return None
            rest = raw[m.end():].strip()
    clock = parse_clock(rest) if rest else (9, 0) if day is not None else None
    if clock is None:
        return None
    if day is None:
        when = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        return when if when > now else when + timedelta(days=1)
    when = now.replace(year=day.year, month=day.month, day=day.day, hour=clock[0], minute=clock[1], second=0,
                       microsecond=0)
    if first in WEEKDAYS and when <= now:
        when += timedelta(days=7)
    return when


def next_time(when: datetime, repeat: str, now: datetime) -> datetime | None:
    if repeat == "once":
        return None
    step = {"hourly": timedelta(hours=1), "daily": timedelta(days=1), "weekly": timedelta(weeks=1)}.get(repeat)
    while when <= now:
        if step is not None:
            when += step
        else:
            month = when.month % 12 + 1
            year = when.year + (1 if month == 1 else 0)
            day = min(when.day, calendar.monthrange(year, month)[1])
            when = when.replace(year=year, month=month, day=day)
    return when


def split_repeat(text: str) -> tuple[str, str]:
    """Pull a repeat word (daily, every week…) off the end of a time."""
    lowered = text.strip().lower()
    for word, repeat in sorted(REPEAT_WORDS.items(), key=lambda kv: -len(kv[0])):
        if lowered.endswith(" " + word) or lowered == word:
            return text.strip()[: len(text.strip()) - len(word)].strip(), repeat
    return text.strip(), "once"


# Views

class AnnounceModal(discord.ui.Modal, title="Schedule an announcement"):
    message = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, max_length=TEXT_MAX,
                                   placeholder="What should SYKE post? Role mentions work.")
    repeat = discord.ui.TextInput(label="Repeat (once, hourly, daily, weekly, monthly)", default="once",
                                  max_length=10, required=False)

    def __init__(self, cog: "Notices", channel, when: datetime) -> None:
        super().__init__()
        self.cog, self.channel, self.when = cog, channel, when

    async def on_submit(self, interaction: discord.Interaction) -> None:
        repeat = REPEAT_WORDS.get(str(self.repeat.value or "once").strip().lower())
        if repeat is None:
            await interaction.response.send_message("Repeat must be once, hourly, daily, weekly or monthly.",
                                                    ephemeral=True)
            return
        card = self.cog.schedule(interaction.guild, interaction.user, self.channel, self.when, repeat,
                                 str(self.message.value))
        await interaction.response.send_message(embed=card, ephemeral=True)


class Notices(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._sticky_tasks: dict[int, asyncio.Task] = {}
        self._sticky_locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        self.announce_loop.start()

    async def cog_unload(self) -> None:
        self.announce_loop.cancel()
        for task in self._sticky_tasks.values():
            task.cancel()

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    def now(self, guild_id: int) -> datetime:
        return datetime.now(self.bot.tz_for(guild_id))

    # Sticky messages

    def stickies(self, guild_id: int) -> dict:
        return self.bot.storage.get_config(guild_id, STICKY)

    def sticky_card(self, text: str) -> discord.Embed:
        card = discord.Embed(description=text, colour=BRAND)
        card.set_footer(text=STICKY_FOOTER)
        return card

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None:
            return
        me = getattr(self.bot, "user", None)
        if me is not None and message.author.id == me.id and any(
                (e.footer.text or "") == STICKY_FOOTER for e in message.embeds):
            return
        sticky = self.stickies(guild.id).get(str(message.channel.id))
        if not sticky:
            return
        task = self._sticky_tasks.get(message.channel.id)
        if task is not None and not task.done():
            task.cancel()
        self._sticky_tasks[message.channel.id] = asyncio.create_task(self._restick(guild, message.channel))

    async def _restick(self, guild, channel, delay: float = STICKY_DELAY) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        lock = self._sticky_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            sticky = self.stickies(guild.id).get(str(channel.id))
            if not sticky:
                return
            if sticky.get("last"):
                with contextlib.suppress(discord.HTTPException):
                    await channel.get_partial_message(sticky["last"]).delete()
            try:
                sent = await channel.send(embed=self.sticky_card(sticky["text"]),
                                          allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.warning("Couldn't repost a sticky in %s", channel.id, exc_info=True)
                return
            stickies = self.stickies(guild.id)
            if str(channel.id) in stickies:
                stickies[str(channel.id)]["last"] = sent.id
                self.bot.storage.set_config(guild.id, STICKY, stickies)

    @commands.hybrid_group(name="sticky", aliases=["stick"], fallback="list", case_insensitive=True,
                           description="Keep a message stuck to the bottom of a channel")
    @moderator("manage_messages")
    async def sticky(self, ctx: commands.Context) -> None:
        stickies = self.stickies(ctx.guild.id)
        p = self.p(ctx)
        if not stickies:
            await ctx.reply(embed=embed("📌 No sticky messages", f"Add one with `{p}sticky set #channel <text>`."),
                            ephemeral=True)
            return
        lines = [f"<#{cid}>: {s['text'][:80]}{'…' if len(s['text']) > 80 else ''}" for cid, s in stickies.items()]
        await ctx.reply(embed=embed("📌 Sticky messages", "\n".join(lines) + f"\n\nRemove one with `{p}sticky remove "
                                                                             f"#channel`."), ephemeral=True)

    @sticky.command(name="set", aliases=["add"], description="Stick a message to the bottom of a channel")
    @app_commands.describe(channel="Which channel (default: this one)", text="The message (use \\n for a new line)")
    @moderator("manage_messages")
    async def sticky_set(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None, *,
                         text: str) -> None:
        channel = channel or ctx.channel
        text = text.replace("\\n", "\n").strip()[:TEXT_MAX]
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links and perms.manage_messages):
            await ctx.reply(embed=problem("I can't keep it there", f"Give SYKE **Send Messages**, **Embed Links** and "
                                                                    f"**Manage Messages** in {channel.mention}."),
                            ephemeral=True)
            return
        stickies = self.stickies(ctx.guild.id)
        if str(channel.id) not in stickies and len(stickies) >= STICKY_MAX:
            await ctx.reply(embed=problem("That's a lot of stickies", f"{STICKY_MAX} is the limit. Remove one first."),
                            ephemeral=True)
            return
        old = stickies.get(str(channel.id), {}).get("last")
        stickies[str(channel.id)] = {"text": text, "last": old}
        self.bot.storage.set_config(ctx.guild.id, STICKY, stickies)
        await ctx.reply(embed=embed("Stuck 📌", f"That message now stays at the bottom of {channel.mention}.", OK),
                        ephemeral=True)
        await self._restick(ctx.guild, channel, delay=0)

    @sticky.command(name="remove", aliases=["off", "delete", "unstick"], description="Unstick a channel's message")
    @app_commands.describe(channel="Which channel (default: this one)")
    @moderator("manage_messages")
    async def sticky_remove(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        channel = channel or ctx.channel
        stickies = self.stickies(ctx.guild.id)
        found = stickies.pop(str(channel.id), None)
        if not found:
            await ctx.reply(f"{channel.mention} has no sticky message.", ephemeral=True)
            return
        self.bot.storage.set_config(ctx.guild.id, STICKY, stickies)
        if found.get("last"):
            with contextlib.suppress(discord.HTTPException):
                await channel.get_partial_message(found["last"]).delete()
        await ctx.reply(embed=embed("Unstuck", f"{channel.mention} has no sticky message now.", WARN), ephemeral=True)

    # Announcements

    def announcements(self, guild_id: int) -> dict:
        return self.bot.storage.get_config(guild_id, ANNOUNCE)

    def schedule(self, guild, author, channel, when: datetime, repeat: str, text: str) -> discord.Embed:
        data = self.announcements(guild.id)
        items = dict(data.get("items") or {})
        if len(items) >= ANNOUNCE_MAX:
            return problem("Too many scheduled", f"{ANNOUNCE_MAX} is the limit. Cancel one first.")
        number = int(data.get("next") or 1)
        perms = getattr(author, "guild_permissions", None)
        items[str(number)] = {"channel": channel.id, "text": text.strip()[:TEXT_MAX], "at": when.timestamp(),
                              "repeat": repeat, "by": author.id,
                              "everyone": bool(perms and (perms.mention_everyone or perms.administrator))}
        self.bot.storage.set_config(guild.id, ANNOUNCE, {"next": number + 1, "items": items})
        stamp = int(when.timestamp())
        repeat_text = "" if repeat == "once" else f", then **{repeat}**"
        return embed(f"Announcement #{number} scheduled 📣",
                     f"Posts in {channel.mention} <t:{stamp}:F> (<t:{stamp}:R>){repeat_text}.\n"
                     f"Cancel it with `announce cancel {number}`.", OK)

    async def send_announcement(self, guild, item: dict) -> bool:
        channel = guild.get_channel(item.get("channel") or 0)
        if channel is None:
            return False
        mentions = discord.AllowedMentions(everyone=bool(item.get("everyone")), roles=True, users=True)
        try:
            await channel.send(item["text"], allowed_mentions=mentions)
        except discord.HTTPException:
            log.warning("Announcement failed in %s", guild.id, exc_info=True)
            return False
        return True

    @tasks.loop(seconds=20)
    async def announce_loop(self) -> None:
        for guild_id, data in self.bot.storage.configs(ANNOUNCE).items():
            items = dict(data.get("items") or {})
            guild = self.bot.get_guild(guild_id)
            if guild is None or not items:
                continue
            now = self.now(guild_id)
            changed = False
            for key, item in list(items.items()):
                if item.get("at", 0) > now.timestamp():
                    continue
                late = now.timestamp() - item["at"]
                if late < 3600:  # a slot missed by more than an hour (bot offline) is skipped, not spammed
                    await self.send_announcement(guild, item)
                when = datetime.fromtimestamp(item["at"], now.tzinfo)
                nxt = next_time(when, item.get("repeat") or "once", now)
                if nxt is None:
                    items.pop(key)
                else:
                    items[key] = {**item, "at": nxt.timestamp()}
                changed = True
            if changed:
                latest = self.announcements(guild_id)
                self.bot.storage.set_config(guild_id, ANNOUNCE, {"next": latest.get("next") or 1, "items": items})

    @announce_loop.before_loop
    async def _before_announce(self) -> None:
        await self.bot.wait_until_ready()

    @commands.hybrid_group(name="announce", aliases=["announcement", "announcements", "schedule"], fallback="list",
                           case_insensitive=True, description="Schedule announcements, once or on repeat")
    @moderator("manage_guild")
    async def announce(self, ctx: commands.Context) -> None:
        items = self.announcements(ctx.guild.id).get("items") or {}
        p = self.p(ctx)
        usage = (f"`{p}announce add #channel <when> | <message>`\n"
                 f"When: `in 2h`, `18:00`, `tomorrow 6pm`, `fri 20:00`, `24.12 18:00`. Add `daily`, `weekly`, "
                 f"`monthly` or `hourly` after the time to repeat it. Times use the server's timezone.")
        if not items:
            await ctx.reply(embed=embed("📣 Nothing scheduled", usage), ephemeral=True)
            return
        lines = []
        for key, item in sorted(items.items(), key=lambda kv: kv[1].get("at", 0)):
            repeat = "" if item.get("repeat") in (None, "once") else f" · {item['repeat']}"
            preview = item["text"].replace("\n", " ")[:60]
            lines.append(f"**#{key}** <#{item['channel']}> <t:{int(item['at'])}:f>{repeat}\n> {preview}")
        await ctx.reply(embed=embed("📣 Scheduled announcements", "\n".join(lines) + "\n\n" + usage), ephemeral=True)

    @announce.command(name="add", aliases=["new", "create"], description="Schedule an announcement")
    @app_commands.describe(channel="Where it gets posted",
                           when="e.g. in 2h, 18:00, tomorrow 6pm, fri 20:00, 24.12 18:00 (add 'daily' to repeat)")
    @moderator("manage_guild")
    async def announce_add(self, ctx: commands.Context, channel: discord.TextChannel, *, when: str) -> None:
        when_text, _, text = when.partition("|")
        when_text, repeat = split_repeat(when_text)
        moment = parse_when(when_text, self.now(ctx.guild.id))
        p = self.p(ctx)
        if moment is None:
            await ctx.reply(embed=problem("When?", f"I couldn't read **{when_text or 'that'}** as a time. Try `in 2h`, "
                                                   f"`18:00`, `tomorrow 6pm`, `fri 20:00` or `24.12 18:00`."),
                            ephemeral=True)
            return
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages:
            await ctx.reply(embed=problem("I can't post there", f"Give SYKE **Send Messages** in {channel.mention}."),
                            ephemeral=True)
            return
        text = text.replace("\\n", "\n").strip()
        if not text:
            if ctx.interaction is not None:
                await ctx.interaction.response.send_modal(AnnounceModal(self, channel, moment))
                return
            await ctx.reply(embed=problem("What should it say?", f"Put the message after a `|`:\n"
                                                                 f"`{p}announce add #news fri 20:00 | Game night! 🎲`"))
            return
        await ctx.reply(embed=self.schedule(ctx.guild, ctx.author, channel, moment, repeat, text), ephemeral=True)

    @announce.command(name="cancel", aliases=["remove", "delete", "stop"], description="Cancel a scheduled announcement")
    @app_commands.describe(number="The announcement's number (see /announce list)")
    @moderator("manage_guild")
    async def announce_cancel(self, ctx: commands.Context, number: int) -> None:
        data = self.announcements(ctx.guild.id)
        items = dict(data.get("items") or {})
        if items.pop(str(number), None) is None:
            await ctx.reply(f"There's no announcement #{number}.", ephemeral=True)
            return
        self.bot.storage.set_config(ctx.guild.id, ANNOUNCE, {"next": data.get("next") or 1, "items": items})
        await ctx.reply(embed=embed("Cancelled", f"Announcement #{number} won't go out.", WARN), ephemeral=True)

    @announce.command(name="test", aliases=["preview"], description="Post a scheduled announcement right now")
    @app_commands.describe(number="The announcement's number")
    @moderator("manage_guild")
    async def announce_test(self, ctx: commands.Context, number: int) -> None:
        item = (self.announcements(ctx.guild.id).get("items") or {}).get(str(number))
        if item is None:
            await ctx.reply(f"There's no announcement #{number}.", ephemeral=True)
            return
        ok = await self.send_announcement(ctx.guild, item)
        await ctx.reply("Posted. It still goes out on schedule too." if ok else "Couldn't post it.", ephemeral=True)
