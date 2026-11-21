"""Music: play songs in voice from YouTube, Spotify, SoundCloud, Apple Music, Deezer and most other sites.

`play <song or link>` joins your voice channel and starts the queue. A now-playing card with
buttons (pause, skip, stop, loop, shuffle, queue, volume) follows each song. Full list:
play, playnext, skip, queue, pause, resume, stop, leave, join, nowplaying, volume, loop, shuffle,
remove, move, seek, clearqueue, autoplay, 247, fx (bass boost, nightcore…), search, and the
`music` group with all of them as slash commands plus `music dj` to limit control to a role.

Sound quality: an Opus source (YouTube's best audio is Opus) is passed to Discord untouched, so
there's no re-encoding loss and almost no CPU. Volume and effects re-encode at the channel's
bitrate. FFmpeg reconnects on network hiccups, the next song's stream is looked up while the
current one plays (no gap between songs), and if a stream dies mid-song SYKE looks it up again
and resumes where it stopped.

Needs on the host: ffmpeg (and a JavaScript runtime such as Deno for YouTube), plus the Python
packages yt-dlp and discord.py[voice]. Without them the commands say music isn't available.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import difflib
import random
import re
import shutil
import time
from typing import Optional
from urllib.parse import quote

import discord
from discord import app_commands
from discord.ext import commands

from . import lavalink as ll
from . import music_lists as lists
from . import music_sources as src
from .moderation import BRAND, OK, WARN, embed, moderator, problem
from .music_sources import FILTERS, Track, duration_text

log = logging.getLogger("syke.music")

CONFIG = "music"
QUEUE_MAX = 500
HISTORY_MAX = 50
IDLE_SECONDS = 180
ALONE_SECONDS = 120
PAGE = 10
RETRIES = 2
QUIZ_CLIP = 15
QUIZ_GRACE = 5
QUIZ_PRIZE = 150
QUIZ_ROUNDS = 5
QUIZ_MAX_ROUNDS = 15
RADIO_BATCH = 5
LOOPS = ("off", "track", "queue")
LOOP_ICON = {"off": "➡️", "track": "🔂", "queue": "🔁"}


def missing_parts() -> list[str]:
    missing = []
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg")
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        missing.append("yt-dlp")
    try:
        import nacl  # noqa: F401
    except ImportError:
        missing.append("PyNaCl")
    return missing


LL_FILTERS = {
    "off": {},
    "bassboost": {"equalizer": [{"band": b, "gain": g} for b, g in ((0, 0.3), (1, 0.25), (2, 0.18), (3, 0.1))]},
    "nightcore": {"timescale": {"speed": 1.2, "pitch": 1.25, "rate": 1.0}},
    "vaporwave": {"timescale": {"speed": 0.8, "pitch": 0.8, "rate": 1.0},
                  "equalizer": [{"band": 0, "gain": 0.2}, {"band": 1, "gain": 0.15}]},
    "8d": {"rotation": {"rotationHz": 0.15}},
    "speed": {"timescale": {"speed": 1.25, "pitch": 1.0, "rate": 1.0}},
    "slowed": {"timescale": {"speed": 0.85, "pitch": 0.92, "rate": 1.0}},
    "karaoke": {"karaoke": {"level": 1.0, "monoLevel": 1.0, "filterBand": 220.0, "filterWidth": 100.0}},
    "treble": {"equalizer": [{"band": b, "gain": 0.2} for b in (10, 11, 12, 13)]},
    "loud": {"volume": 1.4},
}
LL_SPEED = {"nightcore": 1.2, "vaporwave": 0.8, "speed": 1.25, "slowed": 0.85}


def ll_track(data: dict, requester: int = 0) -> Track:
    """A Lavalink track as one of ours."""
    info = data.get("info") or {}
    stream = bool(info.get("isStream"))
    return Track(title=info.get("title") or info.get("uri") or "Unknown", url=info.get("uri") or "",
                 duration=None if stream else (info.get("length") or 0) / 1000 or None,
                 uploader=info.get("author") or "", thumbnail=info.get("artworkUrl") or "",
                 requester=requester, source=info.get("sourceName") or "", is_live=stream,
                 encoded=data.get("encoded") or "")


def normal(text: str) -> str:
    """Lowercase, no brackets (feat., remix…), no punctuation: how quiz answers are compared."""
    text = re.sub(r"[\(\[].*?[\)\]]", " ", (text or "").lower())
    text = re.sub(r"\b(feat|ft)\b.*", " ", text)
    text = text.replace("&", " and ")
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def quiz_match(guess: str, title: str, artist: str = "") -> bool:
    g, t = normal(guess), normal(title)
    if not g or not t:
        return False
    if len(t) >= 3 and (t == g or f" {t} " in f" {g} "):
        return True
    if difflib.SequenceMatcher(None, g, t).ratio() >= 0.82:
        return True
    a = normal(artist)
    return bool(a) and difflib.SequenceMatcher(None, g, f"{t} {a}").ratio() >= 0.85


def progress_bar(position: float, total: float | None, width: int = 16) -> str:
    if not total:
        return "🔴 LIVE"
    done = max(0, min(width, int(width * position / total)))
    return "━" * done + "●" + "─" * (width - done)


SOURCES = {
    "youtube": ("YouTube", "#FF0033", "▶️"),
    "soundcloud": ("SoundCloud", "#FF5500", "☁️"),
    "spotify": ("Spotify", "#1DB954", "🟢"),
    "apple music": ("Apple Music", "#FA2D48", "🍎"),
    "deezer": ("Deezer", "#A238FF", "🎧"),
    "bandcamp": ("Bandcamp", "#1DA0C3", "💿"),
    "twitch": ("Twitch", "#9146FF", "📺"),
    "vimeo": ("Vimeo", "#1AB7EA", "🎬"),
    "mixcloud": ("Mixcloud", "#5000FF", "🎚️"),
}


def style(track: Track) -> tuple[str, discord.Colour, str]:
    """(service name, colour, icon). Spotify/Apple/Deezer songs keep their own look."""
    key = (track.source or "").lower()
    for name, (label, colour, icon) in SOURCES.items():
        if key.startswith(name.replace(" ", "")) or key.startswith(name):
            return label, discord.Colour.from_str(colour), icon
    return (track.source or "Web"), BRAND, "🎵"


def link(track: Track, limit: int = 80) -> str:
    """The song's title as a clickable link (markdown-safe)."""
    title = discord.utils.escape_markdown(track.label[:limit]).replace("[", "(").replace("]", ")")
    url = track.url if track.url.startswith("http") else track.origin
    return f"[{title}]({url})" if url.startswith("http") else f"**{title}**"


def note(text: str, colour: discord.Colour = BRAND) -> discord.Embed:
    return discord.Embed(description=text, colour=colour)


def humans_in(channel) -> int:
    return sum(1 for m in getattr(channel, "members", []) if not m.bot)


class Player:
    """One server's queue and playback state."""

    def __init__(self, cog: "Music", guild: discord.Guild) -> None:
        self.cog = cog
        self.guild = guild
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.history: list[Track] = []
        self.loop = "off"
        self.volume = 100
        self.filter = "off"
        self.autoplay = False
        self.stay = False
        self.channel_id: int | None = None     # where the now-playing card goes
        self.np: discord.Message | None = None
        self.base = 0.0
        self.t0 = 0.0
        self.paused_at: float | None = None
        self.replay_at: float | None = None
        self.skipping = False
        self.stopped = False
        self.retries = 0
        self.lock = asyncio.Lock()
        self.idle_task: asyncio.Task | None = None
        self.prefetch_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.radio: str | None = None          # genre radio: keeps the queue topped up
        self.quiz = False                      # a song quiz owns the voice connection

    @property
    def vc(self) -> discord.VoiceClient | None:
        return self.guild.voice_client  # type: ignore[return-value]

    @property
    def speed(self) -> float:
        if isinstance(self.vc, ll.LavalinkVoice):
            return LL_SPEED.get(self.filter, 1.0)
        return src.SPEED.get(self.filter, 1.0)

    def position(self) -> float:
        if self.current is None:
            return 0.0
        now = self.paused_at if self.paused_at is not None else time.monotonic()
        return max(0.0, self.base + (now - self.t0) * self.speed)

    def queue_seconds(self) -> float:
        return sum(t.duration or 0 for t in self.queue)

    # Playback

    async def start_or_queue(self) -> None:
        if self.quiz:
            return  # songs wait until the quiz is over
        async with self.lock:
            if self.current is None and self.vc is not None:
                await self._advance(user_skip=False)

    async def _advance(self, *, user_skip: bool) -> None:
        previous = self.current
        if previous is not None:
            if self.loop == "track" and not user_skip and await self._play(0):
                return
            if self.loop == "queue":
                self.queue.append(previous)
            self.history.append(previous)
            del self.history[:-HISTORY_MAX]
        self.current = None
        while True:
            if len(self.queue) < 2 and self.radio:
                await self.cog.radio_fill(self)
            if not self.queue and self.autoplay and previous is not None:
                await self.cog.add_related(self, previous)
                previous = None  # one try at related songs per run-out
            if not self.queue:
                await self._finished_card()
                self._arm_idle()
                return
            self.current = self.queue.pop(0)
            self.retries = 0
            if await self._play(0):
                return
            self.current = None

    async def _play(self, position: float) -> bool:
        """Start the current song at `position`. False if it couldn't be played (already reported)."""
        track, vc = self.current, self.vc
        if track is None or vc is None:
            return False
        if isinstance(vc, ll.LavalinkVoice):
            return await self._play_lavalink(track, vc, position)
        try:
            await src.resolve_stream(track)
        except LookupError as error:
            await self.say(problem("Skipped a song", f"**{track.label[:150]}**: {error}"))
            return False
        before, options, codec = src.ffmpeg_args(track, position=position, volume=self.volume,
                                                 filters=FILTERS.get(self.filter, ""))
        bitrate = max(64, min(256, getattr(vc.channel, "bitrate", 128000) // 1000))
        await self._wait_for_encryption(vc)
        try:
            source = discord.FFmpegOpusAudio(track.stream, before_options=before, options=options, codec=codec,
                                             bitrate=bitrate)
            self.skipping = self.stopped = False
            self._loop = asyncio.get_running_loop()
            vc.play(source, after=self._after)
        except discord.ClientException as error:
            log.warning("Couldn't start playback in %s: %s", self.guild.id, error)
            await self.say(problem("Couldn't play", "The voice connection isn't ready. Try `play` again."))
            return False
        self.base, self.t0, self.paused_at = position, time.monotonic(), None
        log.info("Playing %r in %s from %s (%s)", track.label[:80], self.guild.id,
                 "a downloaded file" if track.local else (track.source or "a stream"),
                 "passthrough" if codec else "re-encoded")
        asyncio.create_task(self._audio_check(track))
        self._cancel_idle()
        if position == 0:
            self.cog.record_play(self.guild.id, track)
            await self.now_playing_card()
        self._prefetch()
        return True

    async def _play_lavalink(self, track: Track, vc, position: float) -> bool:
        try:
            encoded = await self.cog.ll_resolve(track)
            self._loop = asyncio.get_running_loop()
            self.skipping = self.stopped = False
            await vc.play_track(encoded, position_ms=int(position * 1000), volume=self.volume,
                                filters=LL_FILTERS.get(self.filter, {}), after=self._after)
        except LookupError as error:
            await self.say(problem("Skipped a song", f"**{track.label[:150]}**: {error}"))
            return False
        except Exception as error:
            log.warning("Lavalink couldn't play in %s: %s", self.guild.id, error)
            await self.say(problem("Couldn't play", "The music server didn't take the song. Try `play` again."))
            return False
        self.base, self.t0, self.paused_at = position, time.monotonic(), None
        log.info("Playing %r in %s through Lavalink (%s)", track.label[:80], self.guild.id, track.source or "?")
        asyncio.create_task(self._audio_check(track))
        self._cancel_idle()
        if position == 0:
            self.cog.record_play(self.guild.id, track)
            await self.now_playing_card()
        self._prefetch()
        return True

    @staticmethod
    async def _wait_for_encryption(vc, seconds: float = 3.0) -> None:
        """Discord's end-to-end voice encryption (DAVE) takes a moment to set up after joining. Audio sent
        before it's ready isn't encrypted and listeners' apps drop it, so wait for it (briefly)."""
        conn = getattr(vc, "_connection", None)
        if conn is None or not getattr(conn, "dave_protocol_version", 0):
            return
        waited = 0.0
        while not getattr(conn, "can_encrypt", True) and waited < seconds:
            await asyncio.sleep(0.25)
            waited += 0.25
        if waited:
            log.info("Waited %.1fs for voice encryption (ready=%s)", waited, getattr(conn, "can_encrypt", None))

    def voice_report(self) -> str:
        vc = self.vc
        if vc is None:
            return "not connected"
        if isinstance(vc, ll.LavalinkVoice):
            node = vc.node
            bits = {"backend": "lavalink", "node_ready": bool(node and node.available),
                    "connected": vc.is_connected(), "playing": vc.is_playing(), "paused": vc.is_paused(),
                    "position_ms": vc.position_ms, "ping_ms": vc.latency_ms, "last_end": vc.last_end_reason,
                    "last_error": vc.last_exception, "channel": getattr(vc.channel, "name", None),
                    "listeners": humans_in(vc.channel),
                    "players_on_node": (node.stats or {}).get("players") if node else None}
            return ", ".join(f"{k}={v}" for k, v in bits.items())
        conn = getattr(vc, "_connection", None)
        player = getattr(vc, "_player", None)
        me = getattr(self.guild, "me", None)
        state = getattr(me, "voice", None)
        bits = {
            "connected": vc.is_connected(),
            "playing": vc.is_playing(),
            "paused": vc.is_paused(),
            "packets": getattr(player, "loops", None),
            "dave_version": getattr(conn, "dave_protocol_version", None),
            "dave_ready": getattr(conn, "can_encrypt", None),
            "mode": getattr(conn, "mode", None),
            "endpoint": getattr(conn, "endpoint", None),
            "latency_ms": round(vc.latency * 1000) if vc.latency != float("inf") else None,
            "server_mute": getattr(state, "mute", None),
            "suppress": getattr(state, "suppress", None),
            "channel": getattr(vc.channel, "name", None),
            "listeners": humans_in(vc.channel),
        }
        return ", ".join(f"{k}={v}" for k, v in bits.items())

    async def _audio_check(self, track: Track) -> None:
        await asyncio.sleep(6)
        if self.current is track:
            log.info("Audio check in %s: %s", self.guild.id, self.voice_report())

    def _after(self, error: Exception | None) -> None:
        if error:
            log.warning("Playback error in %s: %s", self.guild.id, error)
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._on_end()))

    async def _on_end(self) -> None:
        async with self.lock:
            if self.stopped or self.quiz:
                return
            if self.replay_at is not None:
                position, self.replay_at = self.replay_at, None
                if not await self._play(position):
                    await self._advance(user_skip=True)
                return
            track = self.current
            vc = self.vc
            if isinstance(vc, ll.LavalinkVoice):
                if track is not None and not self.skipping and vc.last_end_reason == "loadFailed":
                    reason = vc.last_exception or "it wouldn't load"
                    if not track.fallback:  # YouTube refused it: try the same song on SoundCloud
                        track.fallback, track.encoded = True, ""
                        track.query = track.query or track.title
                        log.info("Lavalink couldn't load %s (%s), trying SoundCloud", track.label, reason)
                        if await self._play(self.position()):
                            return
                    else:
                        await self.say(problem("Skipped a song", f"**{track.label[:150]}**: {reason[:200]}"))
                user_skip, self.skipping = self.skipping, False
                await self._advance(user_skip=user_skip)
                return
            if track is not None and not self.skipping:
                position = self.position()
                cut_short = track.duration and not track.is_live and position < track.duration - 6
                if cut_short and self.retries < RETRIES:  # the stream died mid-song: look it up again, resume
                    self.retries += 1
                    track.resolved_at = 0
                    log.info("Resuming %s at %.0fs after the stream dropped", track.label, position)
                    if await self._play(position):
                        return
            user_skip, self.skipping = self.skipping, False
            await self._advance(user_skip=user_skip)

    def _prefetch(self) -> None:
        if self.prefetch_task is not None and not self.prefetch_task.done():
            return
        upcoming = self.queue[0] if self.queue else None
        lavalink = isinstance(self.vc, ll.LavalinkVoice)
        if upcoming is None or (upcoming.encoded if lavalink else upcoming.fresh()):
            return

        async def run() -> None:
            await asyncio.sleep(3)
            with contextlib.suppress(Exception):
                if lavalink:
                    await self.cog.ll_resolve(upcoming)
                else:
                    await src.resolve_stream(upcoming)

        self.prefetch_task = asyncio.create_task(run())

    def restart(self, position: float | None = None) -> None:
        """Replay the current song from `position` (after a seek, volume or effect change)."""
        vc = self.vc
        if vc is None or self.current is None:
            return
        if isinstance(vc, ll.LavalinkVoice):  # Lavalink changes volume, effects and position live
            target = self.position() if position is None else position
            self.base, self.t0 = target, time.monotonic()
            if self.paused_at is not None:
                self.paused_at = self.t0
            body = {"volume": self.volume, "filters": LL_FILTERS.get(self.filter, {})}
            if position is not None:
                body["position"] = int(position * 1000)
            asyncio.ensure_future(vc.set(**body))
            return
        self.replay_at = self.position() if position is None else position
        if vc.is_paused():
            vc.resume()
        vc.stop()

    def skip(self) -> None:
        vc = self.vc
        if vc is not None and (vc.is_playing() or vc.is_paused()):
            self.skipping = True
            vc.stop()

    def stop(self) -> None:
        self.queue.clear()
        self.loop = "off"
        self.radio = None
        self.stopped = True
        self.replay_at = None
        vc = self.vc
        if vc is not None:
            vc.stop()
        if self.current is not None:
            self.history.append(self.current)
        self.current = None

    def pause(self) -> bool:
        vc = self.vc
        if vc is None or not vc.is_playing():
            return False
        vc.pause()
        self.paused_at = time.monotonic()
        return True

    def resume(self) -> bool:
        vc = self.vc
        if vc is None or not vc.is_paused():
            return False
        vc.resume()
        if self.paused_at is not None:
            self.t0 += time.monotonic() - self.paused_at
        self.paused_at = None
        return True

    # Idle handling

    def _cancel_idle(self) -> None:
        if self.idle_task is not None and not self.idle_task.done():
            self.idle_task.cancel()
        self.idle_task = None

    def _arm_idle(self, seconds: float = IDLE_SECONDS) -> None:
        if self.stay:
            return
        self._cancel_idle()

        async def run() -> None:
            await asyncio.sleep(seconds)
            vc = self.vc
            if vc is not None and not (vc.is_playing() and humans_in(vc.channel)):
                await self.cog.leave(self.guild, reason="idle")

        self.idle_task = asyncio.create_task(run())

    # Messages

    def channel(self):
        return self.guild.get_channel_or_thread(self.channel_id or 0)

    async def say(self, card: discord.Embed) -> None:
        channel = self.channel()
        if channel is not None:
            with contextlib.suppress(discord.HTTPException):
                await channel.send(embed=card)

    def now_card(self) -> discord.Embed:
        track = self.current
        if track is None:
            return note("Nothing's playing. Queue something with `play`.")
        service, colour, icon = style(track)
        card = discord.Embed(title=track.label[:250], url=track.url if track.url.startswith("http") else None,
                             colour=colour)
        requester = self.guild.get_member(track.requester) if track.requester else None
        card.set_author(name="Now playing" + (" · paused" if self.paused_at is not None else ""),
                        icon_url=getattr(getattr(requester, "display_avatar", None), "url", None))
        position = self.position()
        total = None if track.is_live else track.duration
        lines = []
        if track.uploader:
            lines.append(f"by **{discord.utils.escape_markdown(track.uploader[:80])}**")
        lines.append(f"\n`{duration_text(position)}` {progress_bar(position, total)} `{duration_text(total)}`")
        card.description = "\n".join(lines)
        played_from = f"{icon} [{service}]({track.url})" if track.url.startswith("http") else f"{icon} {service}"
        if track.origin and service in ("Spotify", "Apple Music", "Deezer"):
            played_from = f"{icon} [{service}]({track.origin})"
        card.add_field(name="Source", value=played_from)
        card.add_field(name="Requested by", value=f"<@{track.requester}>" if track.requester else "Autoplay")
        card.add_field(name="Volume", value=f"{self.volume}%")
        if self.queue:
            nxt = self.queue[0]
            card.add_field(name="Up next", value=f"{link(nxt, 60)} · `{duration_text(nxt.duration)}`", inline=False)
        if track.thumbnail:
            card.set_thumbnail(url=track.thumbnail)
        bits = [f"{LOOP_ICON[self.loop]} Loop {self.loop}"]
        if self.filter != "off":
            bits.append(f"✨ {self.filter}")
        if self.autoplay:
            bits.append("♾️ Autoplay")
        if self.radio:
            bits.append(f"📻 {self.radio} radio")
        if self.queue:
            bits.append(f"{len(self.queue)} in queue · {duration_text(self.queue_seconds())}")
        card.set_footer(text="  •  ".join(bits))
        return card

    async def now_playing_card(self) -> None:
        channel = self.channel()
        if channel is None:
            return
        old, self.np = self.np, None
        if old is not None:
            with contextlib.suppress(discord.HTTPException):
                await old.delete()
        with contextlib.suppress(discord.HTTPException):
            self.np = await channel.send(embed=self.now_card(), view=Controls(self.cog, self.guild.id),
                                         allowed_mentions=discord.AllowedMentions.none())

    async def refresh_card(self) -> None:
        if self.np is not None and self.current is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.np.edit(embed=self.now_card(), view=Controls(self.cog, self.guild.id))

    async def _finished_card(self) -> None:
        if self.np is not None:
            card = embed("Queue finished", "Add more with `play`." + ("" if self.stay else
                                                                      f" Leaving in {IDLE_SECONDS // 60} minutes if "
                                                                      f"nothing's queued."))
            with contextlib.suppress(discord.HTTPException):
                await self.np.edit(embed=card, view=None)
            self.np = None


class Controls(discord.ui.View):
    def __init__(self, cog: "Music", guild_id: int) -> None:
        super().__init__(timeout=None)
        self.cog, self.guild_id = cog, guild_id
        player = cog.players.get(guild_id)
        if player is not None:
            paused = player.vc is not None and player.vc.is_paused()
            self.pause_button.label = "Resume" if paused else "Pause"
            self.pause_button.emoji = "▶️" if paused else "⏸️"
            self.loop_button.emoji = LOOP_ICON[player.loop]
            self.loop_button.style = discord.ButtonStyle.secondary if player.loop == "off" else discord.ButtonStyle.primary
            track = player.current
            if track is not None:
                service, _, _ = style(track)
                target = track.origin if track.origin and service in ("Spotify", "Apple Music", "Deezer") else track.url
                if target.startswith("http"):
                    self.add_item(discord.ui.Button(label=f"Open on {service}"[:80], url=target, row=2))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        problem_text = self.cog.control_problem(interaction.guild, interaction.user)
        if problem_text:
            await interaction.response.send_message(problem_text, ephemeral=True)
            return False
        return True

    def player(self) -> Player | None:
        return self.cog.players.get(self.guild_id)

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary, row=0)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player()
        if player is None:
            await interaction.response.send_message("Nothing's playing.", ephemeral=True)
            return
        if not player.pause():
            player.resume()
        await interaction.response.edit_message(embed=player.now_card(), view=Controls(self.cog, self.guild_id))

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player()
        if player is None or player.current is None:
            await interaction.response.send_message("Nothing's playing.", ephemeral=True)
            return
        title = link(player.current)
        player.skip()
        await interaction.response.send_message(embed=note(f"⏭️ {interaction.user.mention} skipped {title}"),
                                                allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player()
        if player is None:
            await interaction.response.send_message("Nothing's playing.", ephemeral=True)
            return
        player.stop()
        await interaction.response.edit_message(embed=note(f"⏹️ {interaction.user.mention} stopped the music "
                                                                f"and cleared the queue.", WARN), view=None)
        player.np = None
        player._arm_idle()

    @discord.ui.button(label="Loop", emoji="➡️", style=discord.ButtonStyle.secondary, row=0)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player()
        if player is None:
            await interaction.response.send_message("Nothing's playing.", ephemeral=True)
            return
        player.loop = LOOPS[(LOOPS.index(player.loop) + 1) % len(LOOPS)]
        await interaction.response.edit_message(embed=player.now_card(), view=Controls(self.cog, self.guild_id))

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player()
        if player is None or len(player.queue) < 2:
            await interaction.response.send_message("Need at least two songs queued to shuffle.", ephemeral=True)
            return
        random.shuffle(player.queue)
        await interaction.response.edit_message(embed=player.now_card(), view=Controls(self.cog, self.guild_id))

    @discord.ui.button(label="Queue", emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player()
        view = QueueView(self.cog, self.guild_id, interaction.user.id)
        await interaction.response.send_message(embed=self.cog.queue_card(player, 0), view=view, ephemeral=True)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def quieter_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._volume(interaction, -10)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def louder_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._volume(interaction, +10)

    async def _volume(self, interaction: discord.Interaction, change: int) -> None:
        player = self.player()
        if player is None:
            await interaction.response.send_message("Nothing's playing.", ephemeral=True)
            return
        player.volume = max(0, min(200, player.volume + change))
        player.restart()
        await interaction.response.edit_message(embed=player.now_card(), view=Controls(self.cog, self.guild_id))

    @discord.ui.button(label="Autoplay", emoji="♾️", style=discord.ButtonStyle.secondary, row=1)
    async def autoplay_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player()
        if player is None:
            await interaction.response.send_message("Nothing's playing.", ephemeral=True)
            return
        player.autoplay = not player.autoplay
        await interaction.response.edit_message(embed=player.now_card(), view=Controls(self.cog, self.guild_id))


class QueueView(discord.ui.View):
    def __init__(self, cog: "Music", guild_id: int, owner: int, page: int = 0) -> None:
        super().__init__(timeout=180)
        self.cog, self.guild_id, self.owner, self.page = cog, guild_id, owner, page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner

    async def _turn(self, interaction: discord.Interaction, step: int) -> None:
        player = self.cog.players.get(self.guild_id)
        pages = max(1, math.ceil(len(player.queue) / PAGE)) if player else 1
        self.page = (self.page + step) % pages
        await interaction.response.edit_message(embed=self.cog.queue_card(player, self.page), view=self)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._turn(interaction, -1)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def forward(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._turn(interaction, 1)


class SearchPick(discord.ui.View):
    def __init__(self, cog: "Music", ctx: commands.Context, tracks: list[Track]) -> None:
        super().__init__(timeout=60)
        self.cog, self.ctx, self.tracks = cog, ctx, tracks
        select = discord.ui.Select(placeholder="Pick a song", options=[
            discord.SelectOption(label=t.label[:100], value=str(i),
                                 description=f"{duration_text(t.duration)} · {t.uploader}"[:100])
            for i, t in enumerate(tracks)])
        select.callback = self.picked
        self.add_item(select)
        self.select = select

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.ctx.author.id

    async def picked(self, interaction: discord.Interaction) -> None:
        track = self.tracks[int(self.select.values[0])]
        await interaction.response.edit_message(content=f"Queued **{track.label[:150]}**", embed=None, view=None)
        await self.cog.enqueue(self.ctx, [track], name=None, announce=False)
        self.stop()


class Music(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.players: dict[int, Player] = {}
        self._session = None
        self._alone: dict[int, asyncio.Task] = {}
        self.node: ll.Node | None = None

    async def cog_load(self) -> None:
        conf = ll.settings()
        if conf is not None:
            self.node = ll.Node(self.bot, *conf)
            await self.node.start()
            log.info("Music plays through Lavalink at %s", conf[0])
            return
        missing = missing_parts()
        runtime = shutil.which("deno") or shutil.which("node")
        if missing:
            log.warning("Music is off: missing %s", ", ".join(missing))
        else:
            log.info("Music ready (ffmpeg %s, JavaScript runtime %s)", shutil.which("ffmpeg"), runtime or "none")
        if not runtime:
            log.warning("No Deno or Node found: YouTube may refuse some songs (yt-dlp needs one)")

    async def cog_unload(self) -> None:
        for guild_id in list(self.players):
            guild = self.bot.get_guild(guild_id)
            if guild is not None:
                await self.leave(guild, reason="unload")
        if self._session is not None:
            await self._session.close()
        if self.node is not None:
            await self.node.close()

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    def config(self, guild_id: int) -> dict:
        return self.bot.storage.get_config(guild_id, CONFIG)

    def player(self, guild: discord.Guild) -> Player:
        player = self.players.get(guild.id)
        if player is None:
            player = self.players[guild.id] = Player(self, guild)
            settings = self.config(guild.id)
            player.volume = int(settings.get("volume") or 100)
            player.stay = bool(settings.get("stay"))
        return player

    # Who can control playback

    def control_problem(self, guild, member) -> str | None:
        vc = getattr(guild, "voice_client", None)
        if vc is None:
            return "I'm not in a voice channel."
        if getattr(getattr(member, "voice", None), "channel", None) != vc.channel:
            return f"Join {vc.channel.mention} to control the music."
        dj = self.config(guild.id).get("dj")
        if not dj:
            return None
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and (perms.manage_channels or perms.administrator):
            return None
        if any(r.id == dj for r in getattr(member, "roles", [])):
            return None
        if humans_in(vc.channel) <= 1:
            return None
        player = self.players.get(guild.id)
        if player is not None and player.current is not None and player.current.requester == member.id:
            return None
        return f"Only the <@&{dj}> role controls the music here."

    async def ready_or_explain(self, ctx: commands.Context) -> bool:
        if self.node is not None:
            if self.node.available:
                return True
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.node.ready.wait(), 8)
            if self.node.available:
                return True
            await self.say(ctx, embed=problem("Music server is starting", "SYKE's music server isn't connected yet. "
                                                                          "Try again in a minute."), ephemeral=True)
            return False
        missing = missing_parts()
        if missing:
            await self.say(ctx, embed=problem("Music isn't available yet",
                                          f"The bot's host is missing {', '.join(missing)}. The bot owner needs to "
                                          f"redeploy with the music packages."), ephemeral=True)
            return False
        return True

    async def check_control(self, ctx: commands.Context) -> Player | None:
        text = self.control_problem(ctx.guild, ctx.author)
        if text:
            await self.say(ctx, text, ephemeral=True)
            return None
        return self.players.get(ctx.guild.id) or self.player(ctx.guild)

    # Looking songs up

    async def http(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                                  headers={"User-Agent": src.UA, "Accept-Language": "en"})
        return self._session

    async def fetch_text(self, url: str) -> tuple[str, str]:
        session = await self.http()
        async with session.get(url, allow_redirects=True) as resp:
            return str(resp.url), await resp.text()

    async def fetch_json(self, url: str) -> dict:
        session = await self.http()
        async with session.get(url) as resp:
            return await resp.json(content_type=None)

    async def ll_load(self, identifier: str) -> tuple[list[dict], str | None, str | None]:
        """(tracks, playlist name, error) from Lavalink."""
        data = await self.node.load(identifier)
        kind, body = data.get("loadType"), data.get("data")
        if kind == "track":
            return [body], None, None
        if kind == "search":
            return list(body or []), None, None
        if kind == "playlist":
            return list((body or {}).get("tracks") or []), ((body or {}).get("info") or {}).get("name"), None
        if kind == "error":
            return [], None, ((body or {}).get("message") or "couldn't load")[:200]
        return [], None, None

    async def ll_resolve(self, track: Track) -> str:
        """The Lavalink id for a track, searching for it if it's just a name (Spotify, Apple Music, Deezer)."""
        if track.encoded and not track.fallback:
            return track.encoded
        if track.encoded and track.fallback and "soundcloud" in (track.source or "").lower():
            return track.encoded
        text = track.query or track.title
        prefixes = ["scsearch:"] if track.fallback else ["ytsearch:", "scsearch:"]
        last = None
        for prefix in prefixes:
            try:
                found, _, error = await self.ll_load(prefix + text)
            except Exception as err:
                found, error = [], str(err)
            last = error or last
            if found:
                hit = ll_track(found[0])
                track.encoded = hit.encoded
                if not track.url or track.query or track.fallback:
                    track.url = hit.url or track.url
                track.source = hit.source or track.source
                track.duration = track.duration or hit.duration
                track.thumbnail = track.thumbnail or hit.thumbnail
                if track.title == track.url:
                    track.title = hit.title
                return track.encoded
        raise LookupError(src.clean_error(Exception(last)) if last else f"Couldn't find **{text[:100]}**.")

    async def ll_lookup(self, kind: str, value: str, requester: int) -> tuple[list[Track], str | None]:
        if kind == "url":
            identifier = value.split("&list=")[0] if ("list=RD" in value and "v=" in value) else value
        else:
            identifier = "ytsearch:" + value
        found, name, error = await self.ll_load(identifier)
        if not found and kind == "search":
            found, name, error2 = await self.ll_load("scsearch:" + value)
            error = error2 or error
        if not found:
            if kind == "url":
                title = await self._page_title(value)
                if title:
                    return [Track(title=title, query=title, requester=requester, source="Web")], None
            raise LookupError(src.clean_error(Exception(error)) if error else
                              f"No results for **{value[:100]}**.")
        if kind == "search" or name is None:
            found = found[:1]
        return [ll_track(t, requester) for t in found[: src.PLAYLIST_MAX]], name

    async def lookup(self, query: str, requester: int) -> tuple[list[Track], str | None]:
        """(tracks, playlist name or None). Raises LookupError with a friendly reason."""
        kind, value = src.classify(query)
        if kind == "short":
            final, _ = await self.fetch_text(value)
            kind, value = src.classify(final)
        try:
            if kind == "spotify":
                what, ident = value
                _, page = await self.fetch_text(f"https://open.spotify.com/embed/{what}/{ident}")
                name, tracks = src.spotify_tracks(page, what)
                return self._stamp(tracks, requester), (name if what != "track" else None)
            if kind == "apple":
                _, page = await self.fetch_text(value)
                name, tracks = src.apple_tracks(page)
                return self._stamp(tracks, requester), (name if len(tracks) > 1 else None)
            if kind == "deezer":
                what, ident = value
                data = await self.fetch_json(f"https://api.deezer.com/{what}/{ident}")
                name, tracks = src.deezer_tracks(data, what)
                return self._stamp(tracks, requester), (name if what != "track" else None)
        except LookupError:
            raise
        except Exception as error:
            log.info("Couldn't read %s: %s", query, error)
            raise LookupError("Couldn't read that link. Is it public?") from error
        if self.node is not None:
            return await self.ll_lookup(kind, value, requester)
        if kind == "url":
            url = value
            if "list=RD" in url and "v=" in url:  # a YouTube "mix" link: just the song
                url = url.split("&list=")[0]
            if src.is_youtube(url) and "list=" not in url:
                return [await self._youtube_link(url, requester)], None
            try:
                info = await src.extract(url, flat=True)
            except Exception as error:
                title = await self._page_title(url)
                if not title:
                    raise LookupError(src.clean_error(error)) from error
                return [Track(title=title, query=title, requester=requester, source="Web")], None
            if info and info.get("_type") == "playlist" and info.get("entries") is not None:
                entries = [e for e in info["entries"] if e][: src.PLAYLIST_MAX]
                tracks = [src.track_from_info(e, requester) for e in entries]
                return [t for t in tracks if t.url], info.get("title") or "playlist"
            track = src.track_from_info(info or {}, requester)
            if src.is_youtube(track.url) and not track.is_live:
                return [track], None  # downloaded when it plays
            return [src.fill_stream(track, info)], None
        return [await self._search_one(value, requester)], None

    async def _youtube_link(self, url: str, requester: int) -> Track:
        """A single YouTube video: its title from YouTube's oEmbed (no player request, no bot check).
        The audio is downloaded when it plays."""
        track = Track(title=url, url=url, requester=requester, source="Youtube")
        with contextlib.suppress(Exception):
            data = await self.fetch_json(f"https://www.youtube.com/oembed?format=json&url={quote(url, safe='')}")
            track.title = data.get("title") or url
            track.uploader = data.get("author_name") or ""
            track.thumbnail = data.get("thumbnail_url") or ""
        return track

    async def _search_one(self, text: str, requester: int) -> Track:
        """Find a song by name. A light YouTube search first (no player request, so no bot check); the
        audio itself is fetched when it plays. SoundCloud if YouTube finds nothing."""
        last = None
        try:
            info = await src.extract(src.SEARCH_PREFIX + text, flat=True)
            entries = [e for e in (info or {}).get("entries") or [] if e]
            if entries:
                track = src.track_from_info(entries[0], requester)
                if track.url:
                    return track
        except Exception as error:
            last = error
        try:
            entry = await src.soundcloud_full(text)
            if entry is not None:
                return src.fill_stream(src.track_from_info(entry, requester), entry)
        except Exception as error:
            last = error
        raise LookupError(src.clean_error(last) if last else f"No results for **{text[:100]}**.")

    async def _page_title(self, url: str) -> str:
        with contextlib.suppress(Exception):
            _, page = await self.fetch_text(url)
            return src._og(page, "og:title")
        return ""

    @staticmethod
    def _stamp(tracks: list[Track], requester: int) -> list[Track]:
        if not tracks:
            raise LookupError("Couldn't find any songs in that link.")
        for t in tracks:
            t.requester = requester
        return tracks

    async def add_related(self, player: Player, previous: Track) -> None:
        """Autoplay: queue songs from YouTube's mix for the last song."""
        video = previous.video_id()
        if not video:
            with contextlib.suppress(Exception):
                if self.node is not None:
                    hits, _, _ = await self.ll_load("ytsearch:" + (previous.query or previous.title))
                    video = ll_track(hits[0]).video_id() if hits else None
                else:
                    found = await self._search_one(previous.query or previous.title, 0)
                    video = found.video_id()
        if not video:
            return
        seen = {t.video_id() for t in player.history[-30:]} | {video}
        mix = f"https://www.youtube.com/watch?v={video}&list=RD{video}"
        try:
            if self.node is not None:
                found, _, _ = await self.ll_load(mix)
                candidates = [ll_track(t, 0) for t in found]
            else:
                info = await src.extract(mix, flat=True)
                candidates = [src.track_from_info(e, 0) for e in (info or {}).get("entries") or [] if e]
        except Exception:
            return
        for track in candidates:
            if track.video_id() and track.video_id() not in seen:
                player.queue.append(track)
                if len(player.queue) >= 3:
                    break

    # Joining and leaving

    async def connect(self, ctx: commands.Context) -> Player | None:
        voice = getattr(ctx.author, "voice", None)
        if voice is None or voice.channel is None:
            await self.say(ctx, "Join a voice channel first.", ephemeral=True)
            return None
        target = voice.channel
        vc = ctx.guild.voice_client
        player = self.player(ctx.guild)
        if vc is not None and vc.channel != target:
            if humans_in(vc.channel) and (vc.is_playing() or vc.is_paused()):
                await self.say(ctx, f"I'm already playing in {vc.channel.mention}.", ephemeral=True)
                return None
            await vc.move_to(target)
        elif vc is None:
            perms = target.permissions_for(ctx.guild.me)
            if not (perms.connect and perms.speak):
                await self.say(ctx, embed=problem("I can't join", f"Give SYKE **Connect** and **Speak** in {target.mention}."),
                                ephemeral=True)
                return None
            try:
                if self.node is not None:
                    await target.connect(cls=ll.LavalinkVoice, self_deaf=True, timeout=20, reconnect=True)
                else:
                    await target.connect(self_deaf=True, timeout=20, reconnect=True)
            except (discord.ClientException, asyncio.TimeoutError, RuntimeError) as error:
                log.warning("Voice connect failed in %s: %s", ctx.guild.id, error)
                await self.say(ctx, embed=problem("Couldn't join the voice channel", "Discord didn't let me in. Try again in "
                                                                                 "a moment."), ephemeral=True)
                return None
            if isinstance(target, discord.StageChannel):
                with contextlib.suppress(discord.HTTPException):
                    await ctx.guild.me.edit(suppress=False)
        player.channel_id = ctx.channel.id
        return player

    async def leave(self, guild: discord.Guild, reason: str = "") -> None:
        player = self.players.pop(guild.id, None)
        if player is not None:
            player.stop()
            player._cancel_idle()
            if player.np is not None:
                with contextlib.suppress(discord.HTTPException):
                    await player.np.edit(view=None)
            if reason == "idle":
                await player.say(embed("👋 Left the voice channel", "Nothing was playing. `play` brings me back."))
        vc = guild.voice_client
        if vc is not None:
            with contextlib.suppress(Exception):
                await vc.disconnect(force=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after) -> None:
        guild = member.guild
        me = guild.me
        if member.id == getattr(me, "id", None):
            log.info("SYKE voice state in %s: channel=%s mute=%s deaf=%s suppress=%s", guild.id,
                     getattr(after.channel, "name", None), after.mute, after.deaf, after.suppress)
            if after.channel is None and guild.id in self.players:
                player = self.players.pop(guild.id)
                player.stop()
                player._cancel_idle()
            return
        vc = guild.voice_client
        if vc is None or guild.id not in self.players:
            return
        player = self.players[guild.id]
        if humans_in(vc.channel) == 0 and not player.stay:
            if guild.id not in self._alone or self._alone[guild.id].done():
                async def wait_alone() -> None:
                    await asyncio.sleep(ALONE_SECONDS)
                    current = guild.voice_client
                    if current is not None and humans_in(current.channel) == 0:
                        await player.say(embed("👋 Left the voice channel", "Everyone left, so I did too."))
                        await self.leave(guild)
                self._alone[guild.id] = asyncio.create_task(wait_alone())
        elif guild.id in self._alone:
            self._alone.pop(guild.id).cancel()

    # Queueing

    async def enqueue(self, ctx: commands.Context, tracks: list[Track], name: str | None, *, front: bool = False,
                      announce: bool = True, source_link: str = "") -> None:
        player = await self.connect(ctx)
        if player is None:
            return
        room = QUEUE_MAX - len(player.queue)
        tracks = tracks[:max(0, room)]
        if not tracks:
            await self.say(ctx, f"The queue is full ({QUEUE_MAX} songs).", ephemeral=True)
            return
        was_idle = player.current is None
        wait = (max(0.0, (player.current.duration or 0) - player.position()) if player.current else 0.0)
        wait += sum(t.duration or 0 for t in (player.queue[:0] if front else player.queue))
        if front:
            player.queue[0:0] = tracks
        else:
            player.queue.extend(tracks)
        position = 1 if front else len(player.queue) - len(tracks) + 1
        await player.start_or_queue()
        if not announce:
            return
        first = tracks[0]
        _, colour, icon = style(first)
        if name:
            title = f"[{discord.utils.escape_markdown(name[:120])}]({source_link})" if source_link.startswith("http") \
                else f"**{discord.utils.escape_markdown(name[:120])}**"
            total = sum(t.duration or 0 for t in tracks)
            card = note(f"📜 Queued **{len(tracks)}** songs from {title}" +
                        (f" · `{duration_text(total)}`" if total else ""), colour)
        elif was_idle:
            if ctx.interaction is None:
                return  # the now-playing card is the answer
            card = note(f"{icon} Starting {link(first)}", colour)
        else:
            card = note(f"{icon} Added {link(first)} · `{duration_text(first.duration)}`", colour)
            card.set_footer(text=f"#{position} in the queue" + (f"  •  plays in about {duration_text(wait)}"
                                                                 if wait else ""))
        await self.say(ctx, embed=card)
        if not was_idle:
            await player.refresh_card()

    async def play_query(self, ctx: commands.Context, query: str, *, front: bool = False) -> None:
        if not await self.ready_or_explain(ctx):
            return
        if getattr(getattr(ctx.author, "voice", None), "channel", None) is None:
            await self.say(ctx, "Join a voice channel first.", ephemeral=True)
            return
        vc = ctx.guild.voice_client
        if front and vc is not None and self.control_problem(ctx.guild, ctx.author):
            await self.say(ctx, self.control_problem(ctx.guild, ctx.author), ephemeral=True)
            return
        await ctx.defer()
        typing = ctx.typing() if ctx.interaction is None else contextlib.nullcontext()
        async with typing:  # "SYKE is typing…" until the song starts or is queued
            try:
                tracks, name = await self.lookup(query, ctx.author.id)
            except LookupError as error:
                await self.say(ctx, embed=problem("Couldn't play that", str(error)), ephemeral=True)
                return
            player = self.players.get(ctx.guild.id)
            if player is None or player.current is None:  # fetch the first song while joining voice
                prep = asyncio.create_task(self.ll_resolve(tracks[0]) if self.node is not None
                                           else src.resolve_stream(tracks[0]))
                prep.add_done_callback(lambda t: t.cancelled() or t.exception())
            await self.enqueue(ctx, tracks, name, front=front, source_link=query.strip("<> ") if name else "")

    def queue_card(self, player: Player | None, page: int) -> discord.Embed:
        if player is None or (player.current is None and not player.queue):
            return note("📜 The queue is empty. Add songs with `play`.")
        lines = []
        colour = BRAND
        if player.current is not None:
            _, colour, icon = style(player.current)
            lines.append(f"**Now playing**\n{icon} {link(player.current)} · "
                         f"`{duration_text(player.position())} / {duration_text(player.current.duration)}`")
        start = page * PAGE
        upcoming = player.queue[start:start + PAGE]
        if upcoming:
            lines.append("\n**Up next**")
        for i, track in enumerate(upcoming, start=start + 1):
            who = f" · <@{track.requester}>" if track.requester else ""
            lines.append(f"`{i:>2}.` {link(track, 60)} · `{duration_text(track.duration)}`{who}")
        if not player.queue:
            lines.append("\nNothing after this song.")
        pages = max(1, math.ceil(len(player.queue) / PAGE))
        card = discord.Embed(title="📜 Queue", description="\n".join(lines), colour=colour)
        card.set_footer(text=f"Page {page + 1}/{pages}  •  {len(player.queue)} songs  •  "
                             f"{duration_text(player.queue_seconds())} total  •  loop {player.loop}")
        return card

    async def say(self, ctx, text: str | None = None, *, embed: discord.Embed | None = None, view=None,
                  ephemeral: bool = False, mention_author: bool = False, colour: discord.Colour | None = None):
        """Every music reply as a tidy embed: warnings in amber, the rest in SYKE purple."""
        if embed is None:
            embed = note(text or "", colour or (WARN if ephemeral else BRAND))
        kwargs = {"embed": embed, "mention_author": False, "ephemeral": ephemeral}
        if view is not None:
            kwargs["view"] = view
        return await ctx.reply(**kwargs)

    # The commands (shared by the top-level prefix commands and the /music group)

    async def do_skip(self, ctx, count: int = 1) -> None:
        player = await self.check_control(ctx)
        if player is None:
            return
        if player.current is None:
            await self.say(ctx, "Nothing's playing.", ephemeral=True)
            return
        title = link(player.current)
        if count > 1:
            del player.queue[:count - 1]
        player.skip()
        extra = f" and {count - 1} more" if count > 1 else ""
        await self.say(ctx, f"⏭️ Skipped {title}{extra}.")

    async def do_pause(self, ctx) -> None:
        player = await self.check_control(ctx)
        if player is not None:
            await self.say(ctx, "⏸️ Paused." if player.pause() else "Nothing's playing.", mention_author=False)
            await player.refresh_card()

    async def do_resume(self, ctx) -> None:
        player = await self.check_control(ctx)
        if player is not None:
            await self.say(ctx, "▶️ Resumed." if player.resume() else "Nothing's paused.", mention_author=False)
            await player.refresh_card()

    async def do_stop(self, ctx) -> None:
        player = await self.check_control(ctx)
        if player is None:
            return
        player.stop()
        if player.np is not None:
            with contextlib.suppress(discord.HTTPException):
                await player.np.edit(view=None)
            player.np = None
        player._arm_idle()
        await self.say(ctx, "⏹️ Stopped and cleared the queue.", mention_author=False)

    async def do_leave(self, ctx) -> None:
        if ctx.guild.voice_client is None:
            await self.say(ctx, "I'm not in a voice channel.", ephemeral=True)
            return
        if await self.check_control(ctx) is None:
            return
        await self.leave(ctx.guild)
        await self.say(ctx, "👋 Bye.", mention_author=False)

    async def do_join(self, ctx) -> None:
        if not await self.ready_or_explain(ctx):
            return
        player = await self.connect(ctx)
        if player is not None:
            player._arm_idle()
            await self.say(ctx, f"Joined {ctx.guild.voice_client.channel.mention}. Queue something with `{self.p(ctx)}play`.",
                            mention_author=False)

    async def do_now(self, ctx) -> None:
        player = self.players.get(ctx.guild.id)
        if player is None or player.current is None:
            await self.say(ctx, "Nothing's playing.", ephemeral=True)
            return
        player.channel_id = ctx.channel.id
        if ctx.interaction:
            await self.say(ctx, "Here it is 👇", ephemeral=True)
        await player.now_playing_card()

    async def do_queue(self, ctx, page: int = 1) -> None:
        player = self.players.get(ctx.guild.id)
        pages = max(1, math.ceil(len(player.queue) / PAGE)) if player else 1
        page = max(1, min(page, pages)) - 1
        view = QueueView(self, ctx.guild.id, ctx.author.id, page) if pages > 1 else None
        await self.say(ctx, embed=self.queue_card(player, page), view=view, mention_author=False)

    async def do_volume(self, ctx, level: int | None) -> None:
        player = self.players.get(ctx.guild.id)
        if level is None:
            await self.say(ctx, f"🔊 Volume is **{player.volume if player else self.config(ctx.guild.id).get('volume') or 100}%**.",
                            mention_author=False)
            return
        player = await self.check_control(ctx)
        if player is None:
            return
        player.volume = max(0, min(200, level))
        self.bot.storage.update_config(ctx.guild.id, CONFIG, volume=player.volume)
        player.restart()
        await self.say(ctx, f"🔊 Volume set to **{player.volume}%**.", mention_author=False)

    async def do_loop(self, ctx, mode: str | None) -> None:
        player = await self.check_control(ctx)
        if player is None:
            return
        mode = (mode or "").lower()
        mode = {"song": "track", "one": "track", "single": "track", "all": "queue", "none": "off", "stop": "off"}.get(mode, mode)
        if mode not in LOOPS:
            mode = LOOPS[(LOOPS.index(player.loop) + 1) % len(LOOPS)]
        player.loop = mode
        await self.say(ctx, f"{LOOP_ICON[mode]} Loop: **{mode}**.", mention_author=False)
        await player.refresh_card()

    async def do_shuffle(self, ctx) -> None:
        player = await self.check_control(ctx)
        if player is None:
            return
        if len(player.queue) < 2:
            await self.say(ctx, "Need at least two songs queued to shuffle.", ephemeral=True)
            return
        random.shuffle(player.queue)
        await self.say(ctx, f"🔀 Shuffled {len(player.queue)} songs.", mention_author=False)
        await player.refresh_card()

    async def do_remove(self, ctx, position: int) -> None:
        player = await self.check_control(ctx)
        if player is None:
            return
        if not 1 <= position <= len(player.queue):
            await self.say(ctx, f"Pick a number from 1 to {len(player.queue)}.", ephemeral=True)
            return
        track = player.queue.pop(position - 1)
        await self.say(ctx, f"🗑️ Removed {link(track)}.")

    async def do_move(self, ctx, start: int, to: int) -> None:
        player = await self.check_control(ctx)
        if player is None:
            return
        size = len(player.queue)
        if not (1 <= start <= size and 1 <= to <= size):
            await self.say(ctx, f"Pick numbers from 1 to {size}.", ephemeral=True)
            return
        track = player.queue.pop(start - 1)
        player.queue.insert(to - 1, track)
        await self.say(ctx, f"↕️ Moved {link(track)} to **#{to}**.")

    async def do_seek(self, ctx, when: str) -> None:
        player = await self.check_control(ctx)
        if player is None or player.current is None:
            if player is not None:
                await self.say(ctx, "Nothing's playing.", ephemeral=True)
            return
        text = when.strip()
        relative = text[:1] in "+-"
        seconds = src.parse_time(text.lstrip("+-"))
        if seconds is None:
            await self.say(ctx, "Try `1:30`, `90`, `+15` or `-10`.", ephemeral=True)
            return
        if relative:
            seconds = player.position() + (seconds if text[0] == "+" else -seconds)
        if player.current.is_live:
            await self.say(ctx, "Can't seek in a live stream.", ephemeral=True)
            return
        total = player.current.duration or 0
        seconds = max(0, min(seconds, max(0, total - 1)))
        player.restart(seconds)
        await self.say(ctx, f"⏩ Jumped to `{duration_text(seconds)}`.", mention_author=False)

    async def do_clear(self, ctx) -> None:
        player = await self.check_control(ctx)
        if player is not None:
            count = len(player.queue)
            player.queue.clear()
            await self.say(ctx, f"🧹 Cleared {count} songs from the queue.", mention_author=False)

    async def do_autoplay(self, ctx, enabled: bool | None) -> None:
        player = await self.check_control(ctx)
        if player is None:
            return
        player.autoplay = (not player.autoplay) if enabled is None else enabled
        await self.say(ctx, f"♾️ Autoplay **{'on' if player.autoplay else 'off'}**"
                        + (": similar songs keep coming when the queue runs out." if player.autoplay else "."),
                        mention_author=False)
        await player.refresh_card()

    async def do_stay(self, ctx, enabled: bool | None) -> None:
        perms = getattr(ctx.author, "guild_permissions", None)
        if not (perms and (perms.manage_guild or perms.manage_channels)):
            await self.say(ctx, "You need **Manage Channels** to change 24/7 mode.", ephemeral=True)
            return
        current = bool(self.config(ctx.guild.id).get("stay"))
        value = (not current) if enabled is None else enabled
        self.bot.storage.update_config(ctx.guild.id, CONFIG, stay=value or None)
        player = self.players.get(ctx.guild.id)
        if player is not None:
            player.stay = value
            if value:
                player._cancel_idle()
            elif player.current is None:
                player._arm_idle()
        await self.say(ctx, f"🌙 24/7 mode **{'on' if value else 'off'}**"
                        + (": I'll stay in voice even when nothing's playing." if value else "."), mention_author=False)

    async def do_filter(self, ctx, name: str | None) -> None:
        options = ", ".join(f"`{f}`" for f in FILTERS)
        if not name:
            player = self.players.get(ctx.guild.id)
            await self.say(ctx, f"✨ Effect: **{player.filter if player else 'off'}**. Options: {options}.", mention_author=False)
            return
        name = name.lower().replace(" ", "").replace("-", "")
        name = {"bass": "bassboost", "boost": "bassboost", "none": "off", "clear": "off", "reset": "off",
                "8": "8d", "fast": "speed", "slow": "slowed", "slowedreverb": "slowed"}.get(name, name)
        if name not in FILTERS:
            await self.say(ctx, f"Pick one of {options}.", ephemeral=True)
            return
        player = await self.check_control(ctx)
        if player is None:
            return
        position = player.position()
        player.filter = name
        player.base, player.t0 = position, time.monotonic()
        player.restart(position)
        await self.say(ctx, f"✨ Effect: **{name}**.", mention_author=False)

    async def do_search(self, ctx, query: str) -> None:
        if not await self.ready_or_explain(ctx):
            return
        await ctx.defer()
        try:
            if self.node is not None:
                found, _, error = await self.ll_load(f"ytsearch:{query}")
                if not found and error:
                    raise Exception(error)
                tracks = [ll_track(t, ctx.author.id) for t in found[:5]]
            else:
                info = await src.extract(f"ytsearch5:{query}", flat=True)
                tracks = [src.track_from_info(e, ctx.author.id) for e in (info or {}).get("entries") or [] if e][:5]
        except Exception as error:
            await self.say(ctx, embed=problem("Search failed", src.clean_error(error)), ephemeral=True)
            return
        if not tracks:
            await self.say(ctx, f"No results for **{query[:100]}**.", ephemeral=True)
            return
        lines = [f"`{i}.` {t.label[:80]} `{duration_text(t.duration)}`" for i, t in enumerate(tracks, 1)]
        await self.say(ctx, embed=embed(f"🔎 {query[:100]}", "\n".join(lines)), view=SearchPick(self, ctx, tracks))

    # Play history, radio, song quiz

    def record_play(self, guild_id: int, track: Track) -> None:
        player = self.players.get(guild_id)
        if player is not None and player.quiz:
            return
        try:
            self.bot.storage.record_play(guild_id, track.label, track.url or track.origin, track.requester)
        except Exception:
            log.warning("Couldn't record a play in %s", guild_id, exc_info=True)

    async def radio_fill(self, player: Player, count: int = RADIO_BATCH) -> None:
        genre = player.radio
        if not genre:
            return
        recent = {t.label.lower() for t in player.history[-60:]} | {t.label.lower() for t in player.queue}
        if player.current is not None:
            recent.add(player.current.label.lower())
        if genre in lists.GENRES:
            pool = [(a, t) for a, t in lists.GENRES[genre] if f"{t} — {a}".lower() not in recent]
            if len(pool) < count:  # gone through the list: start over, minus what just played
                pool = [(a, t) for a, t in lists.GENRES[genre]
                        if f"{t} — {a}".lower() not in {x.label.lower() for x in player.history[-8:]}]
            random.shuffle(pool)
            for artist, title in pool[:count]:
                player.queue.append(Track(title=f"{title} — {artist}", query=f"{artist} - {title}", uploader=artist,
                                          source="Radio"))
            return
        seeds = list(lists.RADIO_SEARCHES.get(genre, []))
        random.shuffle(seeds)
        added = 0
        for seed in seeds:
            try:
                if self.node is not None:
                    found, _, _ = await self.ll_load(f"ytsearch:{seed}")
                    candidates = [ll_track(t) for t in found]
                else:
                    info = await src.extract(f"ytsearch10:{seed}", flat=True)
                    candidates = [src.track_from_info(e) for e in (info or {}).get("entries") or [] if e]
            except Exception:
                continue
            random.shuffle(candidates)
            for track in candidates:
                if track.label.lower() in recent or (track.duration and track.duration > 900):
                    continue
                recent.add(track.label.lower())
                player.queue.append(track)
                added += 1
                if added >= count:
                    return
                break  # one per search, so the mix stays varied

    async def do_radio(self, ctx, genre: str | None) -> None:
        p = self.p(ctx)
        genres = ", ".join(f"`{g}`" for g in lists.all_genres())
        if not genre:
            player = self.players.get(ctx.guild.id)
            on = f"📻 **{player.radio}** radio is on. " if player and player.radio else ""
            await self.say(ctx, f"{on}Start one with `{p}radio <genre>`: {genres}. Stop with `{p}radio off`.")
            return
        if genre.lower() in ("off", "stop", "none"):
            player = self.players.get(ctx.guild.id)
            if player is None or not player.radio:
                await self.say(ctx, "No radio is on.", ephemeral=True)
                return
            if await self.check_control(ctx) is None:
                return
            player.radio = None
            await self.say(ctx, "📻 Radio off. The queue plays out, then stops.")
            await player.refresh_card()
            return
        key = lists.resolve_genre(genre)
        if key is None:
            await self.say(ctx, f"No **{discord.utils.escape_markdown(genre[:40])}** station. Try: {genres}.",
                           ephemeral=True)
            return
        if not await self.ready_or_explain(ctx):
            return
        player = self.players.get(ctx.guild.id)
        if player is not None and player.quiz:
            await self.say(ctx, "A song quiz is running. Radio can start after it.", ephemeral=True)
            return
        if ctx.guild.voice_client is not None and self.control_problem(ctx.guild, ctx.author):
            await self.say(ctx, self.control_problem(ctx.guild, ctx.author), ephemeral=True)
            return
        await ctx.defer()
        player = await self.connect(ctx)
        if player is None:
            return
        player.radio, player.autoplay = key, False
        if len(player.queue) < 2:
            await self.radio_fill(player)
        await self.say(ctx, f"📻 **{key.title()} radio** is on: songs keep coming until you `{p}radio off`.")
        await player.start_or_queue()
        await player.refresh_card()

    async def play_clip(self, player: Player, track: Track) -> bool:
        """Start a quiz clip of `track` (a stretch from its middle). False if it couldn't be played."""
        vc = player.vc
        if vc is None:
            return False
        try:
            if isinstance(vc, ll.LavalinkVoice):
                encoded = await self.ll_resolve(track)
                start = 45.0 if not track.duration or track.duration > 120 else max(0.0, track.duration * 0.3)
                await vc.play_track(encoded, position_ms=int(start * 1000), volume=player.volume, filters={}, after=None)
                return True
            await src.resolve_stream(track)
            start = 45.0 if not track.duration or track.duration > 120 else max(0.0, track.duration * 0.3)
            before, options, codec = src.ffmpeg_args(track, position=start, volume=player.volume)
            await player._wait_for_encryption(vc)
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            vc.play(discord.FFmpegOpusAudio(track.stream, before_options=before, options=f"{options} -t {QUIZ_CLIP + 2}",
                                            codec=codec))
            return True
        except Exception as error:
            log.info("Quiz clip failed for %s: %s", track.label, error)
            return False

    async def do_quiz(self, ctx, genre: str | None, rounds: int) -> None:
        p = self.p(ctx)
        if not await self.ready_or_explain(ctx):
            return
        if getattr(getattr(ctx.author, "voice", None), "channel", None) is None:
            await self.say(ctx, "Join a voice channel first: the quiz plays the songs there.", ephemeral=True)
            return
        player = self.players.get(ctx.guild.id)
        if player is not None and player.quiz:
            await self.say(ctx, "A song quiz is already running.", ephemeral=True)
            return
        if player is not None and (player.current is not None or player.queue):
            await self.say(ctx, f"Music is playing. `{p}stop` it first, then start the quiz.", ephemeral=True)
            return
        key = lists.resolve_genre(genre) if genre else None
        if genre and (key is None or key not in lists.GENRES):
            names = ", ".join(f"`{g}`" for g in lists.GENRES)
            await self.say(ctx, f"Pick a genre: {names}, or leave it empty for a mix.", ephemeral=True)
            return
        rounds = max(1, min(QUIZ_MAX_ROUNDS, rounds))
        pool = [(a, t) for g, songs in lists.GENRES.items() if key in (None, g) for a, t in songs]
        random.shuffle(pool)
        await ctx.defer()
        player = await self.connect(ctx)
        if player is None:
            return
        player.quiz = True
        player._cancel_idle()
        eco = self.bot.get_cog("Economy")
        paying = eco is not None and eco._open(ctx.guild.id)
        scores: dict[int, int] = {}
        intro = note(f"🎧 **Song quiz** · {rounds} round{'s' if rounds != 1 else ''} · "
                     f"{key.title() if key else 'mixed genres'}\nI play {QUIZ_CLIP} seconds of a song in "
                     f"{player.vc.channel.mention}. First to type the **song title** here wins the round"
                     + (f" and {eco.money(QUIZ_PRIZE)}." if paying else "."))
        await self.say(ctx, embed=intro)
        channel = ctx.channel
        played = 0
        try:
            while played < rounds and pool:
                artist, title = pool.pop()
                track = Track(title=f"{title} — {artist}", query=f"{artist} - {title}", uploader=artist)
                if not await self.play_clip(player, track):
                    continue
                played += 1
                await channel.send(embed=note(f"🎵 **Round {played}/{rounds}**: name that song! "
                                              f"*{QUIZ_CLIP + QUIZ_GRACE} seconds.*"))
                started = time.monotonic()

                def check(message: discord.Message) -> bool:
                    return (message.channel.id == channel.id and not message.author.bot
                            and quiz_match(message.content or "", title, artist))

                winner = None
                try:
                    hit = await self.bot.wait_for("message", check=check, timeout=QUIZ_CLIP + QUIZ_GRACE)
                    winner = hit.author
                except asyncio.TimeoutError:
                    pass
                took = time.monotonic() - started
                vc = player.vc
                if vc is not None:
                    vc.stop()
                answer = link(track)
                if winner is not None:
                    scores[winner.id] = scores.get(winner.id, 0) + 1
                    prize = ""
                    if paying:
                        self.bot.storage.econ_touch(ctx.guild.id, winner.id, 0)
                        self.bot.storage.econ_adjust(ctx.guild.id, winner.id, cash=QUIZ_PRIZE)
                        prize = f" +{eco.money(QUIZ_PRIZE)}"
                    text = f"✅ {winner.mention} got it in {took:.1f}s!{prize}\nIt was {answer}."
                    colour = OK
                else:
                    text, colour = f"⏱️ Nobody got it. It was {answer}.", WARN
                await channel.send(embed=note(text, colour), allowed_mentions=discord.AllowedMentions.none())
                await asyncio.sleep(2)
        finally:
            player.quiz = False
            vc = player.vc
            if vc is not None and (vc.is_playing() or vc.is_paused()):
                vc.stop()
        if scores:
            board = sorted(scores.items(), key=lambda kv: -kv[1])
            medals = ["🥇", "🥈", "🥉"]
            lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> · **{pts}**" for i, (uid, pts) in enumerate(board)]
            final = note("🏁 **Quiz over!**\n" + "\n".join(lines))
        else:
            final = note("🏁 **Quiz over!** Nobody scored. Tough crowd.", WARN)
        await channel.send(embed=final, allowed_mentions=discord.AllowedMentions.none())
        if player.queue:
            await player.start_or_queue()
        else:
            player._arm_idle()

    async def do_top(self, ctx, what: str = "songs", period: str = "week") -> None:
        days = {"week": 7, "month": 30, "day": 1, "today": 1, "all": 36500, "ever": 36500}.get(period.lower(), 7)
        since = time.time() - days * 86400
        label = {1: "today", 7: "this week", 30: "this month"}.get(days, "of all time")
        if what.lower().startswith("dj"):
            rows = await asyncio.to_thread(self.bot.storage.top_djs, ctx.guild.id, since, 10)
            if not rows:
                await self.say(ctx, f"No DJs {label} yet. Queue something with `{self.p(ctx)}play`.")
                return
            medals = ["🥇", "🥈", "🥉"]
            lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> · **{n}** song{'s' if n != 1 else ''}"
                     for i, (uid, n) in enumerate(rows)]
            card = discord.Embed(title=f"🎧 Biggest DJs {label}", description="\n".join(lines), colour=BRAND)
        else:
            rows = await asyncio.to_thread(self.bot.storage.top_songs, ctx.guild.id, since, 10)
            if not rows:
                await self.say(ctx, f"Nothing played {label} yet. Start with `{self.p(ctx)}play`.")
                return
            lines = []
            for i, (title, url, n) in enumerate(rows, 1):
                track = Track(title=title, url=url or "")
                lines.append(f"`{i:>2}.` {link(track, 70)} · **{n}** play{'s' if n != 1 else ''}")
            card = discord.Embed(title=f"🏆 Most played {label}", description="\n".join(lines), colour=BRAND)
        card.set_footer(text=f"{self.p(ctx)}topsongs [songs|djs] [day|week|month|all]")
        await self.say(ctx, embed=card)

    @commands.command(name="radio", aliases=["station"], help="Endless music by genre: radio drill, radio lofi, radio off")
    @commands.guild_only()
    async def radio_cmd(self, ctx: commands.Context, *, genre: str | None = None) -> None:
        await self.do_radio(ctx, genre)

    @commands.command(name="songquiz", aliases=["musicquiz", "sq", "namethatsong"],
                      help="Guess the song from 15 seconds of it: songquiz [genre] [rounds]")
    @commands.guild_only()
    async def songquiz_cmd(self, ctx: commands.Context, *args: str) -> None:
        genre, rounds = None, QUIZ_ROUNDS
        for arg in args:
            if arg.isdigit():
                rounds = int(arg)
            else:
                genre = f"{genre} {arg}".strip() if genre else arg
        await self.do_quiz(ctx, genre, rounds)

    @commands.command(name="topsongs", aliases=["charts", "topdjs", "musictop"],
                      help="Most played songs and biggest DJs: topsongs [songs|djs] [day|week|month|all]")
    @commands.guild_only()
    async def topsongs_cmd(self, ctx: commands.Context, what: str = "songs", period: str = "week") -> None:
        if ctx.invoked_with and ctx.invoked_with.lower() == "topdjs":
            what, period = "djs", (what if what != "songs" else period)
        if what.lower() in ("day", "today", "week", "month", "all", "ever"):
            what, period = "songs", what
        await self.do_top(ctx, what, period)

    # Top-level commands

    @commands.hybrid_command(name="play", aliases=["pl", "sing"], description="Play a song or playlist in your voice channel")
    @app_commands.describe(query="A song name, or a link from YouTube, Spotify, SoundCloud, Apple Music, Deezer…")
    @app_commands.guild_only()
    @commands.guild_only()
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        await self.play_query(ctx, query)

    @commands.hybrid_command(name="skip", aliases=["next", "s"], description="Skip the current song")
    @app_commands.describe(count="How many songs to skip (default 1)")
    @app_commands.guild_only()
    @commands.guild_only()
    async def skip(self, ctx: commands.Context, count: int = 1) -> None:
        await self.do_skip(ctx, max(1, count))

    @commands.hybrid_command(name="queue", aliases=["qu", "upcoming", "songs"], description="See what's playing next")
    @app_commands.describe(page="Page number")
    @app_commands.guild_only()
    @commands.guild_only()
    async def queue(self, ctx: commands.Context, page: int = 1) -> None:
        await self.do_queue(ctx, page)

    @commands.command(name="playnext", aliases=["pn", "playtop"], help="Queue a song to play right after this one")
    @commands.guild_only()
    async def playnext_cmd(self, ctx: commands.Context, *, query: str) -> None:
        await self.play_query(ctx, query, front=True)

    @commands.command(name="pause", help="Pause the music")
    @commands.guild_only()
    async def pause_cmd(self, ctx: commands.Context) -> None:
        await self.do_pause(ctx)

    @commands.command(name="resume", aliases=["unpause"], help="Resume the music")
    @commands.guild_only()
    async def resume_cmd(self, ctx: commands.Context) -> None:
        await self.do_resume(ctx)

    @commands.command(name="stop", help="Stop the music and clear the queue")
    @commands.guild_only()
    async def stop_cmd(self, ctx: commands.Context) -> None:
        await self.do_stop(ctx)

    @commands.command(name="leave", aliases=["dc", "disconnect"], help="Leave the voice channel")
    @commands.guild_only()
    async def leave_cmd(self, ctx: commands.Context) -> None:
        await self.do_leave(ctx)

    @commands.command(name="join", aliases=["summon", "connect"], help="Join your voice channel")
    @commands.guild_only()
    async def join_cmd(self, ctx: commands.Context) -> None:
        await self.do_join(ctx)

    @commands.command(name="nowplaying", aliases=["np", "song", "current"], help="Show the song that's playing")
    @commands.guild_only()
    async def np_cmd(self, ctx: commands.Context) -> None:
        await self.do_now(ctx)

    @commands.command(name="volume", aliases=["vol", "v"], help="Set the volume, 0 to 200")
    @commands.guild_only()
    async def volume_cmd(self, ctx: commands.Context, level: Optional[int] = None) -> None:
        await self.do_volume(ctx, level)

    @commands.command(name="loop", aliases=["repeat", "lp"], help="Loop: off, track or queue")
    @commands.guild_only()
    async def loop_cmd(self, ctx: commands.Context, mode: Optional[str] = None) -> None:
        await self.do_loop(ctx, mode)

    @commands.command(name="shuffle", aliases=["mix"], help="Shuffle the queue")
    @commands.guild_only()
    async def shuffle_cmd(self, ctx: commands.Context) -> None:
        await self.do_shuffle(ctx)

    @commands.command(name="remove", aliases=["rmsong"], help="Remove a song from the queue by its number")
    @commands.guild_only()
    async def remove_cmd(self, ctx: commands.Context, position: int) -> None:
        await self.do_remove(ctx, position)

    @commands.command(name="move", aliases=["mv"], help="Move a song in the queue: move <from> <to>")
    @commands.guild_only()
    async def move_cmd(self, ctx: commands.Context, start: int, to: int) -> None:
        await self.do_move(ctx, start, to)

    @commands.command(name="seek", aliases=["jump", "goto"], help="Jump to a time: 1:30, 90, +15 or -10")
    @commands.guild_only()
    async def seek_cmd(self, ctx: commands.Context, *, when: str) -> None:
        await self.do_seek(ctx, when)

    @commands.command(name="clearqueue", aliases=["cq", "emptyqueue"], help="Clear the queue (keeps the current song)")
    @commands.guild_only()
    async def clear_cmd(self, ctx: commands.Context) -> None:
        await self.do_clear(ctx)

    @commands.command(name="autoplay", aliases=["ap"], help="Keep playing similar songs when the queue ends")
    @commands.guild_only()
    async def autoplay_cmd(self, ctx: commands.Context, enabled: Optional[bool] = None) -> None:
        await self.do_autoplay(ctx, enabled)

    @commands.command(name="247", aliases=["stay", "24/7"], help="Stay in voice even when nothing's playing")
    @commands.guild_only()
    async def stay_cmd(self, ctx: commands.Context, enabled: Optional[bool] = None) -> None:
        await self.do_stay(ctx, enabled)

    @commands.command(name="fx", aliases=["effect", "effects", "audiofilter"],
                      help="Sound effects: bassboost, nightcore, vaporwave, 8d, slowed, speed, karaoke, treble, loud, off")
    @commands.guild_only()
    async def fx_cmd(self, ctx: commands.Context, *, name: Optional[str] = None) -> None:
        await self.do_filter(ctx, name)

    @commands.command(name="search", aliases=["yt", "find"], help="Search YouTube and pick a result")
    @commands.guild_only()
    async def search_cmd(self, ctx: commands.Context, *, query: str) -> None:
        await self.do_search(ctx, query)

    # The /music group

    @commands.hybrid_group(name="music", fallback="help", case_insensitive=True,
                           description="Music controls: pause, loop, volume, effects and more")
    @app_commands.guild_only()
    @commands.guild_only()
    async def music(self, ctx: commands.Context) -> None:
        p = self.p(ctx)
        body = (f"`{p}play <song or link>` · `{p}playnext` · `{p}skip` · `{p}queue` · `{p}np`\n"
                f"`{p}pause` · `{p}resume` · `{p}stop` · `{p}leave` · `{p}join`\n"
                f"`{p}volume 0-200` · `{p}loop off/track/queue` · `{p}shuffle` · `{p}remove 3` · `{p}move 5 1`\n"
                f"`{p}seek 1:30` · `{p}clearqueue` · `{p}autoplay` · `{p}247` · `{p}fx bassboost` · `{p}search`\n\n"
                "Plays from YouTube, YouTube Music, Spotify, SoundCloud, Apple Music, Deezer, Bandcamp, Twitch, "
                "Vimeo, TikTok, Mixcloud, radio streams and direct audio links.")
        dj = self.config(ctx.guild.id).get("dj")
        body += f"\n\nOnly <@&{dj}> controls the music here." if dj else ""
        await self.say(ctx, embed=embed("🎵 Music", body), ephemeral=True)

    @music.command(name="playnext", description="Queue a song to play right after this one")
    @app_commands.describe(query="A song name or link")
    async def m_playnext(self, ctx: commands.Context, *, query: str) -> None:
        await self.play_query(ctx, query, front=True)

    @music.command(name="pause", description="Pause the music")
    async def m_pause(self, ctx: commands.Context) -> None:
        await self.do_pause(ctx)

    @music.command(name="resume", description="Resume the music")
    async def m_resume(self, ctx: commands.Context) -> None:
        await self.do_resume(ctx)

    @music.command(name="stop", description="Stop the music and clear the queue")
    async def m_stop(self, ctx: commands.Context) -> None:
        await self.do_stop(ctx)

    @music.command(name="leave", description="Leave the voice channel")
    async def m_leave(self, ctx: commands.Context) -> None:
        await self.do_leave(ctx)

    @music.command(name="join", description="Join your voice channel")
    async def m_join(self, ctx: commands.Context) -> None:
        await self.do_join(ctx)

    @music.command(name="nowplaying", description="Show the song that's playing")
    async def m_np(self, ctx: commands.Context) -> None:
        await self.do_now(ctx)

    @music.command(name="volume", description="Set the volume")
    @app_commands.describe(level="0 to 200 (100 is normal)")
    async def m_volume(self, ctx: commands.Context, level: Optional[app_commands.Range[int, 0, 200]] = None) -> None:
        await self.do_volume(ctx, level)

    @music.command(name="loop", description="Loop the song, the queue, or nothing")
    @app_commands.describe(mode="off, track or queue")
    async def m_loop(self, ctx: commands.Context, mode: Optional[str] = None) -> None:
        await self.do_loop(ctx, mode)

    @m_loop.autocomplete("mode")
    async def _loop_modes(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=m, value=m) for m in LOOPS if current.lower() in m]

    @music.command(name="shuffle", description="Shuffle the queue")
    async def m_shuffle(self, ctx: commands.Context) -> None:
        await self.do_shuffle(ctx)

    @music.command(name="remove", description="Remove a song from the queue")
    @app_commands.describe(position="Its number in the queue")
    async def m_remove(self, ctx: commands.Context, position: int) -> None:
        await self.do_remove(ctx, position)

    @music.command(name="move", description="Move a song in the queue")
    @app_commands.describe(start="Its number now", to="Its new number")
    async def m_move(self, ctx: commands.Context, start: int, to: int) -> None:
        await self.do_move(ctx, start, to)

    @music.command(name="seek", description="Jump to a time in the song")
    @app_commands.describe(when="e.g. 1:30, 90, +15 or -10")
    async def m_seek(self, ctx: commands.Context, when: str) -> None:
        await self.do_seek(ctx, when)

    @music.command(name="clear", description="Clear the queue (keeps the current song)")
    async def m_clear(self, ctx: commands.Context) -> None:
        await self.do_clear(ctx)

    @music.command(name="autoplay", description="Keep playing similar songs when the queue ends")
    @app_commands.describe(enabled="On or off (default: switch)")
    async def m_autoplay(self, ctx: commands.Context, enabled: Optional[bool] = None) -> None:
        await self.do_autoplay(ctx, enabled)

    @music.command(name="247", description="Stay in voice even when nothing's playing")
    @app_commands.describe(enabled="On or off (default: switch)")
    async def m_stay(self, ctx: commands.Context, enabled: Optional[bool] = None) -> None:
        await self.do_stay(ctx, enabled)

    @music.command(name="effect", description="Sound effects: bass boost, nightcore, 8D…")
    @app_commands.describe(name="The effect")
    @app_commands.choices(name=[app_commands.Choice(name=f, value=f) for f in FILTERS])
    async def m_filter(self, ctx: commands.Context, name: Optional[str] = None) -> None:
        await self.do_filter(ctx, name)

    @music.command(name="search", description="Search YouTube and pick a result")
    @app_commands.describe(query="What to search for")
    async def m_search(self, ctx: commands.Context, *, query: str) -> None:
        await self.do_search(ctx, query)

    @commands.command(name="musicdebug", hidden=True)
    @commands.guild_only()
    async def musicdebug_cmd(self, ctx: commands.Context) -> None:
        player = self.players.get(ctx.guild.id)
        report = player.voice_report() if player else "no player"
        missing = missing_parts()
        await self.say(ctx, f"```\n{report}\nmissing: {missing or 'nothing'}\nffmpeg: {shutil.which('ffmpeg')}\n```")

    @music.command(name="radio", description="Endless music by genre (or 'off')")
    @app_commands.describe(genre="drill, deutschrap, rap, pop, rnb, throwback, rock, afro, edm, lofi, phonk, chill, off")
    async def m_radio(self, ctx: commands.Context, genre: str) -> None:
        await self.do_radio(ctx, genre)

    @m_radio.autocomplete("genre")
    async def _radio_genres(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=g, value=g) for g in [*lists.all_genres(), "off"]
                if current.lower() in g][:25]

    @music.command(name="quiz", description="Guess the song from 15 seconds of it, for Strip cash")
    @app_commands.describe(genre="A genre, or leave empty for a mix", rounds="How many songs (1-15)")
    @app_commands.choices(genre=[app_commands.Choice(name=g, value=g) for g in lists.GENRES])
    async def m_quiz(self, ctx: commands.Context, genre: str | None = None,
                     rounds: app_commands.Range[int, 1, 15] = QUIZ_ROUNDS) -> None:
        await self.do_quiz(ctx, genre, rounds)

    @music.command(name="top", description="Most played songs or biggest DJs")
    @app_commands.choices(what=[app_commands.Choice(name="songs", value="songs"),
                                app_commands.Choice(name="djs", value="djs")],
                          period=[app_commands.Choice(name=p, value=p) for p in ("day", "week", "month", "all")])
    async def m_top(self, ctx: commands.Context, what: str = "songs", period: str = "week") -> None:
        await self.do_top(ctx, what, period)

    @music.command(name="dj", description="Only this role (and Manage Channels) controls the music. Empty: everyone")
    @app_commands.describe(role="The DJ role, or leave empty to let everyone in voice control it")
    @moderator("manage_guild")
    async def m_dj(self, ctx: commands.Context, role: Optional[discord.Role] = None) -> None:
        self.bot.storage.update_config(ctx.guild.id, CONFIG, dj=role.id if role else None)
        text = (f"Only {role.mention}, people with Manage Channels, and whoever requested the current song can "
                f"control the music." if role else "Everyone in the voice channel can control the music.")
        await self.say(ctx, embed=embed("DJ role saved 🎧", text, OK), ephemeral=True)
