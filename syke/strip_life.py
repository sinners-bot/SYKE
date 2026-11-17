"""THE STRIP lifestyle rewards, settled by SYKE.

The game server decides when a daily activity, weekly challenge, monthly milestone, seasonal objective or
community goal is complete; SYKE pays the (small) reward. Every reward has a key that names what it was for
and when (e.g. ``daily:2026-11-17:visit3``): each key pays a player once, ever, so a retry or a replayed
request can never pay twice. A player's lifestyle rewards are also capped per day and per reward, so even a
compromised game server can't print money.
"""

from __future__ import annotations

import re
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS strip_life_rewards (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, key TEXT NOT NULL, amount INTEGER NOT NULL, cash_after INTEGER NOT NULL,
    day TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id, key)
);
CREATE INDEX IF NOT EXISTS strip_life_rewards_day ON strip_life_rewards (guild_id, user_id, day);
"""
REWARD_MAX = 3000        # the biggest single lifestyle reward (a seasonal objective)
DAILY_CAP = 5000         # all lifestyle rewards for one player in one UTC day
KEY = re.compile(r"^[A-Za-z0-9_:.\-]{3,80}$")


class LifeError(Exception):
    def __init__(self, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


class LifeService:
    def __init__(self, storage, guard, clock=time.time) -> None:
        self.db, self.guard, self.now = storage, guard, clock
        with storage._lock:
            storage._conn.executescript(SCHEMA)
            storage._conn.commit()

    def reward(self, gid: int, uid: int, key, amount) -> dict:
        self.guard(gid, uid)
        key = str(key or "")
        if not KEY.match(key):
            raise LifeError("bad_key", "Which reward?")
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            raise LifeError("bad_amount", "That's not a reward.")
        if amount < 1 or amount > REWARD_MAX:
            raise LifeError("bad_amount", "That's not a reward.")
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now()))
        st = self.db
        with st._lock:
            conn = st._conn
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT amount, cash_after FROM strip_life_rewards WHERE guild_id=? AND user_id=? AND key=?", (gid, uid, key)).fetchone()
                if row:
                    conn.commit()
                    return {"key": key, "amount": row[0], "cash": row[1], "replayed": True}
                today = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM strip_life_rewards WHERE guild_id=? AND user_id=? AND day=?", (gid, uid, day)).fetchone()[0]
                if today + amount > DAILY_CAP:
                    raise LifeError("cap", "That's all the lifestyle rewards for today. The rest will keep.", left=max(0, DAILY_CAP - today))
                w = st._wallet_row(conn, gid, uid)
                w["cash"] += amount
                st._write_wallet(conn, gid, uid, w)
                conn.execute("INSERT INTO strip_life_rewards VALUES (?, ?, ?, ?, ?, ?, ?)", (gid, uid, key, amount, w["cash"], day, self.now()))
                conn.commit()
                return {"key": key, "amount": amount, "cash": w["cash"]}
            except BaseException:
                conn.rollback()
                raise
