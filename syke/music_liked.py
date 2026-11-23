"""Liked songs: everyone's own music library.

Tap ❤️ on the now-playing card (or `like`) to save the song that's playing. `liked` shows your library (or
someone else's) with buttons to play it, shuffle it, add songs, remove or reorder them, and clear it. A
library belongs to the person, not the server, so it follows you to every server SYKE is in.
"""

from __future__ import annotations

import logging
import math
import random
import re
import time

import discord
from discord.ext import commands

from .music_sources import Track

log = logging.getLogger("syke.music.liked")

LIBRARY_MAX = 500
PAGE = 10
PLAY_MAX = 100          # songs queued at once from a library
BRAND = discord.Colour.from_str("#ec4899")


# Storage

def song_key(track: Track) -> str:
    """One entry per song: its page link, or its name for songs that came as a name only."""
    base = (track.origin or track.url or "").split("&")[0].strip()
    if base.startswith("http"):
        return base.lower()
    return "q:" + " ".join((track.query or track.title or "").lower().split())


def row_to_track(row: tuple, requester: int = 0) -> Track:
    _id, title, url, query, uploader, duration, thumbnail, source, origin = row
    return Track(title=title, url=url, query=query, uploader=uploader, duration=duration, thumbnail=thumbnail,
                 source=source, origin=origin, requester=requester)


class Library:
    COLUMNS = "id, title, url, query, uploader, duration, thumbnail, source, origin"

    def __init__(self, storage) -> None:
        self.storage = storage

    def count(self, user_id: int) -> int:
        return self.storage._read("SELECT COUNT(*) FROM liked_songs WHERE user_id = ?", (user_id,))[0][0]

    def songs(self, user_id: int, offset: int = 0, limit: int = LIBRARY_MAX) -> list[tuple]:
        return self.storage._read(f"SELECT {self.COLUMNS} FROM liked_songs WHERE user_id = ? "
                                  "ORDER BY position DESC, id DESC LIMIT ? OFFSET ?", (user_id, limit, offset))

    def has(self, user_id: int, track: Track) -> int | None:
        rows = self.storage._read("SELECT id FROM liked_songs WHERE user_id = ? AND song_key = ?",
                                  (user_id, song_key(track)))
        return rows[0][0] if rows else None

    def add(self, user_id: int, track: Track) -> str:
        """'added', 'already' or 'full'."""
        if self.has(user_id, track):
            return "already"
        if self.count(user_id) >= LIBRARY_MAX:
            return "full"
        url = track.url if (track.url or "").startswith("http") else ""
        query = track.query or ""   # radio and Spotify songs keep their name: replaying finds the best upload again
        now = time.time()
        self.storage._write(
            "INSERT OR IGNORE INTO liked_songs (user_id, song_key, title, url, query, uploader, duration, thumbnail, "
            "source, origin, position, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, song_key(track), (track.title or track.label)[:200], url, query[:200], (track.uploader or "")[:100],
             track.duration, track.thumbnail or "", track.source or "", track.origin or "", now, now))
        return "added"

    def remove_ids(self, user_id: int, ids: list[int]) -> int:
        removed = 0
        for song_id in ids:
            removed += self.storage._write("DELETE FROM liked_songs WHERE user_id = ? AND id = ?", (user_id, song_id))
        return removed

    def remove_number(self, user_id: int, number: int) -> tuple | None:
        rows = self.songs(user_id, number - 1, 1) if number >= 1 else []
        if not rows:
            return None
        self.remove_ids(user_id, [rows[0][0]])
        return rows[0]

    def to_top(self, user_id: int, ids: list[int]) -> None:
        now = time.time()
        for i, song_id in enumerate(reversed(ids)):
            self.storage._write("UPDATE liked_songs SET position = ? WHERE user_id = ? AND id = ?",
                                (now + i * 0.001, user_id, song_id))

    def move(self, user_id: int, source: int, target: int) -> bool:
        """Move song number `source` to number `target` (1 = top)."""
        rows = self.songs(user_id)
        if not (1 <= source <= len(rows) and 1 <= target <= len(rows)):
            return False
        order = [r[0] for r in rows]
        song = order.pop(source - 1)
        order.insert(target - 1, song)
        top = time.time()
        for i, song_id in enumerate(order):   # rewrite positions so the new order sticks
            self.storage._write("UPDATE liked_songs SET position = ? WHERE user_id = ? AND id = ?",
                                (top - i, user_id, song_id))
        return True

    def clear(self, user_id: int) -> int:
        return self.storage._write("DELETE FROM liked_songs WHERE user_id = ?", (user_id,))


# The ❤️ button on the now-playing card (works after restarts: the id carries the server)

class LikeButton(discord.ui.DynamicItem[discord.ui.Button], template=r"syke:like:(?P<guild>\d+)"):
    def __init__(self, guild_id: int, row: int = 2) -> None:
        super().__init__(discord.ui.Button(label="Like", emoji="❤️", style=discord.ButtonStyle.secondary,
                                           custom_id=f"syke:like:{guild_id}", row=row))
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["guild"]), item.row or 2)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True  # liking is for everyone, not just whoever controls the music

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("LikedSongs")
        if cog is None:
            await interaction.response.send_message("Liked songs are offline right now.", ephemeral=True)
            return
        await cog.like_now(interaction, self.guild_id)


class UnlikeView(discord.ui.View):
    def __init__(self, cog: "LikedSongs", user_id: int, song_id: int) -> None:
        super().__init__(timeout=300)
        self.cog, self.user_id, self.song_id = cog, user_id, song_id

    @discord.ui.button(label="Remove from Liked", emoji="💔", style=discord.ButtonStyle.secondary)
    async def unlike(self, interaction: discord.Interaction, _button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("That's not your library.", ephemeral=True)
            return
        self.cog.library.remove_ids(self.user_id, [self.song_id])
        await interaction.response.edit_message(content="💔 Removed from your Liked songs.", embed=None, view=None)


# Library viewer

class AddForm(discord.ui.Modal, title="Add to your Liked songs"):
    def __init__(self, view: "LibraryView") -> None:
        super().__init__(timeout=600)
        self.view = view
        self.query = discord.ui.TextInput(label="Song name or link", max_length=300,
                                          placeholder="Still D.R.E. Dr. Dre, or a YouTube / Spotify link")
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await self.view.cog.add_by_query(interaction.user.id, self.query.value)
        await interaction.followup.send(text, ephemeral=True)
        if self.view.message is not None:
            self.view.page = 0
            await self.view.redraw()


class EditPicker(discord.ui.View):
    """Pick songs on this page, then remove them or move them to the top."""

    def __init__(self, view: "LibraryView", rows: list[tuple]) -> None:
        super().__init__(timeout=300)
        self.library_view = view
        start = view.page * PAGE
        self.picker = discord.ui.Select(
            placeholder="Pick songs", min_values=1, max_values=len(rows),
            options=[discord.SelectOption(label=f"{start + i + 1}. {r[1]}"[:100], value=str(r[0]),
                                          description=(r[4] or None) and r[4][:100]) for i, r in enumerate(rows)])
        self.picker.callback = self._picked
        self.add_item(self.picker)
        self.chosen: list[int] = []

    async def _picked(self, interaction: discord.Interaction) -> None:
        self.chosen = [int(v) for v in self.picker.values]
        await interaction.response.defer()

    @discord.ui.button(label="Remove", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def remove(self, interaction: discord.Interaction, _button) -> None:
        if not self.chosen:
            await interaction.response.send_message("Pick songs first.", ephemeral=True)
            return
        lib = self.library_view
        gone = lib.cog.library.remove_ids(lib.owner_id, self.chosen)
        await interaction.response.edit_message(content=f"🗑️ Removed {gone} song{'s' if gone != 1 else ''}.",
                                                view=None)
        await lib.redraw()

    @discord.ui.button(label="Move to top", emoji="⬆️", style=discord.ButtonStyle.secondary, row=1)
    async def top(self, interaction: discord.Interaction, _button) -> None:
        if not self.chosen:
            await interaction.response.send_message("Pick songs first.", ephemeral=True)
            return
        lib = self.library_view
        lib.cog.library.to_top(lib.owner_id, self.chosen)
        await interaction.response.edit_message(content="⬆️ Moved to the top.", view=None)
        lib.page = 0
        await lib.redraw()


class ClearConfirm(discord.ui.View):
    def __init__(self, view: "LibraryView") -> None:
        super().__init__(timeout=120)
        self.library_view = view

    @discord.ui.button(label="Yes, clear everything", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _button) -> None:
        lib = self.library_view
        gone = lib.cog.library.clear(lib.owner_id)
        await interaction.response.edit_message(content=f"🗑️ Cleared {gone} songs.", view=None)
        lib.page = 0
        await lib.redraw()

    @discord.ui.button(label="Keep them", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _button) -> None:
        await interaction.response.edit_message(content="Kept.", view=None)


class LibraryView(discord.ui.View):
    def __init__(self, cog: "LikedSongs", owner, viewer_id: int, page: int = 0) -> None:
        super().__init__(timeout=900)
        self.cog, self.owner, self.owner_id, self.viewer_id, self.page = cog, owner, owner.id, viewer_id, page
        self.message: discord.Message | None = None
        mine = owner.id == viewer_id
        for item in (self.add_song, self.edit, self.clear):
            if not mine:
                self.remove_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message("Open your own with `liked`.", ephemeral=True)
            return False
        return True

    def pages(self) -> int:
        return max(1, math.ceil(self.cog.library.count(self.owner_id) / PAGE))

    def card(self) -> discord.Embed:
        lib = self.cog.library
        total = lib.count(self.owner_id)
        self.page = max(0, min(self.page, self.pages() - 1))
        rows = lib.songs(self.owner_id, self.page * PAGE, PAGE)
        name = getattr(self.owner, "display_name", "Your")
        title = "❤️ Your Liked songs" if self.owner_id == self.viewer_id else f"❤️ {name}'s Liked songs"
        if not rows:
            body = ("Nothing here yet. Tap **❤️ Like** on the now-playing card, use `like` while a song plays, "
                    "or press **➕ Add**.")
        else:
            lines = []
            for i, row in enumerate(rows, self.page * PAGE + 1):
                _id, song, url, _q, uploader, duration, *_ = row
                label = discord.utils.escape_markdown(song[:70])
                label = f"[{label}]({url})" if url.startswith("http") else f"**{label}**"
                length = f" · `{int(duration // 60)}:{int(duration % 60):02d}`" if duration else ""
                lines.append(f"`{i:>2}.` {label}{length}")
            body = "\n".join(lines)
        card = discord.Embed(title=title, description=body, colour=BRAND)
        seconds = sum((r[5] or 0) for r in lib.songs(self.owner_id)) if total else 0
        hours = f" · {int(seconds // 3600)}h {int(seconds % 3600 // 60)}m" if seconds >= 3600 else \
            (f" · {int(seconds // 60)} min" if seconds else "")
        card.set_footer(text=f"Page {self.page + 1}/{self.pages()} · {total} song{'s' if total != 1 else ''}{hours}")
        return card

    async def redraw(self, interaction: discord.Interaction | None = None) -> None:
        if interaction is not None:
            await interaction.response.edit_message(embed=self.card(), view=self)
        elif self.message is not None:
            try:
                await self.message.edit(embed=self.card(), view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, _button) -> None:
        self.page = (self.page - 1) % self.pages()
        await self.redraw(interaction)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, row=0)
    async def forward(self, interaction: discord.Interaction, _button) -> None:
        self.page = (self.page + 1) % self.pages()
        await self.redraw(interaction)

    @discord.ui.button(label="Play", emoji="🎧", style=discord.ButtonStyle.success, row=1)
    async def play_all(self, interaction: discord.Interaction, _button) -> None:
        await self.cog.play_library(InteractionCtx(interaction), self.owner, shuffle=False)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.primary, row=1)
    async def shuffle_play(self, interaction: discord.Interaction, _button) -> None:
        await self.cog.play_library(InteractionCtx(interaction), self.owner, shuffle=True)

    @discord.ui.button(label="Add", emoji="➕", style=discord.ButtonStyle.secondary, row=2)
    async def add_song(self, interaction: discord.Interaction, _button) -> None:
        await interaction.response.send_modal(AddForm(self))

    @discord.ui.button(label="Edit", emoji="✏️", style=discord.ButtonStyle.secondary, row=2)
    async def edit(self, interaction: discord.Interaction, _button) -> None:
        rows = self.cog.library.songs(self.owner_id, self.page * PAGE, PAGE)
        if not rows:
            await interaction.response.send_message("Nothing to edit on this page.", ephemeral=True)
            return
        await interaction.response.send_message("Pick songs from this page:", view=EditPicker(self, rows),
                                                ephemeral=True)

    @discord.ui.button(label="Clear", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
    async def clear(self, interaction: discord.Interaction, _button) -> None:
        total = self.cog.library.count(self.owner_id)
        if not total:
            await interaction.response.send_message("Your library is already empty.", ephemeral=True)
            return
        await interaction.response.send_message(f"Clear all **{total}** liked songs? This can't be undone.",
                                                view=ClearConfirm(self), ephemeral=True)


class InteractionCtx:
    """Just enough of a command context for the music queue to answer a button press."""

    def __init__(self, interaction: discord.Interaction) -> None:
        self.interaction = interaction
        self.author, self.guild, self.channel = interaction.user, interaction.guild, interaction.channel
        self.bot = interaction.client

    async def reply(self, content=None, *, ephemeral: bool = False, mention_author: bool = False, **kwargs):
        if self.interaction.response.is_done():
            return await self.interaction.followup.send(content, ephemeral=ephemeral, **kwargs)
        return await self.interaction.response.send_message(content, ephemeral=ephemeral, **kwargs)

    send = reply

    async def defer(self, **kwargs):
        if not self.interaction.response.is_done():
            await self.interaction.response.defer(**kwargs)

    def typing(self):
        import contextlib
        return contextlib.nullcontext()


# The cog

class LikedSongs(commands.Cog, name="LikedSongs"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.library = Library(bot.storage)

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(LikeButton)

    @property
    def music(self):
        return self.bot.get_cog("Music")

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    async def like_now(self, interaction: discord.Interaction, guild_id: int) -> None:
        music = self.music
        player = music.players.get(guild_id) if music else None
        track = player.current if player else None
        if track is None:
            await interaction.response.send_message("Nothing's playing right now.", ephemeral=True)
            return
        self._note_party_like(player, interaction.user.id)
        await interaction.response.send_message(**self._like(interaction.user.id, track), ephemeral=True)

    def _note_party_like(self, player, user_id: int) -> None:
        social = self.bot.get_cog("MusicSocial")
        if social is not None and getattr(player, "party", None) is not None:
            social.note_like(player, user_id)

    def _like(self, user_id: int, track: Track) -> dict:
        result = self.library.add(user_id, track)
        title = discord.utils.escape_markdown(track.label[:120])
        if result == "already":
            song_id = self.library.has(user_id, track)
            return {"content": f"❤️ **{title}** is already in your Liked songs.",
                    "view": UnlikeView(self, user_id, song_id)}
        if result == "full":
            return {"content": f"Your library is full ({LIBRARY_MAX} songs). Remove some with `liked`."}
        total = self.library.count(user_id)
        return {"content": f"❤️ Added **{title}** to your Liked songs ({total}). See them with `liked`."}

    async def add_by_query(self, user_id: int, query: str) -> str:
        music = self.music
        if music is None:
            return "Music is offline right now."
        try:
            tracks, _ = await music.lookup(query.strip(), user_id)
        except LookupError as error:
            return f"Couldn't find that: {error}"
        if not tracks:
            return "Couldn't find that."
        result = self.library.add(user_id, tracks[0])
        title = discord.utils.escape_markdown(tracks[0].label[:120])
        return {"added": f"❤️ Added **{title}**.", "already": f"**{title}** is already in your Liked songs.",
                "full": f"Your library is full ({LIBRARY_MAX} songs)."}[result]

    async def play_library(self, ctx, owner, *, shuffle: bool) -> None:
        music = self.music
        rows = self.library.songs(owner.id)
        if not rows:
            await ctx.reply("That library is empty.", ephemeral=True)
            return
        if music is None or not await music.ready_or_explain(ctx):
            return
        if getattr(getattr(ctx.author, "voice", None), "channel", None) is None:
            await ctx.reply("Join a voice channel first.", ephemeral=True)
            return
        tracks = [row_to_track(r, ctx.author.id) for r in rows]
        if shuffle:
            random.shuffle(tracks)
        whose = "your" if owner.id == ctx.author.id else f"{owner.display_name}'s"
        await ctx.defer()
        await music.enqueue(ctx, tracks[:PLAY_MAX], f"{whose} Liked songs" + (" (shuffled)" if shuffle else ""))

    async def show(self, ctx, member=None) -> None:
        owner = member or ctx.author
        view = LibraryView(self, owner, ctx.author.id)
        view.message = await ctx.reply(embed=view.card(), view=view, mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())

    # Commands

    @commands.command(name="like", aliases=["heart", "save", "fav"], help="Save the song that's playing to your Liked songs")
    @commands.guild_only()
    async def like_cmd(self, ctx: commands.Context) -> None:
        music = self.music
        player = music.players.get(ctx.guild.id) if music else None
        if player is None or player.current is None:
            await ctx.reply("Nothing's playing right now.", mention_author=False)
            return
        self._note_party_like(player, ctx.author.id)
        await ctx.reply(**self._like(ctx.author.id, player.current), mention_author=False)

    @commands.group(name="liked", aliases=["likes", "library", "lib", "likedsongs"], invoke_without_command=True,
                    case_insensitive=True, help="Your Liked songs: liked, liked @user, liked play, liked add <song>")
    @commands.guild_only()
    async def liked(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        await self.show(ctx, member)

    @liked.command(name="play", aliases=["p", "listen"])
    async def liked_play(self, ctx: commands.Context, *, how: str = "") -> None:
        member = ctx.message.mentions[0] if getattr(ctx, "message", None) and ctx.message.mentions else ctx.author
        await self.play_library(ctx, member, shuffle="shuffle" in how.lower() or "mix" in how.lower())

    @liked.command(name="shuffle", aliases=["mix"])
    async def liked_shuffle(self, ctx: commands.Context) -> None:
        member = ctx.message.mentions[0] if getattr(ctx, "message", None) and ctx.message.mentions else ctx.author
        await self.play_library(ctx, member, shuffle=True)

    @liked.command(name="add", aliases=["save"])
    async def liked_add(self, ctx: commands.Context, *, song: str) -> None:
        async with ctx.typing():
            text = await self.add_by_query(ctx.author.id, song)
        await ctx.reply(text, mention_author=False)

    @liked.command(name="remove", aliases=["rm", "delete", "unlike"])
    async def liked_remove(self, ctx: commands.Context, number: int) -> None:
        row = self.library.remove_number(ctx.author.id, number)
        if row is None:
            await ctx.reply(f"No song #{number} in your Liked songs.", mention_author=False)
            return
        await ctx.reply(f"💔 Removed **{discord.utils.escape_markdown(row[1][:120])}**.", mention_author=False)

    @liked.command(name="move", aliases=["mv"])
    async def liked_move(self, ctx: commands.Context, source: int, target: int) -> None:
        if self.library.move(ctx.author.id, source, target):
            await ctx.reply(f"Moved song #{source} to #{target}.", mention_author=False)
        else:
            await ctx.reply("Those numbers aren't in your Liked songs.", mention_author=False)

    @liked.command(name="clear", aliases=["wipe", "empty"])
    async def liked_clear(self, ctx: commands.Context) -> None:
        view = LibraryView(self, ctx.author, ctx.author.id)
        total = self.library.count(ctx.author.id)
        if not total:
            await ctx.reply("Your library is already empty.", mention_author=False)
            return
        await ctx.reply(f"Clear all **{total}** liked songs? This can't be undone.", view=ClearConfirm(view),
                        mention_author=False)
