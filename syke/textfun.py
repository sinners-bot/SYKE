"""Silly text-mangling toys: !drunk, !scramble, !zalgo, !uwu, !mock, !keysmash.

Each takes text directly or works on the message you're replying to. All of the
transforms are pure functions (with an optional rng) so they're easy to test,
and every command strips mentions and posts with pings disabled so a mangled
message can never ping anyone.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import ai
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


_HIGH_FILLERS = ["bro", "like", "man", "dude", "whoa", "uhh", "deadass", "ngl"]
_HIGH_TAILS = ["...", " ...woah", " hehe", " 😶‍🌫️", " bro i'm cooked", " wait what were we talking about"]


def high(text: str, rng: random.Random | None = None) -> str:
    """Spaced-out, drawn-out stoner talk: stretched vowels, filler words, trailing off."""
    rng = rng or random.Random()
    out = []
    for w in text.lower().split(" "):
        w = re.sub(r"([aeiou])", lambda m: m.group(1) * (rng.randint(2, 4) if rng.random() < 0.25 else 1), w, count=1)
        out.append(w)
        if rng.random() < 0.14:
            out.append(rng.choice(_HIGH_FILLERS))
    return " ".join(out) + rng.choice(_HIGH_TAILS)


_LEET = str.maketrans({"a": "4", "e": "3", "i": "1", "o": "0", "s": "5", "t": "7",
                       "A": "4", "E": "3", "I": "1", "O": "0", "S": "5", "T": "7"})


def leet(text: str) -> str:
    """h4ck3r l33tsp34k."""
    return text.translate(_LEET)


def stutter(text: str, rng: random.Random | None = None) -> str:
    """N-n-nervous repeated first letters."""
    rng = rng or random.Random()
    out = []
    for w in text.split(" "):
        if w[:1].isalpha() and rng.random() < 0.5:
            out.append(f"{w[0].lower()}-{w}")
        else:
            out.append(w)
    return " ".join(out)


def clap(text: str) -> str:
    """put 👏 a 👏 clap 👏 between 👏 every 👏 word."""
    words = [w for w in text.split() if w]
    return " 👏 ".join(words) if words else text


def reverse(text: str) -> str:
    """txet eht sesreveR."""
    return text[::-1]


# Earnest, affirming slogans — the gag is "forced wholesomeness," never punching down at anyone.
_WOKE = [
    "✊ trans rights are human rights",
    "🏳️‍⚧️ trans people are beautiful and valid",
    "🏳️‍🌈 love is love",
    "be kind, stay inclusive 💕",
    "no human is illegal 🌍",
    "protect and uplift marginalized voices 💪",
    "check your privilege 🙏",
    "respect everyone's pronouns 💞",
    "diversity is our strength",
    "spread love, not hate ✨",
    "all bodies are good bodies",
    "kindness is free, use it generously",
]


def woke(text: str, rng: random.Random | None = None) -> str:
    """Tack an earnest, politically-correct slogan onto whatever they said."""
    rng = rng or random.Random()
    slogan = rng.choice(_WOKE)
    extra = f" {rng.choice(_WOKE)}" if rng.random() < 0.3 else ""
    return f"{text} — {slogan}{extra}"


_SKIP_CENSOR = {"a", "an", "the", "i", "and", "or", "to", "of", "is", "it", "in", "on"}


def censor(text: str, rng: random.Random | None = None) -> str:
    """Classified: about a third of the words blacked out (███) or hidden behind spoilers."""
    rng = rng or random.Random()
    words = text.split(" ")
    candidates = [i for i, w in enumerate(words) if re.sub(r"\W", "", w).lower() not in _SKIP_CENSOR and
                  re.search(r"\w", w) and not w.startswith("http")]
    if not candidates:
        return text
    hits = set(rng.sample(candidates, max(1, round(len(candidates) * 0.35))))
    out = []
    for i, w in enumerate(words):
        if i not in hits:
            out.append(w)
        elif rng.random() < 0.6:
            out.append("█" * max(3, len(re.sub(r"\W", "", w))))
        else:
            out.append(f"||{w}||")
    return " ".join(out)


def _passthrough(text: str) -> str:
    return text


# The effects that can be stuck on a user, by canonical name, plus friendly aliases.
EFFECTS = {"drunk": drunk, "scramble": scramble, "zalgo": zalgo, "uwu": uwu, "mock": mock, "keysmash": keysmash,
           "high": high, "leet": leet, "stutter": stutter, "clap": clap, "reverse": reverse, "woke": woke,
           "censor": censor,
           # These are handled specially in the listener (AI, timing, puppetry or a mark); the function is the
           # fallback when that isn't possible.
           "translator": _passthrough, "narrator": _passthrough, "truthserum": _passthrough,
           "delay": _passthrough, "puppet": _passthrough, "hex": _passthrough}
EFFECT_ALIASES = {"typo": "scramble", "cursed": "zalgo", "owo": "uwu", "spongebob": "mock", "smash": "keysmash",
                  "stoned": "high", "baked": "high", "hacker": "leet", "1337": "leet",
                  "nervous": "stutter", "backwards": "reverse",
                  "redact": "censor", "redacted": "censor", "classified": "censor", "censored": "censor",
                  "translate": "translator", "babel": "translator", "lost": "translator",
                  "narrate": "narrator", "story": "narrator", "storyteller": "narrator",
                  "truth": "truthserum", "serum": "truthserum", "subtext": "truthserum", "truth-serum": "truthserum",
                  "late": "delay", "lag": "delay", "slow": "delay", "delayed": "delay",
                  "control": "puppet", "ventriloquist": "puppet", "puppeteer": "puppet",
                  "hexed": "hex", "mark": "hex", "hexmark": "hex", "skull": "hex"}
AI_EFFECTS = {"translator", "narrator", "truthserum"}
EFFECT_LIST = ("drunk, scramble, zalgo, uwu, mock, keysmash, high, leet, stutter, clap, reverse, woke, censor, "
               "translator, narrator, truthserum, delay, puppet, hex")
DELAY_RANGE = (30, 60)
PUPPET_DEFAULT = 5 * 60
HEX_ROLE_NAME = "Hexed 🕸️"
HEX_COLOUR = "#6B7A2F"       # a sickly swamp green
HEX_REACTIONS = ("💀", "💀", "💀", "🕯️", "🪦")
PUPPET_LINES = (
    "I would like everyone to know that I still sleep with a nightlight.",
    "Pineapple on pizza is the best thing humanity ever did and I'm ready to fight about it.",
    "I just want to say I love all of you. Even the annoying ones. Especially the annoying ones.",
    "Can someone teach me how to tie my shoes? Asking for me.",
    "I have been defeated by a revolving door twice this week.",
    "Real talk, I cry at every animated movie. Every single one.",
    "My search history is 90% 'how to look busy at work'.",
    "I'm not saying I'm the main character, but the soundtrack does follow me around.",
    "Today I said 'you too' when the waiter told me to enjoy my meal.",
    "I peaked in a Minecraft server in 2014 and it's been downhill since.",
    "Whoever is reading this: you're cooler than me and I accept that.",
    "I once lost an argument to a vending machine.",
)


EFFECT_TITLES = {"puppet": "🎭 Puppeted", "delay": "🐌 Delayed", "translator": "🌐 Lost in translation",
                 "censor": "🕵️ Classified", "truthserum": "💉 Truth serum", "narrator": "📖 Narrated",
                 "hex": "🕸️ Hexed"}
EFFECT_BLURBS = {
    "puppet": "{who} is a puppet now. Whatever they type comes out as lines from the script: queue some with "
              "`{p}puppet {name} <line>`, or they'll blurt out random embarrassing ones.",
    "delay": "Everything {who} says now shows up 30 to 60 seconds late. Always one beat behind.",
    "translator": "Every message {who} sends gets translated into a random language.",
    "censor": "Random words in everything {who} says are now ██████ or hidden behind ||spoilers||.",
    "truthserum": "{who} has been given a truth serum: under each message, what they *really* meant.",
    "narrator": "{who} is the main character now. Every message is narrated like a novel.",
    "hex": "{who} has been hexed: a cursed colour, and a 💀 on every message they send. They can still talk.",
}


def parse_duration(text: str) -> int | None:
    """"5m" / "1h30m" / "45s" / "2d" → seconds. None if it isn't a duration."""
    text = (text or "").strip().lower()
    parts = re.findall(r"(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|hour|hours|d|day|days)", text)
    if not parts or re.sub(r"(\d+)\s*[a-z]+", "", text).strip():
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return sum(int(n) * units[u[0]] for n, u in parts) or None


def duration_words(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 86400:
        return f"{seconds // 86400}d {seconds % 86400 // 3600}h".replace(" 0h", "")
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m".replace(" 0m", "")
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def resolve_effect(name: str) -> str | None:
    name = (name or "").lower().strip()
    if name in EFFECTS:
        return name
    return EFFECT_ALIASES.get(name)


# Afflicted users can only type plain text. One role, denied everything else in every channel.
AFFLICT_ROLE_NAME = "SYKE · afflicted"
AFFLICT_CONFIG = "afflict"
AFFLICT_DENY = tuple(p for p in (
    "attach_files", "embed_links", "add_reactions", "use_external_emojis", "use_external_stickers",
    "use_application_commands", "use_external_apps", "use_embedded_activities", "send_voice_messages",
    "send_polls", "send_tts_messages", "create_public_threads", "create_private_threads", "mention_everyone",
) if p in discord.Permissions.VALID_FLAGS)
AFFLICT_REASON = "SYKE: afflicted users can only type plain text"


async def vanish(message: discord.Message) -> bool:
    """Delete a message so it doesn't end up in snipe logs.

    Snipe bots record the ordinary one-message delete event. Deleting through Discord's bulk endpoint
    fires a purge event instead, which they don't log. Bulk delete needs at least two IDs, and Discord
    counts IDs that don't exist toward that minimum, so the message is paired with an empty placeholder.
    Falls back to a normal delete if the bulk call isn't possible.
    """
    bulk = getattr(message.channel, "delete_messages", None)
    if bulk is not None:
        ghost = discord.Object(id=discord.utils.time_snowflake(discord.utils.utcnow(), high=True))
        try:
            await bulk([message, ghost], reason="SYKE: afflicted message")
            return True
        except discord.HTTPException:
            pass
    try:
        await message.delete()
        return True
    except discord.HTTPException:
        return False


class TextFun(commands.Cog, name="TextFun"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._hooks: dict[int, discord.Webhook] = {}   # channel_id -> reusable webhook
        self._delay_until: dict[tuple[int, int], float] = {}  # (guild, user) -> when their last late message lands

    async def cog_load(self) -> None:
        self.expiry_loop.start()

    async def cog_unload(self) -> None:
        self.expiry_loop.cancel()

    # ---- Timed afflictions ----

    def _expiries(self, guild_id: int) -> dict:
        return dict(self.bot.storage.get_config(guild_id, AFFLICT_CONFIG).get("expires") or {})

    def _set_expiry(self, guild_id: int, user_id: int, until: float | None) -> None:
        expires = self._expiries(guild_id)
        if until is None:
            expires.pop(str(user_id), None)
        else:
            expires[str(user_id)] = until
        self.bot.storage.update_config(guild_id, AFFLICT_CONFIG, expires=expires or None)

    def _puppet_lines(self, guild_id: int, user_id: int) -> list[str]:
        return list((self.bot.storage.get_config(guild_id, AFFLICT_CONFIG).get("puppet") or {}).get(str(user_id)) or [])

    def _save_puppet(self, guild_id: int, user_id: int, lines: list[str] | None) -> None:
        puppets = dict(self.bot.storage.get_config(guild_id, AFFLICT_CONFIG).get("puppet") or {})
        if lines:
            puppets[str(user_id)] = lines[:50]
        else:
            puppets.pop(str(user_id), None)
        self.bot.storage.update_config(guild_id, AFFLICT_CONFIG, puppet=puppets or None)

    async def lift(self, guild: discord.Guild, user_id: int, reason: str = "SYKE: cured",
                   member: discord.Member | None = None) -> str | None:
        """Remove someone's affliction and everything that came with it. Returns the effect it was."""
        had = self.bot.storage.clear_effect(guild.id, user_id)
        self._set_expiry(guild.id, user_id, None)
        self._save_puppet(guild.id, user_id, None)
        member = member or guild.get_member(user_id)
        if member is not None:
            roles = [r for r in (await self._affliction_role(guild, create=False), self._hex_role(guild))
                     if r is not None and r in member.roles]
            if roles:
                with contextlib.suppress(discord.HTTPException):
                    await member.remove_roles(*roles, reason=reason)
        return had

    @tasks.loop(seconds=30)
    async def expiry_loop(self) -> None:
        now = time.time()
        for guild_id, cfg in self.bot.storage.configs(AFFLICT_CONFIG).items():
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            for uid, until in (cfg.get("expires") or {}).items():
                if until <= now:
                    await self.lift(guild, int(uid), reason="SYKE: affliction wore off")

    @expiry_loop.before_loop
    async def _before_expiry(self) -> None:
        await self.bot.wait_until_ready()

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

    async def _apply(self, fn, text: str) -> str:
        """Apply an effect. The semantic 'woke' effect uses AI when available, else the slogan."""
        if fn is woke:
            rewritten = await ai.woke_rewrite(self.bot.settings, text)
            if rewritten:
                return rewritten[:MAX_OUT]
        return fn(text)[:MAX_OUT]

    async def _run(self, ctx: commands.Context, text: str, fn) -> None:
        src = await self._source(ctx, text)
        if not src:
            await ctx.reply(embed=notice("Give me something",
                                         "Reply to a message or type some text to mangle."),
                            ephemeral=True)
            return
        out = (await self._apply(fn, src)) or "…"
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

    @commands.hybrid_command(name="high", aliases=["stoned", "baked"], description="Spaced-out stoner talk")
    @app_commands.describe(text="Text to space out (or reply to a message)")
    async def high_cmd(self, ctx: commands.Context, *, text: str = "") -> None:
        await self._run(ctx, text, high)

    @commands.hybrid_command(name="leet", aliases=["hacker", "1337"], description="h4ck3r l33tsp34k")
    @app_commands.describe(text="Text to leetify (or reply to a message)")
    async def leet_cmd(self, ctx: commands.Context, *, text: str = "") -> None:
        await self._run(ctx, text, leet)

    @commands.hybrid_command(name="stutter", aliases=["nervous"], description="N-n-nervous stutter")
    @app_commands.describe(text="Text to stutter (or reply to a message)")
    async def stutter_cmd(self, ctx: commands.Context, *, text: str = "") -> None:
        await self._run(ctx, text, stutter)

    @commands.hybrid_command(name="clap", description="Put 👏 claps 👏 between 👏 words")
    @app_commands.describe(text="Text to clap (or reply to a message)")
    async def clap_cmd(self, ctx: commands.Context, *, text: str = "") -> None:
        await self._run(ctx, text, clap)

    @commands.hybrid_command(name="reverse", aliases=["backwards"], description="Reverse the text")
    @app_commands.describe(text="Text to reverse (or reply to a message)")
    async def reverse_cmd(self, ctx: commands.Context, *, text: str = "") -> None:
        await self._run(ctx, text, reverse)

    @commands.hybrid_command(name="woke", description="Add an earnest politically-correct slogan")
    @app_commands.describe(text="Text to wokeify (or reply to a message)")
    async def woke_cmd(self, ctx: commands.Context, *, text: str = "") -> None:
        await self._run(ctx, text, woke)

    # ---- Afflictions: stick an effect on a user so every message comes out mangled ----

    @commands.hybrid_command(name="afflict", aliases=["curse"],
                             description="Make every message a user sends come out with a chat effect")
    @app_commands.describe(member="Who to afflict",
                           effect="drunk, uwu, censor, translator, narrator, truthserum, delay, puppet, hex…",
                           duration="How long, e.g. 10m or 2h (default: until cured; puppet: 5m)")
    @moderator("manage_messages")
    async def afflict_cmd(self, ctx: commands.Context, member: discord.Member, effect: str,
                          duration: str = "") -> None:
        eff = resolve_effect(effect)
        if eff is None:
            await ctx.reply(embed=notice("Unknown effect", f"Pick one: {EFFECT_LIST}."), ephemeral=True)
            return
        if member.bot:
            await ctx.reply(embed=notice("Can't do that", "Bots can't be afflicted."), ephemeral=True)
            return
        if eff in AI_EFFECTS and self.bot.settings.ai_provider == "none":
            await ctx.reply(embed=notice("Needs the AI", f"**{eff}** is written by SYKE's AI, which is off here."),
                            ephemeral=True)
            return
        seconds = parse_duration(duration) if duration else (PUPPET_DEFAULT if eff == "puppet" else None)
        if duration and seconds is None:
            await ctx.reply(embed=notice("How long?", "Try `10m`, `2h` or `1d`."), ephemeral=True)
            return
        previous = self.bot.storage.get_effect(ctx.guild.id, member.id)
        if previous == "hex" and eff != "hex":
            hex_role = self._hex_role(ctx.guild)
            if hex_role is not None and hex_role in member.roles:
                with contextlib.suppress(discord.HTTPException):
                    await member.remove_roles(hex_role, reason="SYKE: new affliction")
        self.bot.storage.set_effect(ctx.guild.id, member.id, eff, ctx.author.id)
        self._set_expiry(ctx.guild.id, member.id, time.time() + seconds if seconds else None)
        if eff != "puppet":
            self._save_puppet(ctx.guild.id, member.id, None)
        p = self.bot.display_prefix(ctx.guild.id)
        warnings = []
        if eff == "hex":
            marked = await self._hex(member)
            if not marked:
                warnings.append("Give me **Manage Roles** so I can paint them with the hex colour.")
            if not ctx.channel.permissions_for(ctx.guild.me).add_reactions:
                warnings.append("I need **Add Reactions** to 💀 their messages.")
            locked = False
        else:
            locked = await self._lock_to_text(member)
            if ctx.guild.me is not None and not ctx.channel.permissions_for(ctx.guild.me).manage_webhooks:
                warnings.append("I need **Manage Webhooks** (and Manage Messages) to rewrite their messages.")
            if not locked:
                warnings.append("Give me **Manage Roles** so I can lock them to plain text.")
        extra = "".join(f"\n⚠️ {w}" for w in warnings)
        rules = ("\nThey can only type plain text now: no files, links, embeds, apps, reactions, stickers, "
                 "polls or voice messages." if locked else "")
        timing = f"\nWears off in **{duration_words(seconds)}**." if seconds else ""
        what = EFFECT_BLURBS.get(eff, f"Every message {{who}} sends will now come out **{eff}**'d.")
        what = what.format(who=member.mention, p=p, name=member.display_name)
        await ctx.reply(embed=notice(EFFECT_TITLES.get(eff, "Afflicted 😈"),
                                     f"{what}{rules}{timing}\nUndo with `{p}cure {member.display_name}`.{extra}"))

    @commands.hybrid_command(name="puppet", aliases=["ventriloquist"],
                             description="Queue lines for a puppeted user to 'say' (afflict them with puppet first)")
    @app_commands.describe(member="The puppet", line="What they'll say next (leave empty to see the queue)")
    @moderator("manage_messages")
    async def puppet_cmd(self, ctx: commands.Context, member: discord.Member, *, line: str = "") -> None:
        p = self.bot.display_prefix(ctx.guild.id)
        if self.bot.storage.get_effect(ctx.guild.id, member.id) != "puppet":
            await ctx.reply(embed=notice("Not a puppet", f"Afflict them first: `{p}afflict {member.display_name} "
                                                         f"puppet 5m`."), ephemeral=True)
            return
        lines = self._puppet_lines(ctx.guild.id, member.id)
        line = clean_source(line)[:MAX_IN]
        if not line:
            queued = "\n".join(f"`{i}.` {l[:80]}" for i, l in enumerate(lines, 1)) or \
                "Nothing queued: they'll blurt out random embarrassing lines."
            await ctx.reply(embed=notice(f"🎭 {member.display_name}'s script", queued), ephemeral=True)
            return
        lines.append(line)
        self._save_puppet(ctx.guild.id, member.id, lines)
        await ctx.reply(embed=notice("🎭 Line queued", f"The next time {member.mention} talks, it comes out as:\n"
                                                      f"> {line[:200]}\n({len(lines)} in the script)"),
                        ephemeral=True)

    @commands.hybrid_command(name="cure", aliases=["uncurse"], description="Remove a user's chat effect")
    @app_commands.describe(member="Who to cure")
    @moderator("manage_messages")
    async def cure_cmd(self, ctx: commands.Context, member: discord.Member) -> None:
        had = await self.lift(ctx.guild, member.id, member=member)
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
        expires = self._expiries(ctx.guild.id)
        lines = "\n".join(f"<@{uid}> — **{eff}**" + (f" · ends <t:{int(expires[str(uid)])}:R>"
                                                     if str(uid) in expires else "") for uid, eff in rows)
        await ctx.reply(embed=notice("Afflicted users", lines), ephemeral=True)

    # ---- The text-only role ----

    async def _affliction_role(self, guild: discord.Guild, *, create: bool = True) -> discord.Role | None:
        """The server's "SYKE · afflicted" role, created (and denied everywhere) on first use."""
        cfg = self.bot.storage.get_config(guild.id, AFFLICT_CONFIG)
        role = guild.get_role(int(cfg.get("role_id") or 0))
        if role is not None or not create:
            return role
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            return None
        try:
            role = await guild.create_role(name=AFFLICT_ROLE_NAME, permissions=discord.Permissions.none(),
                                           mentionable=False, reason=AFFLICT_REASON)
        except discord.HTTPException:
            return None
        self.bot.storage.update_config(guild.id, AFFLICT_CONFIG, role_id=role.id)
        self.bot.spawn(self._deny_everywhere(guild, role))
        return role

    async def _deny_in(self, channel, role: discord.Role) -> bool:
        overwrite = channel.overwrites_for(role)
        if all(getattr(overwrite, p) is False for p in AFFLICT_DENY):
            return False
        for p in AFFLICT_DENY:
            setattr(overwrite, p, False)
        try:
            await channel.set_permissions(role, overwrite=overwrite, reason=AFFLICT_REASON)
        except discord.HTTPException:
            return False
        return True

    async def _deny_everywhere(self, guild: discord.Guild, role: discord.Role) -> int:
        changed = 0
        for channel in sorted(guild.channels, key=lambda c: not isinstance(c, discord.CategoryChannel)):
            changed += await self._deny_in(channel, role)
        return changed

    async def _lock_to_text(self, member: discord.Member) -> bool:
        """Give an afflicted member the text-only role. False if SYKE can't manage roles."""
        role = await self._affliction_role(member.guild)
        if role is None:
            return False
        if role in member.roles:
            return True
        try:
            await member.add_roles(role, reason=AFFLICT_REASON)
        except discord.HTTPException:
            return False
        return True

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel) -> None:
        role = await self._affliction_role(channel.guild, create=False)
        if role is not None:
            await self._deny_in(channel, role)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        # Leaving and rejoining doesn't shake off an affliction.
        if not member.bot and self.bot.storage.get_effect(member.guild.id, member.id):
            await self._lock_to_text(member)

    # ---- Hex mark ----

    def _hex_role(self, guild: discord.Guild) -> discord.Role | None:
        cfg = self.bot.storage.get_config(guild.id, AFFLICT_CONFIG)
        return guild.get_role(int(cfg.get("hex_role") or 0))

    async def _hex(self, member: discord.Member) -> bool:
        """Paint them with the hex role: a sickly colour, sitting high enough to show."""
        guild = member.guild
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            return False
        role = self._hex_role(guild)
        if role is None:
            try:
                role = await guild.create_role(name=HEX_ROLE_NAME, colour=discord.Colour.from_str(HEX_COLOUR),
                                               permissions=discord.Permissions.none(), hoist=False,
                                               reason="SYKE: hex mark")
            except discord.HTTPException:
                return False
            self.bot.storage.update_config(guild.id, AFFLICT_CONFIG, hex_role=role.id)
            with contextlib.suppress(discord.HTTPException):
                await role.edit(position=max(1, me.top_role.position - 1), reason="SYKE: hex colour shows on top")
        if role in member.roles:
            return True
        try:
            await member.add_roles(role, reason="SYKE: hex mark")
        except discord.HTTPException:
            return False
        return True

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
        effect = self.bot.storage.get_effect(message.guild.id, message.author.id)
        fn = EFFECTS.get(effect) if effect else None
        if fn is None:
            return
        until = self._expiries(message.guild.id).get(str(message.author.id))
        if until is not None and until <= time.time():
            await self.lift(message.guild, message.author.id, reason="SYKE: affliction wore off")
            return
        content = message.content or ""
        if content.startswith(self.bot.display_prefix(message.guild.id)):
            return  # don't mangle command invocations
        if effect == "hex":
            await self._hex_message(message)
            return
        me = message.guild.me
        if me is None or not message.channel.permissions_for(me).manage_messages:
            return
        # Gone instantly, and out of snipe's reach, before anything else happens.
        await vanish(message)
        text = clean_source(content)
        if not text and effect != "puppet":
            return  # files, stickers, polls, forwards: afflicted users only get to type text
        # Webhooks live on the parent; threads are addressed via thread=.
        parent = message.channel.parent if isinstance(message.channel, discord.Thread) else message.channel
        thread = message.channel if isinstance(message.channel, discord.Thread) else discord.utils.MISSING
        hook = await self._webhook_for(parent)
        if hook is None:
            return
        out = await self._rewrite(effect, fn, text, message.author, message.guild.id)
        if not out:
            return
        if effect == "delay":
            key = (message.guild.id, message.author.id)
            land = max(time.time() + random.uniform(*DELAY_RANGE), self._delay_until.get(key, 0) + 1.5)
            self._delay_until[key] = land  # late, but always in the order they were sent
            self.bot.spawn(self._send_later(hook, message.author, thread, out, land))
            return
        await self._send_as(hook, message.author, thread, out)

    async def _rewrite(self, effect: str, fn, text: str, author, guild_id: int) -> str:
        name = getattr(author, "display_name", None) or author.name
        if effect == "puppet":
            lines = self._puppet_lines(guild_id, author.id)
            if lines:
                line = lines.pop(0)
                self._save_puppet(guild_id, author.id, lines)
                return line
            return random.choice(PUPPET_LINES)
        if effect == "translator":
            flag, language = random.choice(ai.LANGUAGES)
            out = await ai.afflict_rewrite(self.bot.settings, "translator", text, name, language)
            return f"{flag} {out}" if out else text
        if effect == "narrator":
            return await ai.afflict_rewrite(self.bot.settings, "narrator", text, name) or f'*{name} said:* "{text}"'
        if effect == "truthserum":
            meant = await ai.afflict_rewrite(self.bot.settings, "truthserum", text, name)
            return f"{text}\n-# 💉 what they really meant: {meant}" if meant else text
        return await self._apply(fn, text)

    async def _send_as(self, hook: discord.Webhook, author, thread, out: str) -> None:
        try:
            await hook.send(out[:MAX_OUT], username=author.display_name[:80],
                            avatar_url=getattr(getattr(author, "display_avatar", None), "url", None),
                            thread=thread, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
        except discord.HTTPException:
            pass

    async def _send_later(self, hook, author, thread, out: str, land: float) -> None:
        await asyncio.sleep(max(0.0, land - time.time()))
        await self._send_as(hook, author, thread, out)

    async def _hex_message(self, message: discord.Message) -> None:
        """Hexed members keep talking normally, but they're marked: the colour, and a 💀 on everything."""
        if self._hex_role(message.guild) not in getattr(message.author, "roles", []):
            await self._hex(message.author)
        with contextlib.suppress(discord.HTTPException):
            await message.add_reaction(random.choice(HEX_REACTIONS))


async def setup(bot) -> None:
    await bot.add_cog(TextFun(bot))
