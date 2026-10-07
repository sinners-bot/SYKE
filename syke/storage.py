"""Per-server settings in SQLite, plus the stored message corpus.

The corpus holds messages from watched channels (and anything `collect`/`scanme` read). It feeds
mimic and yap, and, for rows with timestamps, profiles too. Opted-out members' rows are deleted.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

CARD_TTL_SECONDS = 30 * 24 * 3600
CORPUS_EXTRA_COLUMNS = (
    ("created_at", "REAL"),
    ("author_name", "TEXT NOT NULL DEFAULT ''"),
    ("laugh_reactions", "INTEGER NOT NULL DEFAULT 0"),
    ("total_reactions", "INTEGER NOT NULL DEFAULT 0"),
)
# (message_id, channel_id, author_id, content[, created_at, author_name, laugh_reactions, total_reactions])
CorpusRow = tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked_channels (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);
CREATE TABLE IF NOT EXISTS optouts (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    timezone TEXT
);
CREATE TABLE IF NOT EXISTS corpus (
    guild_id   INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    author_id  INTEGER NOT NULL,
    content    TEXT NOT NULL,
    PRIMARY KEY (guild_id, message_id)
);
CREATE INDEX IF NOT EXISTS corpus_author ON corpus (guild_id, author_id);
CREATE TABLE IF NOT EXISTS message_labels (
    guild_id   INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    traits     TEXT NOT NULL,
    PRIMARY KEY (guild_id, message_id)
);
CREATE TABLE IF NOT EXISTS profile_cards (
    message_id INTEGER PRIMARY KEY,
    owner_id   INTEGER NOT NULL,
    pages      TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS yap_channels (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);
"""


class Storage:
    def __init__(self, path: str) -> None:
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(guild_settings)")}
            if "demo_mode" not in columns:
                self._conn.execute("ALTER TABLE guild_settings ADD COLUMN demo_mode INTEGER NOT NULL DEFAULT 0")
            if "prefix" not in columns:
                self._conn.execute("ALTER TABLE guild_settings ADD COLUMN prefix TEXT")
            corpus_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(corpus)")}
            for column, kind in CORPUS_EXTRA_COLUMNS:
                if column not in corpus_columns:
                    self._conn.execute(f"ALTER TABLE corpus ADD COLUMN {column} {kind}")
            self._conn.commit()

    def _write(self, sql: str, params: tuple) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def _read(self, sql: str, params: tuple) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def track(self, guild_id: int, channel_id: int) -> bool:
        return self._write(
            "INSERT OR IGNORE INTO tracked_channels VALUES (?, ?)", (guild_id, channel_id)
        ) > 0

    def untrack(self, guild_id: int, channel_id: int) -> bool:
        return self._write(
            "DELETE FROM tracked_channels WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ) > 0

    def tracked(self, guild_id: int) -> list[int]:
        rows = self._read("SELECT channel_id FROM tracked_channels WHERE guild_id = ?", (guild_id,))
        return [r[0] for r in rows]

    def tracked_counts(self) -> dict[int, int]:
        """Watched channels per guild."""
        return dict(self._read("SELECT guild_id, COUNT(*) FROM tracked_channels GROUP BY guild_id", ()))

    def tracked_total(self) -> int:
        return self._read("SELECT COUNT(*) FROM tracked_channels", ())[0][0]

    def corpus_total(self) -> int:
        return self._read("SELECT COUNT(*) FROM corpus", ())[0][0]

    def set_optout(self, guild_id: int, user_id: int, opted_out: bool) -> bool:
        if opted_out:
            self.forget(guild_id, user_id)
            return self._write("INSERT OR IGNORE INTO optouts VALUES (?, ?)", (guild_id, user_id)) > 0
        return self._write(
            "DELETE FROM optouts WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ) > 0

    def optouts(self, guild_id: int) -> set[int]:
        rows = self._read("SELECT user_id FROM optouts WHERE guild_id = ?", (guild_id,))
        return {r[0] for r in rows}

    def timezone(self, guild_id: int) -> str | None:
        rows = self._read("SELECT timezone FROM guild_settings WHERE guild_id = ?", (guild_id,))
        return rows[0][0] if rows else None

    def set_timezone(self, guild_id: int, tz_name: str) -> None:
        self._write(
            "INSERT INTO guild_settings (guild_id, timezone) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET timezone = excluded.timezone",
            (guild_id, tz_name),
        )

    def prefix(self, guild_id: int) -> str | None:
        rows = self._read("SELECT prefix FROM guild_settings WHERE guild_id = ?", (guild_id,))
        return rows[0][0] if rows else None

    def set_prefix(self, guild_id: int, prefix: str | None) -> None:
        self._write(
            "INSERT INTO guild_settings (guild_id, prefix) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET prefix = excluded.prefix",
            (guild_id, prefix),
        )

    def add_corpus(self, guild_id: int, rows: list[CorpusRow]) -> int:
        """Store messages; returns how many were new. Known rows gain any timestamp/reactions they lacked."""
        if not rows:
            return 0
        full = [(guild_id, *row, *(None, "", 0, 0)[len(row) - 4:]) for row in rows]
        with self._lock:
            before = self._conn.execute("SELECT COUNT(*) FROM corpus WHERE guild_id = ?", (guild_id,)).fetchone()[0]
            self._conn.executemany(
                "INSERT INTO corpus (guild_id, message_id, channel_id, author_id, content, created_at, "
                "author_name, laugh_reactions, total_reactions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, message_id) DO UPDATE SET "
                "created_at = COALESCE(corpus.created_at, excluded.created_at), "
                "author_name = CASE WHEN excluded.author_name != '' THEN excluded.author_name ELSE corpus.author_name END, "
                "laugh_reactions = MAX(corpus.laugh_reactions, excluded.laugh_reactions), "
                "total_reactions = MAX(corpus.total_reactions, excluded.total_reactions)",
                full,
            )
            self._conn.commit()
            after = self._conn.execute("SELECT COUNT(*) FROM corpus WHERE guild_id = ?", (guild_id,)).fetchone()[0]
            return after - before

    def history(self, guild_id: int, channel_ids: list[int]) -> list[tuple]:
        """Timestamped stored messages from these channels:
        (message_id, channel_id, author_id, author_name, content, created_at, laugh_reactions, total_reactions)."""
        if not channel_ids:
            return []
        marks = ", ".join("?" for _ in channel_ids)
        return self._read(
            "SELECT message_id, channel_id, author_id, author_name, content, created_at, laugh_reactions, "
            f"total_reactions FROM corpus WHERE guild_id = ? AND created_at IS NOT NULL AND channel_id IN ({marks})",
            (guild_id, *channel_ids),
        )

    def save_labels(self, guild_id: int, labels: dict[int, frozenset[str]]) -> None:
        if not labels:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO message_labels VALUES (?, ?, ?)",
                [(guild_id, mid, ",".join(sorted(traits))) for mid, traits in labels.items()],
            )
            self._conn.commit()

    def labels(self, guild_id: int) -> dict[int, frozenset[str]]:
        rows = self._read("SELECT message_id, traits FROM message_labels WHERE guild_id = ?", (guild_id,))
        return {mid: frozenset(t for t in traits.split(",") if t) for mid, traits in rows}

    def corpus(self, guild_id: int, author_id: int | None = None) -> list[tuple[int, str]]:
        """(author_id, content) pairs, for one member or the whole server."""
        if author_id is None:
            return self._read("SELECT author_id, content FROM corpus WHERE guild_id = ?", (guild_id,))
        return self._read(
            "SELECT author_id, content FROM corpus WHERE guild_id = ? AND author_id = ?", (guild_id, author_id)
        )

    def corpus_contents(self, guild_id: int, limit: int) -> list[str]:
        """Newest stored messages, for building the server's vocabulary and Markov voice."""
        rows = self._read(
            "SELECT content FROM corpus WHERE guild_id = ? AND length(content) BETWEEN 3 AND 300 "
            "ORDER BY rowid DESC LIMIT ?",
            (guild_id, limit),
        )
        return [r[0] for r in rows]

    def random_messages(self, guild_id: int, count: int, exclude_id: int = 0) -> list[str]:
        rows = self._read(
            "SELECT content FROM corpus WHERE guild_id = ? AND message_id != ? "
            "AND length(content) BETWEEN 8 AND 300 ORDER BY RANDOM() LIMIT ?",
            (guild_id, exclude_id, count),
        )
        return [r[0] for r in rows]

    def labelled_messages(self, guild_id: int, trait: str, limit: int) -> list[str]:
        """Random stored messages the AI tagged with `trait` while roasting someone."""
        rows = self._read(
            "SELECT c.content FROM corpus c JOIN message_labels l "
            "ON l.guild_id = c.guild_id AND l.message_id = c.message_id "
            "WHERE c.guild_id = ? AND (',' || l.traits || ',') LIKE ? "
            "AND length(c.content) BETWEEN 3 AND 300 ORDER BY RANDOM() LIMIT ?",
            (guild_id, f"%,{trait},%", limit),
        )
        return [r[0] for r in rows]

    def search_messages(self, guild_id: int, words: list[str], limit: int) -> list[str]:
        """Random stored messages containing any of `words`."""
        if not words:
            return []
        likes = " OR ".join("content LIKE ? ESCAPE '\\'" for _ in words)
        escaped = ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for w in words]
        rows = self._read(
            f"SELECT content FROM corpus WHERE guild_id = ? AND length(content) BETWEEN 3 AND 300 "
            f"AND ({likes}) ORDER BY RANDOM() LIMIT ?",
            (guild_id, *escaped, limit),
        )
        return [r[0] for r in rows]

    def set_yap(self, guild_id: int, channel_id: int, enabled: bool) -> bool:
        if enabled:
            return self._write("INSERT OR IGNORE INTO yap_channels VALUES (?, ?)", (guild_id, channel_id)) > 0
        return self._write(
            "DELETE FROM yap_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)
        ) > 0

    def yap_channels(self, guild_id: int) -> set[int]:
        rows = self._read("SELECT channel_id FROM yap_channels WHERE guild_id = ?", (guild_id,))
        return {r[0] for r in rows}

    def corpus_size(self, guild_id: int) -> tuple[int, int]:
        """(messages, distinct authors) collected for a server."""
        rows = self._read("SELECT COUNT(*), COUNT(DISTINCT author_id) FROM corpus WHERE guild_id = ?", (guild_id,))
        return rows[0][0], rows[0][1]

    def forget(self, guild_id: int, author_id: int | None = None) -> int:
        if author_id is None:
            return self._write("DELETE FROM corpus WHERE guild_id = ?", (guild_id,))
        return self._write("DELETE FROM corpus WHERE guild_id = ? AND author_id = ?", (guild_id, author_id))

    def save_card(self, message_id: int, owner_id: int, pages: dict[str, dict]) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM profile_cards WHERE created_at < ?", (now - CARD_TTL_SECONDS,))
            self._conn.execute(
                "INSERT OR REPLACE INTO profile_cards VALUES (?, ?, ?, ?)",
                (message_id, owner_id, json.dumps(pages), now),
            )
            self._conn.commit()

    def card(self, message_id: int) -> tuple[int, dict[str, dict]] | None:
        rows = self._read(
            "SELECT owner_id, pages FROM profile_cards WHERE message_id = ? AND created_at >= ?",
            (message_id, time.time() - CARD_TTL_SECONDS),
        )
        return (rows[0][0], json.loads(rows[0][1])) if rows else None

    def demo_mode(self, guild_id: int) -> bool:
        rows = self._read("SELECT demo_mode FROM guild_settings WHERE guild_id = ?", (guild_id,))
        return bool(rows and rows[0][0])

    def set_demo_mode(self, guild_id: int, enabled: bool) -> None:
        self._write(
            "INSERT INTO guild_settings (guild_id, demo_mode) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET demo_mode = excluded.demo_mode",
            (guild_id, int(enabled)),
        )
