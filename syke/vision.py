"""What SYKE can see: the pictures that matter when he answers someone.

Images, stickers and GIF stills from the message he's answering, the message it replies to, and a picture
the same person posted just before ("rate my fit" right after the photo). Sent to the AI at low detail,
at most a few per reply. When SYKE jumps in on his own he doesn't look: he only repeats what people said.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass

import discord

from .yap import gif_name

MAX_PICTURES = 3
RECENT_PICTURE_SECONDS = 120          # "rate this" a little after posting the picture
EMBED_WAIT = 1.5                       # Discord adds GIF previews to a message a moment after it's sent
MAX_BYTES = 20 * 1024 * 1024
_IMAGE_EXT = re.compile(r"\.(?:png|jpe?g|webp|gif)(?:\?|$)", re.IGNORECASE)
_GIF_LINK = re.compile(r"https?://(?:www\.)?(?:tenor\.com|giphy\.com|media\.tenor\.com|c\.tenor\.com|"
                       r"media\d?\.giphy\.com|i\.giphy\.com|klipy\.com)/\S+", re.IGNORECASE)
_GIPHY_ID = re.compile(r"giphy\.com/(?:gifs|embed|media)/(?:[\w-]*-)?([A-Za-z0-9]{8,})", re.IGNORECASE)


@dataclass
class Picture:
    url: str
    label: str        # "Mo's image", "Zara's gif (crying cat)"


def _who(bot, author) -> str:
    return bot.turn_name(author) if bot is not None else getattr(author, "display_name", "someone")


def _attachment_pictures(message, who: str) -> list[Picture]:
    out = []
    for a in getattr(message, "attachments", None) or []:
        kind = (getattr(a, "content_type", "") or "")
        name = getattr(a, "filename", "") or ""
        if not (kind.startswith("image/") or _IMAGE_EXT.search(name)):
            continue
        if (getattr(a, "size", 0) or 0) > MAX_BYTES:
            continue
        url = getattr(a, "proxy_url", None) or getattr(a, "url", None)
        if url:
            out.append(Picture(url, f"{who}'s {'gif' if kind == 'image/gif' else 'image'}"))
    return out


def _sticker_pictures(message, who: str) -> list[Picture]:
    out = []
    for st in getattr(message, "stickers", None) or []:
        fmt = getattr(getattr(st, "format", None), "name", "")
        if fmt == "lottie":
            continue   # an animation file, not a picture
        url = getattr(st, "url", None)
        if url:
            out.append(Picture(str(url), f"{who}'s sticker ({getattr(st, 'name', '')})"))
    return out


def _embed_pictures(message, who: str) -> list[Picture]:
    """GIF and image link previews Discord attached to the message."""
    out = []
    links = _GIF_LINK.findall(getattr(message, "content", "") or "")
    for e in getattr(message, "embeds", None) or []:
        thumb = getattr(getattr(e, "thumbnail", None), "url", None)
        image = getattr(getattr(e, "image", None), "url", None)
        url = thumb or image
        if not url:
            continue
        source = getattr(e, "url", None) or (links[0] if links else "")
        name = gif_name(source or url)
        kind = "gif" if (getattr(e, "type", "") in ("gifv",) or _GIF_LINK.search(source or "")) else "image"
        out.append(Picture(url, f"{who}'s {kind}" + (f" ({name})" if name else "")))
    if not out:   # no preview yet: Giphy links have a still frame we can point at directly
        for link in links:
            m = _GIPHY_ID.search(link)
            if m:
                name = gif_name(link)
                out.append(Picture(f"https://media.giphy.com/media/{m.group(1)}/giphy_s.gif",
                                   f"{who}'s gif" + (f" ({name})" if name else "")))
    return out


def pictures_in(message, bot=None) -> list[Picture]:
    """Every picture one message carries."""
    who = _who(bot, message.author)
    return [*_attachment_pictures(message, who), *_sticker_pictures(message, who), *_embed_pictures(message, who)]


def waiting_for_preview(message) -> bool:
    """A GIF link whose preview Discord hasn't added yet."""
    return bool(_GIF_LINK.search(getattr(message, "content", "") or "")) and not (getattr(message, "embeds", None))


async def gather(bot, message) -> list[Picture]:
    """The pictures SYKE should look at when answering `message` (most relevant first)."""
    found: list[Picture] = []
    if waiting_for_preview(message):
        await asyncio.sleep(EMBED_WAIT)
        with contextlib.suppress(discord.HTTPException, AttributeError, TypeError):
            message = await message.channel.fetch_message(message.id)
    found += pictures_in(message, bot)
    ref = getattr(message, "reference", None)
    replied = getattr(ref, "resolved", None) if ref else None
    if isinstance(replied, discord.Message) or (replied is not None and hasattr(replied, "attachments")):
        found += [Picture(p.url, p.label + " (the message they're replying to)") for p in pictures_in(replied, bot)]
    if not found:
        history = getattr(message.channel, "history", None)
        if history is not None:
            with contextlib.suppress(discord.HTTPException, AttributeError, TypeError):
                async for m in history(limit=4, before=message):
                    created = getattr(m, "created_at", None)
                    fresh = created is None or time.time() - created.timestamp() <= RECENT_PICTURE_SECONDS
                    if getattr(m.author, "id", None) == message.author.id and fresh:
                        found += pictures_in(m, bot)
                        if found:
                            break
    seen, out = set(), []
    for p in found:
        if p.url not in seen:
            seen.add(p.url)
            out.append(p)
    return out[:MAX_PICTURES]
