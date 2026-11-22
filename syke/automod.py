"""Automod: a word/slur filter, invite & link control, and anti-spam.

All three run from one on_message listener and share a per-server config blob (storage key "automod").
People who can already moderate — administrators, anyone with Manage Messages, SYKE owners, or a role
the server marks as exempt — are never caught. Every action is a numbered case and goes to the mod log.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import re
import unicodedata
import time
import types
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from . import ai
from .models import Msg
from .moderation import (
    BRAND,
    DANGER,
    MAX_TIMEOUT,
    OFF_WORDS,
    OK,
    WARN,
    bot_lacks,
    clean_reason,
    embed,
    human_duration,
    moderator,
    needs,
    parse_duration,
    problem,
)
from .traits import score_message

log = logging.getLogger("syke.automod")

CONFIG_KEY = "automod"
NOTICE_COOLDOWN = 15
MAX_WORDS = 300
MAX_DOMAINS = 100
WORD_LIMIT = 60
DOMAIN_LIMIT = 80
AI_PER_MINUTE = 20          # bypass checks sent to the AI per server per minute
AI_CACHE = 2000

BYPASS_PROMPT = """You moderate a Discord server's word filter. The server blocks these words:
{words}

Decide whether the message below is an attempt to write one of the blocked words while dodging the filter:
misspelling it on purpose, swapping or adding letters, using numbers or symbols for letters, splitting it up,
or spelling it phonetically. Ordinary different words that only look similar (for example 'lucid' or 'lucky'
when 'lucia' is blocked) are NOT attempts. When unsure, say no.

Reply with JSON only: {{"bypass": true or false, "word": "<the blocked word it is trying to write, or empty>"}}"""

INVITE_RE = re.compile(r"\b(?:discord\.gg|discord(?:app)?\.com/invite|discord\.me|dsc\.gg|disboard\.org/server/join)/\S+",
                       re.IGNORECASE)
URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)
DOMAIN_RE = re.compile(r"https?://([^/\s:]+)", re.IGNORECASE)

DEFAULTS = {
    "filter_on": False, "words": [], "filter_action": "delete", "filter_timeout": 600, "filter_ai": True,
    "tox_flag": False, "tox_threshold": 1.6, "review_channel": None,
    "antiinvite": False, "antilink": False, "allow_domains": [], "link_action": "delete",
    "antispam": False, "flood_count": 6, "flood_seconds": 5, "dup_count": 4,
    "mention_limit": 6, "caps_len": 12, "spam_timeout": 300,
    "ignore_roles": [],
}
ACTIONS = ("delete", "warn", "timeout")


@dataclass
class Hit:
    kind: str          # filter / invite / link / spam
    reason: str        # human summary for the log and case
    action: str        # delete / warn / timeout
    duration: int | None = None


def cfg(config: dict, key: str):
    return config.get(key, DEFAULTS[key])


# Bypass-proof matching. Everything is folded to a "skeleton" before comparing: accents and fullwidth
# letters are flattened, look-alike Cyrillic/Greek letters become Latin, leetspeak becomes letters, and
# letters that people swap for each other (l/1/i, u/v) share one symbol. Then a blocked word becomes a
# pattern where every letter may repeat, so 'lucia' also catches 'LUCCIAAA', 'luuucia' and '1ucia'.
CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "һ": "h", "к": "k", "м": "m", "н": "h", "т": "t", "в": "b", "г": "r", "п": "n", "ɡ": "g",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u",
    "χ": "x", "ɑ": "a", "ı": "i", "ł": "l", "ø": "o", "đ": "d", "ß": "ss", "æ": "ae", "œ": "oe",
})
SKELETON = str.maketrans({
    "0": "o", "1": "i", "!": "i", "|": "i", "l": "i", "3": "e", "4": "a", "@": "a", "5": "s", "$": "s",
    "7": "t", "+": "t", "8": "b", "9": "g", "6": "g", "2": "z", "(": "c", "v": "u", "€": "e", "£": "e",
})
INVISIBLE = re.compile(r"[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e\u200b-\u200f\u202a-\u202e"
                       r"\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff]")
# Characters that may sit inside a word only as a disguise (so 'l.u.c.i.a' is one word).
JOINERS = re.compile(r"(?<=[^\W_])[\s.\-_*~'\",:;/\\^`]+(?=[^\W_])")


def fold(text: str) -> str:
    """Lowercase, drop invisible characters and accents, map look-alikes to plain letters."""
    text = INVISIBLE.sub("", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().translate(CONFUSABLES)


def skeleton(text: str) -> str:
    return re.sub(r"[^a-z]", "", fold(text).translate(SKELETON))


def squeeze(text: str) -> str:
    """Collapse repeated letters: 'luccciaaa' -> 'lucia'."""
    return re.sub(r"(.)\1+", r"\1", text)


def normalize(text: str) -> str:
    """Folded, de-leeted, letters only (so 'f.u_c k' and 'fu0ck' still match 'fuck')."""
    return skeleton(text)


@functools.lru_cache(maxsize=2048)
def word_pattern(word: str) -> re.Pattern | None:
    """'lucia' -> i{1,}u{1,}c{1,}i{1,}a{1,} : every letter may be stretched, none dropped ('ass' keeps s{2,})."""
    sk = skeleton(word)
    if not sk:
        return None
    return re.compile("".join(f"{re.escape(run[0])}{{{len(run)},}}" for run, _ in re.findall(r"((.)\2*)", sk)))


def _tokens(content: str) -> list[str]:
    """Words as written, with disguise characters inside a word removed, plus spaced-out letters joined."""
    folded = fold(content)
    joined = JOINERS.sub("", folded)
    words = re.findall(r"[^\s]+", folded) + re.findall(r"[^\s]+", joined)
    # 'a s s' / 'a.s.s': runs of single characters become one word.
    singles = re.findall(r"(?:(?<!\S)\S(?:[\s.\-_*]+|$)){2,}", folded)
    words += [re.sub(r"[\s.\-_*]", "", run) for run in singles]
    return [w for w in (skeleton(t) for t in words) if w]


def filter_hit(content: str, words: list[str]) -> str | None:
    """Return the blacklisted word that `content` triggers, or None.

    Matches whole words (so 'ass' never fires inside 'class'), after undoing the usual disguises:
    stretched letters ('LUCCIAAA'), leetspeak ('1ucia', 'sh1t'), look-alike or accented letters,
    invisible characters, and letters split up by spaces or dots ('l.u.c.i.a', 'b i t c h').
    Blocked words of 5+ letters are also caught inside longer words ('lucias', 'xxluciaxx').
    """
    if not words:
        return None
    tokens = set(_tokens(content))
    stream = skeleton(content)
    lowered = fold(content)
    for word in words:
        w = word.lower().strip()
        if not w:
            continue
        if " " in w:
            nw = skeleton(w)
            if w in lowered or (nw and (nw in stream or (word_pattern(w) and word_pattern(w).search(stream)))):
                return word
            continue
        pattern = word_pattern(w)
        if pattern is None:
            continue
        if any(pattern.fullmatch(t) for t in tokens):
            return word
        if len(skeleton(w)) >= 5 and pattern.search(stream):
            return word
    return None


def _distance(a: str, b: str, limit: int) -> int:
    """Edit distance (with swaps), stopping early once it passes `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if prev2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def near_miss(content: str, words: list[str]) -> str | None:
    """A blocked word that `content` comes suspiciously close to without matching (for the AI to judge).

    'lucai', 'luciya', 'lusia' for 'lucia'. Short words (under 4 letters) are skipped: too many real words
    sit one letter away from them.
    """
    tokens = {squeeze(t) for t in _tokens(content)}
    for word in words:
        w = squeeze(skeleton(word))
        if len(w) < 4 or " " in word.strip():
            continue
        limit = 1 if len(w) < 6 else 2
        for t in tokens:
            if t == w or len(t) < 3:
                continue
            gap = _distance(t, w, limit)
            if gap <= limit and (gap == 1 or t[0] == w[0]):
                return word
    return None


def domains_in(content: str) -> list[str]:
    found = []
    for raw in URL_RE.findall(content):
        url = raw if raw.lower().startswith("http") else "http://" + raw
        m = DOMAIN_RE.match(url)
        if m:
            found.append(m.group(1).lower().lstrip("www.").removeprefix("www."))
    return found


def caps_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(c.isupper() for c in letters) / len(letters)


class AutoMod(commands.Cog, name="Automod"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._recent: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=25))
        self._noticed: dict[tuple[int, int], float] = {}
        self._ai_seen: dict[tuple[int, str], str] = {}
        self._ai_calls: dict[int, deque] = defaultdict(deque)

    def p(self, ctx: commands.Context) -> str:
        return self.bot.display_prefix(ctx.guild.id)

    def config(self, guild_id: int) -> dict:
        return self.bot.storage.get_config(guild_id, CONFIG_KEY)

    def save(self, guild_id: int, **changes) -> dict:
        return self.bot.storage.update_config(guild_id, CONFIG_KEY, **changes)

    # Exemptions and enforcement

    def exempt(self, member, config: dict) -> bool:
        if getattr(member, "bot", False):
            return True
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and (perms.administrator or perms.manage_messages):
            return True
        if getattr(member, "id", None) in self.bot.settings.owner_ids:
            return True
        ignore = set(cfg(config, "ignore_roles"))
        return bool(ignore and any(r.id in ignore for r in getattr(member, "roles", ())))

    def check(self, message: discord.Message, config: dict) -> Hit | None:
        content = message.content or ""
        if cfg(config, "filter_on"):
            word = filter_hit(content, cfg(config, "words"))
            if word:
                return Hit("filter", "blocked word", cfg(config, "filter_action"), cfg(config, "filter_timeout"))
        if cfg(config, "antiinvite") and INVITE_RE.search(content):
            return Hit("invite", "server invite", cfg(config, "link_action"), cfg(config, "filter_timeout"))
        if cfg(config, "antilink"):
            allow = set(cfg(config, "allow_domains"))
            bad = [d for d in domains_in(content) if not any(d == a or d.endswith("." + a) for a in allow)]
            if bad:
                return Hit("link", f"link ({bad[0]})", cfg(config, "link_action"), cfg(config, "filter_timeout"))
        if cfg(config, "antispam"):
            spam = self.spam_hit(message, config)
            if spam:
                return spam
        return None

    def spam_hit(self, message: discord.Message, config: dict) -> Hit | None:
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        recent = self._recent[key]
        norm = normalize(message.content or "")
        recent.append((now, norm))
        window = cfg(config, "flood_seconds")
        in_window = [(t, c) for t, c in recent if now - t <= window]
        timeout = cfg(config, "spam_timeout")
        if len(in_window) >= cfg(config, "flood_count"):
            return Hit("spam", "message flooding", "timeout", timeout)
        if norm and sum(1 for _, c in in_window if c == norm) >= cfg(config, "dup_count"):
            return Hit("spam", "repeated messages", "timeout", timeout)
        mentions = len(set(message.mentions)) + len(getattr(message, "role_mentions", []))
        if mentions >= cfg(config, "mention_limit"):
            return Hit("spam", "mass mentions", "timeout", timeout)
        text = message.content or ""
        if len(text) >= cfg(config, "caps_len") and caps_ratio(text) >= 0.8:
            return Hit("spam", "all-caps spam", "timeout", timeout)
        return None

    async def ai_bypass(self, message: discord.Message, config: dict) -> Hit | None:
        """Close misspellings of a blocked word ('lucai', 'luc1ya') go to the AI to judge."""
        words = cfg(config, "words")
        if not (cfg(config, "filter_on") and cfg(config, "filter_ai") and words) or \
                self.bot.settings.ai_provider == "none":
            return None
        content = message.content or ""
        word = near_miss(content, words)
        if word is None:
            return None
        key = (message.guild.id, skeleton(content)[:300])
        verdict = self._ai_seen.get(key)
        if verdict is None:
            now = time.monotonic()
            calls = self._ai_calls[message.guild.id]
            while calls and now - calls[0] > 60:
                calls.popleft()
            if len(calls) >= AI_PER_MINUTE:
                return None
            calls.append(now)
            verdict = ""
            try:
                listed = "\n".join(f"- {w}" for w in words[:150])
                data = await ai._complete(self.bot.settings, BYPASS_PROMPT.format(words=listed),
                                          f"Message: {content[:500]}", 0.0, 10)
                if data.get("bypass") is True:
                    named = str(data.get("word") or "").lower().strip()
                    verdict = named if named in {w.lower() for w in words} else word
            except Exception as error:  # an AI outage must never block chat
                log.info("automod AI check failed: %s", error)
                return None
            if len(self._ai_seen) >= AI_CACHE:
                self._ai_seen.pop(next(iter(self._ai_seen)))
            self._ai_seen[key] = verdict
        if not verdict:
            return None
        return Hit("filter", "blocked word (filter bypass)", cfg(config, "filter_action"), cfg(config, "filter_timeout"))

    async def act(self, message: discord.Message, config: dict, hit: Hit) -> None:
        guild, member = message.guild, message.author
        deleted = False
        with contextlib.suppress(discord.HTTPException):
            await message.delete()
            deleted = True
        case_action = f"automod ({hit.kind})"
        duration = hit.duration if hit.action == "timeout" else None
        did = "deleted a message"
        if hit.action == "timeout" and duration:
            if not member.guild_permissions.administrator and not bot_lacks(guild, "moderate_members"):
                with contextlib.suppress(discord.HTTPException):
                    await member.timeout(timedelta(seconds=min(duration, MAX_TIMEOUT)),
                                         reason=f"SYKE automod: {hit.reason}")
                    did = f"timed out for {human_duration(duration)}"
        elif hit.action == "warn":
            did = "warned"
        self.bot.storage.add_case(guild.id, member.id, getattr(self.bot.user, "id", 0), case_action, hit.reason, duration)
        await self.notify(message, hit)
        await self.log(guild, embed(
            "🛡️ Automod", f"**Member:** {member.mention} (`{member.id}`)\n**Rule:** {hit.reason}\n"
                          f"**Action:** {did}\n**Channel:** {getattr(message.channel, 'mention', '?')}",
            WARN if hit.action != "timeout" else DANGER,
        ))

    async def notify(self, message: discord.Message, hit: Hit) -> None:
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self._noticed.get(key, 0) < NOTICE_COOLDOWN:
            return
        self._noticed[key] = now
        lines = {"filter": "that word isn't allowed here", "invite": "no server invites here",
                 "link": "links aren't allowed here", "spam": "slow down"}
        with contextlib.suppress(discord.HTTPException):
            await message.channel.send(
                f"{message.author.mention} {lines.get(hit.kind, 'that was removed')}.",
                delete_after=6, allowed_mentions=discord.AllowedMentions(users=[message.author]),
            )

    async def flag_toxic(self, message: discord.Message, config: dict) -> None:
        channel = message.guild.get_channel(cfg(config, "review_channel") or 0)
        if channel is None or not (message.content or "").strip():
            return
        msg = Msg(message.author.id, message.author.display_name, message.content,
                  message.created_at or datetime.now(timezone.utc), message.channel.id)
        try:
            score = score_message(msg, self.bot.tz_for(message.guild.id)).get("toxic", 0.0)
        except Exception:
            return
        if score < cfg(config, "tox_threshold"):
            return
        link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
        card = embed("⚠️ Flagged for review",
                     f"**Member:** {message.author.mention}\n**Channel:** {message.channel.mention}\n"
                     f"**Toxicity:** {score:.1f}\n**Message:** {message.content[:500]}\n[Jump]({link})", WARN)
        with contextlib.suppress(discord.HTTPException):
            await channel.send(embed=card)

    async def log(self, guild: discord.Guild, entry: discord.Embed) -> None:
        channel_id = self.bot.storage.modlog_channel(guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is not None:
            entry.timestamp = datetime.now(timezone.utc)
            with contextlib.suppress(discord.HTTPException):
                await channel.send(embed=entry)

    async def scan(self, message: discord.Message) -> None:
        guild = message.guild
        # Skip DMs, bots/webhooks, and anyone we can't read a role/permission set for.
        if guild is None or message.author.bot or not hasattr(message.author, "guild_permissions"):
            return
        config = self.config(guild.id)
        if not config:
            return
        if self.exempt(message.author, config):
            return
        hit = self.check(message, config)
        if hit is None:
            hit = await self.ai_bypass(message, config)
        if hit is not None:
            await self.act(message, config, hit)
        elif cfg(config, "tox_flag"):
            await self.flag_toxic(message, config)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self.scan(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if (before.content or "") != (after.content or ""):
            await self.scan(after)

    # Settings overview

    def overview(self, guild: discord.Guild, prefix: str) -> discord.Embed:
        c = self.config(guild.id)
        def onoff(k):
            return "🟢 on" if cfg(c, k) else "⚪ off"
        review = guild.get_channel(cfg(c, "review_channel") or 0)
        ignore = ", ".join(f"<@&{r}>" for r in cfg(c, "ignore_roles")) or "none"
        body = (
            f"**Word filter:** {onoff('filter_on')} — {len(cfg(c, 'words'))} word(s), action: {cfg(c, 'filter_action')}\n"
            f"**Toxicity review:** {onoff('tox_flag')} → {review.mention if review else 'no channel'} "
            f"(≥ {cfg(c, 'tox_threshold')})\n"
            f"**Anti-invite:** {onoff('antiinvite')} · **Anti-link:** {onoff('antilink')} "
            f"({len(cfg(c, 'allow_domains'))} allowed)\n"
            f"**Anti-spam:** {onoff('antispam')} — {cfg(c, 'flood_count')} msgs/{cfg(c, 'flood_seconds')}s, "
            f"{cfg(c, 'mention_limit')} mentions, timeout {human_duration(cfg(c, 'spam_timeout'))}\n"
            f"**Exempt roles:** {ignore}\n\n"
            f"Mods (Manage Messages / Admin) are never filtered. Actions go to the mod log "
            f"(`{prefix}modlog #channel`).\n"
            f"`{prefix}filter` · `{prefix}links` · `{prefix}antispam` · `{prefix}automod toxic #channel`"
        )
        return embed("🛡️ Automod", body)

    @commands.hybrid_group(name="automod", fallback="settings", case_insensitive=True,
                           description="Automatic moderation: word filter, links and anti-spam")
    @moderator("manage_guild")
    async def automod(self, ctx: commands.Context) -> None:
        await ctx.reply(embed=self.overview(ctx.guild, self.p(ctx)), ephemeral=True)

    @automod.command(name="ignore", description="Exempt a role from all automod (add or remove it)")
    @app_commands.describe(role="The role to exempt or un-exempt")
    @moderator("manage_guild")
    async def automod_ignore(self, ctx: commands.Context, role: discord.Role) -> None:
        roles = list(cfg(self.config(ctx.guild.id), "ignore_roles"))
        if role.id in roles:
            roles.remove(role.id)
            msg = f"{role.mention} is filtered again."
        else:
            roles.append(role.id)
            msg = f"{role.mention} is now exempt from automod."
        self.save(ctx.guild.id, ignore_roles=roles)
        await ctx.reply(embed=embed("Saved ✅", msg), ephemeral=True)

    @automod.command(name="toxic", description="Flag toxic messages to a review channel instead of deleting")
    @app_commands.describe(channel="Where to post flagged messages. 'off' to stop.",
                           threshold="Sensitivity 1.0 (strict) – 3.0 (loose); default 1.6")
    @moderator("manage_guild")
    async def automod_toxic(self, ctx: commands.Context, channel: discord.TextChannel | None = None,
                            threshold: float | None = None, setting: str = "") -> None:
        if channel is None:
            if setting.strip().lower() in OFF_WORDS:
                self.save(ctx.guild.id, tox_flag=False)
                await ctx.reply(embed=embed("Toxicity review off", "SYKE won't flag messages."), ephemeral=True)
                return
            await ctx.reply(embed=problem("Which channel?", f"`{self.p(ctx)}automod toxic #mod-review`."),
                            ephemeral=True)
            return
        changes = {"tox_flag": True, "review_channel": channel.id}
        if threshold is not None:
            changes["tox_threshold"] = max(0.5, min(4.0, threshold))
        self.save(ctx.guild.id, **changes)
        await ctx.reply(embed=embed("Toxicity review on ⚠️", f"Flagged messages go to {channel.mention} "
                                    f"(≥ {changes.get('tox_threshold', cfg(self.config(ctx.guild.id), 'tox_threshold'))}). "
                                    "Nothing is deleted — mods review and act."), ephemeral=True)

    # Word filter

    @commands.hybrid_group(name="filter", fallback="status", case_insensitive=True,
                           description="The word/slur blacklist")
    @moderator("manage_guild")
    async def filter(self, ctx: commands.Context) -> None:
        c = self.config(ctx.guild.id)
        words = cfg(c, "words")
        shown = ", ".join(f"`{w}`" for w in words[:40]) or "none yet"
        more = f" …and {len(words) - 40} more" if len(words) > 40 else ""
        body = (f"**Status:** {'🟢 on' if cfg(c, 'filter_on') else '⚪ off'} · "
                f"**Action:** {cfg(c, 'filter_action')}"
                + (f" ({human_duration(cfg(c, 'filter_timeout'))})" if cfg(c, 'filter_action') == 'timeout' else "")
                + f"\n**Bypass detection:** stretched letters, leetspeak, look-alike letters and split-up words "
                f"are always caught; AI check for close misspellings: "
                + ("🟢 on" if cfg(c, "filter_ai") and self.bot.settings.ai_provider != "none" else
                   "⚪ off" if self.bot.settings.ai_provider != "none" else "⚪ off (no AI key set)")
                + f"\n**Words ({len(words)}):** {shown}{more}\n\n"
                f"`{self.p(ctx)}filter on|off` · `{self.p(ctx)}filter add <word>` · `{self.p(ctx)}filter remove <word>` · "
                f"`{self.p(ctx)}filter action delete|warn|timeout [10m]` · `{self.p(ctx)}filter smart on|off` · "
                f"`{self.p(ctx)}filter test <text>` · `{self.p(ctx)}filter clear`")
        await ctx.reply(embed=embed("🚫 Word filter", body), ephemeral=True)

    @filter.command(name="on", description="Turn the word filter on")
    @moderator("manage_guild")
    async def filter_on(self, ctx: commands.Context) -> None:
        self.save(ctx.guild.id, filter_on=True)
        note = "" if cfg(self.config(ctx.guild.id), "words") else f" Add words with `{self.p(ctx)}filter add <word>`."
        warn = "" if not bot_lacks(ctx.guild, "manage_messages") else " ⚠️ SYKE needs **Manage Messages** to delete."
        await ctx.reply(embed=embed("Word filter on 🚫", f"Blocked words will be removed.{note}{warn}"), ephemeral=True)

    @filter.command(name="off", description="Turn the word filter off")
    @moderator("manage_guild")
    async def filter_off(self, ctx: commands.Context) -> None:
        self.save(ctx.guild.id, filter_on=False)
        await ctx.reply(embed=embed("Word filter off", "The blacklist is kept for later."), ephemeral=True)

    @filter.command(name="add", description="Add a word or phrase to the blacklist")
    @app_commands.describe(word="The word or phrase to block (spaces allowed)")
    @moderator("manage_guild")
    async def filter_add(self, ctx: commands.Context, *, word: str) -> None:
        word = word.strip().lower()[:WORD_LIMIT]
        if not word:
            await ctx.reply(embed=problem("Add what?", "Give a word to block."), ephemeral=True)
            return
        words = list(cfg(self.config(ctx.guild.id), "words"))
        if word in words:
            await ctx.reply(embed=problem("Already blocked", f"`{word}` is already on the list."), ephemeral=True)
            return
        if len(words) >= MAX_WORDS:
            await ctx.reply(embed=problem("List full", f"The blacklist holds up to {MAX_WORDS} words."), ephemeral=True)
            return
        words.append(word)
        changes = {"words": words}
        if not cfg(self.config(ctx.guild.id), "filter_on"):
            changes["filter_on"] = True
        self.save(ctx.guild.id, **changes)
        await ctx.reply(embed=embed("Added ✅", f"`{word}` is now blocked. {len(words)} word(s) total."),
                        ephemeral=True)

    @filter.command(name="remove", aliases=["rm", "delete"], description="Remove a word from the blacklist")
    @app_commands.describe(word="The word to unblock")
    @moderator("manage_guild")
    async def filter_remove(self, ctx: commands.Context, *, word: str) -> None:
        word = word.strip().lower()
        words = list(cfg(self.config(ctx.guild.id), "words"))
        if word not in words:
            await ctx.reply(embed=problem("Not on the list", f"`{word}` isn't blocked."), ephemeral=True)
            return
        words.remove(word)
        self.save(ctx.guild.id, words=words)
        await ctx.reply(embed=embed("Removed ✅", f"`{word}` is allowed again. {len(words)} left."), ephemeral=True)

    @filter.command(name="smart", aliases=["ai"], description="Turn the AI check for misspelled filter dodges on or off")
    @app_commands.describe(setting="on or off")
    @moderator("manage_guild")
    async def filter_smart(self, ctx: commands.Context, setting: str) -> None:
        on = setting.strip().lower() not in OFF_WORDS
        self.save(ctx.guild.id, filter_ai=on)
        note = "" if self.bot.settings.ai_provider != "none" else " ⚠️ No AI key is set, so it can't run yet."
        await ctx.reply(embed=embed("Saved ✅", f"AI bypass check is **{'on' if on else 'off'}**. Stretched letters, "
                                               f"leetspeak and look-alike letters are caught either way.{note}"),
                        ephemeral=True)

    @filter.command(name="test", description="Check whether a message would be caught by the filter")
    @app_commands.describe(text="The message to test")
    @moderator("manage_guild")
    async def filter_test(self, ctx: commands.Context, *, text: str) -> None:
        c = self.config(ctx.guild.id)
        words = cfg(c, "words")
        word = filter_hit(text, words)
        if word:
            body = f"🚫 Caught instantly: matches `{word}`."
        else:
            close = near_miss(text, words)
            if close is None:
                body = "✅ Allowed: nothing close to a blocked word."
            elif not cfg(c, "filter_ai") or self.bot.settings.ai_provider == "none":
                body = f"✅ Allowed. It's close to `{close}`; turn on `filter smart` to let the AI judge those."
            else:
                fake = types.SimpleNamespace(content=text, guild=ctx.guild)
                hit = await self.ai_bypass(fake, {**c, "filter_on": True})
                body = (f"🚫 Caught by the AI check: a dodge of `{close}`." if hit else
                        f"✅ Allowed: close to `{close}`, but the AI says it's a different word.")
        await ctx.reply(embed=embed("🧪 Filter test", body), ephemeral=True)

    @filter.command(name="clear", description="Empty the whole blacklist")
    @moderator("manage_guild")
    async def filter_clear(self, ctx: commands.Context) -> None:
        self.save(ctx.guild.id, words=[])
        await ctx.reply(embed=embed("Blacklist cleared", "Every word was removed."), ephemeral=True)

    @filter.command(name="action", description="What to do on a blocked word: delete, warn or timeout")
    @app_commands.describe(action="delete, warn, or timeout", duration="For timeout, e.g. 10m")
    @moderator("manage_guild")
    async def filter_action(self, ctx: commands.Context, action: str, duration: str = "") -> None:
        action = action.strip().lower()
        if action not in ACTIONS:
            await ctx.reply(embed=problem("Which action?", "Pick `delete`, `warn` or `timeout`."), ephemeral=True)
            return
        changes = {"filter_action": action}
        if action == "timeout":
            seconds = parse_duration(duration) if duration else cfg(self.config(ctx.guild.id), "filter_timeout")
            if not seconds:
                await ctx.reply(embed=problem("How long?", "Add a duration, e.g. `filter action timeout 10m`."),
                                ephemeral=True)
                return
            changes["filter_timeout"] = min(seconds, MAX_TIMEOUT)
        self.save(ctx.guild.id, **changes)
        extra = f" ({human_duration(changes['filter_timeout'])})" if action == "timeout" else ""
        await ctx.reply(embed=embed("Saved ✅", f"Blocked words will now: **{action}**{extra}."), ephemeral=True)

    # Links & invites

    @commands.hybrid_group(name="links", fallback="status", case_insensitive=True,
                           description="Invite and link control")
    @moderator("manage_guild")
    async def links(self, ctx: commands.Context) -> None:
        c = self.config(ctx.guild.id)
        allow = ", ".join(f"`{d}`" for d in cfg(c, "allow_domains")) or "none"
        body = (f"**Anti-invite:** {'🟢 on' if cfg(c, 'antiinvite') else '⚪ off'} — removes Discord invites\n"
                f"**Anti-link:** {'🟢 on' if cfg(c, 'antilink') else '⚪ off'} — removes links not on the allowlist\n"
                f"**Allowed domains:** {allow}\n\n"
                f"`{self.p(ctx)}links invites on|off` · `{self.p(ctx)}links urls on|off` · "
                f"`{self.p(ctx)}links allow <domain>` · `{self.p(ctx)}links unallow <domain>`")
        await ctx.reply(embed=embed("🔗 Invite & link control", body), ephemeral=True)

    @links.command(name="invites", description="Toggle removing Discord server invites")
    @app_commands.describe(setting="on or off")
    @moderator("manage_guild")
    async def links_invites(self, ctx: commands.Context, setting: str) -> None:
        on = setting.strip().lower() not in OFF_WORDS
        self.save(ctx.guild.id, antiinvite=on)
        warn = "" if not (on and bot_lacks(ctx.guild, "manage_messages")) else " ⚠️ SYKE needs **Manage Messages**."
        await ctx.reply(embed=embed("Saved ✅", f"Anti-invite is **{'on' if on else 'off'}**.{warn}"), ephemeral=True)

    @links.command(name="urls", aliases=["links"], description="Toggle removing links that aren't allowlisted")
    @app_commands.describe(setting="on or off")
    @moderator("manage_guild")
    async def links_urls(self, ctx: commands.Context, setting: str) -> None:
        on = setting.strip().lower() not in OFF_WORDS
        self.save(ctx.guild.id, antilink=on)
        tip = f" Add safe domains with `{self.p(ctx)}links allow youtube.com`." if on else ""
        await ctx.reply(embed=embed("Saved ✅", f"Anti-link is **{'on' if on else 'off'}**.{tip}"), ephemeral=True)

    @links.command(name="allow", description="Allow a domain through the link filter")
    @app_commands.describe(domain="e.g. youtube.com")
    @moderator("manage_guild")
    async def links_allow(self, ctx: commands.Context, domain: str) -> None:
        domain = domain.strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0]
        if not domain or "." not in domain:
            await ctx.reply(embed=problem("Which domain?", "Give a domain like `youtube.com`."), ephemeral=True)
            return
        allow = list(cfg(self.config(ctx.guild.id), "allow_domains"))
        if domain in allow:
            await ctx.reply(embed=problem("Already allowed", f"`{domain}` is already on the allowlist."),
                            ephemeral=True)
            return
        if len(allow) >= MAX_DOMAINS:
            await ctx.reply(embed=problem("Allowlist full", f"Up to {MAX_DOMAINS} domains."), ephemeral=True)
            return
        allow.append(domain)
        self.save(ctx.guild.id, allow_domains=allow)
        await ctx.reply(embed=embed("Allowed ✅", f"Links to `{domain}` are allowed."), ephemeral=True)

    @links.command(name="unallow", aliases=["disallow", "remove"], description="Remove a domain from the allowlist")
    @app_commands.describe(domain="The domain to remove")
    @moderator("manage_guild")
    async def links_unallow(self, ctx: commands.Context, domain: str) -> None:
        domain = domain.strip().lower().removeprefix("www.")
        allow = list(cfg(self.config(ctx.guild.id), "allow_domains"))
        if domain not in allow:
            await ctx.reply(embed=problem("Not allowed anyway", f"`{domain}` isn't on the allowlist."), ephemeral=True)
            return
        allow.remove(domain)
        self.save(ctx.guild.id, allow_domains=allow)
        await ctx.reply(embed=embed("Removed ✅", f"`{domain}` is no longer allowlisted."), ephemeral=True)

    # Anti-spam

    @commands.hybrid_group(name="antispam", fallback="status", case_insensitive=True,
                           description="Catch floods, duplicates, mass mentions and caps")
    @moderator("manage_guild")
    async def antispam(self, ctx: commands.Context) -> None:
        c = self.config(ctx.guild.id)
        body = (f"**Status:** {'🟢 on' if cfg(c, 'antispam') else '⚪ off'}\n"
                f"**Flood:** {cfg(c, 'flood_count')} messages in {cfg(c, 'flood_seconds')}s\n"
                f"**Duplicates:** {cfg(c, 'dup_count')} identical messages\n"
                f"**Mass mentions:** {cfg(c, 'mention_limit')}+ in one message\n"
                f"**Caps:** {cfg(c, 'caps_len')}+ chars, mostly uppercase\n"
                f"**Punishment:** timeout {human_duration(cfg(c, 'spam_timeout'))}\n\n"
                f"`{self.p(ctx)}antispam on|off` · `{self.p(ctx)}antispam rate <count> <seconds>` · "
                f"`{self.p(ctx)}antispam mentions <n>` · `{self.p(ctx)}antispam duration <10m>`")
        await ctx.reply(embed=embed("🚦 Anti-spam", body), ephemeral=True)

    @antispam.command(name="on", description="Turn anti-spam on")
    @moderator("manage_guild")
    async def antispam_on(self, ctx: commands.Context) -> None:
        self.save(ctx.guild.id, antispam=True)
        warn = "" if not bot_lacks(ctx.guild, "moderate_members") else " ⚠️ SYKE needs **Timeout Members**."
        await ctx.reply(embed=embed("Anti-spam on 🚦", f"Floods, duplicates, mass mentions and caps get a timeout.{warn}"),
                        ephemeral=True)

    @antispam.command(name="off", description="Turn anti-spam off")
    @moderator("manage_guild")
    async def antispam_off(self, ctx: commands.Context) -> None:
        self.save(ctx.guild.id, antispam=False)
        await ctx.reply(embed=embed("Anti-spam off", "SYKE will stop catching spam."), ephemeral=True)

    @antispam.command(name="rate", description="Flood limit: how many messages in how many seconds")
    @app_commands.describe(count="Messages", seconds="Within this many seconds")
    @moderator("manage_guild")
    async def antispam_rate(self, ctx: commands.Context, count: commands.Range[int, 3, 30],
                            seconds: commands.Range[int, 1, 60]) -> None:
        self.save(ctx.guild.id, flood_count=int(count), flood_seconds=int(seconds))
        await ctx.reply(embed=embed("Saved ✅", f"Flood limit: {count} messages per {seconds}s."), ephemeral=True)

    @antispam.command(name="mentions", description="How many mentions in one message count as spam")
    @app_commands.describe(count="Mention limit")
    @moderator("manage_guild")
    async def antispam_mentions(self, ctx: commands.Context, count: commands.Range[int, 3, 50]) -> None:
        self.save(ctx.guild.id, mention_limit=int(count))
        await ctx.reply(embed=embed("Saved ✅", f"{count}+ mentions in one message is spam."), ephemeral=True)

    @antispam.command(name="duration", description="How long the anti-spam timeout lasts")
    @app_commands.describe(duration="e.g. 5m, 1h")
    @moderator("manage_guild")
    async def antispam_duration(self, ctx: commands.Context, duration: str) -> None:
        seconds = parse_duration(duration)
        if not seconds:
            await ctx.reply(embed=problem("How long?", "Try `antispam duration 10m`."), ephemeral=True)
            return
        self.save(ctx.guild.id, spam_timeout=min(seconds, MAX_TIMEOUT))
        await ctx.reply(embed=embed("Saved ✅", f"Spam timeouts last {human_duration(min(seconds, MAX_TIMEOUT))}."),
                        ephemeral=True)


def help_lines(p: str) -> str:
    return (
        f"`{p}automod` — overview of the filter, links and anti-spam (admins)\n"
        f"`{p}filter on|off` · `{p}filter add <word>` — block words/slurs, auto-delete\n"
        f"`{p}links invites on` · `{p}links urls on` · `{p}links allow <domain>` — invite & link control\n"
        f"`{p}antispam on` — catch floods, duplicates, mass mentions and caps\n"
        f"`{p}automod toxic #channel` — flag toxic messages for review instead of deleting"
    )
