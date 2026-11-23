"""Rivalries: SYKE notices when two people keep going at each other, keeps score, and comments on it.

Every few minutes the AI reads the chat's vibe (persona.py); it also reports real arguments: who against whom,
who's winning, what about. An argument after a quiet gap of ROUND_GAP is a new round. From round 2 on SYKE may
say something about it ("round 4 of Mo vs Zara 🍿"), only in channels where he yaps, and not too often.
`beef` shows the scoreboard; `beef @a @b` or `beef @user` shows one rivalry.
"""

from __future__ import annotations

import contextlib
import logging
import random
import time

import discord
from discord.ext import commands

from . import ai

log = logging.getLogger("syke.rivalry")

ROUND_GAP = 30 * 60             # a quiet half hour between arguments makes the next one a new round
COMMENT_GAP_PAIR = 45 * 60
COMMENT_GAP_CHANNEL = 10 * 60
HEAT_HALF_LIFE = 7 * 86400

COMMENT_PROMPT = """You are SYKE, a Discord bot with a personality. Two members are at it again: {a} vs {b}, round \
{round}. Score so far: {a} {aw}, {b} {bw}. This round is about: {topic}. {lean} Comment in ONE short line like a \
sports commentator or a nosy friend with popcorn (max 18 words, lowercase is fine, at most one emoji). Your mood: \
{mood}. Don't take sides on anything serious, no slurs, no @mentions.

Reply with JSON only: {{"line": "..."}}"""

OFFLINE_LINES = ["round {round} of {a} vs {b} 🍿", "{a} and {b} again?? round {round}, here we go",
                 "{a} vs {b}, round {round}. {lead}", "somebody get {a} and {b} a room. round {round}"]


def pair(x: int, y: int) -> tuple[int, int]:
    return (x, y) if x < y else (y, x)


def match_name(name: str, speakers: dict[str, int]) -> int | None:
    """The speaker id for a name the AI wrote (exact, then case-insensitive, then prefix)."""
    if not name:
        return None
    if name in speakers:
        return speakers[name]
    key = name.strip().lstrip("@").lower()
    for spoken, uid in speakers.items():
        if spoken.lower() == key:
            return uid
    for spoken, uid in speakers.items():
        if len(key) >= 3 and (spoken.lower().startswith(key) or key.startswith(spoken.lower())):
            return uid
    return None


class Rivalry(commands.Cog, name="Rivalry"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._said_pair: dict[tuple, float] = {}
        self._said_channel: dict[int, float] = {}

    @property
    def storage(self):
        return self.bot.storage

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    def get(self, guild_id: int, x: int, y: int) -> tuple | None:
        a, b = pair(x, y)
        rows = self.storage._read("SELECT a_id, b_id, rounds, a_wins, b_wins, heat, topic, channel_id, started, "
                                  "last_at FROM rivalries WHERE guild_id = ? AND a_id = ? AND b_id = ?", (guild_id, a, b))
        return rows[0] if rows else None

    async def record(self, guild_id: int, arguments: list, window: list[tuple]) -> None:
        """Arguments from a vibe read. window: the chat lines it read, (at, channel, name, text, user_id)."""
        me = getattr(self.bot.user, "id", None)
        speakers = {name: uid for _, _, name, _, uid in window if uid and uid != me and name != "SYKE"}
        for item in arguments[:3]:
            if not isinstance(item, dict):
                continue
            x, y = match_name(str(item.get("a", "")), speakers), match_name(str(item.get("b", "")), speakers)
            if not x or not y or x == y:
                continue
            said = {uid for _, _, _, _, uid in window}
            if x not in said or y not in said:
                continue
            winner = match_name(str(item.get("winner", "")), speakers) if str(item.get("winner", "")).lower() \
                not in ("", "none", "even", "tie") else None
            winner = winner if winner in (x, y) else None
            heat = max(1, min(3, int(item.get("heat") or 1))) if str(item.get("heat", "1")).isdigit() else 1
            topic = ai.public_text(item.get("topic"), 80)
            channel = next((cid for _, cid, _, _, uid in reversed(window) if uid in (x, y)), 0)
            rounds = self.note_round(guild_id, x, y, winner, topic, heat, channel)
            if rounds:
                await self.maybe_comment(guild_id, x, y, rounds, channel, topic, winner)

    def note_round(self, guild_id: int, x: int, y: int, winner: int | None, topic: str, heat: int,
                   channel: int, now: float | None = None) -> int:
        """Count an argument. Returns the round number when a NEW round started, else 0."""
        now = now or time.time()
        a, b = pair(x, y)
        row = self.get(guild_id, a, b)
        if row is None:
            self.storage._write(
                "INSERT INTO rivalries (guild_id, a_id, b_id, rounds, a_wins, b_wins, heat, topic, channel_id, started, "
                "last_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, a, b, int(winner == a), int(winner == b), heat, topic, channel, now, now))
            self._add_round(guild_id, a, b, now, winner, topic, heat)
            return 1
        _, _, rounds, a_wins, b_wins, old_heat, _, _, _, last_at = row
        new_round = now - last_at > ROUND_GAP
        if new_round:
            rounds += 1
            a_wins += int(winner == a)
            b_wins += int(winner == b)
            self._add_round(guild_id, a, b, now, winner, topic, heat)
        else:   # same argument still going: the latest verdict on who's winning stands
            last = self.storage._read("SELECT winner FROM rivalry_rounds WHERE guild_id = ? AND a_id = ? AND b_id = ? "
                                      "ORDER BY at DESC LIMIT 1", (guild_id, a, b))
            previous = last[0][0] if last else 0
            if winner and winner != previous:
                a_wins += int(winner == a) - int(previous == a)
                b_wins += int(winner == b) - int(previous == b)
                self.storage._write("UPDATE rivalry_rounds SET winner = ?, topic = ? WHERE guild_id = ? AND a_id = ? "
                                    "AND b_id = ? AND at = (SELECT MAX(at) FROM rivalry_rounds WHERE guild_id = ? "
                                    "AND a_id = ? AND b_id = ?)", (winner, topic or "", guild_id, a, b, guild_id, a, b))
        faded = old_heat * 0.5 ** ((now - last_at) / HEAT_HALF_LIFE)
        self.storage._write("UPDATE rivalries SET rounds = ?, a_wins = ?, b_wins = ?, heat = ?, topic = ?, "
                            "channel_id = ?, last_at = ? WHERE guild_id = ? AND a_id = ? AND b_id = ?",
                            (rounds, max(0, a_wins), max(0, b_wins), faded + (heat if new_round else 0),
                             topic or "", channel, now, guild_id, a, b))
        return rounds if new_round else 0

    def _add_round(self, guild_id, a, b, at, winner, topic, heat) -> None:
        self.storage._write("INSERT INTO rivalry_rounds (guild_id, a_id, b_id, at, winner, topic, heat) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)", (guild_id, a, b, at, winner or 0, topic or "", heat))

    async def maybe_comment(self, guild_id, x, y, rounds, channel_id, topic, winner) -> None:
        """From round 2 on, SYKE chimes in (yap channels only, not too often)."""
        if rounds < 2 or not channel_id:
            return
        if not self.storage.get_config(guild_id, "rivalry").get("comments", True):
            return
        if channel_id not in self.storage.yap_channels(guild_id):
            return
        now, key = time.time(), (guild_id,) + pair(x, y)
        if now - self._said_pair.get(key, 0) < COMMENT_GAP_PAIR or \
                now - self._said_channel.get(channel_id, 0) < COMMENT_GAP_CHANNEL:
            return
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if channel is None:
            return
        self._said_pair[key], self._said_channel[channel_id] = now, now
        line = await self.comment_line(guild, x, y, rounds, topic, winner)
        with contextlib.suppress(discord.HTTPException):
            sent = await channel.send(line, allowed_mentions=discord.AllowedMentions.none())
            with contextlib.suppress(Exception):
                self.bot.remember_yap(channel_id, sent.content)

    def names(self, guild, *ids) -> list[str]:
        return [getattr(guild.get_member(i), "display_name", "someone") for i in ids]

    async def comment_line(self, guild, x, y, rounds, topic, winner) -> str:
        row = self.get(guild.id, x, y)
        a, b = row[0], row[1]
        a_name, b_name = self.names(guild, a, b)
        aw, bw = row[3], row[4]
        lead = (f"{a_name} leads {aw}-{bw}" if aw > bw else f"{b_name} leads {bw}-{aw}" if bw > aw else f"tied {aw}-{bw}")
        if self.bot.settings.ai_provider != "none":
            persona = self.bot.get_cog("Persona")
            mood = persona.mood_now(guild.id)[0] if persona else "chill"
            winning = self.names(guild, winner)[0] if winner else ""
            with contextlib.suppress(Exception):
                data = await ai._complete(self.bot.settings, COMMENT_PROMPT.format(
                    a=a_name, b=b_name, round=rounds, aw=aw, bw=bw, topic=topic or "who knows",
                    lean=f"{winning} is winning this one." if winning else "", mood=mood), "Comment.", 1.0, 12)
                line = ai.public_text(data.get("line"), 200)
                if line:
                    return line
        return random.choice(OFFLINE_LINES).format(a=a_name, b=b_name, round=rounds, lead=lead)

    # Commands

    @commands.command(name="beef", aliases=["rivalry", "rivalries", "beefs", "rivals"],
                      help="Rivalries SYKE noticed: beef, beef @user, beef @a @b")
    @commands.guild_only()
    async def beef(self, ctx: commands.Context, first: discord.Member | None = None,
                   second: discord.Member | None = None) -> None:
        gid = ctx.guild.id
        if first and second:
            await self.show_pair(ctx, first, second)
            return
        now = time.time()
        if first:
            rows = self.storage._read("SELECT a_id, b_id, rounds, a_wins, b_wins, heat, topic, last_at FROM rivalries "
                                      "WHERE guild_id = ? AND (a_id = ? OR b_id = ?) ORDER BY rounds DESC LIMIT 10",
                                      (gid, first.id, first.id))
            title = f"🥊 {first.display_name}'s rivalries"
        else:
            rows = self.storage._read("SELECT a_id, b_id, rounds, a_wins, b_wins, heat, topic, last_at FROM rivalries "
                                      "WHERE guild_id = ? ORDER BY rounds DESC, last_at DESC LIMIT 25", (gid,))
            rows.sort(key=lambda r: -(r[2] + r[5] * 0.5 ** ((now - r[7]) / HEAT_HALF_LIFE)))
            rows = rows[:10]
            title = "🥊 Beef board"
        if not rows:
            await ctx.reply(embed=discord.Embed(
                title=title, colour=discord.Colour.red(),
                description="No beef yet. Everyone's getting along. Suspicious."), mention_author=False)
            return
        lines = []
        for i, (a, b, rounds, aw, bw, heat, topic, last_at) in enumerate(rows, 1):
            a_name, b_name = (discord.utils.escape_markdown(n) for n in self.names(ctx.guild, a, b))
            fire = "🔥" * max(1, min(3, round(heat * 0.5 ** ((now - last_at) / HEAT_HALF_LIFE))))
            score = f"**{aw}–{bw}**"
            about = f" · {discord.utils.escape_markdown(topic)}" if topic else ""
            lines.append(f"`{i:>2}.` **{a_name}** vs **{b_name}** · {rounds} round{'s' if rounds != 1 else ''} · "
                         f"{score} {fire}\n-# last: <t:{int(last_at)}:R>{about}")
        card = discord.Embed(title=title, description="\n".join(lines), colour=discord.Colour.red())
        card.set_footer(text=f"{self.p(ctx)}beef @a @b for the full history")
        await ctx.reply(embed=card, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    async def show_pair(self, ctx, first, second) -> None:
        row = self.get(ctx.guild.id, first.id, second.id)
        if row is None:
            await ctx.reply(f"No beef between **{first.display_name}** and **{second.display_name}**. Yet.",
                            mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return
        a, b, rounds, aw, bw, heat, topic, channel, started, last_at = row
        a_name, b_name = self.names(ctx.guild, a, b)
        history = self.storage._read("SELECT at, winner, topic FROM rivalry_rounds WHERE guild_id = ? AND a_id = ? "
                                     "AND b_id = ? ORDER BY at DESC LIMIT 10", (ctx.guild.id, a, b))
        lines = []
        for at, winner, about in history:
            who = self.names(ctx.guild, winner)[0] if winner else "even"
            lines.append(f"<t:{int(at)}:d> · {discord.utils.escape_markdown(about or 'something')} · 🏆 {who}")
        leader = a_name if aw > bw else b_name if bw > aw else None
        card = discord.Embed(title=f"🥊 {a_name} vs {b_name}",
                             description=f"**{rounds}** rounds since <t:{int(started)}:D> · score **{aw}–{bw}**"
                                         + (f" · {leader} leads" if leader else " · dead even")
                                         + ("\n\n**Recent rounds**\n" + "\n".join(lines) if lines else ""),
                             colour=discord.Colour.red())
        await ctx.reply(embed=card, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="beefcomments", hidden=True, help="Managers: beefcomments on|off")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def beef_comments(self, ctx: commands.Context, setting: str) -> None:
        on = setting.lower() in ("on", "yes", "true", "1")
        self.storage.update_config(ctx.guild.id, "rivalry", comments=on)
        await ctx.reply(f"SYKE's rivalry comments are **{'on' if on else 'off'}**.", mention_author=False)
