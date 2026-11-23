"""Music with friends: Blend (a shared playlist and a taste match) and listening parties.

`blend @friend` mixes your Liked songs with theirs: the songs you share first, then turns between your picks
and theirs, with a taste match score from shared songs and artists (and, with AI on, how close your tastes
are even without exact matches).

`party` starts a listening party in your voice channel: SYKE takes turns playing a song from each party
member's Liked songs. ❤️ likes and skips are counted per song, and `party end` posts a recap: the most-liked
song, the best picks, and who skipped the most.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field

import discord
from discord.ext import commands

from . import ai
from .music_liked import BRAND, InteractionCtx, row_to_track, song_key
from .music_sources import Track

log = logging.getLogger("syke.music.social")

BLEND_MAX = 50
PARTY_COLOUR = discord.Colour.from_str("#f59e0b")

BLEND_PROMPT = """Two people's music libraries, as their top artists. Judge how close their tastes are (genres, \
eras, moods), even when they don't share exact artists, and describe the overlap in ONE short casual line \
(max 20 words, like Spotify Blend: "you both live for UK drill, Mo just sneaks in more R&B").

Reply with JSON only: {"similarity": 0-100, "line": "..."}"""

RECAP_PROMPT = """You are SYKE, a Discord bot with a personality, hosting a listening party that just ended. \
Sum it up in ONE or TWO short casual lines (max 35 words): roast or praise the picks, call out the skipper. \
Your mood: {mood}. No @mentions, no slurs.

Reply with JSON only: {{"line": "..."}}"""


# Taste matching

_NOISE = re.compile(r"\s*(?:-\s*topic|vevo|official|music|records|tv)\s*$", re.I)


def artist_of(row: tuple) -> str:
    """The artist of a saved song: from 'Title — Artist', else the uploader without ' - Topic' / 'VEVO'."""
    title, uploader = row[1], row[4] or ""
    if " — " in title:
        name = title.split(" — ", 1)[1]
    else:
        name = uploader
    name = _NOISE.sub("", name.split(",")[0].split(" & ")[0].split(" feat")[0]).strip()
    return name.lower()


def taste_match(rows_a: list[tuple], rows_b: list[tuple]) -> tuple[int, list[str], list[str]]:
    """(score 0-100, shared song keys, shared artists) from two libraries."""
    keys_a = {key_of(r) for r in rows_a}
    keys_b = {key_of(r) for r in rows_b}
    shared_songs = keys_a & keys_b
    artists_a, artists_b = Counter(artist_of(r) for r in rows_a), Counter(artist_of(r) for r in rows_b)
    artists_a.pop("", None)
    artists_b.pop("", None)
    shared_artists = set(artists_a) & set(artists_b)
    if not rows_a or not rows_b:
        return 0, [], []
    song_sim = len(shared_songs) / min(len(keys_a), len(keys_b))
    weight = sum(min(artists_a[a], artists_b[a]) for a in shared_artists)
    artist_sim = weight / max(1, min(sum(artists_a.values()), sum(artists_b.values())))
    raw = min(1.0, 0.35 * song_sim + 0.65 * artist_sim)
    score = round(100 * (1 - (1 - raw) ** 2))
    ranked = sorted(shared_artists, key=lambda a: -(artists_a[a] + artists_b[a]))
    return score, sorted(shared_songs), ranked


def key_of(row: tuple) -> str:
    _id, title, url, query, *_ = row
    return song_key(Track(title=title, url=url, query=query))


def blend_order(rows_a: list[tuple], rows_b: list[tuple], limit: int = BLEND_MAX,
                rng: random.Random | None = None) -> list[tuple[tuple, str]]:
    """The blend: shared songs first, then turns between A's and B's picks. Returns (row, 'both'|'a'|'b')."""
    rng = rng or random
    by_key_a = {key_of(r): r for r in rows_a}
    by_key_b = {key_of(r): r for r in rows_b}
    shared = [by_key_a[k] for k in by_key_a if k in by_key_b]
    only_a = [r for k, r in by_key_a.items() if k not in by_key_b]
    only_b = [r for k, r in by_key_b.items() if k not in by_key_a]
    for part in (shared, only_a, only_b):
        rng.shuffle(part)
    out = [(r, "both") for r in shared]
    while (only_a or only_b) and len(out) < limit:
        if only_a:
            out.append((only_a.pop(), "a"))
        if only_b and len(out) < limit:
            out.append((only_b.pop(), "b"))
    return out[:limit]


def bar(score: int) -> str:
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)


# Parties

@dataclass
class Pick:
    key: str
    title: str
    picker: int
    likes: set[int] = field(default_factory=set)
    skipped_by: int | None = None


@dataclass
class Party:
    host: int
    voice_channel_id: int
    text_channel_id: int
    started: float
    members: list[int] = field(default_factory=list)   # rotation order
    turn: int = 0
    picks: list[Pick] = field(default_factory=list)
    played: dict[int, set[str]] = field(default_factory=dict)
    skips: Counter = field(default_factory=Counter)

    def pick_for(self, track: Track | None) -> Pick | None:
        if track is None:
            return None
        key = song_key(track)
        for pick in reversed(self.picks):
            if pick.key == key and pick.picker == track.requester:
                return pick
        return None


def recap_stats(party: Party) -> dict:
    picks = party.picks
    liked = max(picks, key=lambda p: (len(p.likes), p.skipped_by is None), default=None)
    by_picker: dict[int, list[Pick]] = {}
    for pick in picks:
        by_picker.setdefault(pick.picker, []).append(pick)

    def quality(items: list[Pick]) -> float:
        likes = sum(len(p.likes) for p in items)
        skipped = sum(p.skipped_by is not None for p in items)
        return (likes - 1.5 * skipped) / max(1, len(items))

    best = max(by_picker.items(), key=lambda kv: (quality(kv[1]), len(kv[1])), default=(None, []))
    skipper = party.skips.most_common(1)[0] if party.skips else (None, 0)
    return {"songs": len(picks), "most_liked": liked if liked and liked.likes else None,
            "best_picker": best[0] if best[0] is not None and quality(best[1]) > 0 else None,
            "best_likes": sum(len(p.likes) for p in best[1]), "skipper": skipper[0], "skip_count": skipper[1],
            "skipped": sum(p.skipped_by is not None for p in picks)}


class PartyCard(discord.ui.View):
    def __init__(self, cog: "MusicSocial", guild_id: int) -> None:
        super().__init__(timeout=None)
        self.cog, self.guild_id = cog, guild_id

    @discord.ui.button(label="Join the party", emoji="🎉", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, _button) -> None:
        await interaction.response.send_message(self.cog.join_party(interaction.guild, interaction.user),
                                                ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, _button) -> None:
        await interaction.response.send_message(self.cog.leave_party(interaction.guild, interaction.user),
                                                ephemeral=True)


class BlendView(discord.ui.View):
    def __init__(self, cog: "MusicSocial", tracks: list[Track], name: str) -> None:
        super().__init__(timeout=900)
        self.cog, self.tracks, self.name = cog, tracks, name

    @discord.ui.button(label="Play the blend", emoji="🎧", style=discord.ButtonStyle.success)
    async def play(self, interaction: discord.Interaction, _button) -> None:
        await self.cog.queue_tracks(InteractionCtx(interaction), list(self.tracks), self.name)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.primary)
    async def shuffle(self, interaction: discord.Interaction, _button) -> None:
        tracks = list(self.tracks)
        random.shuffle(tracks)
        await self.cog.queue_tracks(InteractionCtx(interaction), tracks, self.name + " (shuffled)")


class MusicSocial(commands.Cog, name="MusicSocial"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @property
    def music(self):
        return self.bot.get_cog("Music")

    @property
    def library(self):
        liked = self.bot.get_cog("LikedSongs")
        return liked.library if liked else None

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    async def queue_tracks(self, ctx, tracks: list[Track], name: str) -> None:
        music = self.music
        if music is None or not await music.ready_or_explain(ctx):
            return
        if getattr(getattr(ctx.author, "voice", None), "channel", None) is None:
            await ctx.reply("Join a voice channel first.", ephemeral=True)
            return
        for track in tracks:
            track.requester = track.requester or ctx.author.id
        await ctx.defer()
        await music.enqueue(ctx, tracks, name)

    # Blend

    async def ai_taste(self, rows_a, rows_b) -> tuple[int | None, str]:
        if self.bot.settings.ai_provider == "none":
            return None, ""

        def top(rows):
            return ", ".join(a for a, _ in Counter(artist_of(r) for r in rows if artist_of(r)).most_common(12))
        with contextlib.suppress(Exception):
            data = await ai._complete(self.bot.settings, BLEND_PROMPT,
                                      f"Person A: {top(rows_a)}\nPerson B: {top(rows_b)}", 0.6, 15)
            similarity = max(0, min(100, int(data.get("similarity", 0))))
            return similarity, ai.public_text(data.get("line"), 160)
        return None, ""

    @commands.command(name="blend", aliases=["tastematch", "compatible", "musicmatch"],
                      help="Blend your Liked songs with someone's: a shared playlist and a taste match score")
    @commands.guild_only()
    async def blend(self, ctx: commands.Context, friend: discord.Member) -> None:
        lib = self.library
        if lib is None:
            await ctx.reply("Liked songs are offline right now.", mention_author=False)
            return
        if friend.id == ctx.author.id:
            await ctx.reply("Blending with yourself is just your Liked songs. Try `liked`.", mention_author=False)
            return
        mine, theirs = lib.songs(ctx.author.id), lib.songs(friend.id)
        empty = [m.display_name for m, rows in ((ctx.author, mine), (friend, theirs)) if not rows]
        if empty:
            await ctx.reply(f"**{' and '.join(empty)}** {'has' if len(empty) == 1 else 'have'} no Liked songs yet. "
                            f"Tap ❤️ Like on the now-playing card to start one.",
                            mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return
        async with ctx.typing():
            score, shared_songs, shared_artists = taste_match(mine, theirs)
            ai_score, line = await self.ai_taste(mine, theirs)
        if ai_score is not None:
            score = round(0.5 * score + 0.5 * ai_score)
        badges = self.bot.get_cog("Achievements")
        if badges is not None:
            for who in (ctx.author, friend):
                await badges.award_event(ctx.guild, who, ctx.channel, "best_blend", score, maximum=True)
        order = blend_order(mine, theirs)
        tracks = [row_to_track(row) for row, _ in order]
        for (row, who), track in zip(order, tracks):
            track.requester = ctx.author.id if who in ("a", "both") else friend.id
        mark = {"both": "💞", "a": "🔹", "b": "🔸"}
        preview = "\n".join(f"{mark[who]} {discord.utils.escape_markdown(row[1][:60])}" for row, who in order[:8])
        verdict = ("soulmates" if score >= 85 else "very in sync" if score >= 65 else "pretty compatible"
                   if score >= 45 else "a little different" if score >= 25 else "worlds apart")
        card = discord.Embed(title=f"🎧 {ctx.author.display_name} + {friend.display_name}",
                             description=f"**{score}% taste match** · {verdict}\n`{bar(score)}`"
                                         + (f"\n*{line}*" if line else ""), colour=BRAND)
        if shared_artists:
            card.add_field(name="You both like", value=", ".join(a.title() for a in shared_artists[:6]), inline=False)
        card.add_field(name=f"The blend · {len(order)} songs",
                       value=preview + ("\n…" if len(order) > 8 else ""), inline=False)
        card.set_footer(text=f"💞 both of you · 🔹 {ctx.author.display_name} · 🔸 {friend.display_name} · "
                             f"{len(shared_songs)} songs in common")
        await ctx.reply(embed=card, view=BlendView(self, tracks, f"{ctx.author.display_name} + "
                                                                  f"{friend.display_name} blend"),
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    # Parties

    def party(self, guild_id: int) -> tuple[object, Party | None]:
        music = self.music
        player = music.players.get(guild_id) if music else None
        return player, getattr(player, "party", None)

    def in_voice(self, guild, user_id: int, channel_id: int) -> bool:
        member = guild.get_member(user_id) if guild else None
        channel = getattr(getattr(member, "voice", None), "channel", None)
        return channel is not None and channel.id == channel_id

    def join_party(self, guild, member) -> str:
        _, party = self.party(guild.id)
        if party is None:
            return "There's no party right now. Start one with `party`."
        if not self.in_voice(guild, member.id, party.voice_channel_id):
            return "Join the party's voice channel first."
        if member.id in party.members:
            return "You're already in the rotation."
        if not self.library or not self.library.count(member.id):
            return "Like some songs first (❤️ on the now-playing card), then join."
        party.members.append(member.id)
        return "🎉 You're in! Your Liked songs are in the rotation."

    def leave_party(self, guild, member) -> str:
        _, party = self.party(guild.id)
        if party is None or member.id not in party.members:
            return "You're not in a party."
        party.members.remove(member.id)
        return "You left the rotation. Your songs won't come up any more."

    async def party_fill(self, player, count: int = 2) -> None:
        """Queue the next picks: one song from the next member in the rotation who's still in voice."""
        party = player.party
        lib = self.library
        if party is None or lib is None:
            return
        added, tries = 0, 0
        while added < count and party.members and tries < len(party.members) * 2:
            tries += 1
            member_id = party.members[party.turn % len(party.members)]
            party.turn += 1
            if not self.in_voice(player.guild, member_id, party.voice_channel_id):
                continue
            played = party.played.setdefault(member_id, set())
            rows = [r for r in lib.songs(member_id) if key_of(r) not in played]
            if not rows:
                played.clear()   # gone through their whole library: start over
                rows = lib.songs(member_id)
            if not rows:
                continue
            row = random.choice(rows)
            track = row_to_track(row, member_id)
            played.add(key_of(row))
            party.picks.append(Pick(song_key(track), track.label, member_id))
            player.queue.append(track)
            added += 1

    def note_like(self, player, user_id: int) -> None:
        party = getattr(player, "party", None)
        pick = party.pick_for(player.current) if party else None
        if pick is not None:
            pick.likes.add(user_id)

    def note_skip(self, player, user_id: int) -> None:
        party = getattr(player, "party", None)
        if party is None:
            return
        pick = party.pick_for(player.current)
        if pick is not None and pick.skipped_by is None:
            pick.skipped_by = user_id
            party.skips[user_id] += 1

    @commands.group(name="party", aliases=["listeningparty", "lp2"], invoke_without_command=True,
                    case_insensitive=True, help="Listening party: everyone's Liked songs in one rotation")
    @commands.guild_only()
    async def party_cmd(self, ctx: commands.Context) -> None:
        music = self.music
        voice = getattr(getattr(ctx.author, "voice", None), "channel", None)
        if music is None or voice is None:
            await ctx.reply("Join a voice channel first, then start the party.", mention_author=False)
            return
        player, party = self.party(ctx.guild.id)
        if party is not None:
            await ctx.reply(embed=self.party_card(ctx.guild, party), view=PartyCard(self, ctx.guild.id),
                            mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return
        lib = self.library
        members = [m.id for m in getattr(voice, "members", []) if not m.bot and lib and lib.count(m.id)]
        if ctx.author.id not in members:
            if not lib or not lib.count(ctx.author.id):
                await ctx.reply("Like a few songs first (tap ❤️ Like on the now-playing card), then start a party.",
                                mention_author=False)
                return
            members.insert(0, ctx.author.id)
        if not await music.ready_or_explain(ctx):
            return
        player = await music.connect(ctx)
        if player is None:
            return
        player.radio = None
        player.party = Party(ctx.author.id, voice.id, ctx.channel.id, time.time(), members)
        badges = self.bot.get_cog("Achievements")
        if badges is not None:
            await badges.award_event(ctx.guild, ctx.author, ctx.channel, "parties")
        await self.party_fill(player, 2)
        await ctx.reply(embed=self.party_card(ctx.guild, player.party), view=PartyCard(self, ctx.guild.id),
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        await player.start_or_queue()

    def party_card(self, guild, party: Party) -> discord.Embed:
        names = [getattr(guild.get_member(uid), "display_name", "someone") for uid in party.members]
        card = discord.Embed(title="🎉 Listening party", colour=PARTY_COLOUR,
                             description="SYKE takes turns playing a song from everyone's Liked songs. Tap **❤️ Like** "
                                         "on songs you love; skips are counted too. There's a recap at the end.")
        card.add_field(name=f"In the rotation ({len(names)})", value=", ".join(names) or "nobody yet", inline=False)
        card.set_footer(text="Join: be in the voice channel and press Join · party end posts the recap")
        return card

    @party_cmd.command(name="join")
    async def party_join(self, ctx: commands.Context) -> None:
        await ctx.reply(self.join_party(ctx.guild, ctx.author), mention_author=False)

    @party_cmd.command(name="leave")
    async def party_leave(self, ctx: commands.Context) -> None:
        await ctx.reply(self.leave_party(ctx.guild, ctx.author), mention_author=False)

    @party_cmd.command(name="end", aliases=["stop", "recap", "over"])
    async def party_end(self, ctx: commands.Context) -> None:
        player, party = self.party(ctx.guild.id)
        if party is None:
            await ctx.reply("There's no party right now.", mention_author=False)
            return
        music = self.music
        if ctx.author.id != party.host and music.control_problem(ctx.guild, ctx.author):
            await ctx.reply("Only the host or someone with music controls can end the party.", mention_author=False)
            return
        await self.end_party(player, keep_playing=False)

    async def end_party(self, player, keep_playing: bool = True) -> None:
        """Post the recap and turn party mode off."""
        party = getattr(player, "party", None)
        if party is None:
            return
        player.party = None
        if not keep_playing:
            player.queue.clear()
        guild = player.guild
        stats = recap_stats(party)

        def name(uid):
            return getattr(guild.get_member(uid), "display_name", "someone") if uid else "nobody"

        minutes = max(1, round((time.time() - party.started) / 60))
        lines = [f"**{stats['songs']}** songs · **{minutes}** min · {len(party.members)} in the rotation"]
        if stats["most_liked"]:
            top = stats["most_liked"]
            lines.append(f"❤️ **Most liked:** {discord.utils.escape_markdown(top.title[:80])} "
                         f"({len(top.likes)} likes, picked by {name(top.picker)})")
        if stats["best_picker"]:
            lines.append(f"🏆 **Best picks:** {name(stats['best_picker'])} ({stats['best_likes']} likes)")
        if stats["skipper"]:
            lines.append(f"⏭️ **Skipped the most:** {name(stats['skipper'])} ({stats['skip_count']} skips)")
        comment = await self.recap_line(guild, party, stats, name)
        card = discord.Embed(title="🎉 Party recap", description="\n".join(lines) + (f"\n\n*{comment}*" if comment else ""),
                             colour=PARTY_COLOUR)
        channel = guild.get_channel(party.text_channel_id) if hasattr(guild, "get_channel") else None
        if channel is not None:
            with contextlib.suppress(discord.HTTPException):
                await channel.send(embed=card, allowed_mentions=discord.AllowedMentions.none())

    async def recap_line(self, guild, party: Party, stats: dict, name) -> str:
        if self.bot.settings.ai_provider == "none" or not party.picks:
            return ""
        persona = self.bot.get_cog("Persona")
        mood = persona.mood_now(guild.id)[0] if persona else "chill"
        played = "\n".join(f"- {p.title} (picked by {name(p.picker)}, {len(p.likes)} likes"
                           + (f", skipped by {name(p.skipped_by)})" if p.skipped_by else ")") for p in party.picks[-25:])
        with contextlib.suppress(Exception):
            data = await ai._complete(self.bot.settings, RECAP_PROMPT.format(mood=mood), played, 1.0, 15)
            return ai.public_text(data.get("line"), 300)
        return ""
