"""Add, steal, list, rename and remove server emojis and stickers.

Steal reads custom emojis and stickers off a message someone replied to (or a message link).
Files and direct PNG, JPG and GIF links become new emojis or stickers. JPG stickers are
converted, and sticker images are fitted onto a 320×320 canvas, which is what Discord accepts.
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import re
import socket
import threading
import urllib.parse
from dataclasses import dataclass, field

import discord
import httpx
from PIL import Image, ImageSequence, UnidentifiedImageError
from PIL.Image import DecompressionBombError

log = logging.getLogger("syke.expressions")

BRAND = discord.Colour.from_str("#8B5CF6")
WARN = discord.Colour.from_str("#F59E0B")

EMOJI_MAX_BYTES = 256 * 1024
STICKER_MAX_BYTES = 512 * 1024
STICKER_PX = 320
MAX_DOWNLOAD = 8 * 1024 * 1024
MAX_ITEMS = 10
MAX_PIXELS = 20_000_000
STICKER_EMOJI = "⭐"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

CUSTOM_EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d{15,22})>")
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
MESSAGE_LINK_RE = re.compile(
    r"https?://(?:(?:ptb|canary|www)\.)?discord(?:app)?\.com/channels/(@me|\d+)/(\d+)/(\d+)/?",
    re.IGNORECASE,
)

_locks: dict[int, asyncio.Lock] = {}
_image_lock = threading.Lock()


class ImageProblem(Exception):
    """A file or link SYKE can explain to the person who sent it."""


def notice(title: str, body: str, colour: discord.Colour = WARN) -> discord.Embed:
    return discord.Embed(title=title, description=body[:4096], colour=colour)


def guild_lock(guild_id: int) -> asyncio.Lock:
    lock = _locks.get(guild_id)
    if lock is None:
        lock = _locks[guild_id] = asyncio.Lock()
    return lock


@dataclass(frozen=True)
class FoundEmoji:
    name: str
    emoji_id: int
    animated: bool

    @property
    def url(self) -> str:
        ext = "gif" if self.animated else "png"
        return f"https://cdn.discordapp.com/emojis/{self.emoji_id}.{ext}?quality=lossless"


@dataclass(frozen=True)
class FoundSticker:
    name: str
    sticker_id: int
    format: str
    url: str


@dataclass(frozen=True)
class FileSource:
    filename: str
    url: str | None = None
    attachment: object | None = None


@dataclass
class Harvest:
    emojis: list[FoundEmoji] = field(default_factory=list)
    stickers: list[FoundSticker] = field(default_factory=list)
    images: list[FileSource] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.emojis or self.stickers or self.images)


@dataclass
class ParsedText:
    name: str | None
    message_link: str | None
    harvest: Harvest


@dataclass
class Choice:
    emojis: list[FoundEmoji]
    stickers: list[FoundSticker]
    images: list[FileSource]
    note: str | None = None
    hint: str | None = None


@dataclass
class Item:
    kind: str
    suggested: str | None
    filename: str
    explicit: bool = False
    url: str | None = None
    attachment: object | None = None
    trusted: bool = False
    sticker_format: str | None = None
    source_id: int | None = None
    origin: str | None = None
    animated: bool | None = None


def clean_name(raw: str, *, limit: int) -> str | None:
    """Discord emoji names: 2-32 of letters, numbers and underscores. Stickers use the same, max 30."""
    text = raw.strip()
    text = re.sub(r"\.(?:png|jpe?g|gif|webp)$", "", text, flags=re.IGNORECASE)
    text = text.replace("-", "_").replace(" ", "_")
    text = re.sub(r"[^A-Za-z0-9_]", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if sum(ch.isalnum() for ch in text) < 2:
        return None
    return text[:limit]


def sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "webp"
    stripped = data.lstrip()
    if stripped.startswith((b"{", b"[")):
        return "json"
    return None


def _clean_url(url: str) -> str:
    return url.rstrip(").,>]\"'")


def looks_like_image_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    ext = path[path.rfind("."):] if "." in path.rsplit("/", 1)[-1] else ""
    return ext in IMAGE_EXTENSIONS or ext == ""


def filename_from_url(url: str) -> str:
    name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
    return name[:80] or "image"


def dedupe_emojis(emojis: list[FoundEmoji]) -> list[FoundEmoji]:
    seen: set[int] = set()
    out: list[FoundEmoji] = []
    for emoji in emojis:
        if emoji.emoji_id in seen:
            continue
        seen.add(emoji.emoji_id)
        out.append(emoji)
    return out


def dedupe_stickers(stickers: list[FoundSticker]) -> list[FoundSticker]:
    seen: set[int] = set()
    out: list[FoundSticker] = []
    for sticker in stickers:
        if sticker.sticker_id in seen:
            continue
        seen.add(sticker.sticker_id)
        out.append(sticker)
    return out


def _emoji_from_obj(emoji) -> FoundEmoji | None:
    if emoji is None or isinstance(emoji, str):
        return None
    emoji_id = getattr(emoji, "id", None)
    if not emoji_id:
        return None
    name = getattr(emoji, "name", None) or "emoji"
    return FoundEmoji(str(name), int(emoji_id), bool(getattr(emoji, "animated", False)))


def _sticker_format(sticker) -> str:
    fmt = getattr(sticker, "format", None)
    name = getattr(fmt, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(fmt, str):
        return fmt
    return "png"


def _sticker_url(sticker, fmt: str) -> str:
    url = getattr(sticker, "url", None)
    if isinstance(url, str) and url.startswith("http"):
        return url
    if fmt == "gif":
        return f"https://media.discordapp.net/stickers/{sticker.id}.gif"
    ext = "json" if fmt == "lottie" else "png"
    return f"https://cdn.discordapp.com/stickers/{sticker.id}.{ext}"


def stickers_of(message) -> list[FoundSticker]:
    found: list[FoundSticker] = []
    for sticker in getattr(message, "stickers", None) or ():
        sticker_id = getattr(sticker, "id", None)
        if not sticker_id:
            continue
        fmt = _sticker_format(sticker)
        found.append(FoundSticker(
            str(getattr(sticker, "name", None) or "sticker"),
            int(sticker_id),
            fmt,
            _sticker_url(sticker, fmt),
        ))
    return dedupe_stickers(found)


def emojis_in_text(text: str) -> list[FoundEmoji]:
    return [
        FoundEmoji(match.group(2), int(match.group(3)), match.group(1) == "a")
        for match in CUSTOM_EMOJI_RE.finditer(text or "")
    ]


def harvest_message(message) -> Harvest:
    """Custom emojis, stickers and image files/links on one Discord message."""
    if message is None:
        return Harvest()
    content = getattr(message, "content", None) or ""
    emojis = emojis_in_text(content)
    for reaction in getattr(message, "reactions", None) or ():
        found = _emoji_from_obj(getattr(reaction, "emoji", None))
        if found is not None:
            emojis.append(found)
    images: list[FileSource] = []
    for attachment in getattr(message, "attachments", None) or ():
        images.append(FileSource(
            filename=str(getattr(attachment, "filename", None) or "image"),
            attachment=attachment,
        ))
    for match in URL_RE.finditer(content):
        url = _clean_url(match.group(0))
        if MESSAGE_LINK_RE.search(url) or not looks_like_image_url(url):
            continue
        images.append(FileSource(filename=filename_from_url(url), url=url))
    return Harvest(dedupe_emojis(emojis), stickers_of(message), images)


def parse_command_text(text: str) -> ParsedText:
    """Pull a name, a message link, pasted custom emojis and image links out of command arguments."""
    emojis: list[FoundEmoji] = []
    images: list[FileSource] = []
    message_link: str | None = None

    def take_emoji(match: re.Match) -> str:
        emojis.append(FoundEmoji(match.group(2), int(match.group(3)), match.group(1) == "a"))
        return " "

    without = CUSTOM_EMOJI_RE.sub(take_emoji, text or "")

    def take_url(match: re.Match) -> str:
        nonlocal message_link
        url = _clean_url(match.group(0))
        if MESSAGE_LINK_RE.search(url):
            message_link = message_link or url
        elif looks_like_image_url(url):
            images.append(FileSource(filename=filename_from_url(url), url=url))
        return " "

    without = URL_RE.sub(take_url, without)
    parts = without.split()
    if parts and parts[0].lower() == "add" and len(parts) > 1:
        parts = parts[1:]
    name = " ".join(parts) or None
    return ParsedText(name, message_link, Harvest(dedupe_emojis(emojis), [], images))


def choose_sources(kind: str, command: Harvest, target: Harvest | None, prefix: str) -> Choice:
    """Decide what a command should upload.

    A file or link on the command itself is an add. Otherwise a reply (or message link) is
    searched: `steal` takes emojis and stickers, `emoji` prefers custom emojis then images,
    `sticker` prefers stickers then images.
    """
    target = target or Harvest()
    if kind in {"emoji", "sticker"} and command.images:
        return Choice([], [], command.images)

    if kind == "both":
        emojis = dedupe_emojis(command.emojis + target.emojis)
        stickers = dedupe_stickers(command.stickers + target.stickers)
        if emojis or stickers:
            return Choice(emojis, stickers, [])
        if command.images or target.images:
            return Choice([], [], [], hint="image")
        return Choice([], [], [], hint="usage")

    if kind == "emoji" and command.emojis:
        return Choice(dedupe_emojis(command.emojis), [], [])
    if kind == "emoji" and target.emojis:
        note = None
        if target.images:
            note = f"Copied the custom emoji. To add the picture instead, attach it or run `{prefix}emoji <name> <link>`."
        return Choice(dedupe_emojis(target.emojis), [], [], note=note)
    if kind == "sticker" and (command.stickers or target.stickers):
        note = None
        if target.images:
            note = f"Copied the sticker. To make one from the picture, attach it: `{prefix}sticker <name>`."
        return Choice([], dedupe_stickers(command.stickers + target.stickers), [], note=note)
    if target.images and kind in {"emoji", "sticker"}:
        return Choice([], [], target.images)
    if kind == "emoji" and (target.stickers or command.stickers):
        return Choice([], [], [], hint="stickers")
    if kind == "sticker" and (target.emojis or command.emojis):
        return Choice([], [], [], hint="emojis")
    return Choice([], [], [], hint="usage")


def usage_embed(prefix: str, kind: str) -> discord.Embed:
    if kind == "sticker":
        title, lead = "Send a sticker", (
            f"Reply to a sticker, or send a PNG, JPG or GIF with `{prefix}sticker <name>`."
        )
    elif kind == "emoji":
        title, lead = "Send an emoji", (
            f"Reply to a custom emoji, or send a PNG, JPG or GIF with `{prefix}emoji <name>`."
        )
    else:
        title, lead = "Nothing to grab", (
            f"Reply to a message with a custom emoji or sticker, then run `{prefix}steal`."
        )
    body = (
        f"{lead}\n\n"
        f"• `{prefix}steal` — custom emojis and stickers from the message you reply to\n"
        f"• `{prefix}emoji <name>` / `{prefix}sticker <name>` — a PNG, JPG or GIF file, link, or reply\n"
        f"• `{prefix}emoji list` · `{prefix}emoji remove <name>` · `{prefix}emoji rename <old> <new>`\n"
        f"• `{prefix}sticker list` · `{prefix}sticker remove <name>` · `{prefix}sticker rename <old> <new>`\n\n"
        "You can also right-click a message → **Apps** → **Steal emoji/sticker**."
    )
    return notice(title, body)


def hint_embed(hint: str, prefix: str) -> discord.Embed:
    if hint == "image":
        return notice(
            "That's a picture, not an emoji yet",
            f"Reply to it with `{prefix}emoji <name>` to add an emoji, or `{prefix}sticker <name>` to add a sticker. "
            "PNG, JPG and GIF files and direct links both work.",
        )
    if hint == "stickers":
        return notice("Those are stickers", f"Add them with `{prefix}sticker` or `{prefix}steal`.")
    if hint == "emojis":
        return notice("Those are emojis", f"Add them with `{prefix}emoji` or `{prefix}steal`.")
    return usage_embed(prefix, "both" if hint == "usage" else hint)


def host_is_public(hostname: str) -> bool:
    host = hostname.strip("[]").rstrip(".").lower()
    if not host or host in {"localhost", "metadata.google.internal"} or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return True


def _check_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ImageProblem("That link isn't a PNG, JPG or GIF I can download.")
    return parsed


def ensure_supported(data: bytes, content_type: str, *, allow_json: bool) -> None:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    kind = sniff(data)
    if ctype.startswith("text/"):
        raise ImageProblem("That link isn't a PNG, JPG or GIF file.")
    if kind == "json":
        if allow_json:
            return
        raise ImageProblem("Send a PNG, JPG or GIF.")
    if kind in {"png", "jpeg", "gif", "webp"}:
        return
    if ctype in {"image/png", "image/jpeg", "image/jpg", "image/pjpeg", "image/gif", "image/webp"}:
        return
    raise ImageProblem("That file isn't a PNG, JPG or GIF.")


async def _fetch(url: str, *, allow_json: bool = False) -> bytes:
    timeout = httpx.Timeout(15.0, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, max_redirects=3) as client:
            async with client.stream("GET", url, headers={"User-Agent": "SYKE"}) as response:
                final_host = response.url.host or ""
                if not await asyncio.to_thread(host_is_public, final_host):
                    raise ImageProblem("That link isn't a public image.")
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise ImageProblem(f"I couldn't download that file ({exc.response.status_code}).") from exc
                ctype = response.headers.get("content-type", "")
                buf = bytearray()
                async for chunk in response.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_DOWNLOAD:
                        raise ImageProblem(
                            "That file is over 8 MB. Emojis have to end up under 256 KB and stickers under 512 KB."
                        )
    except ImageProblem:
        raise
    except httpx.HTTPError as exc:
        raise ImageProblem("I couldn't download that file.") from exc
    data = bytes(buf)
    ensure_supported(data, ctype, allow_json=allow_json)
    return data


async def download_bytes(url: str, *, allow_json: bool = False) -> bytes:
    parsed = _check_url(url)
    if not await asyncio.to_thread(host_is_public, parsed.hostname):
        raise ImageProblem("That link isn't a public image.")
    return await _fetch(url, allow_json=allow_json)


def _open_image(data: bytes) -> Image.Image:
    with _image_lock:
        previous_limit = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        try:
            try:
                buf = io.BytesIO(data)
                image = Image.open(buf)
                image.load()
            except (UnidentifiedImageError, DecompressionBombError, OSError) as exc:
                raise ImageProblem("That file isn't a PNG, JPG or GIF I can read.") from exc
        finally:
            Image.MAX_IMAGE_PIXELS = previous_limit
    frames = max(1, int(getattr(image, "n_frames", 1) or 1))
    if image.width * image.height * min(frames, 48) > MAX_PIXELS:
        raise ImageProblem("That image is too large to process.")
    image._syke_buf = buf  # keep the buffer alive while frames are read
    return image


def _png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _jpeg_bytes(image: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _opaque(image: Image.Image) -> bool:
    if "A" not in image.getbands():
        return True
    low, high = image.getchannel("A").getextrema()
    return low == 255 and high == 255


def _png_under(image: Image.Image, limit: int, *, allow_jpeg: bool) -> bytes:
    data = _png_bytes(image)
    if len(data) <= limit:
        return data
    if allow_jpeg and _opaque(image):
        for quality in (85, 70, 55, 40):
            jpg = _jpeg_bytes(image, quality)
            if len(jpg) <= limit:
                return jpg
    try:
        reduced = image.convert("RGBA").quantize(colors=256, method=Image.Quantize.FASTOCTREE)
        data = _png_bytes(reduced.convert("RGBA"))
    except (ValueError, OSError):
        data = b""
    if data and len(data) <= limit:
        return data
    raise ImageProblem("That image is still too big after shrinking.")


def _on_canvas(image: Image.Image, size: int) -> Image.Image:
    """Fit `image` inside a square canvas, scaling up small pictures so they fill the sticker."""
    frame = image.convert("RGBA")
    if frame.width < 1 or frame.height < 1:
        raise ImageProblem("That image is empty.")
    scale = min(size / frame.width, size / frame.height)
    resized = frame.resize(
        (max(1, round(frame.width * scale)), max(1, round(frame.height * scale))),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(resized, ((size - resized.width) // 2, (size - resized.height) // 2), resized)
    return canvas


def _gif_bytes(frames: list[Image.Image], durations: list[int]) -> bytes:
    converted: list[Image.Image] = []
    for frame in frames:
        rgba = frame.convert("RGBA")
        palette = rgba.convert("P", palette=Image.Palette.ADAPTIVE, colors=255)
        mask = rgba.getchannel("A").point(lambda alpha: 255 if alpha < 128 else 0)
        palette.paste(255, mask)
        palette.info["transparency"] = 255
        converted.append(palette)
    buf = io.BytesIO()
    converted[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=converted[1:],
        duration=durations[:len(converted)] or 100,
        loop=0,
        disposal=2,
        optimize=True,
        transparency=255,
    )
    return buf.getvalue()


def _frame_list(image: Image.Image, *, step: int, canvas: int | None, cap: int | None) -> tuple[list[Image.Image], list[int]]:
    frames: list[Image.Image] = []
    durations: list[int] = []
    for index, frame in enumerate(ImageSequence.Iterator(image)):
        if step > 1 and index % step:
            continue
        rgba = frame.convert("RGBA")
        if canvas is not None:
            rgba = _on_canvas(rgba, canvas)
        elif cap is not None:
            rgba.thumbnail((cap, cap), Image.Resampling.LANCZOS)
        frames.append(rgba)
        durations.append(int(frame.info.get("duration") or 100) * max(step, 1))
        if len(frames) >= 40:
            break
    return frames, durations


def _shrink_static(image: Image.Image, limit: int, *, sticker: bool) -> bytes:
    if sticker:
        return _png_under(_on_canvas(image, STICKER_PX), limit, allow_jpeg=False)
    frame = image.convert("RGBA")
    longest = max(frame.size)
    caps = [longest] if longest <= 256 else []
    caps += [256, 192, 128, 96, 64, 48, 32]
    seen: set[int] = set()
    for cap in caps:
        if cap in seen:
            continue
        seen.add(cap)
        clone = frame.copy()
        clone.thumbnail((cap, cap), Image.Resampling.LANCZOS)
        try:
            return _png_under(clone, limit, allow_jpeg=True)
        except ImageProblem:
            continue
    raise ImageProblem("That image is still over 256 KB after shrinking.")


def _shrink_animation(image: Image.Image, limit: int, *, sticker: bool) -> tuple[bytes, str]:
    total = max(1, int(getattr(image, "n_frames", 1) or 1))
    steps = [1]
    if total > 40:
        steps.append(max(1, total // 40))
    steps.append(max(steps[-1], 2))
    if sticker:
        sizes: list[int | None] = [STICKER_PX]
    else:
        sizes = [128, 96, 64, 48]
    last_error = "That animation is still too big after shrinking."
    seen: set[tuple[int, int]] = set()
    for step in steps:
        for size in sizes:
            key = (step, size or 0)
            if key in seen:
                continue
            seen.add(key)
            frames, durations = _frame_list(
                image,
                step=step,
                canvas=size if sticker else None,
                cap=None if sticker else size,
            )
            if not frames:
                continue
            try:
                data = _gif_bytes(frames, durations)
            except (ValueError, OSError) as exc:
                last_error = "I couldn't convert that animation."
                log.debug("gif encode failed: %s", exc)
                continue
            if len(data) <= limit:
                return data, "gif"
    raise ImageProblem(last_error)


def _already_square(image: Image.Image, size: int) -> bool:
    if image.size != (size, size):
        return False
    for frame in ImageSequence.Iterator(image):
        if frame.size != (size, size):
            return False
    return True


def prepare_emoji_image(data: bytes) -> tuple[bytes, str]:
    """Return emoji file bytes and a filename. GIF stays animated; everything else can be PNG or JPG."""
    kind = sniff(data)
    if kind in {"png", "jpeg", "gif"} and len(data) <= EMOJI_MAX_BYTES:
        ext = {"png": "png", "jpeg": "jpg", "gif": "gif"}[kind]
        return data, f"emoji.{ext}"
    if kind == "json":
        raise ImageProblem("Send a PNG, JPG or GIF.")
    image = _open_image(data)
    animated = kind == "gif" or (getattr(image, "n_frames", 1) or 1) > 1 and kind != "jpeg"
    if animated and (getattr(image, "n_frames", 1) or 1) > 1:
        payload, _ext = _shrink_animation(image, EMOJI_MAX_BYTES, sticker=False)
        return payload, "emoji.gif"
    return _shrink_static(image, EMOJI_MAX_BYTES, sticker=False), "emoji.png"


def prepare_sticker_file(data: bytes, *, trusted: bool = False) -> tuple[bytes, str]:
    """Return sticker bytes and a filename. Trusted Discord stickers are reuploaded as-is when they fit."""
    kind = sniff(data)
    if kind == "json":
        if not trusted:
            raise ImageProblem("Send a PNG, JPG or GIF for a sticker.")
        if len(data) > STICKER_MAX_BYTES:
            raise ImageProblem("That Lottie sticker is over 512 KB.")
        return data, "sticker.json"
    if trusted and kind in {"png", "gif"} and len(data) <= STICKER_MAX_BYTES:
        return data, f"sticker.{kind}"
    image = _open_image(data)
    frames = getattr(image, "n_frames", 1) or 1
    if kind in {"png", "gif"} and frames > 1 and _already_square(image, STICKER_PX) and len(data) <= STICKER_MAX_BYTES:
        return data, f"sticker.{'gif' if kind == 'gif' else 'png'}"
    if frames > 1 and kind != "jpeg":
        payload, ext = _shrink_animation(image, STICKER_MAX_BYTES, sticker=True)
        return payload, f"sticker.{ext}"
    payload = _shrink_static(image, STICKER_MAX_BYTES, sticker=True)
    if sniff(payload) != "png":
        raise ImageProblem("That picture couldn't be turned into a PNG sticker.")
    return payload, "sticker.png"


def name_from_filename(filename: str, *, limit: int) -> str | None:
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return clean_name(stem, limit=limit)


def build_items(choice: Choice, requested: str | None, image_kind: str) -> tuple[list[Item], list[str]]:
    notes: list[str] = []
    if choice.note:
        notes.append(choice.note)
    items: list[Item] = []
    for emoji in choice.emojis:
        items.append(Item(
            "emoji", emoji.name, f"{emoji.name}.{'gif' if emoji.animated else 'png'}",
            url=emoji.url, trusted=True, source_id=emoji.emoji_id, origin=emoji.name,
            animated=emoji.animated,
        ))
    for sticker in choice.stickers:
        items.append(Item(
            "sticker", sticker.name, f"{sticker.name}.{sticker.format}",
            url=sticker.url, trusted=True, sticker_format=sticker.format,
            source_id=sticker.sticker_id, origin=sticker.name,
        ))
    limit = _limit_for(image_kind)
    for image in choice.images:
        items.append(Item(
            image_kind, name_from_filename(image.filename, limit=limit), image.filename,
            url=image.url, attachment=image.attachment,
        ))
    if requested and items:
        files_only = all(not item.trusted for item in items)
        if len(items) == 1 or files_only:
            items[0].suggested = requested
            items[0].explicit = True
            if len(items) > 1:
                notes.append("That name was used for the first file. The rest kept names from their filenames.")
        else:
            notes.append("A custom name only works for a single emoji or sticker, so the original names were kept.")
    return items, notes


def _limit_for(kind: str) -> int:
    return 32 if kind == "emoji" else 30


def _fallback(kind: str) -> str:
    return "emoji" if kind == "emoji" else "sticker"


def allocate_name(item: Item, taken: set[str], preexisting: set[str]) -> str | None:
    """Pick a free name. Stolen names that already exist on the server are not suffixed."""
    limit = _limit_for(item.kind)
    base = clean_name(item.suggested, limit=limit) if item.suggested else None
    if base is None:
        if item.explicit:
            return None
        base = _fallback(item.kind)
    if base.lower() not in taken:
        taken.add(base.lower())
        return base
    if item.explicit or (item.trusted and base.lower() in preexisting):
        return None
    for number in range(2, 30):
        suffix = f"_{number}"
        candidate = base[:limit - len(suffix)] + suffix
        if candidate.lower() not in taken and sum(ch.isalnum() for ch in candidate) >= 2:
            taken.add(candidate.lower())
            return candidate
    return None


def explain_discord(exc: BaseException) -> str:
    if isinstance(exc, discord.RateLimited):
        return "Discord is rate limiting new emojis. Wait a few seconds and try the rest again."
    code = getattr(exc, "code", None)
    known = {
        30008: "This server is out of static emoji slots.",
        30018: "This server is out of animated emoji slots.",
        30039: "This server is out of sticker slots.",
        50013: "SYKE needs the **Manage Expressions** permission to do that.",
        50035: "Discord rejected that file or name.",
    }
    if code in known:
        detail = (getattr(exc, "text", "") or "").split("\n", 1)[0].strip()
        if code == 50035 and detail and detail.lower() not in known[code].lower():
            return f"{known[code]} {detail[:160]}"
        return known[code]
    text = (getattr(exc, "text", "") or str(exc)).split("\n", 1)[0].strip()
    return text[:200] or "Discord rejected that."


def bot_can(guild, *, manage: bool) -> bool | None:
    perms = getattr(getattr(guild, "me", None), "guild_permissions", None)
    if perms is None:
        return None
    if perms.administrator or perms.manage_emojis or perms.manage_expressions:
        return True
    if not manage and getattr(perms, "create_expressions", False):
        return True
    return False


def member_can_manage(bot, member) -> bool:
    if getattr(member, "id", None) in bot.settings.owner_ids:
        return True
    perms = getattr(member, "guild_permissions", None)
    if perms is None:
        return False
    return bool(perms.administrator or perms.manage_guild or perms.manage_emojis or perms.manage_expressions)


def permission_embed(prefix: str) -> discord.Embed:
    return notice(
        "I need Manage Expressions",
        "Give SYKE **Manage Expressions** (Manage Emojis and Stickers), then try again. "
        f"An admin can re-invite SYKE with `{prefix}invite` if that permission can't be toggled on the role.",
    )


def _markup(obj, name: str, animated: bool) -> str:
    text = str(obj)
    if text.startswith("<"):
        return text
    return f"<{'a' if animated else ''}:{name}:{getattr(obj, 'id', 0)}>"


def _unique_emojis(emojis: list) -> list:
    seen: set[int] = set()
    unique = []
    for emoji in emojis:
        emoji_id = getattr(emoji, "id", None)
        if isinstance(emoji_id, int):
            if emoji_id in seen:
                continue
            seen.add(emoji_id)
        unique.append(emoji)
    return unique


def _is_subscription_role(role) -> bool:
    tags = getattr(role, "tags", None)
    return getattr(tags, "subscription_listing_id", None) is not None


def _is_premium_emoji(guild, emoji) -> bool:
    """Server Subscription emojis have their own limit of 25 and do not use normal slots."""
    raw = getattr(emoji, "_roles", None)
    if not raw:
        return False
    get_role = getattr(guild, "get_role", None)
    if get_role is None:
        return False
    found = False
    for role_id in raw:
        try:
            role = get_role(int(role_id))
        except (TypeError, ValueError):
            return False
        if role is None or not _is_subscription_role(role):
            return False
        found = True
    return found


def _uses_emoji_slot(guild, emoji) -> bool:
    """Integration emotes and Server Subscription emojis are in the guild list but use other limits."""
    if getattr(emoji, "managed", False):
        return False
    return not _is_premium_emoji(guild, emoji)


def _slot_emojis(guild, emojis: list) -> list:
    return [emoji for emoji in _unique_emojis(emojis) if _uses_emoji_slot(guild, emoji)]


def _pool_counts(guild, emojis: list) -> tuple[int, int]:
    """Static and animated counts. Discord gives each pool its own `emoji_limit`."""
    static = animated = 0
    for emoji in _slot_emojis(guild, emojis):
        if getattr(emoji, "animated", False):
            animated += 1
        else:
            static += 1
    return static, animated


def _emoji_slot_text(static: int, animated: int, limit) -> str:
    if isinstance(limit, int) and limit > 0:
        return f"{static}/{limit} static · {animated}/{limit} animated"
    return f"{static} static · {animated} animated"


def _limit_reached(limit, count: int) -> bool:
    return isinstance(limit, int) and limit > 0 and count >= limit


def _mark_pool_full(counts: dict, added: dict, pool: str, limit) -> None:
    """Remember a pool Discord just rejected, without forgetting emojis this batch already added."""
    if isinstance(limit, int) and limit > 0:
        counts[pool] = max(counts[pool], limit - added[pool])


def _emoji_slot_block(limit, used: dict, animated: bool | None) -> tuple[str, bool] | None:
    """Refuse an emoji that cannot fit. Fatal only when the other pool is full too."""
    static_full = _limit_reached(limit, used["static"])
    animated_full = _limit_reached(limit, used["animated"])
    if animated is True and animated_full:
        return "this server is out of animated emoji slots", static_full
    if animated is False and static_full:
        return "this server is out of static emoji slots", animated_full
    if animated is None and static_full and animated_full:
        return "this server is out of emoji slots", True
    return None


def _slots_footer(
    guild, emojis: list, stickers: list, author_name: str,
    added_static: int, added_animated: int, added_sticker: int,
) -> str:
    parts = [f"Requested by {author_name}"]
    emoji_limit = getattr(guild, "emoji_limit", None)
    sticker_limit = getattr(guild, "sticker_limit", None)
    if isinstance(emoji_limit, int) and emoji_limit > 0:
        static, animated = _pool_counts(guild, emojis)
        parts.append("Emojis " + _emoji_slot_text(static + added_static, animated + added_animated, emoji_limit))
    if isinstance(sticker_limit, int) and sticker_limit > 0:
        parts.append(f"Stickers {len(stickers) + added_sticker}/{sticker_limit}")
    return " · ".join(parts)


async def _load_item(item: Item) -> bytes:
    if item.attachment is not None:
        size = getattr(item.attachment, "size", 0) or 0
        if size > MAX_DOWNLOAD:
            raise ImageProblem("That file is over 8 MB. Emojis have to end up under 256 KB and stickers under 512 KB.")
        data = await item.attachment.read()
        if len(data) > MAX_DOWNLOAD:
            raise ImageProblem("That file is over 8 MB. Emojis have to end up under 256 KB and stickers under 512 KB.")
        ensure_supported(data, str(getattr(item.attachment, "content_type", "") or ""), allow_json=False)
        return data
    if item.url:
        allow_json = item.trusted and item.sticker_format == "lottie"
        return await download_bytes(item.url, allow_json=allow_json)
    raise ImageProblem("Nothing to download.")


def _payload_is_animated(payload: bytes) -> bool:
    return payload.startswith((b"GIF87a", b"GIF89a"))


async def _create_item(
    guild, item: Item, name: str, data: bytes, author, *, emoji_image: bytes | None = None,
) -> tuple[object, bool]:
    reason = f"SYKE: added by {getattr(author, 'display_name', None) or getattr(author, 'name', 'someone')} ({author.id})"[:500]
    if item.kind == "emoji":
        payload = emoji_image
        if payload is None:
            payload, _filename = await asyncio.to_thread(prepare_emoji_image, data)
        created = await guild.create_custom_emoji(name=name, image=payload, reason=reason)
        return created, _payload_is_animated(payload)
    payload, filename = await asyncio.to_thread(prepare_sticker_file, data, trusted=item.trusted)
    created = await guild.create_sticker(
        name=name,
        description="Added with SYKE",
        emoji=STICKER_EMOJI,
        file=discord.File(io.BytesIO(payload), filename=filename),
        reason=reason,
    )
    return created, False


def _local_ids(existing: list) -> set[int]:
    return {int(getattr(obj, "id", 0)) for obj in existing if getattr(obj, "id", None)}


async def _add(bot, guild, author, *, kind: str, text: str, attachments, command_stickers, reply, prefix: str) -> discord.Embed:
    parsed = parse_command_text(text)
    images = list(parsed.harvest.images)
    for attachment in attachments or []:
        images.append(FileSource(filename=str(getattr(attachment, "filename", None) or "image"), attachment=attachment))
    command = Harvest(parsed.harvest.emojis, list(command_stickers or []), images)
    target_message = reply
    if parsed.message_link:
        try:
            target_message = await resolve_message_link(guild, parsed.message_link)
        except ImageProblem as exc:
            return notice("Can't open that message", str(exc))
    target = harvest_message(target_message)
    choice = choose_sources(kind, command, target, prefix)
    if choice.hint:
        return hint_embed(choice.hint, prefix)
    image_kind = "sticker" if kind == "sticker" else "emoji"
    items, notes = build_items(choice, parsed.name, image_kind)
    if not items:
        return usage_embed(prefix, kind)

    allowed = bot_can(guild, manage=False)
    if allowed is False:
        return permission_embed(prefix)

    if len(items) > MAX_ITEMS:
        notes.append(f"Stopped after {MAX_ITEMS} so Discord doesn't rate limit the rest. Run it again for the others.")
        items = items[:MAX_ITEMS]

    async with guild_lock(getattr(guild, "id", 0)):
        emojis, stickers = await current_expressions(guild)
        lines, added_static, added_animated, added_sticker, any_fail = await _upload_all(
            bot, guild, author, items, emojis, stickers,
        )
    title, colour = _result_title(
        lines, added_static + added_animated + added_sticker, any_fail,
        trusted=all(item.trusted for item in items),
    )
    description = "\n".join(lines + [""] + notes).strip()
    embed = notice(title, description, colour)
    embed.set_footer(text=_slots_footer(
        guild, emojis, stickers, _author_name(author), added_static, added_animated, added_sticker,
    )[:2048])
    return embed


def _result_title(lines: list[str], added: int, any_fail: bool, *, trusted: bool) -> tuple[str, discord.Colour]:
    skipped = any(line.startswith("⏭️") for line in lines)
    if added and not any_fail:
        return ("Yoinked ✅" if trusted else "Added ✅"), BRAND
    if added and any_fail:
        return "Added some", WARN
    if skipped and not any_fail:
        return "Already on this server", WARN
    return "Couldn't add those", WARN


async def _upload_all(bot, guild, author, items: list[Item], emojis: list, stickers: list):
    taken = {
        "emoji": {obj.name.lower() for obj in emojis if getattr(obj, "name", None)},
        "sticker": {obj.name.lower() for obj in stickers if getattr(obj, "name", None)},
    }
    preexisting = {kind: set(names) for kind, names in taken.items()}
    local = {"emoji": _local_ids(emojis), "sticker": _local_ids(stickers)}
    # emoji_limit is the size of the static pool and, separately, the animated pool.
    limits = {
        "emoji": getattr(guild, "emoji_limit", None),
        "sticker": getattr(guild, "sticker_limit", None),
    }
    static, animated = _pool_counts(guild, emojis)
    counts = {"static": static, "animated": animated, "sticker": len(stickers)}
    lines: list[str] = []
    added = {"static": 0, "animated": 0, "sticker": 0}
    any_fail = False
    stop = False
    for index, item in enumerate(items):
        if stop:
            break
        if index and sum(added.values()):
            await asyncio.sleep(0.8)
        line, ok, fatal, pool = await _upload_one(
            bot, guild, author, item, taken, preexisting, local, limits, counts, added,
        )
        lines.append(line)
        if ok and pool:
            added[pool] += 1
        elif not line.startswith("⏭️"):
            any_fail = True
        if fatal:
            stop = True
            any_fail = True
    return lines, added["static"], added["animated"], added["sticker"], any_fail


def _used(counts: dict, added: dict, pool: str) -> int:
    return counts[pool] + added[pool]


async def _upload_one(bot, guild, author, item: Item, taken, preexisting, local, limits, counts, added) -> tuple[str, bool, bool, str | None]:
    label = item.origin or item.suggested or _fallback(item.kind)
    if item.source_id is not None and item.source_id in local[item.kind]:
        return f"⏭️ `{item.origin or label}` is already on this server", False, False, None
    if item.kind == "sticker" and _limit_reached(limits.get("sticker"), _used(counts, added, "sticker")):
        return f"❌ `{label}` — this server is out of sticker slots", False, True, None
    if item.kind == "emoji":
        blocked = _emoji_slot_block(limits.get("emoji"), _usage(counts, added), item.animated)
        if blocked is not None:
            message, fatal = blocked
            return f"❌ `{label}` — {message}", False, fatal, None
    name = allocate_name(item, taken[item.kind], preexisting[item.kind])
    if name is None:
        if item.explicit:
            cleaned = clean_name(item.suggested or "", limit=_limit_for(item.kind))
            if cleaned is None:
                return (f"❌ `{label}` — names need at least 2 letters or numbers "
                        "(letters, numbers and underscores)"), False, False, None
            return f"❌ `{cleaned}` — that name is already taken", False, False, None
        if item.trusted:
            return f"⏭️ `{label}` is already on this server", False, False, None
        return f"❌ `{label}` — that name is already taken", False, False, None
    try:
        data = await _load_item(item)
        emoji_image = None
        if item.kind == "emoji":
            emoji_image, _filename = await asyncio.to_thread(prepare_emoji_image, data)
            blocked = _emoji_slot_block(
                limits.get("emoji"), _usage(counts, added), _payload_is_animated(emoji_image),
            )
            if blocked is not None:
                taken[item.kind].discard(name.lower())
                message, fatal = blocked
                return f"❌ `{name}` — {message}", False, fatal, None
        created, animated = await _create_item(guild, item, name, data, author, emoji_image=emoji_image)
    except ImageProblem as exc:
        taken[item.kind].discard(name.lower())
        return f"❌ `{name}` — {exc}", False, False, None
    except (discord.HTTPException, discord.RateLimited) as exc:
        taken[item.kind].discard(name.lower())
        code = getattr(exc, "code", None)
        emoji_limit = limits.get("emoji")
        if code == 30008:
            _mark_pool_full(counts, added, "static", emoji_limit)
            fatal = _limit_reached(emoji_limit, _used(counts, added, "animated"))
        elif code == 30018:
            _mark_pool_full(counts, added, "animated", emoji_limit)
            fatal = _limit_reached(emoji_limit, _used(counts, added, "static"))
        else:
            fatal = isinstance(exc, (discord.Forbidden, discord.RateLimited)) or code in {30039, 50013}
        return f"❌ `{name}` — {explain_discord(exc)}", False, fatal, None
    expression_id = getattr(created, "id", None)
    if expression_id:
        bot.storage.remember_expression(guild.id, item.kind, int(expression_id), name, author.id)
        local[item.kind].add(int(expression_id))
    if item.kind == "emoji":
        pool = "animated" if animated else "static"
        return f"✅ {_markup(created, name, animated)} `{name}`", True, False, pool
    return f"✅ sticker **{name}**", True, False, "sticker"


def _usage(counts: dict, added: dict) -> dict:
    return {
        "static": _used(counts, added, "static"),
        "animated": _used(counts, added, "animated"),
        "sticker": _used(counts, added, "sticker"),
    }


async def current_expressions(guild) -> tuple[list, list]:
    emojis = list(getattr(guild, "emojis", None) or [])
    stickers = list(getattr(guild, "stickers", None) or [])
    fetch_emojis = getattr(guild, "fetch_emojis", None)
    fetch_stickers = getattr(guild, "fetch_stickers", None)
    if fetch_emojis is not None:
        try:
            emojis = list(await fetch_emojis())
        except discord.HTTPException:
            log.debug("emoji refresh failed", exc_info=True)
    if fetch_stickers is not None:
        try:
            stickers = list(await fetch_stickers())
        except discord.HTTPException:
            log.debug("sticker refresh failed", exc_info=True)
    return emojis, stickers


async def resolve_message_link(guild, url: str):
    match = MESSAGE_LINK_RE.search(url)
    if match is None:
        raise ImageProblem("That doesn't look like a Discord message link.")
    if match.group(1) == "@me":
        raise ImageProblem("DM links don't work. Reply to a message in this server.")
    if int(match.group(1)) != int(guild.id):
        raise ImageProblem("That message is from a different server.")
    channel_id, message_id = int(match.group(2)), int(match.group(3))
    getter = getattr(guild, "get_channel_or_thread", None) or getattr(guild, "get_channel", None)
    channel = getter(channel_id) if getter else None
    if channel is None and hasattr(guild, "fetch_channel"):
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.HTTPException:
            channel = None
    if channel is None:
        raise ImageProblem("I can't see that channel.")
    try:
        return await channel.fetch_message(message_id)
    except discord.NotFound as exc:
        raise ImageProblem("That message is gone.") from exc
    except discord.Forbidden as exc:
        raise ImageProblem("I can't read that channel.") from exc
    except discord.HTTPException as exc:
        raise ImageProblem("I couldn't open that message.") from exc


def _author_name(author) -> str:
    return getattr(author, "display_name", None) or getattr(author, "name", None) or "someone"


def _match_items(items: list, text: str) -> list:
    tag = CUSTOM_EMOJI_RE.search(text or "")
    if tag is not None:
        emoji_id = int(tag.group(3))
        by_id = [item for item in items if int(getattr(item, "id", 0) or 0) == emoji_id]
        if by_id:
            return by_id
    cleaned = clean_name(text, limit=32)
    wanted = {text.strip().lower()}
    if cleaned:
        wanted.add(cleaned.lower())
    return [item for item in items if str(getattr(item, "name", "")).lower() in wanted]


def _owner_line(guild, user_id: int | None, author_id: int) -> str:
    if user_id is None:
        return ""
    if user_id == author_id:
        return " · you"
    member = guild.get_member(user_id) if hasattr(guild, "get_member") else None
    if member is not None:
        return f" · {getattr(member, 'display_name', None) or user_id}"
    return ""


async def _list(bot, guild, author, *, kind: str, prefix: str) -> discord.Embed:
    emojis, stickers = await current_expressions(guild)
    noun = "emoji" if kind == "emoji" else "sticker"
    limit = getattr(guild, "emoji_limit" if kind == "emoji" else "sticker_limit", None)
    skipped_emojis = 0
    if kind == "emoji":
        items = _slot_emojis(guild, emojis)
        skipped_emojis = len(_unique_emojis(emojis)) - len(items)
        static, animated = _pool_counts(guild, emojis)
        emoji_slots = _emoji_slot_text(static, animated, limit)
    else:
        items = stickers
        emoji_slots = ""
    if not items:
        if kind == "emoji" and isinstance(limit, int) and limit > 0:
            slot = f" ({emoji_slots})"
        else:
            slot = f" (0/{limit})" if isinstance(limit, int) and limit > 0 else ""
        embed = notice(
            f"No custom {noun}s{slot}",
            f"Reply to one with `{prefix}steal`, or add a PNG, JPG or GIF with `{prefix}{noun} <name>`.",
            BRAND,
        )
        if skipped_emojis:
            embed.set_footer(text=f"{skipped_emojis} integration or premium emoji{'s' if skipped_emojis != 1 else ''} not counted")
        return embed
    lines = []
    for item in sorted(items, key=lambda obj: str(getattr(obj, "name", "")).lower()):
        owner = bot.storage.expression_added_by(guild.id, kind, int(item.id))
        who = _owner_line(guild, owner, author.id)
        if kind == "emoji":
            shown = _markup(item, item.name, bool(getattr(item, "animated", False)))
        else:
            shown = f"**{item.name}**"
        lines.append(f"{shown} `{item.name}`{who}")
    body = ""
    hidden = 0
    for line in lines:
        if len(body) + len(line) + 1 > 3500:
            hidden = len(lines) - body.count("\n") - (0 if not body else 0)
            # recount properly below
            break
        body = f"{body}\n{line}" if body else line
    shown_count = body.count("\n") + (1 if body else 0)
    hidden = len(lines) - shown_count
    if hidden > 0:
        body += f"\n… and {hidden} more"
    if kind == "emoji":
        slot = emoji_slots
    else:
        slot = f"{len(items)}/{limit}" if isinstance(limit, int) and limit > 0 else str(len(items))
    title = f"Custom {noun}s · {slot}"
    embed = notice(title, body, BRAND)
    other = "sticker" if kind == "emoji" else "emoji"
    if kind == "emoji":
        other_count = len(stickers)
    else:
        other_count = sum(_pool_counts(guild, emojis))
    footer = f"{other_count} custom {other}{'s' if other_count != 1 else ''} · remove with {prefix}{noun} remove <name>"
    if skipped_emojis:
        footer += f" · {skipped_emojis} integration or premium emoji{'s' if skipped_emojis != 1 else ''} not counted"
    embed.set_footer(text=footer)
    return embed


def _can_edit(bot, guild, author, kind: str, item) -> str | None:
    """None when the author may change `item`, otherwise the error body."""
    owner = bot.storage.expression_added_by(guild.id, kind, int(item.id))
    if owner == author.id or member_can_manage(bot, author):
        return None
    noun = "emoji" if kind == "emoji" else "sticker"
    if owner is None:
        return f"Only someone with **Manage Expressions** or **Manage Server** can change {noun}s that weren't added through SYKE."
    return f"Only the person who added this, or someone with **Manage Expressions** or **Manage Server**, can change it."


async def _remove(bot, guild, author, *, kind: str, text: str, prefix: str) -> discord.Embed:
    if not text.strip():
        noun = "emoji" if kind == "emoji" else "sticker"
        return notice("Which one?", f"Usage: `{prefix}{noun} remove <name>`")
    emojis, stickers = await current_expressions(guild)
    items = emojis if kind == "emoji" else stickers
    found = _match_items(items, text)
    noun = "emoji" if kind == "emoji" else "sticker"
    if not found:
        other = stickers if kind == "emoji" else emojis
        other_kind = "sticker" if kind == "emoji" else "emoji"
        if _match_items(other, text):
            return notice("Wrong pile", f"That's a {other_kind}. Use `{prefix}{other_kind} remove`.")
        return notice("Can't find it", f"No {noun} called `{text.strip()[:80]}`.")
    if len(found) > 1:
        names = ", ".join(f"`{item.name}`" for item in found[:8])
        return notice("Which one?", f"More than one match: {names}")
    item = found[0]
    denied = _can_edit(bot, guild, author, kind, item)
    if denied:
        return notice("Not yours", denied)
    if bot_can(guild, manage=True) is False:
        return permission_embed(prefix)
    reason = f"SYKE: removed by {_author_name(author)} ({author.id})"[:500]
    try:
        await item.delete(reason=reason)
    except (discord.HTTPException, discord.RateLimited) as exc:
        return notice("Couldn't remove it", explain_discord(exc))
    bot.storage.forget_expression(guild.id, kind, int(item.id))
    return notice("Removed", f"Removed {noun} `{item.name}`.", BRAND)


async def _rename(bot, guild, author, *, kind: str, text: str, prefix: str) -> discord.Embed:
    parts = text.split()
    noun = "emoji" if kind == "emoji" else "sticker"
    if len(parts) != 2:
        return notice("Which names?", f"Usage: `{prefix}{noun} rename <old> <new>`")
    old, new_raw = parts
    new = clean_name(new_raw, limit=_limit_for(kind))
    if new is None:
        return notice(
            "Invalid name",
            f"`{new_raw[:40]}` needs at least 2 letters or numbers. Use letters, numbers and underscores.",
        )
    emojis, stickers = await current_expressions(guild)
    items = emojis if kind == "emoji" else stickers
    found = _match_items(items, old)
    if not found:
        return notice("Can't find it", f"No {noun} called `{old}`.")
    if len(found) > 1:
        return notice("Which one?", "Rename one at a time, using the exact name.")
    item = found[0]
    if item.name.lower() == new.lower():
        return notice("That's already its name", f"`{item.name}` doesn't need a rename.")
    if any(other.name.lower() == new.lower() and other.id != item.id for other in items):
        return notice("Name taken", f"There's already a {noun} called `{new}`.")
    denied = _can_edit(bot, guild, author, kind, item)
    if denied:
        return notice("Not yours", denied)
    if bot_can(guild, manage=True) is False:
        return permission_embed(prefix)
    reason = f"SYKE: renamed by {_author_name(author)} ({author.id})"[:500]
    previous = item.name
    try:
        await item.edit(name=new, reason=reason)
    except (discord.HTTPException, discord.RateLimited) as exc:
        return notice("Couldn't rename it", explain_discord(exc))
    bot.storage.rename_expression(guild.id, kind, int(item.id), new)
    shown = _markup(item, new, bool(getattr(item, "animated", False))) if kind == "emoji" else f"**{new}**"
    return notice("Renamed", f"`{previous}` is now {shown} `{new}`.", BRAND)


async def referenced_message(ctx):
    """The message this command replies to, including sticker-only messages with no text."""
    message = getattr(ctx, "message", None)
    reference = getattr(message, "reference", None) if message is not None else None
    if reference is None or getattr(reference, "message_id", None) is None:
        return None
    target = getattr(reference, "resolved", None) or getattr(reference, "cached_message", None)
    if target is not None:
        return target
    channel = getattr(ctx, "channel", None) or getattr(message, "channel", None)
    fetch = getattr(channel, "fetch_message", None)
    if fetch is None:
        return None
    try:
        return await fetch(reference.message_id)
    except discord.HTTPException:
        return None


async def handle(bot, guild, author, *, kind: str, action: str, text: str = "", attachments=None,
                 command_stickers=None, reply=None, prefix: str = "!") -> discord.Embed:
    if guild is None:
        return notice("Servers only", "Emojis and stickers live on a server, not in DMs.")
    try:
        if action == "list":
            return await _list(bot, guild, author, kind=kind, prefix=prefix)
        if action == "remove":
            return await _remove(bot, guild, author, kind=kind, text=text, prefix=prefix)
        if action == "rename":
            return await _rename(bot, guild, author, kind=kind, text=text, prefix=prefix)
        return await _add(
            bot, guild, author, kind=kind, text=text, attachments=attachments,
            command_stickers=command_stickers, reply=reply, prefix=prefix,
        )
    except Exception:
        log.exception("expression command failed")
        raise
