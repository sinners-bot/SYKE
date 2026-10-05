"""Reads tracked channels' history into Msg objects, with a per-server cache.

Channels are read in parallel. Once a channel has been read, later refreshes only fetch
messages newer than the last one seen, so only the first scan (or `invalidate`) is slow.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import discord

from .lexicon import LAUGH_EMOJIS
from .models import Msg

log = logging.getLogger("syke.scanner")


@dataclass
class ScanResult:
    messages: list[Msg]
    scanned_channels: list[int]
    skipped_channels: list[int]  # missing permissions or deleted
    finished_at: float


@dataclass
class _ChannelHistory:
    messages: list[Msg] = field(default_factory=list)
    newest_id: int | None = None


def _laughs(message: discord.Message) -> tuple[int, int]:
    laugh = total = 0
    for reaction in message.reactions:
        total += reaction.count
        if str(reaction.emoji) in LAUGH_EMOJIS:
            laugh += reaction.count
    return laugh, total


def to_msg(message: discord.Message) -> Msg:
    laugh, total = _laughs(message)
    return Msg(
        author_id=message.author.id,
        author_name=message.author.display_name,
        content=message.content,
        created_at=message.created_at,
        channel_id=message.channel.id,
        laugh_reactions=laugh,
        total_reactions=total,
        message_id=message.id,
    )


class Scanner:
    def __init__(self, scan_limit: int, cache_minutes: int) -> None:
        self.scan_limit = scan_limit
        self.ttl = cache_minutes * 60
        self._cache: dict[int, tuple[tuple[int, ...], ScanResult]] = {}
        self._history: dict[int, dict[int, _ChannelHistory]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def invalidate(self, guild_id: int) -> None:
        """Forget everything, so the next scan re-reads full history (and fresh reaction counts)."""
        self._cache.pop(guild_id, None)
        self._history.pop(guild_id, None)

    def cached_at(self, guild_id: int) -> float | None:
        entry = self._cache.get(guild_id)
        return entry[1].finished_at if entry else None

    async def _read(self, channel, history: _ChannelHistory) -> None:
        after = discord.Object(id=history.newest_id) if history.newest_id else None
        fresh: list[Msg] = []
        newest = history.newest_id
        async for message in channel.history(limit=self.scan_limit, after=after):
            newest = max(newest or 0, message.id)
            if message.author.bot or not message.content:
                continue
            fresh.append(to_msg(message))
        merged = history.messages + fresh if after else fresh
        history.messages = sorted(merged, key=lambda m: m.message_id)[-self.scan_limit:]
        history.newest_id = newest

    async def scan(self, guild: discord.Guild, channel_ids: list[int]) -> ScanResult:
        key = tuple(sorted(channel_ids))
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            cached = self._cache.get(guild.id)
            if cached and cached[0] == key and time.time() - cached[1].finished_at < self.ttl:
                return cached[1]

            known = self._history.setdefault(guild.id, {})
            for stale in set(known) - set(channel_ids):
                del known[stale]

            jobs: dict[int, asyncio.Task] = {}
            skipped: list[int] = []
            for channel_id in channel_ids:
                channel = guild.get_channel_or_thread(channel_id)
                if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
                    skipped.append(channel_id)
                    known.pop(channel_id, None)
                    continue
                history = known.get(channel_id) or _ChannelHistory()
                jobs[channel_id] = asyncio.ensure_future(self._read(channel, history))
                known[channel_id] = history

            outcomes = await asyncio.gather(*jobs.values(), return_exceptions=True)
            scanned: list[int] = []
            for channel_id, outcome in zip(jobs, outcomes):
                if isinstance(outcome, BaseException):
                    log.warning("Could not read #%s in %s: %s", channel_id, guild.id, outcome)
                    skipped.append(channel_id)
                    known.pop(channel_id, None)
                else:
                    scanned.append(channel_id)

            messages = [m for cid in scanned for m in known[cid].messages]
            result = ScanResult(messages, scanned, skipped, time.time())
            self._cache[guild.id] = (key, result)
            return result
