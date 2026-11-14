"""Silly text-mangling toys: !drunk, !scramble, !zalgo, !uwu, !mock, !keysmash.

Each takes text directly or works on the message you're replying to. All of the
transforms are pure functions (with an optional rng) so they're easy to test,
and every command strips mentions and posts with pings disabled so a mangled
message can never ping anyone.
"""

from __future__ import annotations

import random
import re

import discord
from discord import app_commands
from discord.ext import commands

from .expressions import notice

MAX_IN = 400       # cap input so outputs stay sane
MAX_OUT = 1800     # Discord-safe output cap
_MENTION = re.compile(r"<@[!&]?\d+>|<#\d+>")
_HOME_KEYS = "asdfghjkl"
# Combining marks for zalgo (above + below).
_ZALGO = [chr(c) for c in list(range(0x0300, 0x0315)) + list(range(0x0316, 0x0340))]


def clean_source(text: str) -> str:
    """Trim, drop raw mentions, and neutralise @everyone/@here before mangling."""
    text = (text or "")[:MAX_IN]
    text = _MENTION.sub("", text)
    text = text.replace("@everyone", "everyone").replace("@here", "here")
    return text.strip()


def mock(text: str) -> str:
    """aLtErNaTiNg caps, SpongeBob style."""
    out, up = [], False
    for ch in text:
        if ch.isalpha():
            out.append(ch.upper() if up else ch.lower())
            up = not up
        else:
            out.append(ch)
    return "".join(out)


def uwu(text: str, rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    t = text.replace("ove", "uv")
    t = re.sub(r"[rl]", "w", t)
    t = re.sub(r"[RL]", "W", t)
    t = re.sub(r"n([aeiou])", r"ny\1", t)
    t = re.sub(r"N([aeiouAEIOU])", r"Ny\1", t)
    words = []
    for w in t.split(" "):
        if w[:1].isalpha() and rng.random() < 0.12:
            w = f"{w[0]}-{w}"   # st-stutter
        words.append(w)
    faces = [" uwu", " owo", " >w<", " :3", " ^w^", " (◕ᴥ◕)"]
    return " ".join(words) + rng.choice(faces)


def _scramble_word(w: str, rng: random.Random) -> str:
    if len(w) <= 3:
        return w
    mid = list(w[1:-1])
    rng.shuffle(mid)
    return w[0] + "".join(mid) + w[-1]


def scramble(text: str, rng: random.Random | None = None) -> str:
    """Keep each word's first and last letter, shuffle the middle (typoglycemia)."""
    rng = rng or random.Random()
    return re.sub(r"[A-Za-z]+", lambda m: _scramble_word(m.group(0), rng), text)


def drunk(text: str, rng: random.Random | None = None) -> str:
    """Slur the text: doubled letters, dropped letters, random caps, a *hic* or two."""
    rng = rng or random.Random()
    out = []
    for ch in text:
        if not ch.isalpha():
            out.append(ch)
            continue
        r = rng.random()
        if r < 0.09:
            out.append(ch + ch)         # double it
        elif r < 0.15:
            continue                    # drop it
        elif r < 0.22:
            out.append(ch.upper() if ch.islower() else ch.lower())
        else:
            out.append(ch)
    s = "".join(out)
    if rng.random() < 0.6:
        s += rng.choice([" *hic*", " hic", "...", " sh", " 🍺"])
    return s


def zalgo(text: str, intensity: int = 3, rng: random.Random | None = None) -> str:
    """C̸u̷r̶s̵e̴d̷ glitch text via combining diacritics."""
    rng = rng or random.Random()
    out = []
    for ch in text:
        out.append(ch)
        if ch.strip():
            for _ in range(rng.randint(0, intensity)):
                out.append(rng.choice(_ZALGO))
    return "".join(out)


def keysmash(text: str, rng: random.Random | None = None) -> str:
    """Letters progressively collapse into keyboard mash the further in you go."""
    rng = rng or random.Random()
    n = max(len(text), 1)
    out = []
    for i, ch in enumerate(text):
        if ch.isalpha() and rng.random() < i / n:
            out.append(rng.choice(_HOME_KEYS))
        else:
            out.append(ch)
    tail = "".join(rng.choice(_HOME_KEYS) for _ in range(rng.randint(3, 10)))
    return f"{''.join(out)} {tail}"


class TextFun(commands.Cog, name="TextFun"):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def _source(self, ctx: commands.Context, text: str) -> str | None:
        """The text to mangle: what they typed, or the message they replied to."""
        text = clean_source(text)
        if text:
            return text
        ref = getattr(ctx.message, "reference", None)
        if ref is not None:
            msg = ref.resolved if isinstance(ref.resolved, discord.Message) else getattr(ref, "cached_message", None)
            if msg is None and getattr(ref, "message_id", None):
                try:
                    msg = await ctx.channel.fetch_message(ref.message_id)
                except discord.HTTPException:
                    msg = None
            if msg is not None and msg.content:
                return clean_source(msg.content)
        return None

    async def _run(self, ctx: commands.Context, text: str, fn) -> None:
        src = await self._source(ctx, text)
        if not src:
            await ctx.reply(embed=notice("Give me something",
                                         "Reply to a message or type some text to mangle."),
                            ephemeral=True)
            return
        out = fn(src)[:MAX_OUT] or "…"
        await ctx.reply(out, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="drunk", description="Slur text like they've had a few")
    @app_commands.describe(text="Text to mangle (or reply to a message)")
    async def drunk_cmd(self, ctx: commands.Context, *, text: str = "") -> None:
        await self._run(ctx, text, drunk)

    @commands.hybrid_command(name="scramble", aliases=["typo"], description="Shuffle the middle of each word")
    @app_commands.describe(text="Text to scramble (or reply to a message)")
    async def scramble_cmd(self, ctx: commands.Context, *, text: str = "") -> None:
        await self._run(ctx, text, scramble)

    @commands.hybrid_command(name="zalgo", aliases=["cursed"], description="Corrupt text into cursed glitch text")
    @app_commands.describe(text="Text to curse (or reply to a message)")
    async def zalgo_cmd(self, ctx: commands.Context, *, text: str = "") -> None:
        await self._run(ctx, text, zalgo)

    @commands.hybrid_command(name="uwu", aliases=["owo"], description="Translate text into uwu-speak")
    @app_commands.describe(text="Text to uwuify (or reply to a message)")
    async def uwu_cmd(self, ctx: commands.Context, *, text: str = "") -> None:
        await self._run(ctx, text, uwu)

    @commands.hybrid_command(name="mock", aliases=["spongebob"], description="aLtErNaTiNg mocking caps")
    @app_commands.describe(text="Text to mock (or reply to a message)")
    async def mock_cmd(self, ctx: commands.Context, *, text: str = "") -> None:
        await self._run(ctx, text, mock)

    @commands.hybrid_command(name="keysmash", aliases=["smash"], description="Collapse text into keyboard mash")
    @app_commands.describe(text="Text to smash (or reply to a message)")
    async def keysmash_cmd(self, ctx: commands.Context, *, text: str = "") -> None:
        await self._run(ctx, text, keysmash)


async def setup(bot) -> None:
    await bot.add_cog(TextFun(bot))
