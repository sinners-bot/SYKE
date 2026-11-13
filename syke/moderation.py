"""Mini moderation: restrict what a member can post, timeouts, warnings, purge, lock, kick and ban.

Restrictions work in two layers, so they hold server-wide:

1. A role per restriction ("SYKE · no images", ...) denied that permission in every channel and
   category. New channels get the overwrite as they are created. Discord greys out the upload
   button, so most of the time nothing gets through at all.
2. A listener that deletes anything that still slips through: a role elsewhere that explicitly
   allows the permission, an administrator, a forwarded message, a server sticker (no Discord
   permission covers those), or a reaction.

Timed restrictions are stored in SQLite and lifted by a background loop, so they survive restarts.
Leaving and rejoining doesn't shake them off either.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Optional, TypeVar

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("syke.moderation")

BRAND = discord.Colour.from_str("#8B5CF6")
WARN = discord.Colour.from_str("#F59E0B")
DANGER = discord.Colour.from_str("#EF4444")
OK = discord.Colour.from_str("#22C55E")

F = TypeVar("F", bound=Callable)

# kind -> (label, channel permissions the role is denied)
KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "images": ("images & files", ("attach_files",)),
    "links": ("links & embeds", ("embed_links",)),
    "reactions": ("reactions", ("add_reactions",)),
    "stickers": ("stickers", ("use_external_stickers",)),
    "emojis": ("external emojis", ("use_external_emojis",)),
}
KIND_ORDER = tuple(KINDS)
DEFAULT_KINDS = ("images", "links")
KIND_ALIASES: dict[str, tuple[str, ...]] = {
    **{k: (k,) for k in KINDS},
    **{w: ("images",) for w in (
        "image", "img", "imgs", "pic", "pics", "picture", "pictures", "photo", "photos", "file", "files",
        "attachment", "attachments", "upload", "uploads", "video", "videos",
    )},
    **{w: ("links",) for w in ("link", "embed", "embeds", "url", "urls", "gif", "gifs")},
    **{w: ("reactions",) for w in ("reaction", "react", "reacts")},
    **{w: ("stickers",) for w in ("sticker",)},
    **{w: ("emojis",) for w in ("emoji", "emote", "emotes", "nitro")},
    "media": ("images", "links", "stickers"),
    "all": KIND_ORDER,
    "everything": KIND_ORDER,
}
ROLE_NAME = "SYKE · no {kind}"
PERMANENT_WORDS = {"perm", "permanent", "forever", "indefinite", "indefinitely", "never"}
OFF_WORDS = {"off", "none", "0", "disable", "disabled", "reset", "clear"}
FILLER_WORDS = {"and", "&", "+", "for"}

UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "week": 604800, "weeks": 604800,
}
DURATION_RE = re.compile(r"(?:\d+[a-z]*)+")
DURATION_PART = re.compile(r"(\d+)([a-z]*)")
URL_RE = re.compile(r"(?:https?://|www\.)\S+|discord(?:\.gg|(?:app)?\.com/invite)/\S+", re.IGNORECASE)
CUSTOM_EMOJI = re.compile(r"<a?:\w{2,32}:(\d{15,25})>")

MAX_TIMEOUT = 28 * 86400
MAX_SLOWMODE = 21600
MAX_PURGE = 500
PURGE_SCAN = 1000
BULK_WINDOW = timedelta(days=14)
NOTICE_COOLDOWN = 20
EXPIRY_SECONDS = 30
LIST_LIMIT = 15
REASON_LIMIT = 400


# Parsing

def parse_duration(text: str) -> int | None:
    """Seconds in `10m`, `1h30m`, `2d`, `1 week`-style text (one token). A bare number is minutes.

    Returns None when the token isn't a duration.
    """
    token = (text or "").strip().lower()
    if not token:
        return None
    if token.isdigit():
        return int(token) * 60
    if not DURATION_RE.fullmatch(token):
        return None
    total = 0
    for amount, unit in DURATION_PART.findall(token):
        if unit not in UNITS:
            return None
        total += int(amount) * UNITS[unit]
    return total


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "permanently"
    seconds = int(round(seconds))
    if seconds <= 0:
        return "0s"
    parts = []
    for name, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{name}")
            seconds %= size
        if len(parts) == 2:
            break
    return " ".join(parts)


def parse_kinds(words: list[str]) -> tuple[tuple[str, ...], list[str]]:
    """Restriction kinds at the start of `words`, plus whatever is left over."""
    found: list[str] = []
    rest = list(words)
    while rest:
        parts = [p for p in re.split(r"[,+/&]", rest[0].lower()) if p]
        if not parts or not all(p in KIND_ALIASES or p in FILLER_WORDS for p in parts):
            break
        for part in parts:
            for kind in KIND_ALIASES.get(part, ()):
                if kind not in found:
                    found.append(kind)
        rest.pop(0)
    return tuple(k for k in KIND_ORDER if k in found), rest


def parse_restrict(text: str) -> tuple[tuple[str, ...], int | None, str]:
    """`images links 1h being annoying` -> (kinds, seconds or None for permanent, reason).

    Kinds and the duration can come in any order before the reason. No kinds means images and links.
    """
    words = (text or "").split()
    kinds: list[str] = []
    seconds: int | None = None
    while words:
        found, rest = parse_kinds(words)
        if found:
            kinds.extend(k for k in found if k not in kinds)
            words = rest
            continue
        head = words[0].lower()
        if head in FILLER_WORDS:
            words.pop(0)
            continue
        if head in PERMANENT_WORDS:
            seconds = None
            words.pop(0)
            continue
        duration = parse_duration(head)
        if duration is not None and duration > 0:
            seconds = duration
            words.pop(0)
            continue
        break
    chosen = tuple(k for k in KIND_ORDER if k in kinds) or DEFAULT_KINDS
    return chosen, seconds, clean_reason(" ".join(words))


def clean_reason(text: str) -> str:
    text = " ".join((text or "").split())
    return text[:REASON_LIMIT]


def kind_labels(kinds) -> str:
    labels = [KINDS[k][0] for k in KIND_ORDER if k in kinds]
    if len(labels) <= 1:
        return labels[0] if labels else "nothing"
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def violation(message, kinds, own_emoji_ids: set[int]) -> str | None:
    """What in `message` breaks one of these restrictions, or None."""
    snapshots = list(getattr(message, "message_snapshots", None) or [])
    content = getattr(message, "content", "") or ""
    pieces = [content] + [getattr(s, "content", "") or "" for s in snapshots]
    if "images" in kinds:
        if getattr(message, "attachments", None) or any(getattr(s, "attachments", None) for s in snapshots):
            return KINDS["images"][0]
    if "links" in kinds:
        if any(URL_RE.search(p) for p in pieces):
            return KINDS["links"][0]
        if getattr(message, "embeds", None) or any(getattr(s, "embeds", None) for s in snapshots):
            return KINDS["links"][0]
    if "stickers" in kinds:
        if getattr(message, "stickers", None) or any(getattr(s, "stickers", None) for s in snapshots):
            return KINDS["stickers"][0]
    if "emojis" in kinds:
        for piece in pieces:
            if any(int(eid) not in own_emoji_ids for eid in CUSTOM_EMOJI.findall(piece)):
                return KINDS["emojis"][0]
    return None


# Who can do what

def _top_position(member) -> int:
    return getattr(getattr(member, "top_role", None), "position", 0) or 0


def blocker(guild, moderator, target, *, owner_ids=frozenset(), bot_above: bool = True) -> str | None:
    """Why `moderator` can't act on `target`, or None if they can."""
    me = getattr(guild, "me", None)
    if getattr(target, "id", None) == getattr(moderator, "id", None):
        return "You can't do that to yourself."
    if me is not None and getattr(target, "id", None) == me.id:
        return "Nice try. SYKE doesn't moderate itself."
    if getattr(target, "id", None) == getattr(guild, "owner_id", None):
        return "That's the server owner. Nobody outranks them."
    if not hasattr(target, "top_role"):
        return None  # Not in the server (bans by ID), so there's no role order to respect.
    moderator_is_boss = (
        getattr(moderator, "id", None) == getattr(guild, "owner_id", None)
        or getattr(moderator, "id", None) in owner_ids
    )
    if not moderator_is_boss and _top_position(target) >= _top_position(moderator):
        return f"{target.mention}'s top role is at or above yours."
    if bot_above and me is not None and _top_position(target) >= _top_position(me):
        return f"Drag SYKE's role above {target.mention}'s top role, then try again."
    return None


def _mod_predicate(permission: str):
    async def predicate(ctx: commands.Context) -> bool:
        author = ctx.author
        if author.id in ctx.bot.settings.owner_ids:
            return True
        perms = getattr(author, "guild_permissions", None)
        if perms is not None and (perms.administrator or getattr(perms, permission, False)):
            return True
        raise commands.MissingPermissions([permission])
    predicate.permission = permission  # type: ignore[attr-defined]
    return predicate


def moderator(permission: str) -> Callable[[F], F]:
    """Needs this Discord permission (or Administrator, or SYKE_OWNER_IDS). Servers only."""
    def decorator(func: F) -> F:
        func = app_commands.default_permissions(**{permission: True})(func)
        func = commands.check(_mod_predicate(permission))(func)
        func = app_commands.guild_only()(func)
        return commands.guild_only()(func)
    return decorator


def bot_lacks(guild, *permissions: str) -> list[str]:
    perms = getattr(getattr(guild, "me", None), "guild_permissions", None)
    if perms is None or perms.administrator:
        return []
    return [p for p in permissions if not getattr(perms, p, False)]


def pretty(permission: str) -> str:
    return permission.replace("_", " ").title().replace("Guild", "Server").replace("Emojis", "Expressions")


# Embeds

def embed(title: str, body: str = "", colour: discord.Colour = BRAND) -> discord.Embed:
    return discord.Embed(title=title, description=body, colour=colour)


def problem(title: str, body: str) -> discord.Embed:
    return embed(title, body, WARN)


def needs(guild_perms: list[str]) -> discord.Embed:
    listed = ", ".join(f"**{pretty(p)}**" for p in guild_perms)
    return problem("SYKE can't do that yet", f"Give SYKE's role {listed}, then try again.")


def until(seconds: float | None) -> str:
    if seconds is None:
        return "permanently"
    return f"for {human_duration(seconds)} (ends <t:{int(time.time() + seconds)}:R>)"


def action_embed(title: str, target, moderator, *, reason: str = "", duration: str | None = None,
                 case: int | None = None, colour: discord.Colour = BRAND, extra: str = "") -> discord.Embed:
    lines = [f"**Member:** {getattr(target, 'mention', target)} (`{getattr(target, 'id', '?')}`)"]
    if moderator is not None:
        lines.append(f"**By:** {getattr(moderator, 'mention', moderator)}")
    if duration:
        lines.append(f"**Length:** {duration}")
    lines.append(f"**Reason:** {reason or 'none given'}")
    if extra:
        lines.append(extra)
    result = embed(title, "\n".join(lines), colour)
    if case is not None:
        result.set_footer(text=f"Case #{case}")
    return result


def dm_embed(guild, what: str, *, reason: str = "", duration: str | None = None) -> discord.Embed:
    body = f"You were **{what}** in **{getattr(guild, 'name', 'a server')}**."
    if duration:
        body += f"\n**Length:** {duration}"
    body += f"\n**Reason:** {reason or 'none given'}"
    return embed("Moderation notice", body, WARN)


def case_line(number: int, action: str, reason: str, moderator_id: int, created_at: float,
              duration: float | None) -> str:
    length = f" · {human_duration(duration)}" if duration else ""
    text = reason or "no reason"
    if len(text) > 80:
        text = text[:79] + "…"
    return f"`#{number}` **{action}**{length} · <t:{int(created_at)}:d> by <@{moderator_id}> — {text}"


# The cog

class Moderation(commands.Cog, name="Moderation"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._active: dict[int, dict[int, dict[str, float | None]]] = {}
        self._noticed: dict[tuple[int, int], float] = {}
        self._overwrite_jobs: dict[tuple[int, str], asyncio.Task] = {}

    async def cog_load(self) -> None:
        if not self.expire.is_running():
            self.expire.start()

    async def cog_unload(self) -> None:
        self.expire.cancel()

    def p(self, ctx: commands.Context) -> str:
        return self.bot.display_prefix(ctx.guild.id)

    # Active restrictions, cached per server for the message listener

    def active(self, guild_id: int, user_id: int) -> set[str]:
        server = self._active.get(guild_id)
        if server is None:
            server = {}
            for uid, kind, expires_at, *_ in self.bot.storage.restrictions(guild_id):
                server.setdefault(uid, {})[kind] = expires_at
            self._active[guild_id] = server
        now = time.time()
        return {k for k, ends in server.get(user_id, {}).items() if ends is None or ends > now}

    def forget_cache(self, guild_id: int) -> None:
        self._active.pop(guild_id, None)

    # Roles and channel overwrites

    async def ensure_role(self, guild: discord.Guild, kind: str) -> tuple[discord.Role, bool]:
        role_id = self.bot.storage.restrict_role(guild.id, kind)
        role = guild.get_role(role_id) if role_id else None
        if role is not None:
            return role, False
        role = await guild.create_role(
            name=ROLE_NAME.format(kind=kind), permissions=discord.Permissions.none(),
            mentionable=False, reason=f"SYKE restriction role: no {KINDS[kind][0]}",
        )
        self.bot.storage.set_restrict_role(guild.id, kind, role.id)
        return role, True

    async def apply_overwrites(self, guild: discord.Guild, role: discord.Role, kind: str) -> tuple[int, int]:
        """Deny this kind's permissions to `role` everywhere. Returns (channels changed, channels refused)."""
        denied = KINDS[kind][1]
        changed = refused = 0
        channels = sorted(guild.channels, key=lambda c: not isinstance(c, discord.CategoryChannel))
        for channel in channels:
            overwrite = channel.overwrites_for(role)
            if all(getattr(overwrite, p) is False for p in denied):
                continue
            for p in denied:
                setattr(overwrite, p, False)
            try:
                await channel.set_permissions(role, overwrite=overwrite, reason="SYKE restriction")
                changed += 1
            except discord.HTTPException:
                refused += 1
        if refused:
            log.warning("Restriction %s: %d channel(s) refused the overwrite in %s", kind, refused, guild.id)
        return changed, refused

    def queue_overwrites(self, guild: discord.Guild, role: discord.Role, kind: str) -> None:
        key = (guild.id, kind)
        job = self._overwrite_jobs.get(key)
        if job is not None and not job.done():
            return
        self._overwrite_jobs[key] = self.bot.spawn(self.apply_overwrites(guild, role, kind))

    async def roles_for(self, guild: discord.Guild, kinds) -> list[discord.Role]:
        roles = []
        for kind in kinds:
            role_id = self.bot.storage.restrict_role(guild.id, kind)
            role = guild.get_role(role_id) if role_id else None
            if role is not None:
                roles.append(role)
        return roles

    # Logging and DMs

    async def log(self, guild: discord.Guild, entry: discord.Embed) -> None:
        channel_id = self.bot.storage.modlog_channel(guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        entry.timestamp = datetime.now(timezone.utc)
        with contextlib.suppress(discord.HTTPException):
            await channel.send(embed=entry)

    async def tell(self, user, guild, what: str, *, reason: str = "", duration: str | None = None) -> bool:
        try:
            await user.send(embed=dm_embed(guild, what, reason=reason, duration=duration))
        except (discord.HTTPException, AttributeError):
            return False
        return True

    async def finish(self, ctx: commands.Context, result: discord.Embed) -> None:
        await ctx.reply(embed=result)
        await self.log(ctx.guild, result.copy())

    # Restrict

    @commands.hybrid_command(
        name="restrict", aliases=["mediaban", "nomedia"], usage="@member [what] [duration] [reason]",
        description="Stop a member posting images, links, reactions, stickers or external emojis server-wide",
    )
    @app_commands.describe(
        member="Who to restrict",
        details="What, how long and why, e.g. 'images links 1h spamming'. Empty = images and links, permanently.",
    )
    @moderator("manage_roles")
    async def restrict(self, ctx: commands.Context, member: discord.Member, *, details: str = "") -> None:
        guild = ctx.guild
        if member.bot:
            await ctx.reply(embed=problem("Bots aren't restricted", "Change the bot's own role permissions instead."),
                            ephemeral=True)
            return
        reason_blocked = blocker(guild, ctx.author, member, owner_ids=self.bot.settings.owner_ids, bot_above=False)
        if reason_blocked:
            await ctx.reply(embed=problem("Can't restrict them", reason_blocked), ephemeral=True)
            return
        missing = bot_lacks(guild, "manage_roles")
        if missing:
            await ctx.reply(embed=needs(missing), ephemeral=True)
            return
        kinds, seconds, reason = parse_restrict(details)
        async with ctx.typing():
            roles = []
            try:
                for kind in kinds:
                    role, created = await self.ensure_role(guild, kind)
                    roles.append(role)
                    if created:
                        self.queue_overwrites(guild, role, kind)
                await member.add_roles(*roles, reason=f"SYKE restrict by {ctx.author}: {reason or 'no reason'}")
            except discord.Forbidden:
                await ctx.reply(embed=problem(
                    "Discord said no",
                    "SYKE needs **Manage Roles**, and its role must be above the `SYKE · no …` roles.",
                ), ephemeral=True)
                return
            expires_at = time.time() + seconds if seconds else None
            for kind in kinds:
                self.bot.storage.restrict(guild.id, member.id, kind, expires_at=expires_at,
                                          reason=reason, moderator_id=ctx.author.id)
            self.forget_cache(guild.id)
            length = until(seconds)
            case = self.bot.storage.add_case(guild.id, member.id, ctx.author.id,
                                             f"restrict ({', '.join(kinds)})", reason, seconds)
            await self.tell(member, guild, f"restricted from posting {kind_labels(kinds)}",
                            reason=reason, duration=length)
        notes = []
        if bot_lacks(guild, "manage_messages"):
            notes.append("Give SYKE **Manage Messages** too, so it can delete anything that slips through.")
        if member.guild_permissions.administrator:
            notes.append("They're an administrator, so channel permissions don't apply. "
                         "SYKE deletes what they post instead.")
        await self.finish(ctx, action_embed(
            f"🔇 No {kind_labels(kinds)} for {member.display_name}", member, ctx.author,
            reason=reason, duration=length, case=case, colour=WARN, extra="\n".join(notes),
        ))

    @commands.hybrid_command(name="unrestrict", aliases=["unmediaban"], usage="@member [what]",
                             description="Lift a member's restrictions")
    @app_commands.describe(member="Who to unrestrict", what="Which restrictions to lift, e.g. 'images'. Empty = all.")
    @moderator("manage_roles")
    async def unrestrict(self, ctx: commands.Context, member: discord.Member, *, what: str = "") -> None:
        guild = ctx.guild
        kinds, _ = parse_kinds(what.split())
        lifted = self.bot.storage.unrestrict(guild.id, member.id, list(kinds) if kinds else None)
        self.forget_cache(guild.id)
        roles = [r for r in await self.roles_for(guild, kinds or KIND_ORDER) if r in member.roles]
        if roles:
            with contextlib.suppress(discord.HTTPException):
                await member.remove_roles(*roles, reason=f"SYKE unrestrict by {ctx.author}")
        if not lifted and not roles:
            await ctx.reply(embed=problem("Nothing to lift", f"{member.mention} isn't restricted."), ephemeral=True)
            return
        role_ids = {r.id for r in roles}
        names = lifted or [k for k, rid in self.bot.storage.restrict_roles(guild.id).items() if rid in role_ids]
        case = self.bot.storage.add_case(guild.id, member.id, ctx.author.id, f"unrestrict ({', '.join(names)})")
        await self.finish(ctx, action_embed(
            f"🔊 {member.display_name} can post {kind_labels(names)} again", member, ctx.author,
            case=case, colour=OK,
        ))

    @commands.hybrid_command(name="restrictions", aliases=["restricted"],
                             description="Who is restricted, or re-apply the restriction roles to every channel")
    @app_commands.describe(action="list: show who is restricted. sync: re-apply the roles to every channel.")
    @moderator("manage_roles")
    async def restrictions(self, ctx: commands.Context, action: Literal["list", "sync"] = "list") -> None:
        guild = ctx.guild
        if action == "sync":
            async with ctx.typing():
                changed = refused = 0
                for kind, role_id in self.bot.storage.restrict_roles(guild.id).items():
                    role = guild.get_role(role_id)
                    if role is None:
                        continue
                    done, failed = await self.apply_overwrites(guild, role, kind)
                    changed += done
                    refused += failed
                fixed = await self.reapply_roles(guild)
            body = f"Updated {changed} channel overwrite(s) and gave back {fixed} missing role(s)."
            if refused:
                body += (f"\n{refused} channel(s) refused. SYKE needs **Manage Roles** (Manage Permissions) "
                         "and **View Channel** in them.")
            await ctx.reply(embed=embed("Restrictions synced ✅", body), ephemeral=True)
            return
        rows = self.bot.storage.restrictions(guild.id)
        now = time.time()
        people: dict[int, list[tuple[str, float | None]]] = {}
        for uid, kind, expires_at, *_ in rows:
            if expires_at is None or expires_at > now:
                people.setdefault(uid, []).append((kind, expires_at))
        if not people:
            await ctx.reply(embed=embed("Nobody is restricted", f"Use `{self.p(ctx)}restrict @member` to start."),
                            ephemeral=True)
            return
        lines = []
        for uid, items in list(people.items())[:LIST_LIMIT * 2]:
            parts = []
            for kind, ends in items:
                parts.append(f"{kind} (ends <t:{int(ends)}:R>)" if ends else f"{kind} (permanent)")
            lines.append(f"<@{uid}> — {', '.join(parts)}")
        await ctx.reply(embed=embed(f"🔇 Restricted members ({len(people)})", "\n".join(lines)), ephemeral=True)

    async def reapply_roles(self, guild: discord.Guild) -> int:
        """Give restricted members back any restriction role they lost. Returns how many roles."""
        fixed = 0
        roles = {k: guild.get_role(rid) for k, rid in self.bot.storage.restrict_roles(guild.id).items()}
        now = time.time()
        for uid, kind, expires_at, *_ in self.bot.storage.restrictions(guild.id):
            role = roles.get(kind)
            member = guild.get_member(uid)
            if role is None or member is None or role in member.roles:
                continue
            if expires_at is not None and expires_at <= now:
                continue
            with contextlib.suppress(discord.HTTPException):
                await member.add_roles(role, reason="SYKE restriction still active")
                fixed += 1
        return fixed

    @tasks.loop(seconds=EXPIRY_SECONDS)
    async def expire(self) -> None:
        due: dict[tuple[int, int], list[str]] = {}
        for guild_id, user_id, kind in self.bot.storage.expired_restrictions(time.time()):
            due.setdefault((guild_id, user_id), []).append(kind)
        for (guild_id, user_id), kinds in due.items():
            try:
                await self.lift_expired(guild_id, user_id, kinds)
            except Exception:
                log.exception("Couldn't lift an expired restriction in %s", guild_id)
        for guild_id, user_id in self.bot.storage.due_temp_bans(time.time()):
            try:
                await self.lift_temp_ban(guild_id, user_id)
            except Exception:
                log.exception("Couldn't lift an expired temp ban in %s", guild_id)

    @expire.before_loop
    async def _before_expire(self) -> None:
        await self.bot.wait_until_ready()

    async def lift_temp_ban(self, guild_id: int, user_id: int) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return  # Try again once SYKE is back in that server.
        self.bot.storage.remove_temp_ban(guild_id, user_id)
        try:
            await guild.unban(discord.Object(id=user_id), reason="SYKE temp ban ended")
        except discord.NotFound:
            return  # Already unbanned by a mod.
        except discord.HTTPException:
            return
        self.bot.storage.add_case(guild_id, user_id, getattr(self.bot.user, "id", 0), "unban", "temp ban expired")
        await self.log(guild, embed("⏰ Temp ban ended", f"<@{user_id}> (`{user_id}`) was unbanned.", OK))

    async def lift_expired(self, guild_id: int, user_id: int, kinds: list[str]) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return  # Not in that server right now; try again once it's back.
        self.bot.storage.unrestrict(guild_id, user_id, kinds)
        self.forget_cache(guild_id)
        member = guild.get_member(user_id)
        if member is None:
            with contextlib.suppress(discord.HTTPException):
                member = await guild.fetch_member(user_id)
        if member is not None:
            roles = [r for r in await self.roles_for(guild, kinds) if r in member.roles]
            if roles:
                with contextlib.suppress(discord.HTTPException):
                    await member.remove_roles(*roles, reason="SYKE restriction ended")
        target = member.mention if member is not None else f"<@{user_id}>"
        await self.log(guild, embed("⏰ Restriction ended", f"{target} can post {kind_labels(kinds)} again.", OK))

    # Enforcement

    async def enforce(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot:
            return
        kinds = self.active(guild.id, message.author.id)
        if not kinds:
            return
        own = {e.id for e in getattr(guild, "emojis", ())}
        why = violation(message, kinds, own)
        if why is None:
            return
        try:
            await message.delete()
        except discord.NotFound:
            return
        except discord.HTTPException:
            return  # No Manage Messages here; the role overwrite is the only line of defence.
        key = (guild.id, message.author.id)
        now = time.monotonic()
        if now - self._noticed.get(key, 0) < NOTICE_COOLDOWN:
            return
        self._noticed[key] = now
        with contextlib.suppress(discord.HTTPException):
            await message.channel.send(
                f"{message.author.mention} you're restricted from posting {why} right now.",
                delete_after=8, allowed_mentions=discord.AllowedMentions(users=[message.author]),
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self.enforce(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        await self.enforce(after)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.user_id == getattr(self.bot.user, "id", None):
            return
        if "reactions" not in self.active(payload.guild_id, payload.user_id):
            return
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        with contextlib.suppress(discord.HTTPException):
            await channel.get_partial_message(payload.message_id).remove_reaction(
                payload.emoji, discord.Object(id=payload.user_id),
            )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        kinds = self.active(member.guild.id, member.id)
        if not kinds:
            return
        roles = await self.roles_for(member.guild, kinds)
        if roles:
            with contextlib.suppress(discord.HTTPException):
                await member.add_roles(*roles, reason="SYKE restriction still active after rejoining")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        for kind, role_id in self.bot.storage.restrict_roles(channel.guild.id).items():
            role = channel.guild.get_role(role_id)
            if role is None:
                continue
            overwrite = channel.overwrites_for(role)
            if all(getattr(overwrite, p) is False for p in KINDS[kind][1]):
                continue
            for p in KINDS[kind][1]:
                setattr(overwrite, p, False)
            with contextlib.suppress(discord.HTTPException):
                await channel.set_permissions(role, overwrite=overwrite, reason="SYKE restriction")

    # Timeouts

    @commands.hybrid_command(name="timeout", aliases=["mute", "shush"], usage="@member <duration> [reason]",
                             description="Time a member out: they can't talk, react or join voice")
    @app_commands.describe(member="Who to time out", duration="How long, e.g. 10m, 2h, 1d (max 28d)",
                           reason="Why")
    @moderator("moderate_members")
    async def timeout(self, ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "") -> None:
        guild = ctx.guild
        seconds = parse_duration(duration)
        if not seconds:
            await ctx.reply(embed=problem("How long?", f"Try `{self.p(ctx)}timeout @member 10m spamming`."),
                            ephemeral=True)
            return
        if seconds > MAX_TIMEOUT:
            await ctx.reply(embed=problem("Too long", "Discord caps timeouts at 28 days."), ephemeral=True)
            return
        blocked = blocker(guild, ctx.author, member, owner_ids=self.bot.settings.owner_ids)
        if blocked:
            await ctx.reply(embed=problem("Can't time them out", blocked), ephemeral=True)
            return
        if member.guild_permissions.administrator:
            await ctx.reply(embed=problem("Can't time them out", "Discord doesn't allow timing out administrators."),
                            ephemeral=True)
            return
        missing = bot_lacks(guild, "moderate_members")
        if missing:
            await ctx.reply(embed=needs(missing), ephemeral=True)
            return
        reason = clean_reason(reason)
        try:
            await member.timeout(timedelta(seconds=seconds), reason=f"{ctx.author}: {reason or 'no reason'}")
        except discord.HTTPException:
            await ctx.reply(embed=problem("Discord said no", "Check SYKE's role is above theirs."), ephemeral=True)
            return
        length = until(seconds)
        case = self.bot.storage.add_case(guild.id, member.id, ctx.author.id, "timeout", reason, seconds)
        await self.tell(member, guild, "timed out", reason=reason, duration=length)
        await self.finish(ctx, action_embed(f"⏳ {member.display_name} timed out", member, ctx.author,
                                            reason=reason, duration=length, case=case, colour=WARN))

    @commands.hybrid_command(name="untimeout", aliases=["unmute", "unshush"], description="End a member's timeout")
    @app_commands.describe(member="Whose timeout to end")
    @moderator("moderate_members")
    async def untimeout(self, ctx: commands.Context, member: discord.Member, *, reason: str = "") -> None:
        if not member.is_timed_out():
            await ctx.reply(embed=problem("Not timed out", f"{member.mention} can already talk."), ephemeral=True)
            return
        missing = bot_lacks(ctx.guild, "moderate_members")
        if missing:
            await ctx.reply(embed=needs(missing), ephemeral=True)
            return
        reason = clean_reason(reason)
        try:
            await member.timeout(None, reason=f"{ctx.author}: {reason or 'timeout ended early'}")
        except discord.HTTPException:
            await ctx.reply(embed=problem("Discord said no", "Check SYKE's role is above theirs."), ephemeral=True)
            return
        case = self.bot.storage.add_case(ctx.guild.id, member.id, ctx.author.id, "untimeout", reason)
        await self.finish(ctx, action_embed(f"🔊 {member.display_name}'s timeout ended", member, ctx.author,
                                            reason=reason, case=case, colour=OK))

    # Warnings and history

    @commands.hybrid_command(name="warn", usage="@member <reason>", description="Warn a member (kept in their history)")
    @app_commands.describe(member="Who to warn", reason="What they did")
    @moderator("moderate_members")
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str) -> None:
        if member.bot:
            await ctx.reply(embed=problem("It's a bot", "Bots don't read warnings."), ephemeral=True)
            return
        blocked = blocker(ctx.guild, ctx.author, member, owner_ids=self.bot.settings.owner_ids, bot_above=False)
        if blocked:
            await ctx.reply(embed=problem("Can't warn them", blocked), ephemeral=True)
            return
        reason = clean_reason(reason)
        case = self.bot.storage.add_case(ctx.guild.id, member.id, ctx.author.id, "warn", reason)
        total = len(self.bot.storage.cases(ctx.guild.id, member.id, "warn"))
        told = await self.tell(member, ctx.guild, "warned", reason=reason)
        extra = f"**Warnings:** {total}" + ("" if told else " · couldn't DM them")
        await self.finish(ctx, action_embed(f"⚠️ {member.display_name} warned", member, ctx.author,
                                            reason=reason, case=case, colour=WARN, extra=extra))

    @commands.hybrid_command(name="warnings", aliases=["warns"], description="A member's warnings")
    @app_commands.describe(member="Whose warnings to show")
    @moderator("moderate_members")
    async def warnings(self, ctx: commands.Context, member: discord.Member) -> None:
        rows = self.bot.storage.cases(ctx.guild.id, member.id, "warn")
        if not rows:
            await ctx.reply(embed=embed(f"{member.display_name} has no warnings", "Clean record. Suspicious."),
                            ephemeral=True)
            return
        lines = [case_line(n, a, r, m, c, d) for n, m, a, r, d, c in rows[:LIST_LIMIT]]
        more = f"\n…and {len(rows) - LIST_LIMIT} older" if len(rows) > LIST_LIMIT else ""
        await ctx.reply(embed=embed(f"⚠️ {member.display_name}: {len(rows)} warning(s)", "\n".join(lines) + more),
                        ephemeral=True)

    @commands.hybrid_command(name="history", aliases=["modlogs", "cases", "infractions"],
                             description="Every moderation action on a member")
    @app_commands.describe(member="Whose history to show")
    @moderator("moderate_members")
    async def history(self, ctx: commands.Context, member: discord.User) -> None:
        rows = self.bot.storage.cases(ctx.guild.id, member.id)
        if not rows:
            await ctx.reply(embed=embed(f"No history for {member.display_name}", "Nothing on file."), ephemeral=True)
            return
        lines = [case_line(n, a, r, m, c, d) for n, m, a, r, d, c in rows[:LIST_LIMIT]]
        more = f"\n…and {len(rows) - LIST_LIMIT} older" if len(rows) > LIST_LIMIT else ""
        await ctx.reply(embed=embed(f"📁 {member.display_name}: {len(rows)} case(s)", "\n".join(lines) + more),
                        ephemeral=True)

    @commands.hybrid_command(name="delwarn", aliases=["unwarn", "rmwarn"], usage="<case number>",
                             description="Delete one warning by its case number")
    @app_commands.describe(case="The case number, shown as #12 in warnings")
    @moderator("moderate_members")
    async def delwarn(self, ctx: commands.Context, case: int) -> None:
        found = self.bot.storage.case(ctx.guild.id, case)
        if found is None or found[2] != "warn":
            await ctx.reply(embed=problem("No such warning", f"There's no warning #{case} here."), ephemeral=True)
            return
        self.bot.storage.delete_case(ctx.guild.id, case)
        await ctx.reply(embed=embed("Warning deleted", f"Removed warning #{case} from <@{found[0]}>.", OK))

    @commands.hybrid_command(name="clearwarns", aliases=["clearwarnings"], description="Delete all of a member's warnings")
    @app_commands.describe(member="Whose warnings to clear")
    @moderator("moderate_members")
    async def clearwarns(self, ctx: commands.Context, member: discord.Member) -> None:
        removed = self.bot.storage.clear_cases(ctx.guild.id, member.id, "warn")
        if not removed:
            await ctx.reply(embed=problem("Nothing to clear", f"{member.mention} has no warnings."), ephemeral=True)
            return
        await self.finish(ctx, embed("Warnings cleared",
                                     f"Removed {removed} warning(s) from {member.mention}. By {ctx.author.mention}.", OK))

    # Channels

    @commands.hybrid_command(name="purge", aliases=["clear", "prune"], usage="<amount> [@member]",
                             description="Delete recent messages here, optionally only one member's")
    @app_commands.describe(amount=f"How many messages (1-{MAX_PURGE})", member="Only delete this member's messages")
    @moderator("manage_messages")
    async def purge(self, ctx: commands.Context, amount: commands.Range[int, 1, MAX_PURGE],
                    member: Optional[discord.Member] = None) -> None:
        channel = ctx.channel
        missing = [p for p in ("manage_messages", "read_message_history")
                   if not getattr(channel.permissions_for(ctx.guild.me), p)]
        if missing:
            await ctx.reply(embed=needs(missing), ephemeral=True)
            return
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        elif ctx.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await ctx.message.delete()
        left = amount

        def check(message: discord.Message) -> bool:
            nonlocal left
            if left <= 0 or message.pinned or (member is not None and message.author.id != member.id):
                return False
            left -= 1
            return True

        after = discord.utils.utcnow() - BULK_WINDOW
        try:
            deleted = await channel.purge(limit=PURGE_SCAN if member else amount, check=check, after=after,
                                          oldest_first=False, reason=f"SYKE purge by {ctx.author}")
        except discord.HTTPException:
            await ctx.send(embed=problem("Discord said no", "SYKE couldn't delete those messages."), ephemeral=True)
            return
        whose = f" from {member.mention}" if member else ""
        body = f"Deleted {len(deleted)} message(s){whose}."
        if len(deleted) < amount:
            body += " Discord only bulk-deletes messages from the last 14 days, and pins are kept."
        result = embed("🧹 Purged", body, OK)
        if ctx.interaction is not None:
            await ctx.send(embed=result, ephemeral=True)
        else:
            with contextlib.suppress(discord.HTTPException):
                await ctx.send(embed=result, delete_after=6)
        await self.log(ctx.guild, embed("🧹 Purge", f"{ctx.author.mention} deleted {len(deleted)} message(s){whose} "
                                                   f"in {channel.mention}.", OK))

    @commands.hybrid_command(name="slowmode", aliases=["slow"], usage="<duration|off> [#channel]",
                             description="Set slowmode, e.g. 10s, 2m, or off")
    @app_commands.describe(duration="Time between messages, e.g. 5s, 1m, 2h, or off", channel="Defaults to this one")
    @moderator("manage_channels")
    async def slowmode(self, ctx: commands.Context, duration: str,
                       channel: Optional[discord.TextChannel] = None) -> None:
        channel = channel or ctx.channel
        word = duration.strip().lower()
        if word in OFF_WORDS:
            seconds: int | None = 0
        elif word.isdigit():
            seconds = int(word)  # A bare number is seconds here, like Discord's own setting.
        else:
            seconds = parse_duration(word)
        if seconds is None or seconds < 0:
            await ctx.reply(embed=problem("How slow?", f"Try `{self.p(ctx)}slowmode 10s` or `{self.p(ctx)}slowmode off`."),
                            ephemeral=True)
            return
        if seconds > MAX_SLOWMODE:
            await ctx.reply(embed=problem("Too slow", "Discord caps slowmode at 6 hours."), ephemeral=True)
            return
        try:
            await channel.edit(slowmode_delay=seconds, reason=f"SYKE slowmode by {ctx.author}")
        except (discord.HTTPException, AttributeError):
            await ctx.reply(embed=needs(["manage_channels"]), ephemeral=True)
            return
        if seconds:
            result = embed("🐢 Slowmode on", f"{channel.mention}: one message every {human_duration(seconds)}.")
        else:
            result = embed("🐇 Slowmode off", f"{channel.mention} is back to full speed.", OK)
        await ctx.reply(embed=result)

    @commands.hybrid_command(name="lock", aliases=["lockdown"], usage="[#channel] [reason]",
                             description="Stop @everyone sending messages in a channel")
    @app_commands.describe(channel="Defaults to this one", reason="Why")
    @moderator("manage_channels")
    async def lock(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None, *,
                   reason: str = "") -> None:
        channel = channel or ctx.channel
        guild = ctx.guild
        everyone = guild.default_role
        overwrite = channel.overwrites_for(everyone)
        if overwrite.send_messages is False and self.bot.storage.is_locked(guild.id, channel.id):
            await ctx.reply(embed=problem("Already locked", f"{channel.mention} is already locked."), ephemeral=True)
            return
        previous = {"send_messages": overwrite.send_messages,
                    "send_messages_in_threads": overwrite.send_messages_in_threads,
                    "create_public_threads": overwrite.create_public_threads}
        reason = clean_reason(reason)
        try:
            mine = channel.overwrites_for(guild.me)
            if mine.send_messages is not True:
                mine.send_messages = True
                await channel.set_permissions(guild.me, overwrite=mine, reason="SYKE keeps talking in locked channels")
            for name in previous:
                setattr(overwrite, name, False)
            await channel.set_permissions(everyone, overwrite=overwrite, reason=f"SYKE lock by {ctx.author}: {reason}")
        except (discord.HTTPException, AttributeError):
            await ctx.reply(embed=needs(["manage_roles", "manage_channels"]), ephemeral=True)
            return
        self.bot.storage.unlock_channel(guild.id, channel.id)
        self.bot.storage.lock_channel(guild.id, channel.id, previous)
        body = f"{channel.mention} is locked. Only people with their own Send Messages override can talk."
        if reason:
            body += f"\n**Reason:** {reason}"
        result = embed("🔒 Locked", body, WARN)
        await ctx.reply(embed=result)
        if channel.id != getattr(ctx.channel, "id", None):
            with contextlib.suppress(discord.HTTPException):
                await channel.send(embed=result)
        await self.log(guild, embed("🔒 Channel locked", f"{channel.mention} by {ctx.author.mention}. "
                                                         f"Reason: {reason or 'none given'}", WARN))

    @commands.hybrid_command(name="unlock", description="Let @everyone talk in a locked channel again")
    @app_commands.describe(channel="Defaults to this one")
    @moderator("manage_channels")
    async def unlock(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        channel = channel or ctx.channel
        guild = ctx.guild
        everyone = guild.default_role
        overwrite = channel.overwrites_for(everyone)
        saved = self.bot.storage.unlock_channel(guild.id, channel.id)
        if saved is None and overwrite.send_messages is not False:
            await ctx.reply(embed=problem("Not locked", f"{channel.mention} isn't locked."), ephemeral=True)
            return
        for name in ("send_messages", "send_messages_in_threads", "create_public_threads"):
            value = (saved or {}).get(name)
            setattr(overwrite, name, value if value is None else bool(value))
        try:
            await channel.set_permissions(everyone, overwrite=overwrite, reason=f"SYKE unlock by {ctx.author}")
        except (discord.HTTPException, AttributeError):
            if saved is not None:
                self.bot.storage.lock_channel(guild.id, channel.id, saved)
            await ctx.reply(embed=needs(["manage_roles", "manage_channels"]), ephemeral=True)
            return
        result = embed("🔓 Unlocked", f"{channel.mention} is open again.", OK)
        await ctx.reply(embed=result)
        await self.log(guild, embed("🔓 Channel unlocked", f"{channel.mention} by {ctx.author.mention}.", OK))

    # Removals

    @commands.hybrid_command(name="kick", usage="@member [reason]", description="Kick a member from the server")
    @app_commands.describe(member="Who to kick", reason="Why")
    @moderator("kick_members")
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: str = "") -> None:
        blocked = blocker(ctx.guild, ctx.author, member, owner_ids=self.bot.settings.owner_ids)
        if blocked:
            await ctx.reply(embed=problem("Can't kick them", blocked), ephemeral=True)
            return
        missing = bot_lacks(ctx.guild, "kick_members")
        if missing:
            await ctx.reply(embed=needs(missing), ephemeral=True)
            return
        reason = clean_reason(reason)
        told = await self.tell(member, ctx.guild, "kicked", reason=reason)
        try:
            await member.kick(reason=f"{ctx.author}: {reason or 'no reason'}")
        except discord.HTTPException:
            await ctx.reply(embed=problem("Discord said no", "Check SYKE's role is above theirs."), ephemeral=True)
            return
        case = self.bot.storage.add_case(ctx.guild.id, member.id, ctx.author.id, "kick", reason)
        await self.finish(ctx, action_embed(f"👢 {member.display_name} kicked", member, ctx.author, reason=reason,
                                            case=case, colour=DANGER, extra="" if told else "Couldn't DM them."))

    @commands.hybrid_command(name="ban", usage="@user [reason]", description="Ban someone, even if they already left")
    @app_commands.describe(user="Who to ban (a member, or a user ID)", reason="Why")
    @moderator("ban_members")
    async def ban(self, ctx: commands.Context, user: discord.User, *, reason: str = "") -> None:
        guild = ctx.guild
        target = guild.get_member(user.id) or user
        blocked = blocker(guild, ctx.author, target, owner_ids=self.bot.settings.owner_ids)
        if blocked:
            await ctx.reply(embed=problem("Can't ban them", blocked), ephemeral=True)
            return
        missing = bot_lacks(guild, "ban_members")
        if missing:
            await ctx.reply(embed=needs(missing), ephemeral=True)
            return
        reason = clean_reason(reason)
        if isinstance(target, discord.Member):
            await self.tell(target, guild, "banned", reason=reason)
        try:
            await guild.ban(user, reason=f"{ctx.author}: {reason or 'no reason'}", delete_message_seconds=0)
        except discord.HTTPException:
            await ctx.reply(embed=problem("Discord said no", "Check SYKE's role is above theirs."), ephemeral=True)
            return
        case = self.bot.storage.add_case(guild.id, user.id, ctx.author.id, "ban", reason)
        await self.finish(ctx, action_embed(f"🔨 {user.display_name} banned", user, ctx.author, reason=reason,
                                            case=case, colour=DANGER))

    @commands.hybrid_command(name="unban", usage="<user ID> [reason]", description="Unban someone by their user ID")
    @app_commands.describe(user="The user (paste their ID)", reason="Why")
    @moderator("ban_members")
    async def unban(self, ctx: commands.Context, user: discord.User, *, reason: str = "") -> None:
        missing = bot_lacks(ctx.guild, "ban_members")
        if missing:
            await ctx.reply(embed=needs(missing), ephemeral=True)
            return
        reason = clean_reason(reason)
        try:
            await ctx.guild.unban(user, reason=f"{ctx.author}: {reason or 'no reason'}")
        except discord.NotFound:
            await ctx.reply(embed=problem("Not banned", f"{user.mention} isn't banned here."), ephemeral=True)
            return
        except discord.HTTPException:
            await ctx.reply(embed=needs(["ban_members"]), ephemeral=True)
            return
        case = self.bot.storage.add_case(ctx.guild.id, user.id, ctx.author.id, "unban", reason)
        await self.finish(ctx, action_embed(f"🕊️ {user.display_name} unbanned", user, ctx.author, reason=reason,
                                            case=case, colour=OK))

    @commands.hybrid_command(name="tempban", aliases=["tban"], usage="@user <duration> [reason]",
                             description="Ban someone for a set time, then auto-unban")
    @app_commands.describe(user="Who to ban", duration="How long, e.g. 1d, 12h, 1w", reason="Why")
    @moderator("ban_members")
    async def tempban(self, ctx: commands.Context, user: discord.User, duration: str, *, reason: str = "") -> None:
        guild = ctx.guild
        seconds = parse_duration(duration)
        if not seconds:
            await ctx.reply(embed=problem("How long?", f"Try `{self.p(ctx)}tempban @user 1d spamming`."),
                            ephemeral=True)
            return
        target = guild.get_member(user.id) or user
        blocked = blocker(guild, ctx.author, target, owner_ids=self.bot.settings.owner_ids)
        if blocked:
            await ctx.reply(embed=problem("Can't ban them", blocked), ephemeral=True)
            return
        missing = bot_lacks(guild, "ban_members")
        if missing:
            await ctx.reply(embed=needs(missing), ephemeral=True)
            return
        reason = clean_reason(reason)
        length = until(seconds)
        if isinstance(target, discord.Member):
            await self.tell(target, guild, "banned", reason=reason, duration=length)
        try:
            await guild.ban(user, reason=f"{ctx.author} (temp {human_duration(seconds)}): {reason or 'no reason'}",
                            delete_message_seconds=0)
        except discord.HTTPException:
            await ctx.reply(embed=problem("Discord said no", "Check SYKE's role is above theirs."), ephemeral=True)
            return
        self.bot.storage.add_temp_ban(guild.id, user.id, time.time() + seconds, reason, ctx.author.id)
        case = self.bot.storage.add_case(guild.id, user.id, ctx.author.id, "tempban", reason, seconds)
        await self.finish(ctx, action_embed(f"🔨 {user.display_name} temp-banned", user, ctx.author, reason=reason,
                                            duration=length, case=case, colour=DANGER))

    @commands.hybrid_command(name="softban", usage="@user [reason]",
                             description="Ban then immediately unban to wipe a member's recent messages")
    @app_commands.describe(user="Who to softban", reason="Why")
    @moderator("ban_members")
    async def softban(self, ctx: commands.Context, user: discord.Member, *, reason: str = "") -> None:
        guild = ctx.guild
        blocked = blocker(guild, ctx.author, user, owner_ids=self.bot.settings.owner_ids)
        if blocked:
            await ctx.reply(embed=problem("Can't softban them", blocked), ephemeral=True)
            return
        missing = bot_lacks(guild, "ban_members")
        if missing:
            await ctx.reply(embed=needs(missing), ephemeral=True)
            return
        reason = clean_reason(reason)
        await self.tell(user, guild, "kicked (messages cleared)", reason=reason)
        try:
            await guild.ban(user, reason=f"{ctx.author} (softban): {reason or 'no reason'}",
                            delete_message_seconds=86400)
            await guild.unban(discord.Object(id=user.id), reason="SYKE softban: immediate unban")
        except discord.HTTPException:
            await ctx.reply(embed=problem("Discord said no", "Check SYKE's role is above theirs."), ephemeral=True)
            return
        case = self.bot.storage.add_case(guild.id, user.id, ctx.author.id, "softban", reason)
        await self.finish(ctx, action_embed(f"🧹 {user.display_name} softbanned", user, ctx.author, reason=reason,
                                            case=case, colour=DANGER,
                                            extra="Banned and unbanned to clear their last day of messages."))

    # Settings

    @commands.hybrid_command(name="modlog", usage="<#channel|off>",
                             description="Choose where SYKE posts a log of moderation actions")
    @app_commands.describe(channel="Where to post the log. Leave empty to see the current one.",
                           setting="Type 'off' to stop logging")
    @moderator("manage_guild")
    async def modlog(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None,
                     setting: str = "") -> None:
        guild = ctx.guild
        if channel is None:
            if setting.strip().lower() in OFF_WORDS:
                self.bot.storage.set_modlog_channel(guild.id, None)
                await ctx.reply(embed=embed("Mod log off", "SYKE won't log moderation actions."), ephemeral=True)
                return
            current = self.bot.storage.modlog_channel(guild.id)
            where_now = f"<#{current}>" if current else "not set"
            await ctx.reply(embed=embed("Mod log", f"**Channel:** {where_now}\n"
                                                   f"`{self.p(ctx)}modlog #channel` to set it, "
                                                   f"`{self.p(ctx)}modlog off` to stop."), ephemeral=True)
            return
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links):
            await ctx.reply(embed=problem("I can't post there", f"Give SYKE **Send Messages** and **Embed Links** "
                                                                f"in {channel.mention}."), ephemeral=True)
            return
        self.bot.storage.set_modlog_channel(guild.id, channel.id)
        await ctx.reply(embed=embed("Mod log set ✅", f"Moderation actions go to {channel.mention}."), ephemeral=True)


def help_lines(p: str) -> str:
    return (
        f"`{p}restrict @member [what] [time] [reason]` — no images, links, reactions, stickers or emojis, server-wide\n"
        f"`{p}unrestrict @member [what]` · `{p}restrictions` — lift, or see who's restricted\n"
        f"`{p}timeout @member <10m|2h|1d> [reason]` · `{p}untimeout @member`\n"
        f"`{p}warn @member <reason>` · `{p}warnings @member` · `{p}history @member`\n"
        f"`{p}purge <amount> [@member]` · `{p}slowmode <10s|off>` · `{p}lock` / `{p}unlock`\n"
        f"`{p}kick @member` · `{p}ban @user` · `{p}unban <id>`\n"
        f"`{p}tempban @user <1d>` · `{p}softban @user` · `{p}modlog #channel`"
    )
