"""SYKE behaving like a regular in the chat around him: he joins chains (and complains when someone breaks
one), adds to pile-ons, picks a side on hot takes, gets petty when people use other bots or ChatGPT, notices
pins, bans and new emojis, and double-texts when someone ignores his question.

All of it only happens in yap channels, never when the server set talkativeness to Silent, and each kind
has its own cooldown so SYKE stays a regular, not a spammer.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import random
import re
import time

import discord
from discord.ext import commands, tasks

from . import ai
from .edge import is_upset
from .slurs import count_slurs
from .yap import Turn

log = logging.getLogger("syke")

CHAIN_MIN = 3                 # identical messages from this many people make a chain
CHAIN_JOIN = 0.7              # chance SYKE joins a chain
CHAIN_BREAK_MIN = 4           # a chain this long (SYKE included) is worth complaining about when broken
CHAIN_COMPLAIN = 0.5
CHAIN_MAX_LEN = 80
PILE_MIN = 3                  # different people replying to the same message within PILE_WINDOW
PILE_WINDOW = 180
PILE_JOIN = 0.6
HOT_TAKE_CHANCE = 0.5
JEALOUS_CHANCE = 0.35
EVENT_CHANCE = 0.7
COOLDOWNS = {"chain": 60, "pile": 10 * 60, "hot": 15 * 60, "jealous": 30 * 60, "event": 5 * 60, "double": 2 * 3600}
DOUBLE_AFTER = (6 * 60, 20 * 60)  # follow up an ignored question after this long…
DOUBLE_CHANCE = 0.4               # …this often
DOUBLE_LINES = ("??", "hello??", "ignored lol", "ok don't answer then", "🦗", "left me on read 💀", "wow ok",
                "you saw that", "answer the question 😭")
CHAIN_BREAK_LINES = ("bro broke the chain 😭", "who let {name} break the chain", "chain ruined. thanks {name}",
                     "nah {name} really ended it", "{name} has never been part of anything", "chain killer 💀")
PILE_REACTIONS = ("💀", "😭", "🤣", "☠️")
HOT_TAKE = re.compile(r"\b(?:hot take|unpopular opinion|controversial opinion|change my mind|cmv|i'?m gonna say it|"
                      r"im gonna say it|lowkey overrated|highkey overrated|overrated af|underrated af)\b", re.IGNORECASE)
OTHER_AI = re.compile(r"\b(?:chat\s?gpt|gpt-?\d\w*|openai|claude|gemini|copilot|grok|character\.?ai|c\.ai|"
                      r"perplexity|deepseek|meta ai|siri|alexa)\b", re.IGNORECASE)
LINKY = re.compile(r"https?://|<@|<#|@everyone|@here", re.IGNORECASE)


def chain_key(message) -> str | None:
    """What makes a message part of a chain: same short text, or the same sticker."""
    stickers = getattr(message, "stickers", None) or []
    if stickers and not (message.content or "").strip():
        return f"sticker:{stickers[0].id}"
    text = " ".join((message.content or "").split())
    if not text or len(text) > CHAIN_MAX_LEN or LINKY.search(text) or count_slurs(text):
        return None   # SYKE never repeats a slur, chain or not
    return text.casefold()


class Regular(commands.Cog, name="Regular"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.storage = bot.storage
        self._recent: dict[int, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=12))
        self._chain_joined: dict[int, str] = {}           # channel -> chain SYKE joined
        self._replies: dict[int, dict[int, dict]] = collections.defaultdict(dict)   # channel -> msg -> {authors, t}
        self._piled: set[int] = set()
        self._cool: dict[tuple[int, str], float] = {}
        self._asked: dict[tuple[int, int], dict] = {}      # (channel, person) -> SYKE's unanswered question

    async def cog_load(self) -> None:
        if not self.double_text.is_running():
            self.double_text.start()

    async def cog_unload(self) -> None:
        self.double_text.cancel()

    # Shared helpers

    def chatty(self, guild_id: int) -> bool:
        """Silent servers get none of this."""
        return self.bot.yap_pace(guild_id)[0] > 0

    def ready(self, scope: int, kind: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return now - self._cool.get((scope, kind), -1e9) >= COOLDOWNS[kind]

    def used(self, scope: int, kind: str, now: float | None = None) -> None:
        self._cool[(scope, kind)] = time.time() if now is None else now

    def is_me(self, user) -> bool:
        me = getattr(self.bot, "user", None)
        return me is not None and getattr(user, "id", None) == me.id

    async def chat_of(self, channel, limit: int = 10) -> list[Turn]:
        chat: list[Turn] = []
        with contextlib.suppress(discord.HTTPException, AttributeError, TypeError):
            async for m in channel.history(limit=limit):
                text = self.bot.turn_text(m)
                if text:
                    chat.append(Turn(self.bot.turn_name(m.author), text, self.bot.replied_to(m), m.author.id))
        return chat[::-1]

    async def say(self, guild, channel, moment: str, reply_to=None) -> bool:
        """One AI-written line for a moment, in the server's voice and edge."""
        line = await ai.write_moment(self.bot.settings, moment, await self.chat_of(channel),
                                     await self.bot.server_voice(guild.id), "", self.bot.lore_notes(guild.id),
                                     self.bot.edge_text(guild.id, getattr(reply_to, "content", "") or ""))
        if not line:
            return False
        am = discord.AllowedMentions.none()
        try:
            if reply_to is not None:
                await reply_to.reply(line, mention_author=False, allowed_mentions=am)
            else:
                await channel.send(line, allowed_mentions=am)
        except discord.HTTPException:
            return False
        self.bot.remember_yap(channel.id, line)
        return True

    def yap_channel(self, guild):
        for channel_id in self.storage.yap_channels(guild.id):
            channel = guild.get_channel(channel_id)
            if channel is not None:
                return channel
        return None

    # Watching the chat

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if self.is_me(message.author):
            return
        self.note_answer(message)
        if guild is None or message.channel.id not in self.storage.yap_channels(guild.id):
            return
        if not self.chatty(guild.id):
            return
        if message.author.bot:
            await self.other_bot_used(message)
            return
        if message.author.id in self.storage.optouts(guild.id):
            return
        if message.content and self.bot.is_command_text(guild.id, message.content):
            return
        now = time.time()
        if await self.chains(message, now):
            return
        if await self.pile_on(message, now):
            return
        if await self.hot_take(message, now):
            return
        await self.jealous_of_ai(message, now)

    # Chains

    async def chains(self, message, now: float) -> bool:
        channel = message.channel
        recent = self._recent[channel.id]
        key = chain_key(message)
        previous = list(recent)
        recent.append((message.author.id, key, now))
        # Broken? The last run of identical keys before this one, SYKE included.
        run = []
        for author, k, _ in reversed(previous):
            if k is None or (run and k != run[0][1]):
                break
            run.append((author, k))
        if run and key != run[0][1] and len(run) >= CHAIN_BREAK_MIN:
            joined = self._chain_joined.get(channel.id) == run[0][1]
            self._chain_joined.pop(channel.id, None)
            if joined and random.random() < CHAIN_COMPLAIN and self.ready(channel.id, "chain", now):
                self.used(channel.id, "chain", now)
                line = random.choice(CHAIN_BREAK_LINES).format(name=self.bot.speaker_name(message.author))
                with contextlib.suppress(discord.HTTPException):
                    await channel.send(line, allowed_mentions=discord.AllowedMentions.none())
                return True
            return False
        if key is None:
            return False
        run = [(message.author.id, key)] + run if (not run or run[0][1] == key) else [(message.author.id, key)]
        people = {a for a, _ in run if a != getattr(self.bot.user, "id", None)}
        if len(people) < CHAIN_MIN or self._chain_joined.get(channel.id) == key:
            return False
        if random.random() >= CHAIN_JOIN or not self.ready(channel.id, "chain", now):
            self._chain_joined[channel.id] = key   # sat this one out: don't ask again for the same chain
            return False
        self.used(channel.id, "chain", now)
        self._chain_joined[channel.id] = key
        with contextlib.suppress(discord.HTTPException):
            if key.startswith("sticker:"):
                await channel.send(stickers=[message.stickers[0]])
            else:
                await channel.send(message.content, allowed_mentions=discord.AllowedMentions.none())
        recent.append((getattr(self.bot.user, "id", 0), key, now))
        return True

    # Pile-ons and hot takes

    async def pile_on(self, message, now: float) -> bool:
        ref = getattr(message, "reference", None)
        target = getattr(ref, "resolved", None) if ref else None
        if target is None or not hasattr(target, "author") or getattr(target.author, "bot", False):
            return False
        replies = self._replies[message.channel.id]
        for mid in [m for m, v in replies.items() if now - v["t"] > PILE_WINDOW]:
            del replies[mid]
        entry = replies.setdefault(target.id, {"authors": set(), "t": now})
        if message.author.id != target.author.id:
            entry["authors"].add(message.author.id)
        if len(entry["authors"]) < PILE_MIN or target.id in self._piled:
            return False
        self._piled.add(target.id)
        if is_upset(getattr(target, "content", "") or "") or random.random() >= PILE_JOIN:
            return False   # never pile on someone who's actually hurting
        if not self.ready(message.guild.id, "pile", now):
            return False
        self.used(message.guild.id, "pile", now)
        name = self.bot.speaker_name(target.author)
        if random.random() < 0.5:
            with contextlib.suppress(discord.HTTPException):
                await target.add_reaction(random.choice(PILE_REACTIONS))
            return True
        return await self.say(message.guild, message.channel,
                              f"Everyone is piling on {name} for saying: \"{(target.content or '')[:200]}\". Add ONE "
                              "line to the pile-on, like a regular would (or a 'ratio'). Roast what they said, never "
                              "who they are.", reply_to=target)

    async def hot_take(self, message, now: float) -> bool:
        if not HOT_TAKE.search(message.content or "") or random.random() >= HOT_TAKE_CHANCE:
            return False
        if not self.ready(message.guild.id, "hot", now):
            return False
        self.used(message.guild.id, "hot", now)
        name = self.bot.speaker_name(message.author)
        return await self.say(message.guild, message.channel,
                              f"{name} just posted a hot take: \"{(message.content or '')[:240]}\". Pick a side — "
                              "agree hard or disagree hard — in one line, the way a regular with opinions would.",
                              reply_to=message)

    # Jealousy

    async def jealous_of_ai(self, message, now: float) -> bool:
        text = message.content or ""
        other_bot = next((u for u in getattr(message, "mentions", ()) or ()
                          if getattr(u, "bot", False) and not self.is_me(u)), None)
        mentioned = OTHER_AI.search(text)
        if not (mentioned or other_bot) or random.random() >= JEALOUS_CHANCE:
            return False
        return await self.petty(message.guild, message.channel, message.author,
                                other_bot.display_name if other_bot else mentioned.group(0), message, now)

    async def other_bot_used(self, message) -> bool:
        """Another bot answering someone's command."""
        meta = getattr(message, "interaction_metadata", None) or getattr(message, "interaction", None)
        user = getattr(meta, "user", None)
        if user is None or getattr(user, "bot", False) or random.random() >= JEALOUS_CHANCE:
            return False
        return await self.petty(message.guild, message.channel, user, message.author.display_name, None, time.time())

    async def petty(self, guild, channel, user, rival: str, reply_to, now: float) -> bool:
        if not self.ready(guild.id, "jealous", now):
            return False
        self.used(guild.id, "jealous", now)
        name = self.bot.speaker_name(user)
        return await self.say(guild, channel,
                              f"{name} is using {rival} instead of you (or talking it up). Be petty and jealous about "
                              "it in one line (\"oh so you're asking HIM now\" energy). Funny, not mean-spirited.",
                              reply_to=reply_to)

    # Things that happen in the server

    @commands.Cog.listener()
    async def on_guild_channel_pins_update(self, channel, last_pin) -> None:
        guild = getattr(channel, "guild", None)
        if guild is None or last_pin is None or not self.chatty(guild.id):
            return
        if time.time() - last_pin.timestamp() > 120 or random.random() >= EVENT_CHANCE:
            return   # an unpin, or an old pin
        if not self.ready(guild.id, "event"):
            return
        pinned = None
        with contextlib.suppress(discord.HTTPException, AttributeError):
            pins = await channel.pins()
            pinned = pins[0] if pins else None
        where = channel if channel.id in self.storage.yap_channels(guild.id) else self.yap_channel(guild)
        if pinned is None or where is None:
            return
        self.used(guild.id, "event")
        await self.say(guild, where, f"Someone just pinned {self.bot.speaker_name(pinned.author)}'s message: "
                                     f"\"{(pinned.content or '[a picture]')[:200]}\". React like a regular ('who "
                                     "pinned that 💀' energy), one line.")

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user) -> None:
        if not self.chatty(guild.id) or random.random() >= EVENT_CHANCE or not self.ready(guild.id, "event"):
            return
        where = self.yap_channel(guild)
        if where is None:
            return
        self.used(guild.id, "event")
        await self.say(guild, where, "Someone just got banned from the server. React like a regular in one line "
                                     "('rip whoever got banned' energy). Don't name them, don't guess why.")

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild, before, after) -> None:
        old = {e.id for e in before}
        new = [e for e in after if e.id not in old]
        if not new or not self.chatty(guild.id) or random.random() >= EVENT_CHANCE or not self.ready(guild.id, "event"):
            return
        where = self.yap_channel(guild)
        if where is None:
            return
        self.used(guild.id, "event")
        emoji = new[0]
        await self.say(guild, where, f"The server just added a new emoji: {emoji} (named {emoji.name}). React to it "
                                     f"like a regular ('new emoji goes hard'), and use it: write it exactly as {emoji}.")

    # Double-texting

    def asked(self, channel, person, text: str, now: float | None = None) -> None:
        """SYKE just asked `person` something; follow up later if they ignore it."""
        if not text.rstrip().endswith("?") or getattr(person, "bot", False):
            return
        self._asked[(channel.id, person.id)] = {"t": time.time() if now is None else now, "channel": channel,
                                                "person": person}

    def note_answer(self, message) -> None:
        self._asked.pop((message.channel.id, message.author.id), None)

    @tasks.loop(minutes=1)
    async def double_text(self) -> None:
        await self.follow_up_ignored()

    @double_text.before_loop
    async def _before_double(self) -> None:
        await self.bot.wait_until_ready()

    async def follow_up_ignored(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        sent = 0
        for key, item in list(self._asked.items()):
            waited = now - item["t"]
            if waited < DOUBLE_AFTER[0]:
                continue
            del self._asked[key]
            if waited > DOUBLE_AFTER[1] or random.random() >= DOUBLE_CHANCE:
                continue
            person, channel = item["person"], item["channel"]
            guild = getattr(channel, "guild", None)
            if guild is not None and not self.chatty(guild.id):
                continue
            if not self.ready(person.id, "double", now):
                continue
            self.used(person.id, "double", now)
            with contextlib.suppress(discord.HTTPException):
                await channel.send(random.choice(DOUBLE_LINES), allowed_mentions=discord.AllowedMentions.none())
                sent += 1
        return sent
