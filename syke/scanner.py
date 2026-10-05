"""Reads tracked channels' history into Msg objects, with a per-server cache."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

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
    )


class Scanner:
    def __init__(self, scan_limit: int, cache_minutes: int) -> None:
        self.scan_limit = scan_limit
        self.ttl = cache_minutes * 60
        self._cache: dict[int, tuple[tuple[int, ...], ScanResult]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def invalidate(self, guild_id: int) -> None:
        self._cache.pop(guild_id, None)

    def cached_at(self, guild_id: int) -> float | None:
        entry = self._cache.get(guild_id)
        return entry[1].finished_at if entry else None

    async def scan(self, guild: discord.Guild, channel_ids: list[int]) -> ScanResult:
        key = tuple(sorted(channel_ids))
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            cached = self._cache.get(guild.id)
            if cached and cached[0] == key and time.time() - cached[1].finished_at < self.ttl:
                return cached[1]

            messages: list[Msg] = []
            scanned: list[int] = []
            skipped: list[int] = []
            for channel_id in channel_ids:
                channel = guild.get_channel_or_thread(channel_id)
                if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
                    skipped.append(channel_id)
                    continue
                try:
                    async for message in channel.history(limit=self.scan_limit):
                        if message.author.bot or not message.content:
                            continue
                        messages.append(to_msg(message))
                    scanned.append(channel_id)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("Could not read #%s in %s: %s", channel_id, guild.id, exc)
                    skipped.append(channel_id)

            result = ScanResult(messages, scanned, skipped, time.time())
            self._cache[guild.id] = (key, result)
            return result
