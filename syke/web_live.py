"""Live monitoring for the owner dashboard.

One hub listens to the gateway (raw events, so old uncached messages count too) and pushes to two kinds
of watchers over WebSockets:

* channel watchers (the chat view): new / edit / del / rx (reaction bar) for one channel;
* feed watchers (the Live page): every message, edit, delete, reaction, join, leave and voice change in
  one server, or in every server (scope 0).

Nothing is rendered unless someone is watching. A small ring of recent messages remembers what deleted
or edited messages said, and per-server activity counters power the server overview.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass

import discord

log = logging.getLogger("syke.web.live")

RECENT_CAP = 20_000          # messages remembered for deletes/edits/reaction snippets
ACTIVITY_WINDOW = 24 * 3600
ACTIVITY_CAP = 120_000       # per server
RX_DEBOUNCE = 0.6            # seconds: coalesce reaction bursts before re-rendering a bar
ALL = 0                      # feed scope for "every server"


@dataclass
class Seen:
    guild_id: int
    channel_id: int
    author_id: int
    author: str
    avatar: str
    content: str
    created: float
    attachments: int = 0


def esc(text) -> str:
    from .web import esc as _esc
    return _esc(text)


def _avatar(user) -> str:
    return getattr(getattr(user, "display_avatar", None), "url", "") or ""


def snippet(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class LiveHub:
    sees_blacklisted = True     # the owner sees everything, blacklisted users included (see blacklist.py)

    def __init__(self, dash) -> None:
        self.dash = dash
        self.bot = dash.bot
        self.feeds: dict[int, set] = defaultdict(set)      # guild id (or ALL) -> websockets
        self.recent: OrderedDict[int, Seen] = OrderedDict()
        self.activity: dict[int, deque] = defaultdict(lambda: deque(maxlen=ACTIVITY_CAP))
        self.started = time.time()
        self._rx_pending: dict[int, asyncio.Task] = {}
        self._summaries: dict[int, tuple[float, dict]] = {}
        bot = self.bot
        bot.add_listener(self.on_message, "on_message")
        bot.add_listener(self.on_raw_message_edit, "on_raw_message_edit")
        bot.add_listener(self.on_raw_message_delete, "on_raw_message_delete")
        bot.add_listener(self.on_raw_bulk_message_delete, "on_raw_bulk_message_delete")
        bot.add_listener(self.on_raw_reaction_add, "on_raw_reaction_add")
        bot.add_listener(self.on_raw_reaction_remove, "on_raw_reaction_remove")
        bot.add_listener(self.on_raw_reaction_clear, "on_raw_reaction_clear")
        bot.add_listener(self.on_raw_reaction_clear_emoji, "on_raw_reaction_clear_emoji")
        bot.add_listener(self.on_member_join, "on_member_join")
        bot.add_listener(self.on_member_remove, "on_member_remove")
        bot.add_listener(self.on_voice_state_update, "on_voice_state_update")

    # Who is watching

    def channel_watched(self, channel_id: int) -> bool:
        return bool(self.dash._subs.get(channel_id))

    def feed_watched(self, guild_id: int) -> bool:
        return bool(self.feeds.get(guild_id) or self.feeds.get(ALL))

    async def push_feed(self, guild_id: int, payload: dict) -> None:
        for scope in (guild_id, ALL):
            for ws in list(self.feeds.get(scope, ())):
                try:
                    await ws.send_json(payload)
                except Exception:
                    self.feeds[scope].discard(ws)

    # Memory of recent messages

    def remember(self, m: discord.Message) -> None:
        if m.guild is None:
            return
        self.recent[m.id] = Seen(m.guild.id, m.channel.id, m.author.id, getattr(m.author, "display_name", "someone"),
                                 _avatar(m.author), m.content or "", m.created_at.timestamp(), len(m.attachments))
        self.recent.move_to_end(m.id)
        while len(self.recent) > RECENT_CAP:
            self.recent.popitem(last=False)

    def recall(self, guild_id: int, message_id: int) -> Seen | None:
        seen = self.recent.get(message_id)
        if seen is not None:
            return seen
        cached = self.cached(message_id)
        if cached is not None and cached.guild is not None:
            self.remember(cached)
            return self.recent.get(message_id)
        with contextlib.suppress(Exception):  # stored history, for tracked channels
            rows = self.bot.storage._read(
                "SELECT channel_id, author_id, author_name, content, created_at FROM corpus "
                "WHERE guild_id = ? AND message_id = ?", (guild_id, message_id))
            if rows:
                cid, uid, name, content, created = rows[0]
                return Seen(guild_id, cid, uid, name or f"user {uid}", "", content or "", created or 0.0)
        return None

    def cached(self, message_id: int) -> discord.Message | None:
        state = getattr(self.bot, "_connection", None)
        getter = getattr(state, "_get_message", None)
        return getter(message_id) if getter else None

    # Activity (for the server overview)

    def note_activity(self, m: discord.Message) -> None:
        self.activity[m.guild.id].append((time.time(), m.channel.id, m.author.id))

    def activity_summary(self, guild_id: int) -> dict:
        hit = self._summaries.get(guild_id)
        if hit and time.monotonic() - hit[0] < 10:
            return hit[1]
        summary = self._summarize(guild_id)
        self._summaries[guild_id] = (time.monotonic(), summary)
        return summary

    def _summarize(self, guild_id: int) -> dict:
        now = time.time()
        events = self.activity.get(guild_id, ())
        hour = day = 0
        channels: dict[int, int] = defaultdict(int)
        people_hour: set[int] = set()
        people_day: set[int] = set()
        buckets = [0] * 24
        for ts, cid, uid in events:
            age = now - ts
            if age > ACTIVITY_WINDOW:
                continue
            day += 1
            people_day.add(uid)
            channels[cid] += 1
            buckets[23 - min(23, int(age // 3600))] += 1
            if age <= 3600:
                hour += 1
                people_hour.add(uid)
        return {"hour": hour, "day": day, "people_hour": len(people_hour), "people_day": len(people_day),
                "channels": sorted(channels.items(), key=lambda kv: -kv[1]), "buckets": buckets,
                "since": max(self.started, now - ACTIVITY_WINDOW)}

    # Feed rendering

    def _where(self, guild, channel_id: int, message_id: int | None = None) -> str:
        channel = guild.get_channel(channel_id) if guild else None
        name = getattr(channel, "name", None) or str(channel_id)
        target = f"/g/{guild.id}/c/{channel_id}" + (f"?to={message_id}" if message_id else "")
        return (f"<a class='fg' href='/g/{guild.id}/live'>{esc(guild.name)}</a>"
                f"<a class='fc' href='{target}'>#{esc(name)}</a>")

    def item(self, kind: str, guild, channel_id: int | None, body: str, *, icon: str, label: str,
             message_id: int | None = None, user_id: int | None = None) -> dict:
        where = self._where(guild, channel_id, message_id) if channel_id else \
            f"<a class='fg' href='/g/{guild.id}/live'>{esc(guild.name)}</a>"
        html = (f"<div class='fi k-{kind}' data-k='{kind}' data-g='{guild.id}' data-c='{channel_id or 0}' "
                f"data-u='{user_id or 0}' data-ts='{int(time.time())}'>"
                f"<div class='fh'><span class='fk'>{icon} {esc(label)}</span>{where}"
                f"<time class='ts' data-ts='{int(time.time())}'></time></div>{body}</div>")
        return {"t": "feed", "k": kind, "g": str(guild.id), "c": str(channel_id or 0), "html": html}

    def _quote(self, seen: Seen | None, fallback: str = "a message SYKE never saw") -> str:
        if seen is None:
            return f"<div class='fq muted'>{esc(fallback)}</div>"
        att = f" <span class='muted'>📎 {seen.attachments}</span>" if seen.attachments else ""
        av = f"<img class='fav' src='{esc(seen.avatar)}' alt=''>" if seen.avatar else ""
        return (f"<div class='fq'>{av}<b>{esc(seen.author)}</b> "
                f"<span>{esc(snippet(seen.content, 300)) or '<i>(no text)</i>'}</span>{att}</div>")

    # Gateway events

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        self.remember(message)
        self.note_activity(message)
        if self.channel_watched(message.channel.id):
            with contextlib.suppress(Exception):
                refs, deleted = await self.dash._live_refs(message)
                await self.dash._broadcast(message.channel.id, {
                    "t": "new", "mid": str(message.id), "html": self.dash._one_message(message, None, refs, deleted)})
        if self.feed_watched(message.guild.id):
            with contextlib.suppress(Exception):
                body = self.dash._one_message(message, None, {}, set())
                await self.push_feed(message.guild.id, self.item(
                    "msg", message.guild, message.channel.id, body, icon="💬", label="Message",
                    message_id=message.id, user_id=message.author.id))

    async def on_raw_message_edit(self, payload) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return
        after = getattr(payload, "message", None)
        before = self.recent.get(payload.message_id)
        old_text = before.content if before else (payload.cached_message.content if payload.cached_message else None)
        if after is None or not self._is_message(after):
            return
        if old_text is not None and old_text == (after.content or ""):
            return  # an embed loading or a pin, not a real edit
        self.remember(after)
        if self.channel_watched(payload.channel_id):
            with contextlib.suppress(Exception):
                refs, deleted = await self.dash._live_refs(after)
                await self.dash._broadcast(payload.channel_id, {
                    "t": "edit", "mid": str(after.id), "html": self.dash._one_message(after, None, refs, deleted),
                    "before": old_text})
        if self.feed_watched(guild.id):
            was = (f"<div class='fq old'><span class='muted'>before:</span> {esc(snippet(old_text, 300))}</div>"
                   if old_text else "<div class='fq muted'>before: not remembered</div>")
            now = (f"<div class='fq'><span class='muted'>after:</span> {esc(snippet(after.content, 300))}</div>")
            body = (f"<div class='fq'><img class='fav' src='{esc(_avatar(after.author))}' alt=''>"
                    f"<b>{esc(after.author.display_name)}</b> edited a message</div>{was}{now}")
            await self.push_feed(guild.id, self.item("edit", guild, payload.channel_id, body, icon="✏️",
                                                     label="Edited", message_id=after.id, user_id=after.author.id))

    @staticmethod
    def _is_message(obj) -> bool:
        return hasattr(obj, "author") and hasattr(obj, "content") and hasattr(obj, "id")

    async def on_raw_message_delete(self, payload) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return
        if self.channel_watched(payload.channel_id):
            await self.dash._broadcast(payload.channel_id, {"t": "del", "mid": str(payload.message_id)})
        if self.feed_watched(guild.id):
            seen = self.recall(guild.id, payload.message_id)
            await self.push_feed(guild.id, self.item(
                "del", guild, payload.channel_id, self._quote(seen), icon="🗑️", label="Deleted",
                user_id=seen.author_id if seen else None))

    async def on_raw_bulk_message_delete(self, payload) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return
        ids = sorted(payload.message_ids)
        if self.channel_watched(payload.channel_id):
            for mid in ids:
                await self.dash._broadcast(payload.channel_id, {"t": "del", "mid": str(mid)})
        if self.feed_watched(guild.id):
            quotes = "".join(self._quote(self.recall(guild.id, mid)) for mid in ids[-10:])
            more = f"<div class='muted'>…and {len(ids) - 10} more</div>" if len(ids) > 10 else ""
            await self.push_feed(guild.id, self.item("del", guild, payload.channel_id, quotes + more, icon="🧹",
                                                     label=f"Purged {len(ids)} messages"))

    async def _reaction(self, payload, verb: str) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return
        if self.channel_watched(payload.channel_id):
            self._schedule_bar(guild, payload.channel_id, payload.message_id)
        if not self.feed_watched(guild.id):
            return
        if getattr(payload, "user_id", None) == getattr(self.bot.user, "id", None):
            return
        member = getattr(payload, "member", None) or guild.get_member(getattr(payload, "user_id", 0) or 0)
        who = getattr(member, "display_name", None) or f"user {getattr(payload, 'user_id', '?')}"
        emoji = getattr(payload, "emoji", None)
        glyph = self.dash._emoji_html(emoji) if emoji is not None else ""
        seen = self.recall(guild.id, payload.message_id)
        target = f"{esc(seen.author)}'s message" if seen else "a message"
        av = _avatar(member)
        pic = f"<img class='fav' src='{esc(av)}' alt=''>" if av else ""
        head = f"<div class='fq'>{pic}<b>{esc(who)}</b> {verb} <span class='frx'>{glyph}</span> on {target}</div>"
        body = head + (self._quote(seen) if seen else "")
        await self.push_feed(guild.id, self.item("rx", guild, payload.channel_id, body, icon="✨",
                                                 label="Reaction" if verb == "reacted" else "Reaction removed",
                                                 message_id=payload.message_id,
                                                 user_id=getattr(payload, "user_id", None)))

    async def on_raw_reaction_add(self, payload) -> None:
        await self._reaction(payload, "reacted")

    async def on_raw_reaction_remove(self, payload) -> None:
        await self._reaction(payload, "removed")

    async def on_raw_reaction_clear(self, payload) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is not None and self.channel_watched(payload.channel_id):
            self._schedule_bar(guild, payload.channel_id, payload.message_id)

    async def on_raw_reaction_clear_emoji(self, payload) -> None:
        await self.on_raw_reaction_clear(payload)

    def _schedule_bar(self, guild, channel_id: int, message_id: int) -> None:
        """Re-render a message's reaction bar once a burst of reactions settles."""
        if message_id in self._rx_pending:
            return

        async def later():
            try:
                await asyncio.sleep(RX_DEBOUNCE)
                message = self.cached(message_id)
                if message is None:
                    channel = guild.get_channel_or_thread(channel_id) if hasattr(guild, "get_channel_or_thread") \
                        else guild.get_channel(channel_id)
                    if channel is None:
                        return
                    with contextlib.suppress(discord.HTTPException):
                        message = await channel.fetch_message(message_id)
                if message is not None:
                    await self.dash._broadcast(channel_id, {"t": "rx", "mid": str(message_id),
                                                            "html": self.dash._reactions_bar(message)})
            finally:
                self._rx_pending.pop(message_id, None)

        self._rx_pending[message_id] = asyncio.ensure_future(later())

    async def on_member_join(self, member) -> None:
        guild = getattr(member, "guild", None)
        if guild is None or not self.feed_watched(guild.id):
            return
        age = (discord.utils.utcnow() - member.created_at).days if getattr(member, "created_at", None) else None
        note = f" · account {age} days old" if age is not None else ""
        body = (f"<div class='fq'><img class='fav' src='{esc(_avatar(member))}' alt=''>"
                f"<a href='/g/{guild.id}/u/{member.id}'><b>{esc(member.display_name)}</b></a> joined"
                f"<span class='muted'>{note}</span></div>")
        await self.push_feed(guild.id, self.item("join", guild, None, body, icon="📥", label="Joined",
                                                 user_id=member.id))

    async def on_member_remove(self, member) -> None:
        guild = getattr(member, "guild", None)
        if guild is None or not self.feed_watched(guild.id):
            return
        body = (f"<div class='fq'><img class='fav' src='{esc(_avatar(member))}' alt=''>"
                f"<b>{esc(member.display_name)}</b> left</div>")
        await self.push_feed(guild.id, self.item("leave", guild, None, body, icon="📤", label="Left",
                                                 user_id=member.id))

    async def on_voice_state_update(self, member, before, after) -> None:
        guild = getattr(member, "guild", None)
        if guild is None or not self.feed_watched(guild.id):
            return
        b, a = getattr(before, "channel", None), getattr(after, "channel", None)
        if b == a:
            if bool(getattr(before, "self_stream", False)) != bool(getattr(after, "self_stream", False)) and a:
                what = "started streaming in" if after.self_stream else "stopped streaming in"
            else:
                return  # mute/deafen toggles: too noisy for the feed
            place = a
        elif a is None:
            what, place = "left", b
        elif b is None:
            what, place = "joined", a
        else:
            what, place = f"moved from 🔊 {esc(b.name)} to", a
        body = (f"<div class='fq'><img class='fav' src='{esc(_avatar(member))}' alt=''>"
                f"<b>{esc(member.display_name)}</b> {what} 🔊 {esc(getattr(place, 'name', '?'))}</div>")
        await self.push_feed(guild.id, self.item("voice", guild, None, body, icon="🎙️", label="Voice",
                                                 user_id=member.id))
