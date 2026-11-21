"""Slur counter: how often each member has used racist slurs, and who uses them most.

`racist [@user]` shows someone's count with a breakdown; `racist top` ranks the server. Counts come
from the messages SYKE stores from watched channels (opted-out members are skipped), and catch common
spellings (plurals, -a/-er endings, l33t letters like n1gga). Words are always shown censored.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter

import discord
from discord.ext import commands

from .moderation import embed, problem

CACHE_SECONDS = 600
TOP = 10
RED = discord.Colour.from_str("#DC2626")

# label (shown censored) -> pattern for the word and its common spellings
_I = r"[i1!|l]"
_G = r"[g9q]"
_E = r"[e3]"
_A = r"[a4@]"
SLURS: dict[str, str] = {
    "n-word": rf"n+{_I}+{_G}{_G}+(?:{_A}+h*|{_E}+r+|{_A}+r+|uh+|{_A}+z+|u+h*)s*z?",
    "paki": rf"p{_A}k{_I}s?",
    "chink": rf"ch{_I}nks?",
    "gook": r"g[o0]{2}ks?",
    "spic": rf"sp{_I}cs?",
    "wetback": rf"w{_E}tb{_A}cks?",
    "beaner": rf"b{_E}{_A}n{_E}rs?",
    "kike": rf"k{_I}k{_E}s?",
    "raghead": rf"r{_A}gh{_E}{_A}ds?",
    "towelhead": rf"t[o0]w{_E}lh{_E}{_A}ds?",
    "sand n-word": rf"s{_A}nd\s*n{_I}{_G}{_G}+{_E}rs?",
    "zipperhead": rf"z{_I}pp{_E}rh{_E}{_A}ds?",
    "jigaboo": rf"j{_I}g{_A}b[o0]{{2}}s?",
    "porch monkey": r"porch\s*monk[e3]ys?",
    "coolie": r"c[o0]{2}l[i1][e3]s?",
    "kanake": rf"k{_A}n{_A}k{_E}n?",
    "neger": rf"n{_E}g{_E}r(?:n|in|innen)?",
    "zigeuner": rf"z{_I}g{_E}un{_E}r",
}
_COMPILED = {label: re.compile(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", re.I) for label, pattern in SLURS.items()}
# Cheap SQL pre-filter, so only likely messages are read in Python.
_LIKE = ["nig", "n1g", "niq", "n!g", "nlg", "pak", "p4k", "p@k", "chink", "ch1nk", "gook", "g00k", "spic", "sp1c",
         "wetback", "beaner", "kike", "k1ke", "raghead", "towelhead", "zipperhead", "jigaboo", "porch",
         "coolie", "kanake", "neger", "zigeuner"]


def censor(label: str) -> str:
    """"n-word" stays as is; everything else keeps its first letter: p***."""
    if label.endswith("n-word"):
        return label
    return " ".join(w[0] + "*" * (len(w) - 1) for w in label.split())


def count_slurs(text: str) -> Counter:
    found: Counter = Counter()
    if not text:
        return found
    for label, pattern in _COMPILED.items():
        hits = len(pattern.findall(text))
        if hits:
            found[label] += hits
    if found.get("sand n-word"):  # "sand n…" isn't also counted as a plain n-word
        found["n-word"] = max(0, found["n-word"] - found["sand n-word"])
        if not found["n-word"]:
            del found["n-word"]
    return found


def tally(rows: list[tuple], optouts: set[int], is_command) -> dict[int, dict]:
    """{author_id: {"total", "words": Counter, "name", "first", "last"}} from (author_id, author_name, content, created_at)."""
    people: dict[int, dict] = {}
    for author_id, name, content, created in rows:
        if author_id in optouts or is_command(content or ""):
            continue
        found = count_slurs(content)
        if not found:
            continue
        person = people.setdefault(author_id, {"total": 0, "words": Counter(), "name": name or "", "first": None,
                                               "last": None})
        person["total"] += sum(found.values())
        person["words"].update(found)
        person["name"] = name or person["name"]
        if created:
            person["first"] = created if person["first"] is None else min(person["first"], created)
            person["last"] = created if person["last"] is None else max(person["last"], created)
    return people


def breakdown(words: Counter, limit: int = 4) -> str:
    return ", ".join(f"{censor(w)} ×{n}" for w, n in words.most_common(limit))


class SlurCount(commands.Cog, name="SlurCount"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._cache: dict[int, tuple[float, dict]] = {}

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    def _rows(self, guild_id: int) -> list[tuple]:
        likes = " OR ".join("lower(content) LIKE ?" for _ in _LIKE)
        return self.bot.storage._read(
            f"SELECT author_id, author_name, content, created_at FROM corpus WHERE guild_id = ? AND ({likes})",
            (guild_id, *[f"%{w}%" for w in _LIKE]))

    async def counts(self, guild_id: int) -> dict[int, dict]:
        cached = self._cache.get(guild_id)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            return cached[1]
        rows = await asyncio.to_thread(self._rows, guild_id)
        optouts = self.bot.storage.optouts(guild_id)
        people = await asyncio.to_thread(tally, rows, optouts, lambda text: self.bot.is_command_text(guild_id, text))
        self._cache[guild_id] = (time.time(), people)
        return people

    @commands.group(name="racist", aliases=["slurs", "slurcount", "racism"], invoke_without_command=True,
                    case_insensitive=True,
                    help="How many racist slurs someone has used: racist [@user], or racist top for the ranking")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.member)
    async def racist(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        who = member or ctx.author
        if who.bot:
            await ctx.reply(embed=problem("Bots don't count", "Pick a person."))
            return
        if who.id in self.bot.storage.optouts(ctx.guild.id):
            await ctx.reply(embed=problem("Off limits", f"{who.display_name} has opted out of SYKE."))
            return
        async with ctx.typing():
            people = await self.counts(ctx.guild.id)
        person = people.get(who.id)
        if not person:
            await ctx.reply(embed=embed(f"🧼 {who.display_name}: clean", "No racist slurs in the messages SYKE has "
                                                                        "stored for them.", discord.Colour.green()))
            return
        ranked = sorted(people.items(), key=lambda kv: -kv[1]["total"])
        place = next(i for i, (uid, _) in enumerate(ranked, 1) if uid == who.id)
        card = embed(f"🚨 {who.display_name}'s slur count", f"**{person['total']:,}** racist slur"
                                                           f"{'s' if person['total'] != 1 else ''}", RED)
        card.add_field(name="Most used", value=breakdown(person["words"]) or "—", inline=False)
        card.add_field(name="Rank", value=f"#{place} of {len(ranked)} in this server")
        if person["first"]:
            card.add_field(name="Since", value=f"<t:{int(person['first'])}:D> · last <t:{int(person['last'])}:R>")
        card.set_thumbnail(url=who.display_avatar.url)
        card.set_footer(text=f"From stored messages in watched channels · {self.p(ctx)}racist top for the ranking")
        await ctx.reply(embed=card, allowed_mentions=discord.AllowedMentions.none())

    @racist.command(name="top", aliases=["leaderboard", "lb", "rank", "ranking"])
    async def racist_top(self, ctx: commands.Context) -> None:
        async with ctx.typing():
            people = await self.counts(ctx.guild.id)
        ranked = [(uid, p) for uid, p in sorted(people.items(), key=lambda kv: -kv[1]["total"])
                  if ctx.guild.get_member(uid) is not None][:TOP]
        if not ranked:
            await ctx.reply(embed=embed("🧼 Nobody", "No racist slurs in the messages SYKE has stored here.",
                                        discord.Colour.green()))
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> · **{p['total']:,}** · {breakdown(p['words'], 2)}"
                 for i, (uid, p) in enumerate(ranked)]
        total = sum(p["total"] for p in people.values())
        card = embed("🚨 Most racist in the server", "\n".join(lines), RED)
        card.set_footer(text=f"{total:,} slurs from {len(people)} people · stored messages in watched channels")
        await ctx.reply(embed=card, allowed_mentions=discord.AllowedMentions.none())
