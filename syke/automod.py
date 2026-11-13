"""Automod: a word/slur filter, invite & link control, and anti-spam.

All three run from one on_message listener and share a per-server config blob (storage key "automod").
People who can already moderate — administrators, anyone with Manage Messages, SYKE owners, or a role
the server marks as exempt — are never caught. Every action is a numbered case and goes to the mod log.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

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

INVITE_RE = re.compile(r"\b(?:discord\.gg|discord(?:app)?\.com/invite|discord\.me|dsc\.gg|disboard\.org/server/join)/\S+",
                       re.IGNORECASE)
URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)
DOMAIN_RE = re.compile(r"https?://([^/\s:]+)", re.IGNORECASE)
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})

DEFAULTS = {
    "filter_on": False, "words": [], "filter_action": "delete", "filter_timeout": 600,
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


def normalize(text: str) -> str:
    """Lowercase, de-leet and strip non-letters, so 'f.u_c k' and 'fu0ck' still match 'fuck'."""
    return re.sub(r"[^a-z0-9]", "", text.lower().translate(LEET))


def filter_hit(content: str, words: list[str]) -> str | None:
    """Return the blacklisted word that `content` triggers, or None.

    Matches whole words (so 'ass' never fires inside 'class'), de-leetspeak ('fu0ck' → 'fuck'),
    and long blacklisted words embedded or spaced out ('b i t c h'). Embedded matching is limited to
    5+ character words to avoid false positives on short ones.
    """
    if not words:
        return None
    lowered = content.lower()
    collapsed = normalize(content)                                   # de-leeted, letters only
    tokens = {normalize(t) for t in re.findall(r"[a-z0-9']+", lowered)}
    tokens.discard("")
    for word in words:
        w = word.lower().strip()
        if not w:
            continue
        nw = normalize(w)
        if " " in w:
            if w in lowered or (nw and nw in collapsed):
                return word
        elif nw in tokens or (len(nw) >= 5 and nw in collapsed):
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
                + f"\n**Words ({len(words)}):** {shown}{more}\n\n"
                f"`{self.p(ctx)}filter on|off` · `{self.p(ctx)}filter add <word>` · `{self.p(ctx)}filter remove <word>` · "
                f"`{self.p(ctx)}filter action delete|warn|timeout [10m]` · `{self.p(ctx)}filter clear`")
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
