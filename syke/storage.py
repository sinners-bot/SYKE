"""Per-server settings in SQLite, plus the text corpus used by the Markov commands.

The corpus only holds messages from watched channels, and opted-out members' rows are deleted.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

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

    def add_corpus(self, guild_id: int, rows: list[tuple[int, int, int, str]]) -> int:
        """Store (message_id, channel_id, author_id, content) rows; returns how many were new."""
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO corpus VALUES (?, ?, ?, ?, ?)",
                [(guild_id, *row) for row in rows],
            )
            self._conn.commit()
            return self._conn.total_changes - before

    def corpus(self, guild_id: int, author_id: int | None = None) -> list[tuple[int, str]]:
        """(author_id, content) pairs, for one member or the whole server."""
        if author_id is None:
            return self._read("SELECT author_id, content FROM corpus WHERE guild_id = ?", (guild_id,))
        return self._read(
            "SELECT author_id, content FROM corpus WHERE guild_id = ? AND author_id = ?", (guild_id, author_id)
        )

    def random_messages(self, guild_id: int, count: int, exclude_id: int = 0) -> list[str]:
        rows = self._read(
            "SELECT content FROM corpus WHERE guild_id = ? AND message_id != ? "
            "AND length(content) BETWEEN 8 AND 300 ORDER BY RANDOM() LIMIT ?",
            (guild_id, exclude_id, count),
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

    def demo_mode(self, guild_id: int) -> bool:
        rows = self._read("SELECT demo_mode FROM guild_settings WHERE guild_id = ?", (guild_id,))
        return bool(rows and rows[0][0])

    def set_demo_mode(self, guild_id: int, enabled: bool) -> None:
        self._write(
            "INSERT INTO guild_settings (guild_id, demo_mode) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET demo_mode = excluded.demo_mode",
            (guild_id, int(enabled)),
        )
