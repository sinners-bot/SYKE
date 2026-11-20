"""Quote cards: turn any message into a black-and-white quote image with the member's avatar.

Reply to a message with `!quote` (or right-click it → Apps → "Make a quote") and SYKE posts a
1200×630 card: the avatar fades in from the left, the words sit on the right, signed with the
member's name and @username. Buttons under the card switch colour on and off, flip the layout,
or delete it.

`quote channel #quotes` makes SYKE also post every card it makes into that channel, with a link
back to the original message, so the server builds a wall of quotes.

Emojis are drawn as images (Twemoji for Unicode emoji, Discord's CDN for custom ones), fetched
once and cached.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
from collections import OrderedDict
from pathlib import Path

import discord
import emoji as emoji_lib
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .moderation import OK, WARN, embed, moderator, problem

log = logging.getLogger("syke.quotes")

CONFIG = "quotes"
FONTS = Path(__file__).parent / "assets" / "fonts"
W, H = 1200, 630
TEXT_LEFT, TEXT_RIGHT = 590, 1150
TEXT_TOP, TEXT_BOTTOM = 70, 470
MAX_SIZE, MIN_SIZE = 64, 26
MAX_CHARS = 600
EMOJI_CACHE = 256
VIEW_SECONDS = 600
TWEMOJI = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/{}.png"
CUSTOM_EMOJI = "https://cdn.discordapp.com/emojis/{}.png?size=96"
LINK = re.compile(r"https?://discord(?:app)?\.com/channels/(\d+|@me)/(\d+)/(\d+)")

_fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    if key not in _fonts:
        try:
            _fonts[key] = ImageFont.truetype(str(FONTS / name), size)
        except OSError:
            _fonts[key] = ImageFont.truetype(str(FONTS / "Poppins-Medium.ttf"), size)
    return _fonts[key]


# Text → tokens

_CUSTOM = re.compile(r"<(a?):(\w+):(\d+)>")
_USER = re.compile(r"<@!?(\d+)>")
_ROLE = re.compile(r"<@&(\d+)>")
_CHANNEL = re.compile(r"<#(\d+)>")
_STAMP = re.compile(r"<t:(\d+)(?::\w)?>")
_MARKDOWN = re.compile(r"(\*\*|__|~~|\|\||\*|`{1,3})")


def clean_text(message) -> str:
    """The message's words, with mentions turned into names and markdown stripped."""
    text = message.content or ""
    names = {u.id: "@" + (getattr(u, "display_name", None) or u.name) for u in getattr(message, "mentions", []) or []}
    roles = {r.id: "@" + r.name for r in getattr(message, "role_mentions", []) or []}
    channels = {c.id: "#" + c.name for c in getattr(message, "channel_mentions", []) or []}
    text = _USER.sub(lambda m: names.get(int(m.group(1)), "@someone"), text)
    text = _ROLE.sub(lambda m: roles.get(int(m.group(1)), "@role"), text)
    text = _CHANNEL.sub(lambda m: channels.get(int(m.group(1)), "#channel"), text)
    text = _STAMP.sub("", text)
    text = re.sub(r"^>+ ?", "", text, flags=re.M)
    text = _MARKDOWN.sub("", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def twemoji_code(char: str) -> str:
    points = [f"{ord(c):x}" for c in char]
    if "200d" not in points:
        points = [p for p in points if p != "fe0f"]
    return "-".join(points)


def tokenize(text: str) -> list[tuple[str, str]]:
    """Split text into ("word", text), ("emoji", url), ("space", " ") and ("break", "") tokens."""
    pieces: list[tuple[str, str]] = []
    cursor = 0
    spans: list[tuple[int, int, str]] = []
    for m in _CUSTOM.finditer(text):
        ext = "gif" if m.group(1) else "png"
        spans.append((m.start(), m.end(), CUSTOM_EMOJI.replace(".png", "." + ext).format(m.group(3))))
    taken = [(s, e) for s, e, _ in spans]
    for found in emoji_lib.emoji_list(text):
        start, end = found["match_start"], found["match_end"]
        if any(s <= start < e for s, e in taken):
            continue
        spans.append((start, end, TWEMOJI.format(twemoji_code(found["emoji"]))))
    for start, end, url in sorted(spans):
        if start > cursor:
            pieces.append(("text", text[cursor:start]))
        pieces.append(("emoji", url))
        cursor = end
    if cursor < len(text):
        pieces.append(("text", text[cursor:]))
    tokens: list[tuple[str, str]] = []
    for kind, value in pieces:
        if kind == "emoji":
            tokens.append(("emoji", value))
            continue
        for part in re.split(r"(\n| )", value):
            if part == "\n":
                tokens.append(("break", ""))
            elif part == " ":
                tokens.append(("space", " "))
            elif part:
                tokens.append(("word", part))
    return tokens


def emoji_urls(tokens: list[tuple[str, str]]) -> list[str]:
    return list(dict.fromkeys(v for k, v in tokens if k == "emoji"))


# Layout

def _width(token: tuple[str, str], f: ImageFont.FreeTypeFont, size: int) -> float:
    kind, value = token
    if kind == "emoji":
        return size * 1.05
    return f.getlength(value)


def wrap(tokens: list[tuple[str, str]], f: ImageFont.FreeTypeFont, size: int, width: int) -> list[list[tuple[str, str]]]:
    lines: list[list[tuple[str, str]]] = [[]]
    used = 0.0
    for token in tokens:
        kind, value = token
        if kind == "break":
            lines.append([])
            used = 0.0
            continue
        if kind == "space":
            if lines[-1]:
                lines[-1].append(token)
                used += f.getlength(" ")
            continue
        w = _width(token, f, size)
        if kind == "word" and w > width:  # one very long word: break it by characters
            for ch in value:
                cw = f.getlength(ch)
                if used + cw > width and lines[-1]:
                    lines.append([])
                    used = 0.0
                lines[-1].append(("word", ch))
                used += cw
            continue
        if used + w > width and lines[-1]:
            while lines[-1] and lines[-1][-1][0] == "space":
                lines[-1].pop()
            lines.append([])
            used = 0.0
        lines[-1].append(token)
        used += w
    for line in lines:
        while line and line[-1][0] == "space":
            line.pop()
    return lines


def fit(tokens: list[tuple[str, str]]) -> tuple[int, list[list[tuple[str, str]]]]:
    width, height = TEXT_RIGHT - TEXT_LEFT, TEXT_BOTTOM - TEXT_TOP
    size = MAX_SIZE
    while True:
        f = font("Inter-Light.otf", size)
        lines = wrap(tokens, f, size, width)
        if len(lines) * size * 1.25 <= height or size <= MIN_SIZE:
            break
        size -= 2
    max_lines = int(height // (size * 1.25))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1] + [("word", "…")]
    return size, lines


def _avatar_layer(avatar: bytes | None, colour: bool, flip: bool) -> Image.Image:
    card = Image.new("RGB", (W, H), (0, 0, 0))
    if not avatar:
        return card
    try:
        face = Image.open(io.BytesIO(avatar)).convert("RGB")
    except OSError:
        return card
    face = ImageOps.fit(face, (H, H), Image.LANCZOS)
    if not colour:
        face = ImageOps.grayscale(face).convert("RGB")
    row = Image.new("L", (H, 1))
    row.putdata([255 if x < H * 0.42 else int(255 * max(0.0, 1 - (x - H * 0.42) / (H * 0.58)) ** 1.6)
                 for x in range(H)])
    fade = row.resize((H, H))
    if flip:
        face, fade = ImageOps.mirror(face), ImageOps.mirror(fade)
        card.paste(face, (W - H, 0), fade)
    else:
        card.paste(face, (0, 0), fade)
    return card


def render_quote(tokens: list[tuple[str, str]], name: str, username: str, avatar: bytes | None,
                 emojis: dict[str, Image.Image] | None = None, *, colour: bool = False, flip: bool = False) -> bytes:
    emojis = emojis or {}
    card = _avatar_layer(avatar, colour, flip)
    left, right = (W - TEXT_RIGHT, W - TEXT_LEFT) if flip else (TEXT_LEFT, TEXT_RIGHT)
    size, lines = fit(tokens)
    f = font("Inter-Light.otf", size)
    draw = ImageDraw.Draw(card)
    line_h = size * 1.25
    name_font, user_font = font("Inter-LightItalic.otf", 30), font("Inter-Regular.otf", 22)
    block = len(lines) * line_h + 34 + 38 + 22
    y = (H - block) / 2
    centre = (left + right) / 2
    for line in lines:
        width = sum(_width(t, f, size) if t[0] != "space" else f.getlength(" ") for t in line)
        x = centre - width / 2
        for kind, value in line:
            if kind == "emoji":
                img = emojis.get(value)
                if img is not None:
                    side = int(size * 0.95)
                    icon = img.convert("RGBA").resize((side, side), Image.LANCZOS)
                    if not colour:
                        alpha = icon.getchannel("A")
                        icon = ImageOps.grayscale(icon.convert("RGB")).convert("RGBA")
                        icon.putalpha(alpha)
                    card.paste(icon, (int(x + size * 0.05), int(y + (line_h - side) / 2)), icon)
                x += size * 1.05
            else:
                draw.text((x, y + (line_h - size) / 2 - size * 0.08), value, font=f, fill=(255, 255, 255))
                x += f.getlength(value)
        y += line_h
    y += 34
    signed = f"- {name}"
    draw.text((centre - name_font.getlength(signed) / 2, y), signed, font=name_font, fill=(255, 255, 255))
    y += 38
    handle = f"@{username}"
    draw.text((centre - user_font.getlength(handle) / 2, y), handle, font=user_font, fill=(140, 140, 140))
    mark = font("Inter-Regular.otf", 16)
    mark_x = 16 if flip else W - 16 - mark.getlength("SYKE")
    draw.text((mark_x, H - 28), "SYKE", font=mark, fill=(70, 70, 70))
    out = io.BytesIO()
    card.save(out, "PNG", optimize=True)
    return out.getvalue()


# Discord side

class QuoteData:
    def __init__(self, tokens, name, username, avatar, emojis, jump_url, quoted_id, maker_id) -> None:
        self.tokens, self.name, self.username, self.avatar, self.emojis = tokens, name, username, avatar, emojis
        self.jump_url, self.quoted_id, self.maker_id = jump_url, quoted_id, maker_id
        self.colour = False
        self.flip = False

    def render(self) -> bytes:
        return render_quote(self.tokens, self.name, self.username, self.avatar, self.emojis,
                            colour=self.colour, flip=self.flip)


class QuoteView(discord.ui.View):
    def __init__(self, data: QuoteData) -> None:
        super().__init__(timeout=VIEW_SECONDS)
        self.data = data
        self.message: discord.Message | None = None
        if data.jump_url:
            self.add_item(discord.ui.Button(label="Original", url=data.jump_url, row=0))

    async def _redraw(self, interaction: discord.Interaction) -> None:
        image = await asyncio.to_thread(self.data.render)
        await interaction.response.edit_message(attachments=[discord.File(io.BytesIO(image), filename="quote.png")],
                                                view=self)

    @discord.ui.button(label="Colour", style=discord.ButtonStyle.secondary, row=0)
    async def colour_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.data.colour = not self.data.colour
        button.label = "Black & white" if self.data.colour else "Colour"
        await self._redraw(interaction)

    @discord.ui.button(label="Flip", style=discord.ButtonStyle.secondary, row=0)
    async def flip_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.data.flip = not self.data.flip
        await self._redraw(interaction)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, row=0)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        perms = getattr(interaction.user, "guild_permissions", None)
        if interaction.user.id not in (self.data.maker_id, self.data.quoted_id) and not (perms and perms.manage_messages):
            await interaction.response.send_message("Only whoever made it, the person quoted, or a mod can delete it.",
                                                    ephemeral=True)
            return
        await interaction.response.defer()
        with contextlib.suppress(discord.HTTPException):
            await interaction.message.delete()
        self.stop()

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        keep = discord.ui.View()
        if self.data.jump_url:
            keep.add_item(discord.ui.Button(label="Original", url=self.data.jump_url))
        with contextlib.suppress(discord.HTTPException):
            await self.message.edit(view=keep)


class Quotes(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._emoji_cache: OrderedDict[str, Image.Image | None] = OrderedDict()
        self._session = None
        self.menu = app_commands.ContextMenu(name="Make a quote", callback=self.quote_menu)
        self.menu.guild_only = True

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.menu.name, type=self.menu.type)
        if self._session is not None:
            await self._session.close()

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    async def fetch_emojis(self, urls: list[str]) -> dict[str, Image.Image]:
        found: dict[str, Image.Image] = {}
        missing = [u for u in urls if u not in self._emoji_cache]
        if missing:
            import aiohttp
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6))

            async def grab(url: str) -> None:
                try:
                    async with self._session.get(url) as resp:
                        data = await resp.read() if resp.status == 200 else b""
                    img = Image.open(io.BytesIO(data)).convert("RGBA") if data else None
                except Exception:
                    img = None
                self._emoji_cache[url] = img
                while len(self._emoji_cache) > EMOJI_CACHE:
                    self._emoji_cache.popitem(last=False)

            await asyncio.gather(*(grab(u) for u in missing[:40]))
        for url in urls:
            img = self._emoji_cache.get(url)
            if img is not None:
                found[url] = img
        return found

    async def build(self, message: discord.Message, maker) -> QuoteData | str:
        text = clean_text(message)
        if not text:
            return "That message has no words to quote."
        tokens = tokenize(text)
        author = message.author
        avatar = None
        with contextlib.suppress(discord.HTTPException, AttributeError):
            avatar = await author.display_avatar.replace(size=1024, static_format="png").read()
        emojis = await self.fetch_emojis(emoji_urls(tokens))
        name = getattr(author, "display_name", None) or author.name
        return QuoteData(tokens, name, author.name, avatar, emojis, message.jump_url, author.id, maker.id)

    async def post(self, destination, message: discord.Message, maker, *, interaction=None) -> str | None:
        data = await self.build(message, maker)
        if isinstance(data, str):
            return data
        image = await asyncio.to_thread(data.render)
        view = QuoteView(data)
        file = discord.File(io.BytesIO(image), filename="quote.png")
        if interaction is not None:
            sent = await interaction.followup.send(file=file, view=view, wait=True)
        else:
            sent = await destination.send(file=file, view=view)
        view.message = sent
        await self.to_wall(message, maker, image, getattr(sent, "channel", None))
        return None

    async def to_wall(self, message, maker, image: bytes, made_in) -> None:
        guild = getattr(message, "guild", None)
        if guild is None:
            return
        channel_id = self.bot.storage.get_config(guild.id, CONFIG).get("channel")
        wall = guild.get_channel(channel_id or 0)
        if wall is None or getattr(made_in, "id", None) == wall.id:
            return
        note = discord.ui.View()
        note.add_item(discord.ui.Button(label="Original", url=message.jump_url))
        with contextlib.suppress(discord.HTTPException):
            await wall.send(f"Quoted by {maker.mention} in {message.channel.mention}",
                            file=discord.File(io.BytesIO(image), filename="quote.png"), view=note,
                            allowed_mentions=discord.AllowedMentions.none())

    async def resolve(self, ctx: commands.Context, link: str) -> discord.Message | None:
        ref = getattr(ctx.message, "reference", None) if ctx.interaction is None else None
        if ref is not None:
            resolved = ref.resolved
            if isinstance(resolved, discord.Message):
                return resolved
            with contextlib.suppress(discord.HTTPException):
                return await ctx.channel.fetch_message(ref.message_id)
        link = (link or "").strip()
        m = LINK.search(link)
        channel, message_id = ctx.channel, None
        if m:
            channel = ctx.guild.get_channel_or_thread(int(m.group(2))) if ctx.guild else None
            message_id = int(m.group(3))
        elif link.isdigit():
            message_id = int(link)
        if channel is None or message_id is None:
            return None
        if ctx.guild and getattr(channel, "guild", None) != ctx.guild:
            return None
        perms = channel.permissions_for(ctx.author)
        if not perms.read_message_history:
            return None
        with contextlib.suppress(discord.HTTPException):
            return await channel.fetch_message(message_id)
        return None

    @commands.hybrid_group(name="quote", aliases=["quoteit", "miq"], fallback="make", case_insensitive=True,
                           description="Turn a message into a quote card")
    @app_commands.describe(message="A message link (or reply to the message with !quote)")
    @app_commands.guild_only()
    @commands.guild_only()
    async def quote(self, ctx: commands.Context, *, message: str = "") -> None:
        target = await self.resolve(ctx, message)
        if target is None:
            await ctx.reply(embed=problem("What should I quote?",
                                          f"Reply to a message with `{self.p(ctx)}quote`, paste a message link, or "
                                          f"right-click a message → **Apps** → **Make a quote**."), ephemeral=True)
            return
        if ctx.interaction:
            await ctx.interaction.response.defer(thinking=True)
            error = await self.post(ctx.channel, target, ctx.author, interaction=ctx.interaction)
            if error:
                await ctx.interaction.followup.send(error, ephemeral=True)
            return
        async with ctx.typing():
            error = await self.post(ctx.channel, target, ctx.author)
        if error:
            await ctx.reply(error)

    async def quote_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await interaction.response.defer(thinking=True)
        error = await self.post(interaction.channel, message, interaction.user, interaction=interaction)
        if error:
            await interaction.followup.send(error, ephemeral=True)

    @quote.command(name="channel", description="Also post every quote card into this channel")
    @app_commands.describe(channel="The quotes channel")
    @moderator("manage_guild")
    async def quote_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.attach_files):
            await ctx.reply(embed=problem("I can't post there", f"Give SYKE **Send Messages** and **Attach Files** in "
                                                                f"{channel.mention}."), ephemeral=True)
            return
        self.bot.storage.update_config(ctx.guild.id, CONFIG, channel=channel.id)
        await ctx.reply(embed=embed("Quotes channel set 💬", f"Every quote card made here also lands in {channel.mention}, "
                                                            f"with a link to the original.", OK), ephemeral=True)

    @quote.command(name="off", description="Stop posting quotes into the quotes channel")
    @moderator("manage_guild")
    async def quote_off(self, ctx: commands.Context) -> None:
        self.bot.storage.update_config(ctx.guild.id, CONFIG, channel=None)
        await ctx.reply(embed=embed("Quotes channel off", "Quote cards still work, they just stay where they're made.",
                                    WARN), ephemeral=True)
