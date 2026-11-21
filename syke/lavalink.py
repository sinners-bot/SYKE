"""A small Lavalink v4 client: SYKE asks a Lavalink server to play, Lavalink streams the audio into voice.

Lavalink (https://lavalink.dev) is the audio server most big music bots run. It finds the tracks
(YouTube through its youtube-source plugin, SoundCloud, Bandcamp, Twitch, Vimeo, direct links),
encodes them and sends the voice packets itself, including Discord's end-to-end voice encryption
(DAVE, Lavalink 4.2+). SYKE only joins the voice channel through the Discord gateway and hands the
voice details (token, endpoint, session and, for DAVE, the channel id) to Lavalink.

Pieces:
* `Node`: the websocket (events: track started/ended/failed, player position) and the REST calls
  (load tracks, update or destroy a player). Reconnects and resumes on its own.
* `LavalinkVoice`: a discord.py VoiceProtocol, so `channel.connect(cls=LavalinkVoice)` joins voice and
  `guild.voice_client` works as usual. It mirrors the bits of discord.VoiceClient that the music player
  uses (is_playing, pause, stop…) and plays Lavalink tracks.

Owner settings (environment variables):
  SYKE_LAVALINK_URL       e.g. http://lavalink.railway.internal:2333
  SYKE_LAVALINK_PASSWORD  the server's password
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, Callable

import discord

log = logging.getLogger("syke.lavalink")

RESUME_SECONDS = 60
CONNECT_TIMEOUT = 20


def settings() -> tuple[str, str] | None:
    url = os.getenv("SYKE_LAVALINK_URL", "").strip().rstrip("/")
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    return url, os.getenv("SYKE_LAVALINK_PASSWORD", "youshallnotpass")


class LavalinkError(Exception):
    pass


class Node:
    def __init__(self, bot, url: str, password: str) -> None:
        self.bot = bot
        self.url = url
        self.password = password
        self.session_id: str | None = None
        self.ready = asyncio.Event()
        self.voices: dict[int, "LavalinkVoice"] = {}
        self._http = None
        self._task: asyncio.Task | None = None
        self._closed = False
        self.stats: dict = {}

    @property
    def available(self) -> bool:
        return self.ready.is_set() and self.session_id is not None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
        if self._http is not None:
            await self._http.close()

    async def http(self):
        import aiohttp
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                                               headers={"Authorization": self.password})
        return self._http

    # Websocket

    async def _run(self) -> None:
        import aiohttp
        await self.bot.wait_until_ready()
        delay = 2
        while not self._closed:
            headers = {"Authorization": self.password, "User-Id": str(self.bot.user.id), "Client-Name": "SYKE/1.0"}
            if self.session_id:
                headers["Session-Id"] = self.session_id
            ws_url = self.url.replace("http://", "ws://").replace("https://", "wss://") + "/v4/websocket"
            try:
                session = await self.http()
                async with session.ws_connect(ws_url, headers=headers, heartbeat=30, timeout=15) as ws:
                    delay = 2
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle(msg.json())
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                return
            except Exception as error:
                log.warning("Lavalink connection failed (%s), retrying in %ss", error, delay)
            self.ready.clear()
            if self._closed:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _handle(self, data: dict) -> None:
        op = data.get("op")
        if op == "ready":
            resumed = data.get("resumed")
            self.session_id = data.get("sessionId")
            self.ready.set()
            log.info("Lavalink ready at %s (session %s%s)", self.url, self.session_id, ", resumed" if resumed else "")
            with contextlib.suppress(Exception):
                await self.rest("PATCH", f"/v4/sessions/{self.session_id}",
                                {"resuming": True, "timeout": RESUME_SECONDS})
            if not resumed:  # players from before are gone on the server: re-send voice details
                for voice in list(self.voices.values()):
                    await voice._send_voice()
        elif op == "stats":
            self.stats = data
        elif op == "playerUpdate":
            voice = self.voices.get(int(data.get("guildId", 0)))
            if voice is not None:
                voice._player_update(data.get("state") or {})
        elif op == "event":
            voice = self.voices.get(int(data.get("guildId", 0)))
            if voice is not None:
                voice._event(data)

    # REST

    async def rest(self, method: str, path: str, body: Any = None, params: dict | None = None) -> Any:
        session = await self.http()
        async with session.request(method, self.url + path, json=body, params=params) as resp:
            if resp.status == 204:
                return None
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise LavalinkError(f"{resp.status}: {(data or {}).get('message') or data}")
            return data

    async def load(self, identifier: str) -> dict:
        return await self.rest("GET", "/v4/loadtracks", params={"identifier": identifier}) or {}

    async def update(self, guild_id: int, body: dict, no_replace: bool = False) -> dict:
        if not self.available:
            raise LavalinkError("Lavalink isn't connected.")
        return await self.rest("PATCH", f"/v4/sessions/{self.session_id}/players/{guild_id}", body,
                               params={"noReplace": "true" if no_replace else "false"})

    async def destroy(self, guild_id: int) -> None:
        if self.available:
            with contextlib.suppress(Exception):
                await self.rest("DELETE", f"/v4/sessions/{self.session_id}/players/{guild_id}")


def find_node(client) -> Node | None:
    for cog in getattr(client, "cogs", {}).values():
        node = getattr(cog, "node", None)
        if isinstance(node, Node):
            return node
    return None


class LavalinkVoice(discord.VoiceProtocol):
    """Voice through Lavalink. Looks enough like discord.VoiceClient for the music player."""

    def __init__(self, client, channel) -> None:
        super().__init__(client, channel)
        self.guild = channel.guild
        self.node: Node | None = find_node(client)
        self._session_id: str | None = None
        self._token: str | None = None
        self._endpoint: str | None = None
        self._voice_ready = asyncio.Event()
        self._playing = False
        self._paused = False
        self._after: Callable[[Exception | None], None] | None = None
        self._track: str | None = None
        self.last_end_reason: str | None = None
        self.last_exception: str | None = None
        self.position_ms = 0
        self.latency_ms: float | None = None
        self.connected = False
        if self.node is not None:
            self.node.voices[self.guild.id] = self

    # discord.py voice protocol

    async def on_voice_state_update(self, data) -> None:
        channel_id = data.get("channel_id")
        if channel_id is None:
            self.connected = False
            await self._teardown()
            return
        self._session_id = data.get("session_id")
        channel = self.guild.get_channel(int(channel_id))
        if channel is not None:
            self.channel = channel
        await self._send_voice()

    async def on_voice_server_update(self, data) -> None:
        self._token = data.get("token")
        self._endpoint = data.get("endpoint")
        await self._send_voice()

    async def _send_voice(self) -> None:
        if not (self._session_id and self._token and self._endpoint and self.node is not None):
            return
        try:
            await self.node.ready.wait()
            await self.node.update(self.guild.id, {"voice": {
                "token": self._token, "endpoint": self._endpoint, "sessionId": self._session_id,
                "channelId": str(self.channel.id)}})
            self.connected = True
            self._voice_ready.set()
        except Exception as error:
            log.warning("Couldn't hand voice to Lavalink in %s: %s", self.guild.id, error)

    async def connect(self, *, timeout: float, reconnect: bool, self_deaf: bool = False,
                      self_mute: bool = False) -> None:
        if self.node is None:
            raise RuntimeError("Lavalink isn't set up.")
        await asyncio.wait_for(self.node.ready.wait(), timeout)
        await self.guild.change_voice_state(channel=self.channel, self_deaf=self_deaf, self_mute=self_mute)
        await asyncio.wait_for(self._voice_ready.wait(), timeout)

    async def move_to(self, channel) -> None:
        self._voice_ready.clear()
        await self.guild.change_voice_state(channel=channel, self_deaf=True)
        self.channel = channel
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._voice_ready.wait(), CONNECT_TIMEOUT)

    async def disconnect(self, *, force: bool = False) -> None:
        with contextlib.suppress(Exception):
            await self.guild.change_voice_state(channel=None)
        await self._teardown()

    async def _teardown(self) -> None:
        self._playing = self._paused = False
        if self.node is not None:
            self.node.voices.pop(self.guild.id, None)
            await self.node.destroy(self.guild.id)
        self.cleanup()

    # What the music player uses

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self._playing and not self._paused

    def is_paused(self) -> bool:
        return self._playing and self._paused

    @property
    def latency(self) -> float:
        return (self.latency_ms or 0) / 1000 if self.latency_ms is not None else float("inf")

    async def play_track(self, encoded: str, *, position_ms: int = 0, volume: int = 100, filters: dict | None = None,
                         after: Callable[[Exception | None], None] | None = None) -> None:
        if self.node is None:
            raise LavalinkError("Lavalink isn't set up.")
        self._after = None  # the track this replaces ends with "replaced": don't advance the queue for it
        body = {"track": {"encoded": encoded}, "position": int(position_ms), "volume": int(volume),
                "paused": False, "filters": filters or {}}
        await self.node.update(self.guild.id, body)
        self._track, self._playing, self._paused = encoded, True, False
        self.last_end_reason = self.last_exception = None
        self._after = after

    def stop(self) -> None:
        if self._playing and self.node is not None:
            asyncio.ensure_future(self._safe_update({"track": {"encoded": None}}))

    def pause(self) -> None:
        self._paused = True
        asyncio.ensure_future(self._safe_update({"paused": True}))

    def resume(self) -> None:
        self._paused = False
        asyncio.ensure_future(self._safe_update({"paused": False}))

    async def set(self, **body) -> None:
        """Change volume, filters or position on the fly, without restarting the song."""
        await self._safe_update(body)

    async def _safe_update(self, body: dict) -> None:
        try:
            await self.node.update(self.guild.id, body)
        except Exception as error:
            log.warning("Lavalink update failed in %s: %s", self.guild.id, error)

    def _player_update(self, state: dict) -> None:
        self.position_ms = int(state.get("position") or 0)
        ping = state.get("ping")
        self.latency_ms = ping if isinstance(ping, (int, float)) and ping >= 0 else self.latency_ms
        self.connected = bool(state.get("connected", self.connected))

    def _event(self, data: dict) -> None:
        kind = data.get("type")
        if kind == "TrackExceptionEvent":
            self.last_exception = ((data.get("exception") or {}).get("message") or "")[:300]
            log.info("Lavalink track error in %s: %s", self.guild.id, self.last_exception)
        elif kind == "TrackStuckEvent":
            log.info("Lavalink track stuck in %s", self.guild.id)
        elif kind == "TrackEndEvent":
            reason = data.get("reason")
            if reason == "replaced":
                return
            self.last_end_reason = reason
            self._playing = self._paused = False
            after, self._after = self._after, None
            if after is not None:
                after(None)
        elif kind == "WebSocketClosedEvent":
            code = data.get("code")
            log.info("Discord voice closed for %s (code %s, %s)", self.guild.id, code, data.get("reason"))
            if code in (4014, 4022):  # disconnected by someone / call terminated
                self.connected = False
