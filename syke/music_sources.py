"""Where songs come from: turning a link or a search into playable tracks.

* yt-dlp handles almost every site directly: YouTube and YouTube Music, SoundCloud, Bandcamp,
  Vimeo, Twitch, TikTok, Mixcloud, Audiomack, Dailymotion, X, Reddit, direct audio files and
  internet radio streams.
* Spotify, Apple Music and Deezer don't hand out audio, so their links are read for the song
  names (title and artist) and each song is found on YouTube when its turn comes.
* Anything else falls back to the page's title, searched on YouTube.

Playlists and albums come in "flat": names and links only, so a 200-song playlist queues in a
second. A track's actual stream address is looked up just before it plays (and the next one is
looked up while the current one plays), because those addresses expire after a few hours.

Owner settings (environment variables, set in Railway):
  SYKE_MUSIC_COOKIES   Netscape cookies.txt contents for YouTube, if YouTube asks the server to sign in
  SYKE_MUSIC_PROXY     a proxy for yt-dlp, e.g. http://user:pass@host:port
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("syke.music")

PLAYLIST_MAX = 200
STREAM_TTL = 3 * 3600  # re-resolve a stream address older than this
SEARCH_PREFIX = "ytsearch1:"
FALLBACK_PREFIX = "scsearch1:"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0 Safari/537.36")

SPOTIFY = re.compile(r"https?://open\.spotify\.com/(?:intl-\w+/)?(?:embed/)?(track|album|playlist|artist)/([A-Za-z0-9]+)")
APPLE = re.compile(r"https?://music\.apple\.com/")
DEEZER = re.compile(r"https?://(?:www\.)?deezer\.com/(?:\w{2}/)?(track|album|playlist)/(\d+)")
SHORT_LINKS = re.compile(r"https?://(?:spotify\.link|deezer\.page\.link|link\.deezer\.com)/")
URL = re.compile(r"^https?://\S+$")


@dataclass
class Track:
    title: str
    url: str = ""                 # page link (YouTube watch link, SoundCloud page, …)
    query: str = ""               # searched when there's no url yet (Spotify/Apple/Deezer songs)
    duration: float | None = None
    uploader: str = ""
    thumbnail: str = ""
    requester: int = 0
    source: str = ""
    stream: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    acodec: str = ""
    is_live: bool = False
    resolved_at: float = 0.0
    local: bool = False           # stream is a downloaded file on disk
    origin: str = ""              # the Spotify / Apple Music / Deezer link it came from

    @property
    def label(self) -> str:
        return self.title or self.query or self.url

    def fresh(self) -> bool:
        if self.local:
            return os.path.exists(self.stream)
        if is_youtube(self.url) and not self.is_live:
            return False  # YouTube songs are always downloaded first (YouTube refuses FFmpeg's direct requests)
        return bool(self.stream) and time.time() - self.resolved_at < STREAM_TTL

    def video_id(self) -> str | None:
        parsed = urlparse(self.url or "")
        host = parsed.netloc.lower()
        if host.endswith("youtu.be"):
            return parsed.path.strip("/") or None
        if "youtube.com" in host:
            return (parse_qs(parsed.query).get("v") or [None])[0]
        return None


def is_youtube(url: str) -> bool:
    host = urlparse(url or "").netloc.lower()
    return host.endswith("youtube.com") or host.endswith("youtu.be")


def classify(query: str) -> tuple[str, Any]:
    q = query.strip().strip("<>")
    if SHORT_LINKS.match(q):
        return "short", q
    m = SPOTIFY.match(q)
    if m:
        return "spotify", (m.group(1), m.group(2))
    if APPLE.match(q):
        return "apple", q
    m = DEEZER.match(q)
    if m:
        return "deezer", (m.group(1), m.group(2))
    if URL.match(q):
        return "url", q
    return "search", q


def duration_text(seconds: float | None) -> str:
    if seconds is None:
        return "LIVE"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def parse_time(text: str) -> int | None:
    """"1:30" → 90, "90" → 90, "1h2m3s" → 3723."""
    text = text.strip().lower()
    if re.fullmatch(r"\d+(:\d{1,2}){0,2}", text):
        total = 0
        for part in text.split(":"):
            total = total * 60 + int(part)
        return total
    m = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", text)
    if m and any(m.groups()):
        h, mi, s = (int(g or 0) for g in m.groups())
        return h * 3600 + mi * 60 + s
    return None


# Spotify / Apple / Deezer: song names

def _next_data(page: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', page, re.S)
    return json.loads(m.group(1)) if m else {}


def spotify_tracks(page: str, kind: str) -> tuple[str, list[Track]]:
    """Song names from an open.spotify.com/embed/<kind>/<id> page."""
    data = _next_data(page)
    entity = (((data.get("props") or {}).get("pageProps") or {}).get("state") or {}).get("data", {}).get("entity") or {}
    if not entity:
        return "", []
    name = entity.get("name") or entity.get("title") or ""
    tracks = []
    if kind == "track":
        artists = ", ".join(a.get("name", "") for a in entity.get("artists") or []) or entity.get("subtitle", "")
        tracks.append(_named(entity.get("name") or entity.get("title") or "", artists,
                             (entity.get("duration") or 0) / 1000 or None, "Spotify", _spotify_link(entity.get("uri"))))
    else:
        for item in entity.get("trackList") or []:
            tracks.append(_named(item.get("title", ""), item.get("subtitle", ""),
                                 (item.get("duration") or 0) / 1000 or None, "Spotify", _spotify_link(item.get("uri"))))
    image = ""
    for img in ((entity.get("visualIdentity") or {}).get("image") or entity.get("coverArt", {}).get("sources") or []):
        image = img.get("url", "")
        break
    for t in tracks:
        t.thumbnail = t.thumbnail or image
    return name, [t for t in tracks if t.title][:PLAYLIST_MAX]


def _spotify_link(uri: str | None) -> str:
    m = re.fullmatch(r"spotify:(track|album|playlist|episode):(\w+)", uri or "")
    return f"https://open.spotify.com/{m.group(1)}/{m.group(2)}" if m else ""


def _named(title: str, artist: str, duration: float | None, source: str, origin: str = "") -> Track:
    artist = artist.replace("\u00a0", " ").strip()
    query = f"{artist} - {title}" if artist else title
    return Track(title=f"{title} — {artist}" if artist else title, query=query, duration=duration, uploader=artist,
                 source=source, origin=origin or "")


def apple_tracks(page: str) -> tuple[str, list[Track]]:
    """Song names from an Apple Music page's schema.org data."""
    found: list[Track] = []
    name = ""
    for block in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.+?)</script>', page, re.S):
        try:
            data = json.loads(html.unescape(block))
        except ValueError:
            continue
        for node in data if isinstance(data, list) else [data]:
            kind = node.get("@type")
            if kind == "MusicRecording":
                found.append(_named(node.get("name", ""), _artist(node), _iso_seconds(node.get("duration")), "Apple Music",
                                    node.get("url", "")))
                name = name or node.get("name", "")
            elif kind in ("MusicAlbum", "MusicPlaylist"):
                name = node.get("name", "")
                for rec in node.get("tracks") or node.get("track") or []:
                    if isinstance(rec, dict):
                        found.append(_named(rec.get("name", ""), _artist(rec) or _artist(node),
                                            _iso_seconds(rec.get("duration")), "Apple Music", rec.get("url", "")))
    if not found:
        title = _og(page, "og:title")
        if title:
            found.append(Track(title=title, query=title.replace(" by ", " - "), source="Apple Music"))
            name = title
    return name, [t for t in found if t.title][:PLAYLIST_MAX]


def _artist(node: dict) -> str:
    by = node.get("byArtist") or node.get("author") or []
    if isinstance(by, dict):
        by = [by]
    return ", ".join(a.get("name", "") for a in by if isinstance(a, dict))


def _iso_seconds(value) -> float | None:
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value or "")
    if not m or not any(m.groups()):
        return None
    h, mi, s = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def _og(page: str, prop: str) -> str:
    m = re.search(rf'<meta[^>]+property="{re.escape(prop)}"[^>]+content="([^"]*)"', page)
    if not m:
        m = re.search(rf'<meta[^>]+content="([^"]*)"[^>]+property="{re.escape(prop)}"', page)
    return html.unescape(m.group(1)).strip() if m else ""


def deezer_tracks(data: dict, kind: str) -> tuple[str, list[Track]]:
    if kind == "track":
        return data.get("title", ""), [_named(data.get("title", ""), (data.get("artist") or {}).get("name", ""),
                                              data.get("duration"), "Deezer", data.get("link", ""))]
    items = (data.get("tracks") or {}).get("data") or []
    return data.get("title", ""), [_named(i.get("title", ""), (i.get("artist") or {}).get("name", ""),
                                          i.get("duration"), "Deezer", i.get("link", "")) for i in items][:PLAYLIST_MAX]


# yt-dlp

def _cookie_file() -> str | None:
    text = os.getenv("SYKE_MUSIC_COOKIES", "").strip()
    if not text:
        return None
    path = os.path.join(tempfile.gettempdir(), "syke-music-cookies.txt")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text.replace("\\n", "\n") + "\n")
    return path


def ydl_options(flat: bool = False) -> dict:
    options = {
        "format": "bestaudio[acodec=opus]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": not flat,
        "skip_download": True,
        "cachedir": os.path.join(tempfile.gettempdir(), "syke-ytdlp-cache"),  # YouTube's player code, solved once
        "socket_timeout": 15,
        "source_address": "0.0.0.0",
        "default_search": "ytsearch",
        "playlistend": PLAYLIST_MAX,
        "js_runtimes": {"deno": {}, "node": {}},
        "remote_components": ["ejs:github"],
    }
    if flat:
        options["extract_flat"] = "in_playlist"
    cookies = _cookie_file()
    if cookies:
        options["cookiefile"] = cookies
    proxy = os.getenv("SYKE_MUSIC_PROXY", "").strip()
    if proxy:
        options["proxy"] = proxy
    return options


def _extract(target: str, flat: bool) -> dict | None:
    import yt_dlp  # imported lazily: the bot runs without it, music just says it's unavailable

    with yt_dlp.YoutubeDL(ydl_options(flat)) as ydl:
        return ydl.extract_info(target, download=False)


async def extract(target: str, flat: bool = False) -> dict | None:
    return await asyncio.to_thread(_extract, target, flat)


def _thumb(info: dict) -> str:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = info.get("thumbnails") or []
    return thumbs[-1].get("url", "") if thumbs else ""


def track_from_info(info: dict, requester: int = 0) -> Track:
    url = info.get("webpage_url") or info.get("original_url") or info.get("url") or ""
    if info.get("ie_key") == "Youtube" or (info.get("_type") == "url" and info.get("id") and "youtube" in
                                           (info.get("ie_key") or "").lower()):
        url = url if url.startswith("http") else f"https://www.youtube.com/watch?v={info['id']}"
    return Track(title=info.get("title") or info.get("fulltitle") or url, url=url,
                 duration=info.get("duration"), uploader=info.get("uploader") or info.get("channel") or "",
                 thumbnail=_thumb(info), requester=requester,
                 source=info.get("extractor_key") or info.get("ie_key") or "", is_live=bool(info.get("is_live")))


def fill_stream(track: Track, info: dict) -> Track:
    """Copy the playable stream (and the details only a full lookup has) onto a track."""
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            raise LookupError("No results.")
        info = entries[0]
    stream = info.get("url")
    if not stream and info.get("requested_formats"):
        stream = info["requested_formats"][0].get("url")
    if not stream:
        raise LookupError("No playable audio.")
    track.stream = stream
    track.headers = {k: v for k, v in (info.get("http_headers") or {}).items() if k.lower() in ("user-agent", "referer", "cookie")}
    track.acodec = (info.get("acodec") or "").lower()
    track.is_live = bool(info.get("is_live"))
    track.duration = track.duration or info.get("duration")
    if not track.url or track.query:
        track.url = info.get("webpage_url") or track.url
    if info.get("title") and (not track.title or track.title == track.url):
        track.title = info["title"]
    track.uploader = track.uploader or info.get("uploader") or info.get("channel") or ""
    track.thumbnail = track.thumbnail or _thumb(info)
    track.source = track.source or info.get("extractor_key") or ""
    track.resolved_at = time.time()
    return track


CACHE_DIR = os.path.join(tempfile.gettempdir(), "syke-music")
CACHE_BYTES = 1_500_000_000
CACHE_SECONDS = 3 * 3600
DOWNLOAD_MAX_SECONDS = 2 * 3600


def _tidy_cache() -> None:
    try:
        files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)]
    except OSError:
        return
    files = sorted((f for f in files if os.path.isfile(f)), key=os.path.getmtime)
    total = sum(os.path.getsize(f) for f in files)
    now = time.time()
    for f in files:
        if now - os.path.getmtime(f) > CACHE_SECONDS or total > CACHE_BYTES:
            try:
                total -= os.path.getsize(f)
                os.remove(f)
            except OSError:
                pass


def _download(target: str) -> dict:
    """Let yt-dlp fetch the audio to disk (it makes the requests YouTube accepts), returning its info."""
    import yt_dlp

    os.makedirs(CACHE_DIR, exist_ok=True)
    _tidy_cache()
    options = ydl_options()
    options.update(skip_download=False, outtmpl=os.path.join(CACHE_DIR, "%(id)s.%(ext)s"), noprogress=True,
                   retries=5, fragment_retries=5, overwrites=False, continuedl=True,
                   match_filter=yt_dlp.utils.match_filter_func(f"!is_live & duration <=? {DOWNLOAD_MAX_SECONDS}"))
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(target, download=True)
        if info and info.get("_type") == "playlist":
            info = next((e for e in info.get("entries") or [] if e), None)
        if not info:
            raise LookupError("No results.")
        downloads = info.get("requested_downloads") or []
        path = downloads[0].get("filepath") if downloads else None
        path = path or ydl.prepare_filename(info)
    if not path or not os.path.exists(path):
        raise LookupError("too long or live")  # filtered out: stream it instead
    os.utime(path)
    info["_local_path"] = path
    return info


_resolving: dict[int, asyncio.Lock] = {}


async def resolve_stream(track: Track) -> Track:
    """Make a track playable (one lookup at a time per track, so a prefetch and a play never download twice)."""
    lock = _resolving.setdefault(id(track), asyncio.Lock())
    try:
        async with lock:
            return await _resolve(track)
    finally:
        if not lock.locked():
            _resolving.pop(id(track), None)


async def _resolve(track: Track) -> Track:
    """Make a track playable right before it plays (searching for it if it's just a name).

    YouTube songs are downloaded to a temporary file (a few MB, a second or two); anything else, and
    very long or live YouTube videos, stream directly. If YouTube refuses, the same song is tried on
    SoundCloud so the queue keeps going.
    """
    if track.fresh():
        return track
    last: Exception | None = None
    youtube_target = track.url if is_youtube(track.url) else (SEARCH_PREFIX + track.query if track.query else "")
    if youtube_target and not track.is_live:
        try:
            info = await asyncio.to_thread(_download, youtube_target)
            fill_stream(track, info)
            track.stream, track.headers, track.local = info["_local_path"], {}, True
            track.acodec = (info.get("acodec") or "").lower()
            return track
        except Exception as error:
            last = error
            log.info("YouTube download failed for %s: %s", youtube_target, error)
            if "too long or live" in str(error) and track.url:
                try:
                    return fill_stream(track, await extract(track.url))
                except Exception as stream_error:
                    last = stream_error
    targets = [] if is_youtube(track.url) or not track.url or track.query else [track.url]
    fallback = track.query or (track.title if is_youtube(track.url) else "")
    if fallback:
        targets.append(FALLBACK_PREFIX + fallback)
    for target in targets:
        try:
            info = await extract(target)
            if info:
                track.local = False
                if target.startswith(FALLBACK_PREFIX):  # it's SoundCloud's copy now: link and label that
                    entry = next((e for e in info.get("entries") or [] if e), info)
                    track.url = entry.get("webpage_url") or track.url
                    track.source = "SoundCloud"
                    track.resolved_at = 0
                return fill_stream(track, info)
        except Exception as error:  # yt-dlp raises many kinds; try the next source
            last = error
            log.info("Lookup failed for %s: %s", target, error)
    raise LookupError(clean_error(last) if last else "Nothing playable found.")


def clean_error(error: Exception | None) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(error or "")).replace("ERROR: ", "")
    if "Sign in to confirm" in text or "not a bot" in text:
        return "YouTube blocked the server. The bot owner needs to add YouTube cookies (SYKE_MUSIC_COOKIES)."
    if "Private video" in text or "private" in text.lower() and "video" in text.lower():
        return "That video is private."
    if "age" in text.lower() and "restrict" in text.lower():
        return "That video is age-restricted."
    if "Unsupported URL" in text:
        return "That link isn't a song or video SYKE can play."
    return (text.split(";")[0] or "Couldn't play that.")[:200]


def ffmpeg_args(track: Track, *, position: float = 0, volume: int = 100, filters: str = "") -> tuple[str, str, str | None]:
    """(before_options, options, codec) for discord.FFmpegOpusAudio.

    With no filter and full volume an Opus source is passed straight through (no re-encoding, the
    original quality, almost no CPU). Otherwise it's re-encoded with the effects applied.
    """
    before = ["-nostdin"]
    if (track.stream or "").startswith("http"):
        before += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
                   "-reconnect_delay_max", "5"]
    if track.headers:
        header_text = "".join(f"{k}: {v}\r\n" for k, v in track.headers.items())
        before += ["-headers", _quote(header_text)]
    if position > 0 and not track.is_live:
        before += ["-ss", f"{position:.2f}"]
    effects = [f for f in (filters,) if f]
    if volume != 100:
        effects.append(f"volume={volume / 100:.2f}")
    options = ["-vn"]
    if effects:
        options += ["-af", _quote(",".join(effects))]
    codec = "opus" if track.acodec == "opus" and not effects else None
    return " ".join(before), " ".join(options), codec


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


FILTERS = {
    "off": "",
    "bassboost": "bass=g=8:f=110:w=0.6",
    "nightcore": "asetrate=48000*1.25,aresample=48000,atempo=1.0",
    "vaporwave": "asetrate=48000*0.8,aresample=48000",
    "8d": "apulsator=hz=0.09",
    "speed": "atempo=1.25",
    "slowed": "atempo=0.85,asetrate=48000*0.95,aresample=48000",
    "karaoke": "pan=stereo|c0=c0-c1|c1=c1-c0",
    "treble": "treble=g=6",
    "loud": "loudnorm=I=-12:TP=-1:LRA=11",
}
SPEED = {"nightcore": 1.25, "vaporwave": 0.8, "speed": 1.25, "slowed": 0.85 * 0.95}
