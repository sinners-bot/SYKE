"""SYKE as a regular, not a bot: he remembers what people tell the server and asks about it later, notices
when someone is back or has a birthday, starts conversations when chat goes quiet, and has an edge level
for how dark his humour runs (`$edge`).

Everything he says here goes only to yap channels; facts are only picked up from channels SYKE tracks, and
never about people who opted out.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import random
import re
import time
from datetime import datetime

import discord
from discord.ext import commands, tasks

from .owners import is_owner
from . import ai
from .edge import EDGE_KEY, EDGE_LEVELS, clamp
from .freaky import FREAKY_KEY, FREAKY_LEVELS
from .freaky import clamp as freaky_clamp
from .yap import Turn

log = logging.getLogger("syke")

MOMENTS_KEY = "human_moments"
BDAY_KEY = "birthdays"

FACT_BATCH = 25                 # messages per fact-extraction call
FACT_FLUSH_SECONDS = 15 * 60    # ...or whatever has piled up by then
FACT_BUFFER_MAX = 120
BACK_AFTER_DAYS = 21            # "yo X is back" after this long away
FOLLOWUP_CHANCE = 0.6           # when someone with a due follow-up talks, SYKE asks this often
MOMENT_COOLDOWN = 12 * 60       # at most one unprompted moment (follow-up, welcome back...) per server per 12 min
OPENER_QUIET = (45 * 60, 8 * 3600)   # chat quiet this long (min, max) before SYKE might start something
OPENER_HOURS = range(11, 24)    # local hours SYKE starts conversations in
OPENER_MIN_ACTIVITY = 15        # messages in the channel over the last day: dead channels stay dead
OPENER_GAP = 5 * 3600           # between openers in one channel
OPENER_IGNORED_GAP = 12 * 3600  # after an opener nobody answered
OPENER_CHANCE = 0.3             # per check once everything else allows it

# A cheap first pass: first-person messages that mention something about their life.
FACT_HINT = re.compile(
    r"\b(?:i|i'm|im|i've|ive|i'll|ill|my|me)\b.{0,80}\b(?:exam|test|quiz|finals?|midterms?|interview|job|work|"
    r"shift|boss|school|uni|college|class|birthday|bday|trip|vacation|holiday|flight|moving|move|moved|apartment|"
    r"house|surgery|doctor|dentist|hospital|date|gf|bf|girlfriend|boyfriend|broke up|crush|dog|cat|puppy|kitten|"
    r"pet|car|license|licence|main|maining|rank|ranked|elo|tournament|match|game|concert|gym|diet|marathon|"
    r"wedding|graduat\w*|new phone|new pc|bought|got a|getting a|starting|tomorrow|tonight|next week|weekend|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)


def fact_worthy(text: str) -> bool:
    words = len(text.split())
    return 4 <= words <= 80 and bool(FACT_HINT.search(text)) and "http" not in text


class Human(commands.Cog, name="Human"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.storage = bot.storage
        self._facts: dict[int, list[tuple[int, str, str]]] = collections.defaultdict(list)
        self._facts_since: dict[int, float] = {}
        self._seen: dict[tuple[int, int], float] = {}           # (guild, user) -> last message time
        self._activity: dict[int, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=400))
        self._last_moment: dict[int, float] = {}
        self._openers: dict[int, tuple[float, int]] = {}         # channel -> (sent at, message id)

    async def cog_load(self) -> None:
        self.fact_loop.start()
        self.opener_loop.start()

    async def cog_unload(self) -> None:
        self.fact_loop.cancel()
        self.opener_loop.cancel()

    # $edge

    @commands.command(name="edge", help="How dark SYKE's humour runs here: edge 0-3 (managers change it)")
    @commands.guild_only()
    async def edge(self, ctx: commands.Context, level: str | None = None) -> None:
        current = self.bot.edge_level(ctx.guild.id)
        if level is None:
            lines = [f"{'➜' if n == current else '•'} **{n} · {name}**: {about}" for n, (name, about) in EDGE_LEVELS.items()]
            await ctx.reply(embed=discord.Embed(
                title=f"😈 SYKE's edge here: {current} · {EDGE_LEVELS[current][0]}",
                description="\n".join(lines) + "\n\nNo slurs and no punching down at who people are, at any level. "
                            "When someone is genuinely hurting, he drops the jokes.\n"
                            f"Managers: `{ctx.clean_prefix}edge 0-3`",
                colour=discord.Colour.dark_red()), mention_author=False)
            return
        perms = getattr(ctx.author, "guild_permissions", None)
        if not (is_owner(ctx.author) or (perms and (perms.manage_guild or perms.administrator))):
            await ctx.reply("Only managers can change SYKE's edge.", mention_author=False)
            return
        if not level.strip().isdigit() or not 0 <= int(level) <= 3:
            await ctx.reply("Pick a level from 0 to 3.", mention_author=False)
            return
        new = clamp(level)
        self.storage.update_config(ctx.guild.id, EDGE_KEY, level=new)
        name, about = EDGE_LEVELS[new]
        await ctx.reply(f"😈 SYKE's edge is now **{new} · {name}**: {about}", mention_author=False)

    @commands.command(name="freaky", help="How freaky SYKE gets here: freaky 0-2 (managers change it)")
    @commands.guild_only()
    async def freaky(self, ctx: commands.Context, level: str | None = None) -> None:
        current = self.bot.freaky_level(ctx.guild.id)
        if level is None:
            lines = [f"{'➜' if n == current else '•'} **{n} · {name}**: {about}"
                     for n, (name, about) in FREAKY_LEVELS.items()]
            await ctx.reply(embed=discord.Embed(
                title=f"😏 SYKE's freaky level here: {current} · {FREAKY_LEVELS[current][0]}",
                description="\n".join(lines) + "\n\nNever explicit, never in DMs, never aimed at someone who "
                            "didn't start it, and never with anyone who said they're under 18. Level 2 only kicks "
                            "in inside age-restricted channels.\n"
                            f"Managers: `{ctx.clean_prefix}freaky 0-2`",
                colour=discord.Colour.magenta()), mention_author=False)
            return
        perms = getattr(ctx.author, "guild_permissions", None)
        if not (is_owner(ctx.author) or (perms and (perms.manage_guild or perms.administrator))):
            await ctx.reply("Only managers can change how freaky SYKE gets.", mention_author=False)
            return
        if not level.strip().isdigit() or not 0 <= int(level) <= 2:
            await ctx.reply("Pick a level from 0 to 2.", mention_author=False)
            return
        new = freaky_clamp(level)
        self.storage.update_config(ctx.guild.id, FREAKY_KEY, level=new)
        name, about = FREAKY_LEVELS[new]
        tip = ("\nMark a channel **age-restricted** in its settings for level 2 to apply there."
               if new == 2 else "")
        await ctx.reply(f"😏 SYKE's freaky level is now **{new} · {name}**: {about}{tip}", mention_author=False)

    # Watching chat

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot or not message.content:
            return
        if self.bot.is_command_text(guild.id, message.content):
            return
        if message.author.id in self.storage.optouts(guild.id):
            return
        now = time.time()
        channel_id = message.channel.id
        yap_here = channel_id in self.storage.yap_channels(guild.id)
        if yap_here:
            self._activity[channel_id].append(now)
        if channel_id in self.storage.tracked(guild.id) and fact_worthy(message.content):
            buffer = self._facts[guild.id]
            buffer.append((message.author.id, self.bot.speaker_name(message.author), message.content))
            del buffer[:-FACT_BUFFER_MAX]
            self._facts_since.setdefault(guild.id, now)
            if len(buffer) >= FACT_BATCH:
                await self.flush_facts(guild.id)
        previous = self.last_seen(guild.id, message.author.id, message.id)
        self._seen[(guild.id, message.author.id)] = now
        if not yap_here or self.talking_to_syke(message):
            return   # SYKE answers people talking to him elsewhere; these moments are unprompted
        if now - self._last_moment.get(guild.id, -1e9) < MOMENT_COOLDOWN:
            return
        moment, mark = self.pick_moment(guild, message, previous, now)
        if moment is None:
            return
        self._last_moment[guild.id] = now
        line = await self.write(message, moment)
        if line:
            await self.bot.deliver_turn(message, {"reply": line})
            self.bot.remember_turn(guild.id, message.author, message.content, {"reply": line})
            mark()

    def talking_to_syke(self, message) -> bool:
        me = getattr(self.bot, "user", None)
        if me is None:
            return False
        if any(getattr(u, "id", None) == me.id for u in getattr(message, "mentions", ()) or ()):
            return True
        ref = getattr(message, "reference", None)
        resolved = getattr(ref, "resolved", None) if ref else None
        return getattr(getattr(resolved, "author", None), "id", None) == me.id

    def last_seen(self, guild_id: int, user_id: int, message_id: int) -> float | None:
        key = (guild_id, user_id)
        if key in self._seen:
            return self._seen[key]
        return self.storage.last_seen(guild_id, user_id, message_id)

    def pick_moment(self, guild, message, previous: float | None, now: float):
        """What SYKE would say unprompted to this person right now, if anything: (moment, mark_done)."""
        name = self.bot.speaker_name(message.author)
        uid = message.author.id
        moments = dict(self.storage.get_config(guild.id, MOMENTS_KEY))
        today = datetime.now(self.bot.tz_for(guild.id)).date()
        # Their birthday, the first time they talk today
        people = (self.storage.get_config(guild.id, BDAY_KEY) or {}).get("people") or {}
        born = people.get(str(uid))
        wished = moments.get("bday") or {}
        if born and (int(born[0]), int(born[1])) == (today.month, today.day) and wished.get(str(uid)) != today.isoformat():
            def done():
                data = dict(self.storage.get_config(guild.id, MOMENTS_KEY))
                data["bday"] = {**(data.get("bday") or {}), str(uid): today.isoformat()}
                self.storage.set_config(guild.id, MOMENTS_KEY, data)
            return (f"It's {name}'s birthday today and they just showed up in chat. Wish them happy birthday "
                    "your way: personal, in the server's style, not a greeting card."), done
        # Back after weeks away
        if previous and now - previous >= BACK_AFTER_DAYS * 86400:
            days = int((now - previous) // 86400)
            return (f"{name} just talked for the first time in {days} days. React like a regular who noticed "
                    "they're back (\"yo look who's back\" energy), maybe tease them about vanishing."), lambda: None
        # Something they told the server, now due
        due = self.storage.due_followup(guild.id, uid, now)
        if due and random.random() < FOLLOWUP_CHANCE:
            memory_id, fact, followup = due
            return (f"{name} just said something in chat. A few days ago they mentioned: {fact}. Ask them about it "
                    f"like a friend who remembered (something like: {followup}). If what they just said makes "
                    "that awkward, tie it in naturally."), lambda: self.storage.mark_asked(memory_id)
        return None, None

    async def write(self, message, moment: str) -> str | None:
        guild_id = message.guild.id
        chat = await self.bot.recent_chat(message, 10)
        chat = [*chat, Turn(self.bot.turn_name(message.author), self.bot.turn_text(message),
                            self.bot.replied_to(message), message.author.id)]
        return await ai.write_moment(
            self.bot.settings, moment, chat, await self.bot.server_voice(guild_id),
            self.bot.memory_text(guild_id, message.author, chat), self.bot.lore_notes(guild_id),
            self.bot.edge_text(guild_id, message.content))

    # Facts people mention

    async def flush_facts(self, guild_id: int) -> int:
        items = self._facts.pop(guild_id, [])
        self._facts_since.pop(guild_id, None)
        if not items or self.bot.settings.ai_provider == "none":
            return 0
        today = datetime.now(self.bot.tz_for(guild_id)).strftime("%A %Y-%m-%d")
        found = await ai.extract_facts(self.bot.settings, [(name, text) for _, name, text in items], today)
        optouts = self.storage.optouts(guild_id)
        saved = 0
        for item in found:
            uid = items[item["who"]][0]
            if uid in optouts:
                continue
            due = time.time() + item["days"] * 86400 if item["followup"] else 0
            saved += bool(self.storage.add_memory(guild_id, uid, "fact", item["fact"], item["followup"], due))
        if saved:
            log.info("Remembered %d new facts in %s", saved, guild_id)
        return saved

    @tasks.loop(minutes=5)
    async def fact_loop(self) -> None:
        now = time.time()
        for guild_id, since in list(self._facts_since.items()):
            if now - since >= FACT_FLUSH_SECONDS:
                with contextlib.suppress(Exception):
                    await self.flush_facts(guild_id)

    @fact_loop.before_loop
    async def _before_facts(self) -> None:
        await self.bot.wait_until_ready()

    # Starting conversations

    @tasks.loop(minutes=10)
    async def opener_loop(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self.maybe_open(guild)
            except Exception:
                log.exception("conversation starter failed in %s", guild.id)

    @opener_loop.before_loop
    async def _before_openers(self) -> None:
        await self.bot.wait_until_ready()

    def opener_channel(self, guild, now: float):
        """A yap channel that's gone quiet but is normally alive, where SYKE may start something."""
        moments = self.storage.get_config(guild.id, MOMENTS_KEY)
        day = datetime.now(self.bot.tz_for(guild.id)).date().isoformat()
        if datetime.now(self.bot.tz_for(guild.id)).hour not in OPENER_HOURS:
            return None
        daily = self.bot.yap_pace(guild.id)[2]
        if daily <= 0 or (moments.get("opener_day") == day and int(moments.get("opener_count") or 0) >= daily):
            return None
        for channel_id in self.storage.yap_channels(guild.id):
            seen = self._activity.get(channel_id)
            if not seen:
                continue
            quiet = now - seen[-1]
            if not OPENER_QUIET[0] <= quiet <= OPENER_QUIET[1]:
                continue
            if sum(1 for t in seen if now - t < 86400) < OPENER_MIN_ACTIVITY:
                continue
            last = self._openers.get(channel_id)
            if last:
                answered = any(t > last[0] for t in seen)
                if now - last[0] < (OPENER_GAP if answered else OPENER_IGNORED_GAP):
                    continue
            channel = guild.get_channel(channel_id)
            if channel is not None:
                return channel
        return None

    async def maybe_open(self, guild, now: float | None = None, chance: float = OPENER_CHANCE) -> bool:
        now = time.time() if now is None else now
        channel = self.opener_channel(guild, now)
        if channel is None or random.random() >= chance:
            return False
        chat: list[Turn] = []
        people: list = []
        with contextlib.suppress(discord.HTTPException, AttributeError):
            async for m in channel.history(limit=12):
                text = self.bot.turn_text(m)
                if text and not self.bot.is_command_text(guild.id, m.content or ""):
                    chat.append(Turn(self.bot.turn_name(m.author), text, self.bot.replied_to(m), m.author.id))
                    if not m.author.bot and m.author not in people:
                        people.append(m.author)
        chat.reverse()
        quiet = int((now - self._activity[channel.id][-1]) // 60)
        memory = "\n".join(filter(None, (self.bot.memory_text(guild.id, p) for p in people[:3])))
        moment = (f"The chat has been quiet for {quiet} minutes. Start a conversation the way a regular does after a "
                  "while: ask what people are up to, revive a running joke from IN-JOKES, ask someone (by name, no @) "
                  "about something from MEMORY, or drop a take on something this server cares about. Don't "
                  "mention that it's quiet in a boring way.")
        line = await ai.write_moment(self.bot.settings, moment, chat, await self.bot.server_voice(guild.id),
                                     memory, self.bot.lore_notes(guild.id), self.bot.edge_text(guild.id))
        if not line:
            return False
        try:
            sent = await channel.send(line, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return False
        self._openers[channel.id] = (now, getattr(sent, "id", 0))
        self.bot.remember_yap(channel.id, line)
        data = dict(self.storage.get_config(guild.id, MOMENTS_KEY))
        day = datetime.now(self.bot.tz_for(guild.id)).date().isoformat()
        data["opener_count"] = (int(data.get("opener_count") or 0) + 1) if data.get("opener_day") == day else 1
        data["opener_day"] = day
        self.storage.set_config(guild.id, MOMENTS_KEY, data)
        log.info("Started a conversation in %s", channel.id)
        return True
