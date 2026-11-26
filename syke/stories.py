"""Stories: SYKE plays audiobooks in voice — public-domain books read by LibriVox volunteers, by genre.

`story` lists the genres, `story horror` picks a horror story, `story the raven` looks for that title. Chapters
go into the music queue like songs, so skip, pause, queue and leave all work the same way. Shorter books are
preferred, because a story should fit an evening, not a week.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass

import discord
import httpx
from discord.ext import commands

from .music_sources import Track

log = logging.getLogger("syke.stories")

API = "https://librivox.org/api/feed/audiobooks/"
TIMEOUT = 20
CACHE_SECONDS = 6 * 3600
MAX_CHAPTERS = 40
STORY_SECONDS = (8 * 60, 4 * 3600)      # what counts as a "story": 8 minutes to 4 hours
HEADERS = {"User-Agent": "SYKE Discord bot (audiobooks from LibriVox)"}

# key -> (label, LibriVox genre to search, what it is)
GENRES: dict[str, tuple[str, str, str]] = {
    "horror": ("👻 Horror", "Horror", "ghosts, monsters and things in the dark"),
    "mystery": ("🔎 Mystery", "Mystery", "detectives, crimes and whodunits"),
    "scifi": ("🚀 Sci-fi", "Science Fiction", "space, the future and strange machines"),
    "fantasy": ("🐉 Fantasy", "Fantasy", "magic, quests and other worlds"),
    "adventure": ("🗺️ Adventure", "Action & Adventure", "pirates, explorers and wild journeys"),
    "romance": ("💘 Romance", "Romance", "love stories"),
    "comedy": ("😂 Comedy", "Humor", "funny stories"),
    "kids": ("🧸 Kids", "Children", "stories for children"),
    "fairytales": ("🧚 Fairy tales", "Fairy tales", "fairy tales, fables and folk stories"),
    "myths": ("⚡ Myths", "Myths", "myths and legends"),
    "classics": ("📚 Classics", "Literary Fiction", "classic literature"),
    "poetry": ("🪶 Poetry", "Poetry", "poems read aloud"),
    "history": ("🏛️ History", "History", "true history"),
}
ALIASES = {"scary": "horror", "spooky": "horror", "ghost": "horror", "crime": "mystery", "detective": "mystery",
           "sci-fi": "scifi", "science fiction": "scifi", "space": "scifi", "magic": "fantasy", "funny": "comedy",
           "humor": "comedy", "humour": "comedy", "children": "kids", "bedtime": "kids", "fairy": "fairytales",
           "fairy tale": "fairytales", "fables": "fairytales", "legends": "myths", "mythology": "myths",
           "love": "romance", "poems": "poetry", "classic": "classics", "pirates": "adventure"}


@dataclass
class Book:
    id: str
    title: str
    authors: str
    seconds: int
    url: str
    chapters: list[dict]
    rss: str = ""
    language: str = ""


def genre_key(text: str) -> str | None:
    t = " ".join((text or "").lower().replace("story", "").replace("stories", "").split())
    if t in GENRES:
        return t
    if t in ALIASES:
        return ALIASES[t]
    return next((k for k in GENRES if t and (t in k or k in t)), None)


def _seconds(value) -> int:
    """'01:02:03', '12:34', '754' or 754 -> seconds."""
    if isinstance(value, (int, float)):
        return int(value)
    parts = [p for p in str(value or "").split(":") if p.strip().isdigit()]
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total


def parse_books(data) -> list[Book]:
    books = []
    for raw in (data or {}).get("books") or []:
        authors = ", ".join(" ".join(filter(None, (a.get("first_name"), a.get("last_name")))).strip()
                            for a in raw.get("authors") or [] if isinstance(a, dict))
        chapters = []
        for s in raw.get("sections") or []:
            url = s.get("listen_url") or s.get("url") or ""
            if not url.startswith("http"):
                continue
            readers = ", ".join(r.get("display_name", "") for r in s.get("readers") or [] if isinstance(r, dict))
            chapters.append({"n": int(s.get("section_number") or len(chapters) + 1), "title": s.get("title") or "",
                             "url": url, "seconds": _seconds(s.get("playtime")), "reader": readers})
        chapters.sort(key=lambda c: c["n"])
        books.append(Book(str(raw.get("id") or ""), raw.get("title") or "Untitled", authors or "Unknown author",
                          _seconds(raw.get("totaltimesecs") or raw.get("totaltime")),
                          raw.get("url_librivox") or "", chapters, raw.get("url_rss") or "",
                          raw.get("language") or ""))
    return books


def rss_chapters(xml: str) -> list[dict]:
    """Chapters from a LibriVox RSS feed, when the API didn't list them."""
    chapters = []
    for i, item in enumerate(re.findall(r"<item>(.*?)</item>", xml or "", re.S)):
        url = re.search(r"<enclosure[^>]+url=\"([^\"]+)\"", item)
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
        length = re.search(r"<itunes:duration>([^<]+)</itunes:duration>", item)
        if url:
            chapters.append({"n": i + 1, "title": (title.group(1).strip() if title else f"Part {i + 1}"),
                             "url": url.group(1), "seconds": _seconds(length.group(1) if length else 0), "reader": ""})
    return chapters


def pick_story(books: list[Book], rng: random.Random | None = None) -> Book | None:
    """An English book of story length if there is one, else any English book, else anything."""
    rng = rng or random
    english = [b for b in books if (b.language or "English").lower().startswith("english")] or books
    fits = [b for b in english if STORY_SECONDS[0] <= b.seconds <= STORY_SECONDS[1]]
    pool = fits or english
    return rng.choice(pool) if pool else None


def tracks_for(book: Book, requester: int) -> list[Track]:
    tracks = []
    for c in book.chapters[:MAX_CHAPTERS]:
        part = c["title"] if c["title"] and c["title"].lower() != book.title.lower() else f"Part {c['n']}"
        tracks.append(Track(title=f"{book.title} · {part}"[:200], url=c["url"], duration=c["seconds"] or None,
                            uploader=(f"read by {c['reader']}" if c["reader"] else book.authors)[:100],
                            requester=requester, source="librivox"))
    return tracks


class Stories(commands.Cog, name="Stories"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._cache: dict[str, tuple[float, list[Book]]] = {}

    async def fetch(self, **params) -> list[Book]:
        query = {"format": "json", "extended": "1", "limit": "50", **{k: str(v) for k, v in params.items()}}
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
            resp = await client.get(API, params=query)
            if resp.status_code == 404:      # LibriVox answers 404 when nothing matches
                return []
            resp.raise_for_status()
            return parse_books(resp.json())

    async def books_in(self, genre: str) -> list[Book]:
        hit = self._cache.get(genre)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        term = GENRES[genre][1]
        books = await self.fetch(genre=term, offset=random.choice((0, 0, 50, 100, 150)))
        if not books:
            books = await self.fetch(genre=term)
        self._cache[genre] = (time.time(), books)
        return books

    async def with_chapters(self, book: Book) -> Book:
        if book.chapters or not book.rss:
            return book
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
            resp = await client.get(book.rss)
            if resp.status_code < 400:
                book.chapters = rss_chapters(resp.text)
        return book

    async def find(self, what: str | None) -> Book | None:
        key = genre_key(what or "") if what else random.choice(list(GENRES))
        if key is not None:
            return pick_story(await self.books_in(key))
        title = " ".join((what or "").split())[:80]
        books = await self.fetch(title=f"^{title}") or await self.fetch(title=title)
        return pick_story(books) if books else None

    def genre_card(self, prefix: str) -> discord.Embed:
        lines = [f"**{label}** · `{prefix}story {key}` · {about}" for key, (label, _, about) in GENRES.items()]
        card = discord.Embed(title="📖 Stories SYKE can read you", colour=discord.Colour.dark_teal(),
                             description="\n".join(lines) + f"\n\nOr ask for a title: `{prefix}story the raven`. "
                                         "Join a voice channel first. Skip, pause and leave work like music.")
        card.set_footer(text="Public-domain audiobooks read by LibriVox volunteers")
        return card

    async def play_story(self, ctx: commands.Context, what: str | None) -> bool:
        music = self.bot.get_cog("Music")
        if music is None:
            await ctx.reply("Music isn't running right now, so I can't read you a story.", mention_author=False)
            return False
        if getattr(getattr(ctx.author, "voice", None), "channel", None) is None:
            await ctx.reply("Join a voice channel first and I'll read you something.", mention_author=False)
            return False
        try:
            book = await self.find(what)
            if book is not None:
                book = await self.with_chapters(book)
        except (httpx.HTTPError, ValueError) as error:
            log.warning("LibriVox lookup failed: %s", error)
            await ctx.reply("The story library isn't answering right now. Try again in a bit.", mention_author=False)
            return False
        if book is None or not book.chapters:
            await ctx.reply(f"Couldn't find a story for **{discord.utils.escape_markdown(what or '')[:80]}**. "
                            f"Try a genre: `{self.prefix(ctx)}story` lists them.", mention_author=False)
            return False
        tracks = tracks_for(book, ctx.author.id)
        log.info("Story in %s: %s (%d parts)", ctx.guild.id, book.title, len(tracks))
        await music.enqueue(ctx, tracks, f"{book.title} by {book.authors}", source_link=book.url)
        return True

    def prefix(self, ctx) -> str:
        return getattr(ctx, "clean_prefix", None) or self.bot.display_prefix(ctx.guild.id)

    @commands.command(name="story", aliases=["stories", "audiobook", "audiobooks", "readme"],
                      usage="[genre or title]", help="SYKE reads you a story in voice (audiobooks by genre)")
    @commands.guild_only()
    async def story(self, ctx: commands.Context, *, what: str | None = None) -> None:
        if not what:
            await ctx.reply(embed=self.genre_card(self.prefix(ctx)), mention_author=False)
            return
        async with ctx.typing():
            await self.play_story(ctx, what)
