"""Achievements: badges that unlock on their own from what people actually do.

Most badges are worked out from data SYKE already keeps (stored messages, Liked songs, promises, how SYKE
feels about you, rivalries, Strip cash). A few come from counters other features bump (radio starts, parties,
song quiz wins, a blend score, starting lore). `check` runs when someone chats (at most every few minutes per
person) or right after a counted event, and new badges are announced once, in that channel.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime

import discord
from discord.ext import commands

log = logging.getLogger("syke.achievements")

CHECK_EVERY = 10 * 60
GOLD = discord.Colour.from_str("#f5b301")


@dataclass(frozen=True)
class Badge:
    key: str
    emoji: str
    name: str
    how: str
    goal: float = 1


BADGES: list[Badge] = [
    Badge("chatterbox", "💬", "Chatterbox", "Send 1,000 messages", 1000),
    Badge("night_owl", "🦉", "Night Owl", "Send 100 messages between 2 and 5 am", 100),
    Badge("early_bird", "🐦", "Early Bird", "Send 100 messages between 5 and 8 am", 100),
    Badge("lore_starter", "📜", "Lore Starter", "Start a phrase that makes the server's lore"),
    Badge("radio_dj", "📻", "Radio DJ", "Start the radio 10 times", 10),
    Badge("tastemaker", "❤️", "Tastemaker", "Save 50 Liked songs", 50),
    Badge("party_host", "🎉", "Party Starter", "Host a listening party"),
    Badge("soulmate", "💞", "Musical Soulmate", "Get an 85% or higher Blend with someone"),
    Badge("quiz_champ", "🎵", "Name That Tune", "Win 10 song quiz rounds", 10),
    Badge("promise_keeper", "🤞", "Kept 10 Promises", "Keep 10 promises SYKE heard you make", 10),
    Badge("promise_breaker", "🤥", "Serial Promise Breaker", "Break 5 promises", 5),
    Badge("survived_grudge", "🛡️", "Survived SYKE's Grudge", "Fall to SYKE's bad side, then win him back"),
    Badge("syke_fave", "💖", "SYKE's Favourite", "Become one of SYKE's besties"),
    Badge("beef_champ", "🥊", "Beef Champion", "Win 5 rounds of a rivalry", 5),
    Badge("high_roller", "💰", "High Roller", "Have $100,000 on THE STRIP", 100_000),
    Badge("father", "👑", "SYKE's Dad", "Create SYKE"),
]
BY_KEY = {b.key: b for b in BADGES}


class Achievements(commands.Cog, name="Achievements"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._checked: dict[tuple[int, int], float] = {}

    @property
    def storage(self):
        return self.bot.storage

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    # Counters other features bump

    def bump(self, guild_id: int, user_id: int, counter: str, amount: float = 1) -> None:
        self.storage._write("INSERT INTO achievement_counts (guild_id, user_id, counter, value) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(guild_id, user_id, counter) DO UPDATE SET value = value + excluded.value",
                            (guild_id, user_id, counter, amount))

    def set_max(self, guild_id: int, user_id: int, counter: str, value: float) -> None:
        self.storage._write("INSERT INTO achievement_counts (guild_id, user_id, counter, value) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(guild_id, user_id, counter) DO UPDATE SET value = MAX(value, excluded.value)",
                            (guild_id, user_id, counter, value))

    def counter(self, guild_id: int, user_id: int, counter: str) -> float:
        rows = self.storage._read("SELECT value FROM achievement_counts WHERE guild_id = ? AND user_id = ? AND counter = ?",
                                  (guild_id, user_id, counter))
        return rows[0][0] if rows else 0.0

    # Progress

    def progress(self, guild_id: int, user_id: int) -> dict[str, float]:
        """How far someone is on every badge (compare with Badge.goal)."""
        read = self.storage._read
        c = lambda name: self.counter(guild_id, user_id, name)  # noqa: E731
        stored = read("SELECT COUNT(*) FROM corpus WHERE guild_id = ? AND author_id = ?", (guild_id, user_id))[0][0]
        tz = self.bot.tz_for(guild_id)
        shift = datetime.now(tz).utcoffset()
        offset = shift.total_seconds() if shift else 0
        hours = dict(read("SELECT CAST(strftime('%H', created_at + ?, 'unixepoch') AS INTEGER) AS h, COUNT(*) "
                          "FROM corpus WHERE guild_id = ? AND author_id = ? AND created_at IS NOT NULL GROUP BY h",
                          (offset, guild_id, user_id)))
        liked = read("SELECT COUNT(*) FROM liked_songs WHERE user_id = ?", (user_id,))[0][0]
        kept = read("SELECT COUNT(*) FROM persona_promises WHERE guild_id = ? AND user_id = ? AND status = 'kept'",
                    (guild_id, user_id))[0][0]
        broken = read("SELECT COUNT(*) FROM persona_promises WHERE guild_id = ? AND user_id = ? AND status = 'broken'",
                      (guild_id, user_id))[0][0]
        bond = 0.0
        persona = self.bot.get_cog("Persona")
        if persona is not None:
            bond = persona.bond(guild_id, user_id)
        wins = read("SELECT COUNT(*) FROM rivalry_rounds WHERE guild_id = ? AND winner = ?", (guild_id, user_id))[0][0]
        wallet = read("SELECT cash + bank FROM econ_wallets WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        is_father = user_id in self.bot.settings.owner_ids
        return {
            "chatterbox": max(stored, c("messages")),
            "night_owl": max(sum(hours.get(h, 0) for h in (2, 3, 4)), c("night")),
            "early_bird": max(sum(hours.get(h, 0) for h in (5, 6, 7)), c("morning")),
            "lore_starter": c("lore_started"),
            "radio_dj": c("radio_starts"),
            "tastemaker": liked,
            "party_host": c("parties"),
            "soulmate": 1 if c("best_blend") >= 85 else 0,
            "quiz_champ": c("quiz_wins"),
            "promise_keeper": kept,
            "promise_breaker": broken,
            "survived_grudge": 1 if c("grudge_low") and bond >= 0 and not is_father else 0,
            "syke_fave": 1 if bond >= 70 and not is_father else 0,
            "beef_champ": wins,
            "high_roller": wallet[0][0] if wallet else 0,
            "father": 1 if is_father else 0,
        }

    def unlocked(self, guild_id: int, user_id: int) -> dict[str, float]:
        return dict(self.storage._read("SELECT badge, unlocked_at FROM achievements WHERE guild_id = ? AND user_id = ?",
                                       (guild_id, user_id)))

    def evaluate(self, guild_id: int, user_id: int) -> list[Badge]:
        """Unlock everything newly earned. Returns the new badges."""
        have = self.unlocked(guild_id, user_id)
        done = self.progress(guild_id, user_id)
        new = []
        now = time.time()
        for badge in BADGES:
            if badge.key not in have and done.get(badge.key, 0) >= badge.goal:
                self.storage._write("INSERT OR IGNORE INTO achievements (guild_id, user_id, badge, unlocked_at) "
                                    "VALUES (?, ?, ?, ?)", (guild_id, user_id, badge.key, now))
                new.append(badge)
        return new

    async def check(self, guild, member, channel=None, announce: bool = True) -> list[Badge]:
        if guild is None or member is None or getattr(member, "bot", False):
            return []
        try:
            new = self.evaluate(guild.id, member.id)
        except Exception:
            log.exception("achievement check failed")
            return []
        if new and announce and channel is not None and self.announcing(guild.id):
            names = ", ".join(f"{b.emoji} **{b.name}**" for b in new)
            card = discord.Embed(description=f"🏅 {member.mention} unlocked {names}\n-# "
                                             + " · ".join(b.how for b in new), colour=GOLD)
            with contextlib.suppress(discord.HTTPException, AttributeError):
                await channel.send(embed=card, allowed_mentions=discord.AllowedMentions.none())
        return new

    def announcing(self, guild_id: int) -> bool:
        return self.storage.get_config(guild_id, "achievements").get("announce", True)

    def badge_line(self, guild_id: int, user_id: int, since: float = 0.0) -> str:
        have = self.unlocked(guild_id, user_id)
        return " ".join(f"{b.emoji} {b.name}" for b in BADGES if have.get(b.key, -1) >= since)

    async def award_event(self, guild, member, channel, counter: str, amount: float = 1, *, maximum: bool = False):
        """A feature tells us something happened (radio start, party, quiz win, blend score…)."""
        if guild is None or member is None:
            return
        if maximum:
            self.set_max(guild.id, member.id, counter, amount)
        else:
            self.bump(guild.id, member.id, counter, amount)
        await self.check(guild, member, channel)

    # Listening

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot or not message.content:
            return
        if self.bot.is_command_text(guild.id, message.content):
            return
        hour = datetime.now(self.bot.tz_for(guild.id)).hour
        self.bump(guild.id, message.author.id, "messages")
        if 2 <= hour < 5:
            self.bump(guild.id, message.author.id, "night")
        elif 5 <= hour < 8:
            self.bump(guild.id, message.author.id, "morning")
        key = (guild.id, message.author.id)
        if time.time() - self._checked.get(key, 0) >= CHECK_EVERY:
            self._checked[key] = time.time()
            await self.check(guild, message.author, message.channel)

    # Commands

    @commands.command(name="achievements", aliases=["badges", "achieve", "trophies"],
                      help="Your badges (or someone's): what's unlocked and how close you are to the rest")
    @commands.guild_only()
    async def achievements_cmd(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        member = member or ctx.author
        await self.check(ctx.guild, member, None, announce=False)
        have = self.unlocked(ctx.guild.id, member.id)
        done = self.progress(ctx.guild.id, member.id)
        got, todo = [], []
        for badge in BADGES:
            if badge.key == "father" and badge.key not in have:
                continue   # secret unless you have it
            if badge.key in have:
                got.append(f"{badge.emoji} **{badge.name}** · <t:{int(have[badge.key])}:d>\n-# {badge.how}")
            else:
                value = done.get(badge.key, 0)
                bar = ""
                if badge.goal > 1:
                    share = min(1.0, value / badge.goal)
                    bar = f" · `{'█' * round(share * 8)}{'░' * (8 - round(share * 8))}` {int(value):,}/{int(badge.goal):,}"
                todo.append(f"🔒 {badge.name}{bar}\n-# {badge.how}")
        total = len([b for b in BADGES if b.key != "father" or b.key in have])
        card = discord.Embed(title=f"🏅 {member.display_name}'s badges · {len(got)}/{total}", colour=GOLD)
        if got:
            card.add_field(name="Unlocked", value="\n".join(got)[:1024], inline=False)
        if todo:
            card.add_field(name="Still to unlock", value="\n".join(todo)[:1024], inline=False)
        card.set_footer(text="Badges unlock on their own, just keep doing your thing")
        await ctx.reply(embed=card, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="badgeannounce", hidden=True, help="Managers: badgeannounce on|off")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def badge_announce(self, ctx: commands.Context, setting: str) -> None:
        on = setting.lower() in ("on", "yes", "true", "1")
        self.storage.update_config(ctx.guild.id, "achievements", announce=on)
        await ctx.reply(f"Badge announcements are **{'on' if on else 'off'}**.", mention_author=False)
