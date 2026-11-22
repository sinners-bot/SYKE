"""`lore`: the server's inside jokes and catchphrases, mined from the messages SYKE stores.

A phrase counts as lore when it keeps coming back: several people use it, across several weeks, and
messages carrying it get more reactions than the server's average. SYKE finds who said it first and when,
and (with AI on) explains the joke from real examples. `lore <phrase>` tells one phrase's story.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import time
from collections import Counter
from datetime import datetime, timezone

import discord
from discord.ext import commands

from . import ai
from .lexicon import CHAT_SLANG, LAUGH_WORDS, STOPWORDS
from .moderation import embed, problem
from .slurs import count_slurs

SCAN_LIMIT = 150_000        # newest messages read per server
MIN_USES = 6
MAX_N = 3
PRUNE_EVERY = 20_000
CACHE_SECONDS = 6 * 3600
SHOW = 8

_STRIP = re.compile(r"https?://\S+|<a?:\w+:\d+>|<[@#&!]+\d+>|`[^`]*`")
_WORD = re.compile(r"[a-z0-9']+")
_BORING = STOPWORDS | CHAT_SLANG | LAUGH_WORDS


def tokens(text: str) -> list[str]:
    words = _WORD.findall(_STRIP.sub(" ", (text or "").lower()))
    return [w.strip("'") for w in words[:60] if w.strip("'")]


def grams(words: list[str]) -> set[str]:
    """Every 1-3 word phrase worth considering in one message (each counted once per message)."""
    found = set()
    edge = [w not in STOPWORDS for w in words]                 # may a phrase start/end here?
    keen = [w not in _BORING and not w.isdigit() for w in words]  # does this word carry meaning?
    count = len(words)
    for i in range(count):
        if not edge[i]:
            continue
        if keen[i] and len(words[i]) >= 4:
            found.add(words[i])
        for n in range(2, MAX_N + 1):
            j = i + n - 1
            if j >= count:
                break
            if edge[j] and any(keen[i:j + 1]):
                found.add(" ".join(words[i:j + 1]))
    return found


def _week(ts: float) -> int:
    return int(ts // (7 * 86400))


def mine(rows, *, is_command=lambda t: False, optouts=frozenset(), limit: int = 40) -> list[dict]:
    """rows: (message_id, channel_id, author_id, author_name, content, created_at, total_reactions), oldest first.

    Returns the best lore candidates with their stats, best first.
    """
    usable = [r for r in rows if r[2] not in optouts and r[4] and r[5] and not is_command(r[4])]
    if not usable:
        return []
    # Pass 1: how many messages carry each phrase. Phrases seen once get pruned as we go to cap memory.
    counts: Counter = Counter()
    for i, row in enumerate(usable, 1):
        counts.update(grams(tokens(row[4])))
        if i % PRUNE_EVERY == 0:
            for key in [k for k, v in counts.items() if v <= 1]:
                del counts[key]
    wanted = {k for k, v in counts.items() if v >= MIN_USES}
    if not wanted:
        return []
    base = sum(r[6] or 0 for r in usable) / len(usable)
    # Pass 2: who, when and how well it landed, for the survivors only.
    stats: dict[str, dict] = {}
    for row in usable:
        hits = grams(tokens(row[4])) & wanted
        for phrase in hits:
            s = stats.get(phrase)
            if s is None:
                s = stats[phrase] = {"phrase": phrase, "uses": 0, "authors": Counter(), "names": {}, "weeks": set(),
                                     "reactions": 0, "first": row, "best": row, "last": row[5]}
            s["uses"] += 1
            s["authors"][row[2]] += 1
            s["names"][row[2]] = row[3] or s["names"].get(row[2]) or "someone"
            s["weeks"].add(_week(row[5]))
            s["reactions"] += row[6] or 0
            s["last"] = row[5]
            if (row[6] or 0) > (s["best"][6] or 0):
                s["best"] = row
    found = []
    for s in stats.values():
        n = len(s["phrase"].split())
        authors, weeks = len(s["authors"]), len(s["weeks"])
        if authors < 2 or weeks < 2 or count_slurs(s["phrase"]):
            continue
        lift = (s["reactions"] / s["uses"] + 0.3) / (base + 0.3)
        if lift < (1.6 if n == 1 else 1.15):
            continue
        top_id, top_uses = s["authors"].most_common(1)[0]
        s.update(lift=lift, people=authors, week_count=weeks, owner=top_id if top_uses / s["uses"] >= 0.6 else None,
                 score=math.log1p(s["uses"]) * math.log1p(authors) * min(lift, 6) * math.log1p(weeks)
                 * (1.3 if n > 1 else 1.0))
        found.append(s)
    found.sort(key=lambda s: -s["score"])
    return _dedupe(found[:limit * 4])[:limit]


def _dedupe(found: list[dict]) -> list[dict]:
    """'big chungus' and 'chungus' are one joke: keep the form people actually use."""
    dropped: set[str] = set()
    for a in found:
        for b in found:
            pa, pb = a["phrase"], b["phrase"]
            if pa == pb or pa in dropped or pb in dropped or f" {pa} " not in f" {pb} ":
                continue
            dropped.add(pa if b["uses"] >= 0.6 * a["uses"] else pb)
    return [s for s in found if s["phrase"] not in dropped]


def day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y")


def offline_story(s: dict) -> str:
    first = s["first"]
    who = s["names"].get(s["owner"]) if s["owner"] else None
    head = f"{who}'s catchphrase. " if who else ""
    return (f"{head}First said by **{first[3] or 'someone'}** on {day(first[5])}. Since then **{s['people']}** people "
            f"have used it **{s['uses']}** times over {s['week_count']} weeks.")


LORE_PROMPT = """You are SYKE, the server's resident historian. Below are phrases that keep coming back in a Discord \
server, each with real messages that use it (first one first). Pick the ones that are genuinely this server's \
inside jokes, running bits, nicknames or catchphrases, and skip anything that is just ordinary chat (common words, \
generic slang, names of games or celebrities used normally).

For each pick, explain the joke in 1-2 sentences from the messages: what it means here, how it started if the first \
message shows it, and how people use it now. Be specific and funny, refer to people by name. Never invent events \
that the messages don't support.

Reply with JSON only: {"lore": [{"phrase": "<exactly as given>", "title": "<2-5 word name for the bit>", \
"emoji": "<one emoji>", "story": "<1-2 sentences>"}]}. Pick at most 8, best first."""


def prompt_for(found: list[dict], examples: dict[str, list[tuple]]) -> str:
    parts = []
    for s in found:
        lines = [f'PHRASE "{s["phrase"]}": {s["uses"]} uses by {s["people"]} people since {day(s["first"][5])}']
        for row in examples.get(s["phrase"], []):
            lines.append(f"  {day(row[5])} {(row[3] or '?')[:20]}: {' '.join(row[4].split())[:180]}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


class Lore(commands.Cog, name="Lore"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._cache: dict[int, tuple[float, int, list[dict], dict]] = {}

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    def _rows(self, guild_id: int) -> list[tuple]:
        rows = self.bot.storage._read(
            "SELECT message_id, channel_id, author_id, author_name, content, created_at, total_reactions FROM corpus "
            "WHERE guild_id = ? AND created_at IS NOT NULL ORDER BY created_at DESC LIMIT ?", (guild_id, SCAN_LIMIT))
        rows.reverse()
        return rows

    async def candidates(self, guild_id: int) -> list[dict]:
        version = getattr(self.bot, "_history_version", {}).get(guild_id, 0)
        hit = self._cache.get(guild_id)
        if hit and hit[1] == version and time.time() - hit[0] < CACHE_SECONDS:
            return hit[2]
        rows = await asyncio.to_thread(self._rows, guild_id)
        optouts = frozenset(self.bot.storage.optouts(guild_id))
        found = await asyncio.to_thread(mine, rows, is_command=lambda t: self.bot.is_command_text(guild_id, t),
                                        optouts=optouts)
        self._cache[guild_id] = (time.time(), version, found, {})
        return found

    def examples(self, guild_id: int, phrase: str, s: dict, n: int = 4) -> list[tuple]:
        """The first use, then the best-received ones."""
        like = f"%{phrase}%"
        best = self.bot.storage._read(
            "SELECT message_id, channel_id, author_id, author_name, content, created_at, total_reactions FROM corpus "
            "WHERE guild_id = ? AND lower(content) LIKE ? ORDER BY total_reactions DESC LIMIT ?",
            (guild_id, like, n * 3))
        optouts = self.bot.storage.optouts(guild_id)
        picked = [s["first"]]
        for row in best:
            if row[0] != s["first"][0] and row[2] not in optouts and f" {phrase} " in f" {' '.join(tokens(row[4]))} ":
                picked.append(row)
            if len(picked) >= n:
                break
        return picked

    async def explain(self, guild_id: int, found: list[dict]) -> dict[str, dict]:
        hit = self._cache.get(guild_id)
        if hit and hit[3]:
            return hit[3]
        if self.bot.settings.ai_provider == "none":
            return {}
        shortlist = found[:20]
        examples = {s["phrase"]: await asyncio.to_thread(self.examples, guild_id, s["phrase"], s, 4) for s in shortlist}
        stories: dict[str, dict] = {}
        with contextlib.suppress(Exception):
            data = await ai._complete(self.bot.settings, LORE_PROMPT, prompt_for(shortlist, examples), 0.7, 40)
            for item in data.get("lore") or []:
                phrase = str(item.get("phrase", "")).lower().strip()
                if any(s["phrase"] == phrase for s in shortlist):
                    stories[phrase] = item
        if hit and stories:
            self._cache[guild_id] = (*hit[:3], stories)
        return stories

    def link(self, guild_id: int, row: tuple) -> str:
        return f"https://discord.com/channels/{guild_id}/{row[1]}/{row[0]}"

    async def lore(self, ctx: commands.Context, phrase: str = "") -> None:
        guild = ctx.guild
        async with ctx.typing():
            if phrase.strip():
                await self.one(ctx, phrase.strip().lower())
                return
            found = await self.candidates(guild.id)
            if not found:
                await ctx.reply(embed=problem(
                    "No lore yet", f"SYKE needs more history to find running jokes. Try `{self.p(ctx)}collect since 1y`."))
                return
            stories = await self.explain(guild.id, found)
        if stories:
            picks = [s for s in found[:20] if s["phrase"] in stories]
            picks.sort(key=lambda s: list(stories).index(s["phrase"]))
        else:
            picks = found
        picks = picks[:SHOW]
        card = embed(f"📜 The lore of {guild.name}",
                     "The running jokes and catchphrases that keep coming back here.")
        for s in picks:
            story = stories.get(s["phrase"], {})
            icon = ai.public_text(story.get("emoji"), 8) or "🔁"
            title = ai.public_text(story.get("title"), 60)
            name = f"{icon} “{s['phrase']}”" + (f" · {title}" if title else "")
            text = ai.public_text(story.get("story"), 300) if story else offline_story(s)
            first = s["first"]
            text += (f"\n-# started by {first[3] or 'someone'}, [{day(first[5])}]({self.link(guild.id, first)}) · "
                     f"{s['uses']} uses · {s['people']} people")
            card.add_field(name=name[:256], value=text[:1024], inline=False)
        card.set_footer(text=f"{self.p(ctx)}lore <phrase> for one phrase's full story"
                        + ("" if stories else " · offline read"))
        await ctx.reply(embed=card, allowed_mentions=discord.AllowedMentions.none())

    async def one(self, ctx: commands.Context, phrase: str) -> None:
        guild = ctx.guild
        words = " ".join(tokens(phrase))
        if not words:
            await ctx.reply(embed=problem("Which phrase?", f"`{self.p(ctx)}lore big chungus`"))
            return

        def load():
            rows = self.bot.storage._read(
                "SELECT message_id, channel_id, author_id, author_name, content, created_at, total_reactions "
                "FROM corpus WHERE guild_id = ? AND created_at IS NOT NULL AND lower(content) LIKE ? "
                "ORDER BY created_at", (guild.id, f"%{words}%"))
            optouts = self.bot.storage.optouts(guild.id)
            return [r for r in rows if r[2] not in optouts and not self.bot.is_command_text(guild.id, r[4])
                    and f" {words} " in f" {' '.join(tokens(r[4]))} "]

        rows = await asyncio.to_thread(load)
        if not rows:
            await ctx.reply(embed=problem("Never said here", f"Nobody has said “{words}” in the messages SYKE has."))
            return
        first, best = rows[0], max(rows, key=lambda r: r[6] or 0)
        people = Counter(r[2] for r in rows)
        names = {r[2]: r[3] or "someone" for r in rows}
        months = Counter(datetime.fromtimestamp(r[5], timezone.utc).strftime("%Y-%m") for r in rows)
        peak, peak_n = months.most_common(1)[0]
        top = ", ".join(f"{names[uid]} ×{n}" for uid, n in people.most_common(5))
        quote = " ".join(first[4].split())[:200]
        body = (f"**First said** by **{first[3] or 'someone'}** on [{day(first[5])}]({self.link(guild.id, first)}):\n"
                f"> {discord.utils.escape_markdown(quote)}\n\n"
                f"Used **{len(rows)}** times by **{len(people)}** people. Last seen {day(rows[-1][5])}.\n"
                f"Peak month: **{datetime.strptime(peak, '%Y-%m').strftime('%B %Y')}** ({peak_n} uses)\n"
                f"Biggest users: {top}")
        if (best[6] or 0) > 0 and best[0] != first[0]:
            body += (f"\nBest received: [{best[3] or 'someone'}, {best[6]} reactions]({self.link(guild.id, best)})")
        await ctx.reply(embed=embed(f"📜 “{words}”", body[:4000]), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="lore", aliases=["insidejokes", "catchphrases", "lorebook"],
                      help="The server's inside jokes and catchphrases: lore, or lore <phrase> for one phrase's story")
    @commands.guild_only()
    @commands.cooldown(1, 20, commands.BucketType.guild)
    async def lore_cmd(self, ctx: commands.Context, *, phrase: str = "") -> None:
        await self.lore(ctx, phrase)
