"""Archive SYKE's memory: export a server's stored messages to a file you keep, and import it back.

The file is gzip-compressed JSON lines:
  {"type": "syke-memory", "version": 1, "guild_id": …, "guild_name": …, "exported_at": …, "messages": N}
  {"m": [message_id, channel_id, author_id, content, created_at, author_name, laugh_reactions, total_reactions]}
  {"l": [message_id, "trait,trait"]}          (the AI's trait labels, when there are any)

Exports are written next to the database (on the Railway volume, so they survive redeploys) in an
`exports` folder, and can be downloaded from the owner dashboard. Importing adds messages SYKE doesn't
have yet and never duplicates; it can load into the same server or a different one.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

FORMAT = "syke-memory"
VERSION = 1
PAGE = 5000
NAME = re.compile(r"^syke-memory-\d+-\d{8}-\d{6}\.jsonl\.gz$")


def export_dir(db_path: str) -> Path:
    folder = Path(db_path).resolve().parent / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def safe_path(db_path: str, name: str) -> Path | None:
    """An export file by name, only if it's one of ours (no paths, no other files)."""
    if not NAME.match(name or ""):
        return None
    path = export_dir(db_path) / name
    return path if path.is_file() else None


def iter_messages(storage, guild_id: int):
    last = -1
    while True:
        rows = storage._read(
            "SELECT message_id, channel_id, author_id, content, created_at, author_name, laugh_reactions, "
            "total_reactions FROM corpus WHERE guild_id = ? AND message_id > ? ORDER BY message_id LIMIT ?",
            (guild_id, last, PAGE))
        if not rows:
            return
        yield from rows
        last = rows[-1][0]


def export_guild(storage, db_path: str, guild_id: int, guild_name: str) -> tuple[Path, int, int]:
    """Write the export file. Returns (path, messages, bytes)."""
    count = storage._read("SELECT COUNT(*) FROM corpus WHERE guild_id = ?", (guild_id,))[0][0]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = export_dir(db_path) / f"syke-memory-{guild_id}-{stamp}.jsonl.gz"
    tmp = path.with_suffix(".tmp")
    written = 0
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as out:
        out.write(json.dumps({"type": FORMAT, "version": VERSION, "guild_id": guild_id, "guild_name": guild_name,
                              "exported_at": time.time(), "messages": count}) + "\n")
        for row in iter_messages(storage, guild_id):
            out.write(json.dumps({"m": list(row)}, ensure_ascii=False) + "\n")
            written += 1
        for message_id, traits in storage._read(
                "SELECT message_id, traits FROM message_labels WHERE guild_id = ?", (guild_id,)):
            out.write(json.dumps({"l": [message_id, traits]}) + "\n")
    os.replace(tmp, path)
    return path, written, path.stat().st_size


def read_header(data: bytes) -> dict | None:
    try:
        with gzip.open(io.BytesIO(data), "rt", encoding="utf-8") as f:
            header = json.loads(f.readline())
    except (OSError, ValueError, EOFError):
        return None
    return header if header.get("type") == FORMAT else None


def import_file(storage, source, guild_id: int, batch: int = 2000) -> tuple[int, int, dict]:
    """Load an export (a path or the file's bytes) into `guild_id`. Returns (read, new, header)."""
    handle = gzip.open(source, "rt", encoding="utf-8") if isinstance(source, (str, Path)) else \
        gzip.open(io.BytesIO(source), "rt", encoding="utf-8")
    read = added = 0
    rows: list[tuple] = []
    labels: dict[int, frozenset[str]] = {}
    with handle as f:
        try:
            header = json.loads(f.readline())
        except ValueError as error:
            raise ValueError("That isn't a SYKE memory file.") from error
        if header.get("type") != FORMAT:
            raise ValueError("That isn't a SYKE memory file.")
        if int(header.get("version", 0)) > VERSION:
            raise ValueError("That file comes from a newer SYKE. Update SYKE first.")
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            if "m" in item:
                m = item["m"]
                rows.append((int(m[0]), int(m[1]), int(m[2]), str(m[3]), m[4], m[5] or "", int(m[6] or 0),
                             int(m[7] or 0)))
                read += 1
                if len(rows) >= batch:
                    added += storage.add_corpus(guild_id, rows)
                    rows = []
            elif "l" in item:
                labels[int(item["l"][0])] = frozenset(t for t in str(item["l"][1]).split(",") if t)
    if rows:
        added += storage.add_corpus(guild_id, rows)
    if labels:
        storage.save_labels(guild_id, labels)
    return read, added, header


def stats(storage, guild_id: int) -> dict:
    count, authors, channels, first, last = storage._read(
        "SELECT COUNT(*), COUNT(DISTINCT author_id), COUNT(DISTINCT channel_id), MIN(created_at), MAX(created_at) "
        "FROM corpus WHERE guild_id = ?", (guild_id,))[0]
    size = storage._read("SELECT COALESCE(SUM(length(content)), 0) FROM corpus WHERE guild_id = ?", (guild_id,))[0][0]
    return {"messages": count, "authors": authors, "channels": channels, "first": first, "last": last,
            "text_bytes": size}


def list_exports(db_path: str, guild_id: int | None = None) -> list[tuple[str, int, float]]:
    """(name, bytes, modified) of saved exports, newest first."""
    found = []
    for path in export_dir(db_path).glob("syke-memory-*.jsonl.gz"):
        if guild_id is not None and not path.name.startswith(f"syke-memory-{guild_id}-"):
            continue
        stat = path.stat()
        found.append((path.name, stat.st_size, stat.st_mtime))
    return sorted(found, key=lambda f: -f[2])


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# Discord commands (SYKE owners only)

import asyncio  # noqa: E402

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

ATTACH_LIMIT = 8 * 1024 * 1024


class MemoryArchive(commands.Cog, name="MemoryArchive"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._busy: set[int] = set()

    @property
    def db_path(self) -> str:
        return self.bot.settings.db_path

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    def download_url(self, guild_id: int, name: str) -> str | None:
        base = (self.bot.settings.web_base_url or "").rstrip("/")
        return f"{base}/g/{guild_id}/memory/export/{name}" if base else None

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.author.id in self.bot.settings.owner_ids:
            return True
        from .moderation import problem
        await ctx.reply(embed=problem("Owners only", "Only SYKE's owners can archive its memory."))
        return False

    @commands.group(name="memory", aliases=["archive", "mem"], invoke_without_command=True, case_insensitive=True,
                    help="SYKE owners: what SYKE remembers here, and export/import it")
    @commands.guild_only()
    async def memory(self, ctx: commands.Context) -> None:
        from .moderation import embed
        info = await asyncio.to_thread(stats, self.bot.storage, ctx.guild.id)
        p = self.p(ctx)
        span = "nothing yet"
        if info["first"]:
            span = f"<t:{int(info['first'])}:D> → <t:{int(info['last'])}:D>"
        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        saved = list_exports(self.db_path, ctx.guild.id)
        card = embed("🧠 SYKE's memory of this server",
                     f"**{info['messages']:,}** messages from **{info['authors']:,}** members in "
                     f"**{info['channels']:,}** channels\n{span}\n"
                     f"Text: {human_size(info['text_bytes'])} · whole database: {human_size(db_size)}\n"
                     f"Saved exports here: **{len(saved)}**")
        card.set_footer(text=f"{p}memory export · {p}memory exports · {p}memory import · {p}memory delete <file>")
        await ctx.reply(embed=card)

    @memory.command(name="stats", aliases=["info"])
    async def memory_stats(self, ctx: commands.Context) -> None:
        await self.memory.callback(self, ctx)

    @memory.command(name="export", aliases=["save", "backup"])
    async def memory_export(self, ctx: commands.Context) -> None:
        from .moderation import OK, embed, problem
        if ctx.guild.id in self._busy:
            await ctx.reply(embed=problem("Already busy", "An export or import is running for this server."))
            return
        self._busy.add(ctx.guild.id)
        try:
            async with ctx.typing():
                path, count, size = await asyncio.to_thread(export_guild, self.bot.storage, self.db_path,
                                                            ctx.guild.id, ctx.guild.name)
        finally:
            self._busy.discard(ctx.guild.id)
        url = self.download_url(ctx.guild.id, path.name)
        body = (f"**{count:,}** messages saved as `{path.name}` ({human_size(size)}), kept on SYKE's disk.\n"
                + (f"[Download it from the dashboard]({url}) (log in as an owner)." if url else
                   "Set `SYKE_WEB_BASE_URL` to get a download link from the dashboard."))
        card = embed("📦 Memory exported", body, OK)
        sent_file = False
        if size <= ATTACH_LIMIT:
            try:
                await ctx.author.send(embed=card, file=discord.File(path, filename=path.name))
                sent_file = True
            except discord.HTTPException:
                pass
        if sent_file:
            card.description += "\nThe file is in your DMs too."
        await ctx.reply(embed=card)

    @memory.command(name="exports", aliases=["list", "files"])
    async def memory_exports(self, ctx: commands.Context) -> None:
        from .moderation import embed
        saved = list_exports(self.db_path, ctx.guild.id)
        if not saved:
            await ctx.reply(embed=embed("📦 No exports yet", f"Make one with `{self.p(ctx)}memory export`."))
            return
        lines = []
        for name, size, modified in saved[:15]:
            url = self.download_url(ctx.guild.id, name)
            label = f"[{name}]({url})" if url else f"`{name}`"
            lines.append(f"{label} · {human_size(size)} · <t:{int(modified)}:R>")
        await ctx.reply(embed=embed("📦 Saved exports", "\n".join(lines)))

    @memory.command(name="import", aliases=["load", "restore"])
    async def memory_import(self, ctx: commands.Context, name: str = "") -> None:
        from .moderation import OK, embed, problem
        source = None
        if ctx.message is not None and ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            if not attachment.filename.endswith(".jsonl.gz"):
                await ctx.reply(embed=problem("Wrong file", "Attach a `syke-memory-….jsonl.gz` export."))
                return
            source = await attachment.read()
        elif name:
            path = safe_path(self.db_path, name.strip())
            if path is None:
                await ctx.reply(embed=problem("No such export", f"See `{self.p(ctx)}memory exports` for the names."))
                return
            source = path
        else:
            await ctx.reply(embed=problem("Import what?", f"Attach an export file to `{self.p(ctx)}memory import`, "
                                                          f"or give a saved one's name."))
            return
        if ctx.guild.id in self._busy:
            await ctx.reply(embed=problem("Already busy", "An export or import is running for this server."))
            return
        self._busy.add(ctx.guild.id)
        try:
            async with ctx.typing():
                read, added, header = await asyncio.to_thread(import_file, self.bot.storage, source, ctx.guild.id)
        except (ValueError, OSError) as error:
            await ctx.reply(embed=problem("Couldn't import", str(error)[:300] or "The file is damaged."))
            return
        finally:
            self._busy.discard(ctx.guild.id)
        self.bot.bump_history(ctx.guild.id)
        origin = header.get("guild_name") or header.get("guild_id")
        note = "" if header.get("guild_id") == ctx.guild.id else f" (exported from **{origin}**)"
        await ctx.reply(embed=embed("📥 Memory imported", f"Read **{read:,}** messages{note}, **{added:,}** new.", OK))

    @memory.command(name="delete", aliases=["remove", "rm"])
    async def memory_delete(self, ctx: commands.Context, name: str) -> None:
        from .moderation import WARN, embed, problem
        path = safe_path(self.db_path, name.strip())
        if path is None or not path.name.startswith(f"syke-memory-{ctx.guild.id}-"):
            await ctx.reply(embed=problem("No such export", f"See `{self.p(ctx)}memory exports`."))
            return
        path.unlink()
        await ctx.reply(embed=embed("Deleted", f"`{path.name}` is gone. SYKE's memory itself is untouched.", WARN))
