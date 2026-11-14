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
from .moderation import moderator

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


# The effects that can be stuck on a user, by canonical name, plus friendly aliases.
EFFECTS = {"drunk": drunk, "scramble": scramble, "zalgo": zalgo, "uwu": uwu, "mock": mock, "keysmash": keysmash}
EFFECT_ALIASES = {"typo": "scramble", "cursed": "zalgo", "owo": "uwu", "spongebob": "mock", "smash": "keysmash"}


def resolve_effect(name: str) -> str | None:
    name = (name or "").lower().strip()
    if name in EFFECTS:
        return name
    return EFFECT_ALIASES.get(name)


class TextFun(commands.Cog, name="TextFun"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._hooks: dict[int, discord.Webhook] = {}   # channel_id -> reusable webhook

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

    # ---- Afflictions: stick an effect on a user so every message comes out mangled ----

    @commands.hybrid_command(name="afflict", aliases=["curse"],
                             description="Make every message a user sends come out with a chat effect")
    @app_commands.describe(member="Who to afflict", effect="drunk, scramble, zalgo, uwu, mock or keysmash")
    @moderator("manage_messages")
    async def afflict_cmd(self, ctx: commands.Context, member: discord.Member, effect: str) -> None:
        eff = resolve_effect(effect)
        if eff is None:
            await ctx.reply(embed=notice("Unknown effect",
                                         "Pick one: `drunk`, `scramble`, `zalgo`, `uwu`, `mock`, `keysmash`."),
                            ephemeral=True)
            return
        if member.bot:
            await ctx.reply(embed=notice("Can't do that", "Bots can't be afflicted."), ephemeral=True)
            return
        self.bot.storage.set_effect(ctx.guild.id, member.id, eff, ctx.author.id)
        p = self.bot.display_prefix(ctx.guild.id)
        extra = ""
        if ctx.guild.me is not None and not ctx.channel.permissions_for(ctx.guild.me).manage_webhooks:
            extra = "\n\n⚠️ I need **Manage Webhooks** (and Manage Messages) to rewrite their messages."
        await ctx.reply(embed=notice("Afflicted 😈",
                                     f"Every message {member.mention} sends will now come out **{eff}**'d. "
                                     f"Undo with `{p}cure {member.display_name}`.{extra}"))

    @commands.hybrid_command(name="cure", aliases=["uncurse"], description="Remove a user's chat effect")
    @app_commands.describe(member="Who to cure")
    @moderator("manage_messages")
    async def cure_cmd(self, ctx: commands.Context, member: discord.Member) -> None:
        had = self.bot.storage.clear_effect(ctx.guild.id, member.id)
        if had:
            await ctx.reply(embed=notice("Cured", f"{member.mention} is back to normal (was **{had}**)."))
        else:
            await ctx.reply(embed=notice("Nothing to cure", f"{member.mention} has no active effect."),
                            ephemeral=True)

    @commands.hybrid_command(name="afflicted", description="List users with an active chat effect")
    @moderator("manage_messages")
    async def afflicted_cmd(self, ctx: commands.Context) -> None:
        rows = self.bot.storage.list_effects(ctx.guild.id)
        if not rows:
            await ctx.reply(embed=notice("All clear", "No one is afflicted right now."), ephemeral=True)
            return
        lines = "\n".join(f"<@{uid}> — **{eff}**" for uid, eff in rows)
        await ctx.reply(embed=notice("Afflicted users", lines), ephemeral=True)

    async def _webhook_for(self, channel: discord.TextChannel) -> discord.Webhook | None:
        """A reusable SYKE webhook in `channel` for reposting rewritten messages."""
        cached = self._hooks.get(channel.id)
        if cached is not None:
            return cached
        me = channel.guild.me
        if me is None or not channel.permissions_for(me).manage_webhooks:
            return None
        try:
            for h in await channel.webhooks():
                if h.token and getattr(h.user, "id", None) == self.bot.user.id:
                    self._hooks[channel.id] = h
                    return h
            hook = await channel.create_webhook(name="SYKE")
        except discord.HTTPException:
            return None
        self._hooks[channel.id] = hook
        return hook

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        content = message.content or ""
        if not content.strip():
            return  # nothing to rewrite (attachment/sticker-only) — leave it alone
        prefix = self.bot.display_prefix(message.guild.id)
        if content.startswith(prefix):
            return  # don't mangle command invocations
        effect = self.bot.storage.get_effect(message.guild.id, message.author.id)
        fn = EFFECTS.get(effect) if effect else None
        if fn is None:
            return
        # Webhooks live on the parent; threads are addressed via thread=.
        parent = message.channel.parent if isinstance(message.channel, discord.Thread) else message.channel
        thread = message.channel if isinstance(message.channel, discord.Thread) else discord.utils.MISSING
        me = message.guild.me
        if me is None or not message.channel.permissions_for(me).manage_messages:
            return
        hook = await self._webhook_for(parent)
        if hook is None:
            return
        out = fn(clean_source(content))[:MAX_OUT]
        if not out:
            return
        if message.attachments:
            out = (out + "\n" + "\n".join(a.url for a in message.attachments))[:MAX_OUT]
        try:
            await message.delete()
            await hook.send(out, username=message.author.display_name[:80],
                            avatar_url=getattr(getattr(message.author, "display_avatar", None), "url", None),
                            thread=thread, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


async def setup(bot) -> None:
    await bot.add_cog(TextFun(bot))
