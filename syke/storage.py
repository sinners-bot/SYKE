"""Per-server settings in SQLite. SYKE never stores message content."""

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
"""


class Storage:
    def __init__(self, path: str) -> None:
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
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
