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
CORPUS_CHUNK = 500
CORPUS_EXTRA_COLUMNS = (
    ("created_at", "REAL"),
    ("author_name", "TEXT NOT NULL DEFAULT ''"),
    ("laugh_reactions", "INTEGER NOT NULL DEFAULT 0"),
    ("total_reactions", "INTEGER NOT NULL DEFAULT 0"),
    ("reply_to", "INTEGER NOT NULL DEFAULT 0"),     # author the message replied to (0 = not a reply / unknown)
)
# Timers added to econ_wallets after it first shipped (migrated in on startup).
WALLET_EXTRA_COLUMNS = (
    ("rob_at", "REAL NOT NULL DEFAULT 0"),
    ("crime_at", "REAL NOT NULL DEFAULT 0"),
    ("heist_at", "REAL NOT NULL DEFAULT 0"),
    ("jail_until", "REAL NOT NULL DEFAULT 0"),
    ("week_at", "REAL NOT NULL DEFAULT 0"),
    ("week_streak", "INTEGER NOT NULL DEFAULT 0"),
    ("job_at", "REAL NOT NULL DEFAULT 0"),
    ("job_shifts", "INTEGER NOT NULL DEFAULT 0"),
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
CREATE TABLE IF NOT EXISTS expressions (
    guild_id      INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    expression_id INTEGER NOT NULL,
    name          TEXT NOT NULL,
    added_by      INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    PRIMARY KEY (guild_id, kind, expression_id)
);
CREATE TABLE IF NOT EXISTS joins (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    inviter_id   INTEGER,
    inviter_name TEXT NOT NULL DEFAULT '',
    code         TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL,
    joined_at    REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS verification (
    guild_id         INTEGER PRIMARY KEY,
    role_id          INTEGER,
    remove_role_id   INTEGER,
    channel_id       INTEGER,
    text             TEXT,
    panel_channel_id INTEGER,
    panel_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS restrictions (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    expires_at   REAL,
    reason       TEXT NOT NULL DEFAULT '',
    moderator_id INTEGER NOT NULL,
    created_at   REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id, kind)
);
CREATE TABLE IF NOT EXISTS restrict_roles (
    guild_id INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, kind)
);
CREATE TABLE IF NOT EXISTS mod_cases (
    guild_id     INTEGER NOT NULL,
    number       INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    action       TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    duration     REAL,
    created_at   REAL NOT NULL,
    PRIMARY KEY (guild_id, number)
);
CREATE INDEX IF NOT EXISTS mod_cases_user ON mod_cases (guild_id, user_id);
CREATE TABLE IF NOT EXISTS guild_kv (
    guild_id INTEGER NOT NULL,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS guess_scores (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    points   INTEGER NOT NULL DEFAULT 0,
    played   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS locked_channels (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    previous   TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (guild_id, channel_id)
);
CREATE TABLE IF NOT EXISTS temp_bans (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    unban_at     REAL NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    moderator_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS effects (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    effect       TEXT NOT NULL,
    moderator_id INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS music_plays (
    guild_id   INTEGER NOT NULL,
    played_at  REAL NOT NULL,
    title      TEXT NOT NULL,
    url        TEXT NOT NULL DEFAULT '',
    requester  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS music_plays_time ON music_plays (guild_id, played_at);
CREATE TABLE IF NOT EXISTS econ_wallets (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    cash      INTEGER NOT NULL DEFAULT 0,
    chips     INTEGER NOT NULL DEFAULT 0,
    bank      INTEGER NOT NULL DEFAULT 0,
    streak    INTEGER NOT NULL DEFAULT 0,
    daily_at  REAL NOT NULL DEFAULT 0,
    work_at   REAL NOT NULL DEFAULT 0,
    hustle_at REAL NOT NULL DEFAULT 0,
    seen      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS econ_items (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    item     TEXT NOT NULL,
    qty      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, item)
);
CREATE TABLE IF NOT EXISTS econ_buffs (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    buff     TEXT NOT NULL,
    until    REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id, buff)
);
CREATE TABLE IF NOT EXISTS econ_shops (
    guild_id     INTEGER NOT NULL,
    shop         TEXT NOT NULL,
    owner_id     INTEGER NOT NULL,
    level        INTEGER NOT NULL DEFAULT 1,
    invested     INTEGER NOT NULL DEFAULT 0,
    register     INTEGER NOT NULL DEFAULT 0,
    collected_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, shop)
);
CREATE TABLE IF NOT EXISTS persona_bonds (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    score      REAL NOT NULL DEFAULT 0,
    updated    REAL NOT NULL DEFAULT 0,
    insults    INTEGER NOT NULL DEFAULT 0,
    kindness   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS persona_events (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    weight     REAL NOT NULL DEFAULT 1,
    note       TEXT NOT NULL DEFAULT '',
    at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS persona_events_user ON persona_events (guild_id, user_id, at);
CREATE INDEX IF NOT EXISTS persona_events_time ON persona_events (guild_id, at);
CREATE TABLE IF NOT EXISTS persona_mood (
    guild_id   INTEGER PRIMARY KEY,
    mood       REAL NOT NULL DEFAULT 0,
    updated    REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS persona_promises (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0,
    text       TEXT NOT NULL,
    made_at    REAL NOT NULL,
    due        REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS persona_promises_user ON persona_promises (guild_id, user_id, status);
CREATE TABLE IF NOT EXISTS polls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0,
    author_id  INTEGER NOT NULL,
    question   TEXT NOT NULL,
    options    TEXT NOT NULL,
    anonymous  INTEGER NOT NULL DEFAULT 0,
    multi      INTEGER NOT NULL DEFAULT 0,
    blind      INTEGER NOT NULL DEFAULT 0,
    style      TEXT NOT NULL DEFAULT 'buttons',
    created    REAL NOT NULL,
    ends_at    REAL,
    closed     INTEGER NOT NULL DEFAULT 0,
    prediction TEXT NOT NULL DEFAULT '',
    predicted  INTEGER NOT NULL DEFAULT -1
);
CREATE INDEX IF NOT EXISTS polls_open ON polls (closed, ends_at);
CREATE TABLE IF NOT EXISTS poll_votes (
    poll_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    option     INTEGER NOT NULL,
    at         REAL NOT NULL,
    PRIMARY KEY (poll_id, user_id, option)
);
CREATE TABLE IF NOT EXISTS questions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0,
    author_id  INTEGER NOT NULL,
    question   TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'names',
    created    REAL NOT NULL,
    ends_at    REAL,
    closed     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS questions_open ON questions (closed, ends_at);
CREATE TABLE IF NOT EXISTS question_answers (
    question_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    answer      TEXT NOT NULL,
    at          REAL NOT NULL,
    PRIMARY KEY (question_id, user_id)
);
CREATE TABLE IF NOT EXISTS liked_songs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    song_key   TEXT NOT NULL,
    title      TEXT NOT NULL,
    url        TEXT NOT NULL DEFAULT '',
    query      TEXT NOT NULL DEFAULT '',
    uploader   TEXT NOT NULL DEFAULT '',
    duration   REAL,
    thumbnail  TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    origin     TEXT NOT NULL DEFAULT '',
    position   REAL NOT NULL,
    added_at   REAL NOT NULL,
    UNIQUE (user_id, song_key)
);
CREATE INDEX IF NOT EXISTS liked_songs_user ON liked_songs (user_id, position);
CREATE TABLE IF NOT EXISTS rivalries (
    guild_id   INTEGER NOT NULL,
    a_id       INTEGER NOT NULL,
    b_id       INTEGER NOT NULL,
    rounds     INTEGER NOT NULL DEFAULT 0,
    a_wins     INTEGER NOT NULL DEFAULT 0,
    b_wins     INTEGER NOT NULL DEFAULT 0,
    heat       REAL NOT NULL DEFAULT 0,
    topic      TEXT NOT NULL DEFAULT '',
    channel_id INTEGER NOT NULL DEFAULT 0,
    started    REAL NOT NULL,
    last_at    REAL NOT NULL,
    round_winner INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, a_id, b_id)
);
CREATE TABLE IF NOT EXISTS rivalry_rounds (
    guild_id   INTEGER NOT NULL,
    a_id       INTEGER NOT NULL,
    b_id       INTEGER NOT NULL,
    at         REAL NOT NULL,
    winner     INTEGER NOT NULL DEFAULT 0,
    topic      TEXT NOT NULL DEFAULT '',
    heat       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS rivalry_rounds_pair ON rivalry_rounds (guild_id, a_id, b_id, at);
CREATE TABLE IF NOT EXISTS achievements (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    badge       TEXT NOT NULL,
    unlocked_at REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id, badge)
);
CREATE TABLE IF NOT EXISTS achievement_counts (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    counter  TEXT NOT NULL,
    value    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, counter)
);
CREATE TABLE IF NOT EXISTS persona_diary (
    guild_id   INTEGER NOT NULL,
    day        TEXT NOT NULL,
    text       TEXT NOT NULL,
    written    REAL NOT NULL,
    PRIMARY KEY (guild_id, day)
);
"""


class Storage:
    def __init__(self, path: str) -> None:
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._local = threading.local()
        # Writes take this lock; reads don't. Each thread has its own connection, and WAL mode lets
        # them read while another thread writes, so the bot's event loop never waits on a long
        # background write (a big scan) just to look up a prefix or a watched channel.
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(guild_settings)")}
            if "demo_mode" not in columns:
                self._conn.execute("ALTER TABLE guild_settings ADD COLUMN demo_mode INTEGER NOT NULL DEFAULT 0")
            if "prefix" not in columns:
                self._conn.execute("ALTER TABLE guild_settings ADD COLUMN prefix TEXT")
            if "modlog_channel" not in columns:
                self._conn.execute("ALTER TABLE guild_settings ADD COLUMN modlog_channel INTEGER")
            wallet_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(econ_wallets)")}
            for column, kind in WALLET_EXTRA_COLUMNS:
                if column not in wallet_columns:
                    self._conn.execute(f"ALTER TABLE econ_wallets ADD COLUMN {column} {kind}")
            corpus_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(corpus)")}
            for column, kind in CORPUS_EXTRA_COLUMNS:
                if column not in corpus_columns:
                    self._conn.execute(f"ALTER TABLE corpus ADD COLUMN {column} {kind}")
            self._conn.commit()

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, check_same_thread=False, timeout=30)
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _write(self, sql: str, params: tuple) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def _insert(self, sql: str, params: tuple) -> int:
        """Run an INSERT and return the new row's id."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def _read(self, sql: str, params: tuple) -> list[tuple]:
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

    def remember_expression(self, guild_id: int, kind: str, expression_id: int, name: str, added_by: int) -> None:
        """Remember who added an emoji or sticker through SYKE. Renames keep the original adder."""
        self._write(
            "INSERT INTO expressions (guild_id, kind, expression_id, name, added_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, kind, expression_id) DO UPDATE SET name = excluded.name",
            (guild_id, kind, expression_id, name, added_by, time.time()),
        )

    def expression_added_by(self, guild_id: int, kind: str, expression_id: int) -> int | None:
        rows = self._read(
            "SELECT added_by FROM expressions WHERE guild_id = ? AND kind = ? AND expression_id = ?",
            (guild_id, kind, expression_id),
        )
        return rows[0][0] if rows else None

    def rename_expression(self, guild_id: int, kind: str, expression_id: int, name: str) -> None:
        self._write(
            "UPDATE expressions SET name = ? WHERE guild_id = ? AND kind = ? AND expression_id = ?",
            (name, guild_id, kind, expression_id),
        )

    def forget_expression(self, guild_id: int, kind: str, expression_id: int) -> None:
        self._write(
            "DELETE FROM expressions WHERE guild_id = ? AND kind = ? AND expression_id = ?",
            (guild_id, kind, expression_id),
        )

    def set_prefix(self, guild_id: int, prefix: str | None) -> None:
        self._write(
            "INSERT INTO guild_settings (guild_id, prefix) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET prefix = excluded.prefix",
            (guild_id, prefix),
        )

    def add_corpus(self, guild_id: int, rows: list[CorpusRow]) -> int:
        """Store messages; returns how many were new. Known rows gain any timestamp/reactions they lacked.

        Big batches go in chunks, so the write lock is never held for long.
        """
        if not rows:
            return 0
        defaults = (None, "", 0, 0, 0)   # created_at, author_name, laughs, reactions, reply_to
        full = [(guild_id, *row[:4], *row[4:9], *defaults[max(0, len(row) - 4):]) for row in rows]
        added = 0
        for start in range(0, len(full), CORPUS_CHUNK):
            chunk = full[start:start + CORPUS_CHUNK]
            with self._lock:
                before = self._conn.total_changes
                self._conn.executemany(
                    "INSERT INTO corpus (guild_id, message_id, channel_id, author_id, content, created_at, "
                    "author_name, laugh_reactions, total_reactions, reply_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, message_id) DO NOTHING",
                    chunk,
                )
                added += self._conn.total_changes - before
                self._conn.executemany(
                    "UPDATE corpus SET "
                    "created_at = COALESCE(created_at, ?), "
                    "author_name = CASE WHEN ? != '' THEN ? ELSE author_name END, "
                    "laugh_reactions = MAX(laugh_reactions, ?), "
                    "total_reactions = MAX(total_reactions, ?), "
                    "reply_to = CASE WHEN reply_to = 0 THEN ? ELSE reply_to END "
                    "WHERE guild_id = ? AND message_id = ?",
                    [(r[5], r[6], r[6], r[7], r[8], r[9] or 0, r[0], r[1]) for r in chunk],
                )
                self._conn.commit()
        return added

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

    def author_rows(self, guild_id: int, author_id: int, since: float) -> list[tuple]:
        """(message_id, channel_id, content, created_at, laugh_reactions, total_reactions) for one member."""
        return self._read(
            "SELECT message_id, channel_id, content, created_at, laugh_reactions, total_reactions FROM corpus "
            "WHERE guild_id = ? AND author_id = ? AND created_at IS NOT NULL AND created_at >= ?",
            (guild_id, author_id, since),
        )

    def channel_activity(self, guild_id: int, channel_ids: list[int], since: float, limit: int = 250_000) -> list[tuple]:
        """(channel_id, author_id, author_name, created_at) in these channels, oldest first."""
        if not channel_ids:
            return []
        marks = ", ".join("?" for _ in channel_ids)
        return self._read(
            "SELECT channel_id, author_id, author_name, created_at FROM corpus WHERE guild_id = ? "
            f"AND created_at IS NOT NULL AND created_at >= ? AND channel_id IN ({marks}) "
            "ORDER BY created_at LIMIT ?",
            (guild_id, since, *channel_ids, limit),
        )

    def reply_counts(self, guild_id: int, author_id: int, since: float) -> list[tuple[int, int]]:
        """(user replied to, how often) for someone's messages since a time."""
        return self._read("SELECT reply_to, COUNT(*) AS n FROM corpus WHERE guild_id = ? AND author_id = ? "
                          "AND created_at >= ? AND reply_to != 0 AND reply_to != author_id GROUP BY reply_to "
                          "ORDER BY n DESC", (guild_id, author_id, since))

    def word_sample(self, guild_id: int, since: float, limit: int = 30000) -> list[str]:
        """The newest stored messages' text (for a server-wide word baseline)."""
        return [r[0] for r in self._read("SELECT content FROM corpus WHERE guild_id = ? AND created_at >= ? "
                                         "ORDER BY created_at DESC LIMIT ?", (guild_id, since, limit))]

    def author_counts(self, guild_id: int, since: float) -> list[tuple[int, int]]:
        """(author_id, messages) since a time, busiest first."""
        return self._read(
            "SELECT author_id, COUNT(*) AS n FROM corpus WHERE guild_id = ? AND created_at >= ? "
            "GROUP BY author_id ORDER BY n DESC", (guild_id, since),
        )

    def mentions_since(self, guild_id: int, user_id: int, since: float, limit: int = 20) -> list[tuple]:
        """(message_id, channel_id, author_id, author_name, content, created_at) mentioning someone."""
        return self._read(
            "SELECT message_id, channel_id, author_id, author_name, content, created_at FROM corpus "
            "WHERE guild_id = ? AND created_at >= ? AND author_id != ? AND (content LIKE ? OR content LIKE ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (guild_id, since, user_id, f"%<@{user_id}>%", f"%<@!{user_id}>%", limit),
        )

    def record_play(self, guild_id: int, title: str, url: str, requester: int) -> None:
        self._write("INSERT INTO music_plays VALUES (?, ?, ?, ?, ?)", (guild_id, time.time(), title[:200], url or "",
                                                                        requester or 0))

    def top_songs(self, guild_id: int, since: float, limit: int = 10) -> list[tuple[str, str, int]]:
        """(title, url, plays) since a time, most played first."""
        return self._read(
            "SELECT title, MAX(url), COUNT(*) AS n FROM music_plays WHERE guild_id = ? AND played_at >= ? "
            "GROUP BY lower(title) ORDER BY n DESC, MAX(played_at) DESC LIMIT ?", (guild_id, since, limit))

    def top_djs(self, guild_id: int, since: float, limit: int = 10) -> list[tuple[int, int]]:
        """(requester, songs queued that played) since a time."""
        return self._read(
            "SELECT requester, COUNT(*) AS n FROM music_plays WHERE guild_id = ? AND played_at >= ? AND requester != 0 "
            "GROUP BY requester ORDER BY n DESC LIMIT ?", (guild_id, since, limit))

    def recent_plays(self, guild_id: int, limit: int = 30) -> list[tuple[str, str]]:
        return self._read("SELECT title, url FROM music_plays WHERE guild_id = ? ORDER BY played_at DESC LIMIT ?",
                          (guild_id, limit))

    def corpus_between(self, guild_id: int, start: float, end: float, limit: int = 3000) -> list[tuple]:
        """(message_id, channel_id, author_id, author_name, content, created_at, laugh_reactions, total_reactions)
        stored between two times, oldest first."""
        return self._read(
            "SELECT message_id, channel_id, author_id, author_name, content, created_at, laugh_reactions, "
            "total_reactions FROM corpus WHERE guild_id = ? AND created_at >= ? AND created_at < ? "
            "ORDER BY created_at LIMIT ?", (guild_id, start, end, limit))

    def top_reacted(self, guild_id: int, since: float, minimum: int = 3, limit: int = 60) -> list[tuple]:
        """The most-reacted stored messages: same columns as corpus_between, best first."""
        return self._read(
            "SELECT message_id, channel_id, author_id, author_name, content, created_at, laugh_reactions, "
            "total_reactions FROM corpus WHERE guild_id = ? AND COALESCE(created_at, 0) >= ? "
            "AND total_reactions >= ? AND length(content) BETWEEN 6 AND 400 "
            "ORDER BY total_reactions DESC, laugh_reactions DESC, created_at DESC LIMIT ?",
            (guild_id, since, minimum, limit))

    def bump_reactions(self, guild_id: int, message_id: int, total: int, laugh: int) -> None:
        """Keep a stored message's reaction counts current as people react."""
        self._write(
            "UPDATE corpus SET total_reactions = MAX(0, total_reactions + ?), "
            "laugh_reactions = MAX(0, laugh_reactions + ?) WHERE guild_id = ? AND message_id = ?",
            (total, laugh, guild_id, message_id))

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

    def remember_join(
        self,
        guild_id: int,
        user_id: int,
        *,
        inviter_id: int | None,
        inviter_name: str,
        code: str,
        kind: str,
        joined_at: float,
    ) -> None:
        """Remember who brought `user_id` in. A later rejoin replaces the previous row."""
        self._write(
            "INSERT INTO joins (guild_id, user_id, inviter_id, inviter_name, code, kind, joined_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "inviter_id = excluded.inviter_id, inviter_name = excluded.inviter_name, "
            "code = excluded.code, kind = excluded.kind, joined_at = excluded.joined_at",
            (guild_id, user_id, inviter_id, inviter_name, code, kind, joined_at),
        )

    def join_of(self, guild_id: int, user_id: int) -> tuple[int | None, str, str, str, float] | None:
        """`(inviter_id, inviter_name, code, kind, joined_at)` or None if this join wasn't seen."""
        rows = self._read(
            "SELECT inviter_id, inviter_name, code, kind, joined_at FROM joins "
            "WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return rows[0] if rows else None

    def verification(self, guild_id: int) -> tuple:
        """`(role_id, remove_role_id, channel_id, text, panel_channel_id, panel_message_id)`."""
        rows = self._read(
            "SELECT role_id, remove_role_id, channel_id, text, panel_channel_id, panel_message_id "
            "FROM verification WHERE guild_id = ?",
            (guild_id,),
        )
        return rows[0] if rows else (None, None, None, None, None, None)

    def update_verification(self, guild_id: int, **changes) -> None:
        """Change some verification fields. Pass None to clear a field."""
        columns = ("role_id", "remove_role_id", "channel_id", "text", "panel_channel_id", "panel_message_id")
        unknown = set(changes) - set(columns)
        if unknown:
            raise KeyError(", ".join(sorted(unknown)))
        with self._lock:
            row = self._conn.execute(
                "SELECT role_id, remove_role_id, channel_id, text, panel_channel_id, panel_message_id "
                "FROM verification WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            data = dict(zip(columns, row if row else (None,) * len(columns)))
            data.update(changes)
            self._conn.execute(
                "INSERT INTO verification (guild_id, role_id, remove_role_id, channel_id, text, "
                "panel_channel_id, panel_message_id) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET "
                "role_id = excluded.role_id, remove_role_id = excluded.remove_role_id, "
                "channel_id = excluded.channel_id, text = excluded.text, "
                "panel_channel_id = excluded.panel_channel_id, panel_message_id = excluded.panel_message_id",
                (guild_id, *(data[name] for name in columns)),
            )
            self._conn.commit()

    def clear_verification(self, guild_id: int) -> bool:
        return self._write("DELETE FROM verification WHERE guild_id = ?", (guild_id,)) > 0

    # Moderation

    def restrict(self, guild_id: int, user_id: int, kind: str, *, expires_at: float | None,
                 reason: str, moderator_id: int) -> None:
        """Add or replace one restriction. A new duration replaces the old one."""
        self._write(
            "INSERT INTO restrictions (guild_id, user_id, kind, expires_at, reason, moderator_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, kind) DO UPDATE SET expires_at = excluded.expires_at, "
            "reason = excluded.reason, moderator_id = excluded.moderator_id, created_at = excluded.created_at",
            (guild_id, user_id, kind, expires_at, reason, moderator_id, time.time()),
        )

    def unrestrict(self, guild_id: int, user_id: int, kinds: list[str] | None = None) -> list[str]:
        """Lift restrictions (all of them when `kinds` is None). Returns the kinds that were active."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind FROM restrictions WHERE guild_id = ? AND user_id = ?", (guild_id, user_id),
            ).fetchall()
            active = [r[0] for r in rows if kinds is None or r[0] in kinds]
            self._conn.executemany(
                "DELETE FROM restrictions WHERE guild_id = ? AND user_id = ? AND kind = ?",
                [(guild_id, user_id, kind) for kind in active],
            )
            self._conn.commit()
        return active

    def set_effect(self, guild_id: int, user_id: int, effect: str, moderator_id: int) -> None:
        """Afflict a user with a persistent chat effect (replacing any current one)."""
        self._write(
            "INSERT INTO effects (guild_id, user_id, effect, moderator_id, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET effect = excluded.effect, "
            "moderator_id = excluded.moderator_id, created_at = excluded.created_at",
            (guild_id, user_id, effect, moderator_id, time.time()),
        )

    def clear_effect(self, guild_id: int, user_id: int) -> str | None:
        """Lift a user's effect; returns the effect that was active, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT effect FROM effects WHERE guild_id = ? AND user_id = ?", (guild_id, user_id),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM effects WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            self._conn.commit()
            return row[0]

    def get_effect(self, guild_id: int, user_id: int) -> str | None:
        rows = self._read("SELECT effect FROM effects WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        return rows[0][0] if rows else None

    def list_effects(self, guild_id: int) -> list[tuple[int, str]]:
        return [(r[0], r[1]) for r in
                self._read("SELECT user_id, effect FROM effects WHERE guild_id = ?", (guild_id,))]

    def restrictions(self, guild_id: int, user_id: int | None = None) -> list[tuple]:
        """`(user_id, kind, expires_at, reason, moderator_id, created_at)` rows, soonest expiry first."""
        sql = ("SELECT user_id, kind, expires_at, reason, moderator_id, created_at FROM restrictions "
               "WHERE guild_id = ?")
        params: tuple = (guild_id,)
        if user_id is not None:
            sql += " AND user_id = ?"
            params += (user_id,)
        return self._read(sql + " ORDER BY expires_at IS NULL, expires_at, user_id", params)

    def expired_restrictions(self, now: float) -> list[tuple[int, int, str]]:
        """`(guild_id, user_id, kind)` for every restriction whose time is up."""
        return self._read(
            "SELECT guild_id, user_id, kind FROM restrictions WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        )

    def restrict_role(self, guild_id: int, kind: str) -> int | None:
        rows = self._read("SELECT role_id FROM restrict_roles WHERE guild_id = ? AND kind = ?", (guild_id, kind))
        return rows[0][0] if rows else None

    def restrict_roles(self, guild_id: int) -> dict[str, int]:
        return dict(self._read("SELECT kind, role_id FROM restrict_roles WHERE guild_id = ?", (guild_id,)))

    def set_restrict_role(self, guild_id: int, kind: str, role_id: int) -> None:
        self._write(
            "INSERT INTO restrict_roles VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, kind) DO UPDATE SET role_id = excluded.role_id",
            (guild_id, kind, role_id),
        )

    def add_case(self, guild_id: int, user_id: int, moderator_id: int, action: str,
                 reason: str = "", duration: float | None = None) -> int:
        """Record a moderation action. Returns its case number, counted per server."""
        with self._lock:
            number = self._conn.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM mod_cases WHERE guild_id = ?", (guild_id,),
            ).fetchone()[0]
            self._conn.execute(
                "INSERT INTO mod_cases (guild_id, number, user_id, moderator_id, action, reason, duration, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, number, user_id, moderator_id, action, reason, duration, time.time()),
            )
            self._conn.commit()
        return number

    def cases(self, guild_id: int, user_id: int, action: str | None = None) -> list[tuple]:
        """`(number, moderator_id, action, reason, duration, created_at)`, newest first."""
        sql = ("SELECT number, moderator_id, action, reason, duration, created_at FROM mod_cases "
               "WHERE guild_id = ? AND user_id = ?")
        params: tuple = (guild_id, user_id)
        if action is not None:
            sql += " AND action = ?"
            params += (action,)
        return self._read(sql + " ORDER BY number DESC", params)

    def case(self, guild_id: int, number: int) -> tuple | None:
        """`(user_id, moderator_id, action, reason, duration, created_at)` or None."""
        rows = self._read(
            "SELECT user_id, moderator_id, action, reason, duration, created_at FROM mod_cases "
            "WHERE guild_id = ? AND number = ?",
            (guild_id, number),
        )
        return rows[0] if rows else None

    def delete_case(self, guild_id: int, number: int) -> bool:
        return self._write("DELETE FROM mod_cases WHERE guild_id = ? AND number = ?", (guild_id, number)) > 0

    def clear_cases(self, guild_id: int, user_id: int, action: str) -> int:
        return self._write(
            "DELETE FROM mod_cases WHERE guild_id = ? AND user_id = ? AND action = ?", (guild_id, user_id, action),
        )

    def lock_channel(self, guild_id: int, channel_id: int, previous: dict[str, bool | None]) -> bool:
        """Remember a channel's @everyone overwrites before locking. False if it was already locked."""
        return self._write(
            "INSERT OR IGNORE INTO locked_channels VALUES (?, ?, ?)", (guild_id, channel_id, json.dumps(previous)),
        ) > 0

    def is_locked(self, guild_id: int, channel_id: int) -> bool:
        return bool(self._read(
            "SELECT 1 FROM locked_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id),
        ))

    def unlock_channel(self, guild_id: int, channel_id: int) -> dict[str, bool | None] | None:
        """The overwrites saved when the channel was locked (None if it wasn't), and forget the lock."""
        with self._lock:
            row = self._conn.execute(
                "SELECT previous FROM locked_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id),
            ).fetchone()
            self._conn.execute(
                "DELETE FROM locked_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id),
            )
            self._conn.commit()
        if row is None:
            return None
        try:
            saved = json.loads(row[0] or "{}")
        except ValueError:
            saved = {}
        return saved if isinstance(saved, dict) else {}

    def add_temp_ban(self, guild_id: int, user_id: int, unban_at: float, reason: str,
                     moderator_id: int) -> None:
        self._write(
            "INSERT INTO temp_bans (guild_id, user_id, unban_at, reason, moderator_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET unban_at = excluded.unban_at, reason = excluded.reason, "
            "moderator_id = excluded.moderator_id",
            (guild_id, user_id, unban_at, reason, moderator_id),
        )

    def remove_temp_ban(self, guild_id: int, user_id: int) -> bool:
        return self._write("DELETE FROM temp_bans WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)) > 0

    def due_temp_bans(self, now: float) -> list[tuple[int, int]]:
        """`(guild_id, user_id)` for temp bans whose time is up."""
        return self._read("SELECT guild_id, user_id FROM temp_bans WHERE unban_at <= ?", (now,))

    def temp_bans(self, guild_id: int) -> list[tuple[int, float, str]]:
        """`(user_id, unban_at, reason)` still pending, soonest first."""
        return self._read(
            "SELECT user_id, unban_at, reason FROM temp_bans WHERE guild_id = ? ORDER BY unban_at", (guild_id,),
        )

    def modlog_channel(self, guild_id: int) -> int | None:
        rows = self._read("SELECT modlog_channel FROM guild_settings WHERE guild_id = ?", (guild_id,))
        return rows[0][0] if rows else None

    def set_modlog_channel(self, guild_id: int, channel_id: int | None) -> None:
        self._write(
            "INSERT INTO guild_settings (guild_id, modlog_channel) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET modlog_channel = excluded.modlog_channel",
            (guild_id, channel_id),
        )

    # Feature settings (welcome, awards) as small JSON blobs per server

    def get_config(self, guild_id: int, key: str) -> dict:
        rows = self._read("SELECT value FROM guild_kv WHERE guild_id = ? AND key = ?", (guild_id, key))
        if not rows:
            return {}
        try:
            data = json.loads(rows[0][0])
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def update_config(self, guild_id: int, key: str, **changes) -> dict:
        """Merge `changes` into a server's config blob. A None value removes that field."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM guild_kv WHERE guild_id = ? AND key = ?", (guild_id, key),
            ).fetchone()
            try:
                data = json.loads(row[0]) if row else {}
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            for name, value in changes.items():
                if value is None:
                    data.pop(name, None)
                else:
                    data[name] = value
            self._conn.execute(
                "INSERT INTO guild_kv VALUES (?, ?, ?) ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                (guild_id, key, json.dumps(data)),
            )
            self._conn.commit()
        return data

    def set_config(self, guild_id: int, key: str, data: dict) -> None:
        """Replace a server's whole config blob for `key`."""
        self._write("INSERT INTO guild_kv VALUES (?, ?, ?) ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
                    (guild_id, key, json.dumps(data)))

    def clear_config(self, guild_id: int, key: str) -> bool:
        return self._write("DELETE FROM guild_kv WHERE guild_id = ? AND key = ?", (guild_id, key)) > 0

    def configs(self, key: str) -> dict[int, dict]:
        """Every server's blob for `key`."""
        found = {}
        for guild_id, value in self._read("SELECT guild_id, value FROM guild_kv WHERE key = ?", (key,)):
            try:
                data = json.loads(value)
            except ValueError:
                continue
            if isinstance(data, dict):
                found[guild_id] = data
        return found

    # Economy — THE STRIP

    _WALLET_COLS = ("cash", "chips", "bank", "streak", "daily_at", "work_at", "hustle_at", "seen",
                    "rob_at", "crime_at", "heist_at", "jail_until", "week_at", "week_streak", "job_at", "job_shifts")
    _WALLET_TIMES = {"streak", "daily_at", "work_at", "hustle_at", "rob_at", "crime_at", "heist_at", "jail_until",
                     "week_at", "week_streak", "job_at", "job_shifts"}

    def _wallet_row(self, conn, guild_id: int, user_id: int) -> dict:
        row = conn.execute(
            f"SELECT {', '.join(self._WALLET_COLS)} FROM econ_wallets WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        if row is None:
            blank = {c: 0.0 for c in self._WALLET_COLS}
            blank.update(cash=0, chips=0, bank=0, streak=0, seen=0, week_streak=0, job_shifts=0)
            return blank
        return dict(zip(self._WALLET_COLS, row))

    def econ_wallet(self, guild_id: int, user_id: int) -> dict:
        """A user's wallet (all zeros if they've never been touched). Read-only."""
        return self._wallet_row(self._conn, guild_id, user_id)

    def econ_touch(self, guild_id: int, user_id: int, starting_cash: int = 0) -> dict:
        """Ensure a wallet exists; on first ever touch, grant the starting cash. Returns the wallet."""
        with self._lock:
            conn = self._conn
            w = self._wallet_row(conn, guild_id, user_id)
            if not w["seen"]:
                w["cash"] += max(0, int(starting_cash))
                w["seen"] = 1
                conn.execute(
                    "INSERT INTO econ_wallets (guild_id, user_id, cash, seen) VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(guild_id, user_id) DO UPDATE SET cash = excluded.cash, seen = 1",
                    (guild_id, user_id, w["cash"]),
                )
                conn.commit()
            return w

    def econ_adjust(self, guild_id: int, user_id: int, *, cash: int = 0, chips: int = 0,
                    bank: int = 0, **times) -> dict:
        """Atomically add to balances (clamped at zero) and/or set cooldown/streak fields.

        `times` may contain any of streak/daily_at/work_at/hustle_at to overwrite outright.
        Returns the updated wallet.
        """
        bad = set(times) - self._WALLET_TIMES
        if bad:
            raise ValueError(f"unknown wallet fields: {sorted(bad)}")
        with self._lock:
            conn = self._conn
            w = self._wallet_row(conn, guild_id, user_id)
            w["seen"] = 1
            w["cash"] = max(0, w["cash"] + int(cash))
            w["chips"] = max(0, w["chips"] + int(chips))
            w["bank"] = max(0, w["bank"] + int(bank))
            for key, value in times.items():
                w[key] = value
            self._write_wallet(conn, guild_id, user_id, w)
            conn.commit()
            return w

    def _write_wallet(self, conn, guild_id: int, user_id: int, w: dict) -> None:
        cols = [c for c in self._WALLET_COLS if c != "seen"]
        conn.execute(
            f"INSERT INTO econ_wallets (guild_id, user_id, {', '.join(cols)}, seen) "
            f"VALUES (?, ?, {', '.join('?' for _ in cols)}, 1) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols) + ", seen=1",
            (guild_id, user_id, *[w[c] for c in cols]),
        )

    def econ_transfer(self, guild_id: int, sender: int, recipient: int, amount: int,
                      tax: int = 0) -> bool:
        """Move `amount` cash from sender to recipient (recipient receives amount - tax).

        Returns False without changing anything if the sender can't cover `amount`.
        """
        amount = int(amount)
        tax = max(0, int(tax))
        if amount <= 0 or tax >= amount:
            return False
        with self._lock:
            conn = self._conn
            s = self._wallet_row(conn, guild_id, sender)
            if s["cash"] < amount:
                return False
            r = self._wallet_row(conn, guild_id, recipient)
            conn.execute(
                "INSERT INTO econ_wallets (guild_id, user_id, cash, seen) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET cash = excluded.cash, seen = 1",
                (guild_id, sender, s["cash"] - amount),
            )
            conn.execute(
                "INSERT INTO econ_wallets (guild_id, user_id, cash, seen) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET cash = excluded.cash, seen = 1",
                (guild_id, recipient, r["cash"] + (amount - tax)),
            )
            conn.commit()
            return True

    def econ_stake(self, guild_id: int, user_id: int, amount: int) -> tuple[int, int] | None:
        """Atomically take a bet: chips first, then cash for any shortfall (the cage swaps it
        1:1). Returns (from_chips, from_cash), or None — changing nothing — if they can't cover it."""
        amount = int(amount)
        if amount <= 0:
            return None
        with self._lock:
            conn = self._conn
            w = self._wallet_row(conn, guild_id, user_id)
            if w["chips"] + w["cash"] < amount:
                return None
            from_chips = min(w["chips"], amount)
            from_cash = amount - from_chips
            conn.execute(
                "INSERT INTO econ_wallets (guild_id, user_id, cash, chips, seen) VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET cash = excluded.cash, chips = excluded.chips, seen = 1",
                (guild_id, user_id, w["cash"] - from_cash, w["chips"] - from_chips),
            )
            conn.commit()
            return from_chips, from_cash

    def econ_rank(self, guild_id: int, user_id: int) -> int | None:
        """A player's position on the Strip's rich list (1 = richest), or None if they're broke."""
        w = self.econ_wallet(guild_id, user_id)
        net = w["cash"] + w["chips"] + w["bank"]
        if not w["seen"] or net <= 0:
            return None
        ahead = self._read(
            "SELECT COUNT(*) FROM econ_wallets WHERE guild_id = ? AND seen = 1 AND (cash + bank + chips) > ?",
            (guild_id, net),
        )[0][0]
        return ahead + 1

    def econ_move(self, guild_id: int, payer: int, payee: int | None, amount: int, *,
                  min_left: int = 0, cash_only: bool = True) -> int:
        """Move up to `amount` cash from payer to payee (None = out of the economy), never taking the
        payer below `min_left`. Atomic. Returns how much actually moved."""
        with self._lock:
            conn = self._conn
            a = self._wallet_row(conn, guild_id, payer)
            moved = max(0, min(int(amount), a["cash"] - min_left))
            if moved <= 0:
                return 0
            a["cash"] -= moved
            self._write_wallet(conn, guild_id, payer, a)
            if payee is not None:
                b = self._wallet_row(conn, guild_id, payee)
                b["cash"] += moved
                self._write_wallet(conn, guild_id, payee, b)
            conn.commit()
            return moved

    def econ_spend(self, guild_id: int, user_id: int, amount: int) -> bool:
        """Take exactly `amount` cash, all or nothing. False (no change) if they can't cover it."""
        amount = int(amount)
        with self._lock:
            conn = self._conn
            w = self._wallet_row(conn, guild_id, user_id)
            if amount < 0 or w["cash"] < amount:
                return False
            w["cash"] -= amount
            self._write_wallet(conn, guild_id, user_id, w)
            conn.commit()
            return True

    def econ_set(self, guild_id: int, user_id: int, **balances: int) -> dict:
        """Set balances outright (owner tools). Only cash/chips/bank."""
        bad = set(balances) - {"cash", "chips", "bank"}
        if bad:
            raise ValueError(f"unknown balances: {sorted(bad)}")
        with self._lock:
            conn = self._conn
            w = self._wallet_row(conn, guild_id, user_id)
            for key, value in balances.items():
                w[key] = max(0, int(value))
            self._write_wallet(conn, guild_id, user_id, w)
            conn.commit()
            return w

    def econ_global(self) -> dict:
        """Totals across every server: servers, players, and money in circulation."""
        row = self._read(
            "SELECT COUNT(DISTINCT guild_id), COUNT(*), COALESCE(SUM(cash),0), COALESCE(SUM(chips),0), "
            "COALESCE(SUM(bank),0) FROM econ_wallets WHERE seen = 1", (),
        )[0]
        return {"servers": row[0], "players": row[1], "cash": row[2], "chips": row[3], "bank": row[4]}

    # inventory
    def econ_items(self, guild_id: int, user_id: int) -> dict[str, int]:
        rows = self._read("SELECT item, qty FROM econ_items WHERE guild_id = ? AND user_id = ? AND qty > 0 "
                          "ORDER BY item", (guild_id, user_id))
        return {item: qty for item, qty in rows}

    def econ_item_add(self, guild_id: int, user_id: int, item: str, qty: int = 1) -> int:
        """Add (or, with a negative qty, remove) items. Never goes below zero; returns the new count.
        Raises ValueError without changing anything if removing more than they hold."""
        with self._lock:
            conn = self._conn
            row = conn.execute("SELECT qty FROM econ_items WHERE guild_id = ? AND user_id = ? AND item = ?",
                               (guild_id, user_id, item)).fetchone()
            have = row[0] if row else 0
            new = have + int(qty)
            if new < 0:
                raise ValueError(f"only {have} {item}")
            conn.execute("INSERT INTO econ_items VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id, item) "
                         "DO UPDATE SET qty = excluded.qty", (guild_id, user_id, item, new))
            conn.commit()
            return new

    # buffs
    def econ_buffs(self, guild_id: int, user_id: int, now: float) -> dict[str, float]:
        """Active buffs → when they wear off."""
        rows = self._read("SELECT buff, until FROM econ_buffs WHERE guild_id = ? AND user_id = ? AND until > ?",
                          (guild_id, user_id, now))
        return dict(rows)

    def econ_buff(self, guild_id: int, user_id: int, buff: str, until: float) -> None:
        """Grant a buff (extending it if it's already active)."""
        self._write("INSERT INTO econ_buffs VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id, buff) "
                    "DO UPDATE SET until = MAX(until, excluded.until)", (guild_id, user_id, buff, until))

    def econ_unbuff(self, guild_id: int, user_id: int, buff: str) -> bool:
        return self._write("DELETE FROM econ_buffs WHERE guild_id = ? AND user_id = ? AND buff = ?",
                           (guild_id, user_id, buff)) > 0

    # storefronts
    _SHOP_COLS = ("shop", "owner_id", "level", "invested", "register", "collected_at")

    def econ_shops(self, guild_id: int) -> dict[str, dict]:
        rows = self._read(f"SELECT {', '.join(self._SHOP_COLS)} FROM econ_shops WHERE guild_id = ?", (guild_id,))
        return {r[0]: dict(zip(self._SHOP_COLS, r)) for r in rows}

    def econ_shop(self, guild_id: int, shop: str) -> dict | None:
        return self.econ_shops(guild_id).get(shop)

    def econ_shop_buy(self, guild_id: int, shop: str, owner_id: int, price: int, now: float) -> bool:
        """Buy an unowned storefront with cash. False (no change) if it's taken or they can't afford it."""
        with self._lock:
            conn = self._conn
            if conn.execute("SELECT 1 FROM econ_shops WHERE guild_id = ? AND shop = ?", (guild_id, shop)).fetchone():
                return False
            w = self._wallet_row(conn, guild_id, owner_id)
            if w["cash"] < price:
                return False
            w["cash"] -= price
            self._write_wallet(conn, guild_id, owner_id, w)
            conn.execute("INSERT INTO econ_shops VALUES (?, ?, ?, 1, ?, 0, ?)",
                         (guild_id, shop, owner_id, price, now))
            conn.commit()
            return True

    def econ_shop_update(self, guild_id: int, shop: str, **fields) -> None:
        bad = set(fields) - {"owner_id", "level", "invested", "register", "collected_at"}
        if bad:
            raise ValueError(f"unknown shop fields: {sorted(bad)}")
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self._write(f"UPDATE econ_shops SET {sets} WHERE guild_id = ? AND shop = ?",
                        (*fields.values(), guild_id, shop))

    def econ_shop_tip(self, guild_id: int, shop: str, amount: int) -> None:
        """Drop money into a storefront's register (no-op if nobody owns it)."""
        if amount > 0:
            self._write("UPDATE econ_shops SET register = register + ? WHERE guild_id = ? AND shop = ?",
                        (int(amount), guild_id, shop))

    def econ_shop_release(self, guild_id: int, shop: str) -> bool:
        return self._write("DELETE FROM econ_shops WHERE guild_id = ? AND shop = ?", (guild_id, shop)) > 0

    def econ_reset_shops(self, guild_id: int) -> int:
        n = self._write("DELETE FROM econ_shops WHERE guild_id = ?", (guild_id,))
        self._write("DELETE FROM econ_items WHERE guild_id = ?", (guild_id,))
        self._write("DELETE FROM econ_buffs WHERE guild_id = ?", (guild_id,))
        return n

    def econ_top(self, guild_id: int, limit: int = 15) -> list[dict]:
        """Richest wallets by net worth (cash + bank + chips), descending."""
        rows = self._read(
            "SELECT user_id, cash, chips, bank, (cash + bank + chips) AS net FROM econ_wallets "
            "WHERE guild_id = ? AND seen = 1 AND (cash + bank + chips) > 0 "
            "ORDER BY net DESC, user_id ASC LIMIT ?", (guild_id, limit),
        )
        return [{"user_id": r[0], "cash": r[1], "chips": r[2], "bank": r[3], "net": r[4]} for r in rows]

    def econ_circulation(self, guild_id: int) -> dict:
        """Totals across the server: players, and cash/chips/bank in circulation."""
        row = self._read(
            "SELECT COUNT(*), COALESCE(SUM(cash),0), COALESCE(SUM(chips),0), COALESCE(SUM(bank),0) "
            "FROM econ_wallets WHERE guild_id = ? AND seen = 1", (guild_id,),
        )[0]
        return {"players": row[0], "cash": row[1], "chips": row[2], "bank": row[3]}

    def econ_reset(self, guild_id: int, user_id: int) -> bool:
        return self._write(
            "DELETE FROM econ_wallets WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)) > 0

    def econ_reset_guild(self, guild_id: int) -> int:
        return self._write("DELETE FROM econ_wallets WHERE guild_id = ?", (guild_id,))

    # !guess

    def guess_candidates(self, guild_id: int, limit: int = 40) -> list[tuple]:
        """Random stored messages worth guessing: `(message_id, channel_id, author_id, author_name, content)`."""
        return self._read(
            "SELECT message_id, channel_id, author_id, author_name, content FROM corpus "
            "WHERE guild_id = ? AND author_name != '' AND length(content) BETWEEN 15 AND 280 "
            "AND content NOT LIKE '%<@%' AND content NOT LIKE '%http%' ORDER BY RANDOM() LIMIT ?",
            (guild_id, limit),
        )

    def search_channel(self, guild_id: int, channel_id: int, query: str,
                        limit: int = 80) -> list[tuple]:
        """Stored messages in a channel whose text contains `query` (newest first).

        Returns `(message_id, author_id, author_name, content, created_at)`.
        """
        if not query.strip():
            return []
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        return self._read(
            "SELECT message_id, author_id, author_name, content, created_at FROM corpus "
            "WHERE guild_id = ? AND channel_id = ? AND content != '' AND content LIKE ? ESCAPE '\\' "
            "ORDER BY created_at IS NULL, created_at DESC LIMIT ?",
            (guild_id, channel_id, like, limit),
        )

    def member_stats(self, guild_id: int, user_id: int) -> tuple[int, float | None, float | None, str]:
        """`(stored message count, first seen, last seen, latest name)` for one member."""
        rows = self._read(
            "SELECT COUNT(*), MIN(created_at), MAX(created_at), "
            "(SELECT author_name FROM corpus WHERE guild_id = ? AND author_id = ? AND author_name != '' "
            " ORDER BY created_at DESC LIMIT 1) "
            "FROM corpus WHERE guild_id = ? AND author_id = ?",
            (guild_id, user_id, guild_id, user_id),
        )
        count, first, last, name = rows[0]
        return int(count or 0), first, last, name or ""

    def member_top_channels(self, guild_id: int, user_id: int, limit: int = 5) -> list[tuple[int, int]]:
        """`(channel_id, message count)` where this member talks most."""
        return self._read(
            "SELECT channel_id, COUNT(*) FROM corpus WHERE guild_id = ? AND author_id = ? "
            "GROUP BY channel_id ORDER BY COUNT(*) DESC LIMIT ?",
            (guild_id, user_id, limit),
        )

    def member_post_times(self, guild_id: int, user_id: int, limit: int = 5000) -> list[float]:
        """Created-at timestamps of a member's stored messages, newest first — for activity patterns."""
        rows = self._read(
            "SELECT created_at FROM corpus WHERE guild_id = ? AND author_id = ? AND created_at IS NOT NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (guild_id, user_id, limit),
        )
        return [r[0] for r in rows]

    def joins_all(self, guild_id: int) -> list[tuple]:
        """Every recorded join: `(user_id, inviter_id, inviter_name, code, kind, joined_at)`."""
        return self._read(
            "SELECT user_id, inviter_id, inviter_name, code, kind, joined_at FROM joins WHERE guild_id = ?",
            (guild_id,),
        )

    def top_authors(self, guild_id: int, limit: int = 40) -> list[tuple[int, str, int]]:
        """`(author_id, latest name, stored messages)` for the most active members."""
        return self._read(
            "SELECT author_id, MAX(author_name), COUNT(*) FROM corpus WHERE guild_id = ? AND author_name != '' "
            "GROUP BY author_id ORDER BY COUNT(*) DESC LIMIT ?",
            (guild_id, limit),
        )

    def add_guess_score(self, guild_id: int, user_id: int, points: int) -> int:
        """Add points (0 just counts a game played). Returns the new total."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO guess_scores (guild_id, user_id, points, played) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + excluded.points, played = played + 1",
                (guild_id, user_id, points),
            )
            self._conn.commit()
            return self._conn.execute(
                "SELECT points FROM guess_scores WHERE guild_id = ? AND user_id = ?", (guild_id, user_id),
            ).fetchone()[0]

    def guess_leaders(self, guild_id: int, limit: int = 10) -> list[tuple[int, int, int]]:
        """`(user_id, points, played)`, best first."""
        return self._read(
            "SELECT user_id, points, played FROM guess_scores WHERE guild_id = ? AND points > 0 "
            "ORDER BY points DESC, played ASC LIMIT ?",
            (guild_id, limit),
        )

    def demo_mode(self, guild_id: int) -> bool:
        rows = self._read("SELECT demo_mode FROM guild_settings WHERE guild_id = ?", (guild_id,))
        return bool(rows and rows[0][0])

    def set_demo_mode(self, guild_id: int, enabled: bool) -> None:
        self._write(
            "INSERT INTO guild_settings (guild_id, demo_mode) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET demo_mode = excluded.demo_mode",
            (guild_id, int(enabled)),
        )
