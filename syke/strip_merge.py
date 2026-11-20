"""Moving THE STRIP from its own game-only wallets onto a real server's SYKE wallets, once.

The game used to keep everyone's money under a made-up "guild" (STRIP_GUILD_ID, "1" by default), separate from the
wallet people already have in their Discord server. When the game is pointed at a real server, `merge(src, dst)` adds
what each player had in the game-only wallet to their wallet in that server: cash, chips and bank; items; buffs;
storefronts (when the server's slot for that business is free, otherwise the owner is paid back what they put in plus
what was in the till); dishes on a menu; open market listings; home storage; casino stats; live roulette bets (refunded).
The game-only wallets are then emptied. It runs at most once for each (src, dst) pair, in one transaction.
"""
from __future__ import annotations

import json
import time

_TABLE = """CREATE TABLE IF NOT EXISTS strip_wallet_merges (
    src INTEGER NOT NULL, dst INTEGER NOT NULL, done REAL NOT NULL, summary TEXT NOT NULL,
    PRIMARY KEY (src, dst)
)"""


def _has(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _add_cash(db, conn, gid: int, uid: int, cash: int = 0, chips: int = 0, bank: int = 0) -> None:
    w = db._wallet_row(conn, gid, uid)
    w["cash"] += int(cash)
    w["chips"] += int(chips)
    w["bank"] += int(bank)
    db._write_wallet(conn, gid, uid, w)


def _add_item(conn, gid: int, uid: int, item: str, qty: int) -> None:
    if qty > 0:
        conn.execute("INSERT INTO econ_items (guild_id, user_id, item, qty) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(guild_id, user_id, item) DO UPDATE SET qty = qty + excluded.qty", (gid, uid, item, qty))


def _merge_games(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if k not in out:
            out[k] = v
        elif isinstance(v, dict) and isinstance(out[k], dict):
            out[k] = {**out[k], **{kk: (out[k].get(kk, 0) + vv if isinstance(vv, (int, float)) and isinstance(out[k].get(kk, 0), (int, float)) else vv)
                                   for kk, vv in v.items()}}
        elif isinstance(v, (int, float)) and isinstance(out[k], (int, float)):
            out[k] = out[k] + v
    return out


def status(db, src: int, dst: int):
    with db._lock:
        conn = db._conn
        conn.execute(_TABLE)
        row = conn.execute("SELECT done, summary FROM strip_wallet_merges WHERE src=? AND dst=?", (src, dst)).fetchone()
        return {"done": row[0], "summary": json.loads(row[1])} if row else None


def merge(db, src: int, dst: int) -> dict:
    src, dst = int(src), int(dst)
    if src == dst:
        return {"merged": False, "reason": "same"}
    with db._lock:
        conn = db._conn
        if conn.in_transaction:
            conn.commit()
        conn.execute(_TABLE)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT done, summary FROM strip_wallet_merges WHERE src=? AND dst=?", (src, dst)).fetchone()
            if row:
                conn.rollback()
                return {"merged": False, "reason": "already", "done": row[0], "summary": json.loads(row[1])}
            s = {"players": 0, "cash": 0, "bank": 0, "chips": 0, "items": 0, "shops_moved": 0, "shops_refunded": 0,
                 "refunded": 0, "listings": 0, "stored": 0, "bets_refunded": 0}
            # wallets: add, then empty the game-only one (the row stays, so it never hands out a new starting balance)
            for uid, cash, chips, bank in conn.execute(
                    "SELECT user_id, cash, chips, bank FROM econ_wallets WHERE guild_id=?", (src,)).fetchall():
                if cash or chips or bank:
                    _add_cash(db, conn, dst, uid, cash, chips, bank)
                    s["players"] += 1
                    s["cash"] += cash
                    s["chips"] += chips
                    s["bank"] += bank
            conn.execute("UPDATE econ_wallets SET cash=0, chips=0, bank=0 WHERE guild_id=?", (src,))
            # items
            for uid, item, qty in conn.execute("SELECT user_id, item, qty FROM econ_items WHERE guild_id=? AND qty>0", (src,)).fetchall():
                _add_item(conn, dst, uid, item, qty)
                s["items"] += qty
            conn.execute("DELETE FROM econ_items WHERE guild_id=?", (src,))
            # buffs: keep whichever lasts longer
            for uid, buff, until in conn.execute("SELECT user_id, buff, until FROM econ_buffs WHERE guild_id=?", (src,)).fetchall():
                conn.execute("INSERT INTO econ_buffs (guild_id, user_id, buff, until) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(guild_id, user_id, buff) DO UPDATE SET until = MAX(until, excluded.until)", (dst, uid, buff, until))
            conn.execute("DELETE FROM econ_buffs WHERE guild_id=?", (src,))
            # storefronts: move when the server's slot is free, otherwise pay the owner back
            moved = set()
            for shop, owner, invested, register in conn.execute(
                    "SELECT shop, owner_id, invested, register FROM econ_shops WHERE guild_id=?", (src,)).fetchall():
                if conn.execute("SELECT 1 FROM econ_shops WHERE guild_id=? AND shop=?", (dst, shop)).fetchone():
                    _add_cash(db, conn, dst, owner, int(invested) + int(register))
                    s["shops_refunded"] += 1
                    s["refunded"] += int(invested) + int(register)
                    conn.execute("DELETE FROM econ_shops WHERE guild_id=? AND shop=?", (src, shop))
                else:
                    conn.execute("UPDATE econ_shops SET guild_id=? WHERE guild_id=? AND shop=?", (dst, src, shop))
                    moved.add(shop)
                    s["shops_moved"] += 1
            # dishes on a menu follow their storefront; otherwise they go back to the cook
            if _has(conn, "strip_kitchen_stock"):
                for store, dish, owner, qty, price in conn.execute(
                        "SELECT store, dish, owner, qty, price FROM strip_kitchen_stock WHERE guild_id=?", (src,)).fetchall():
                    if store in moved:
                        conn.execute("INSERT INTO strip_kitchen_stock (guild_id, store, dish, owner, qty, price) VALUES (?, ?, ?, ?, ?, ?) "
                                     "ON CONFLICT(guild_id, store, dish) DO UPDATE SET qty = qty + excluded.qty", (dst, store, dish, owner, qty, price))
                    else:
                        _add_item(conn, dst, owner, dish, qty)
                conn.execute("DELETE FROM strip_kitchen_stock WHERE guild_id=?", (src,))
            if _has(conn, "strip_market"):
                s["listings"] = conn.execute("SELECT COUNT(*) FROM strip_market WHERE guild_id=? AND status='open'", (src,)).fetchone()[0]
                conn.execute("UPDATE strip_market SET guild_id=? WHERE guild_id=?", (dst, src))
            if _has(conn, "strip_home_storage"):
                for uid, home, item, qty in conn.execute(
                        "SELECT user_id, home, item, qty FROM strip_home_storage WHERE guild_id=?", (src,)).fetchall():
                    conn.execute("INSERT INTO strip_home_storage (guild_id, user_id, home, item, qty) VALUES (?, ?, ?, ?, ?) "
                                 "ON CONFLICT(guild_id, user_id, home, item) DO UPDATE SET qty = qty + excluded.qty", (dst, uid, home, item, qty))
                    s["stored"] += qty
                conn.execute("DELETE FROM strip_home_storage WHERE guild_id=?", (src,))
            if _has(conn, "strip_life_rewards"):
                conn.execute("INSERT OR IGNORE INTO strip_life_rewards SELECT ?, user_id, key, amount, cash_after, day, ts "
                             "FROM strip_life_rewards WHERE guild_id=?", (dst, src))
                conn.execute("DELETE FROM strip_life_rewards WHERE guild_id=?", (src,))
            if _has(conn, "strip_casino_stats"):
                cols = ["visits", "wagered", "returned", "wins", "losses", "won_total", "lost_total"]
                for r in conn.execute(f"SELECT user_id, {', '.join(cols)}, biggest_win, biggest_loss, by_game, last_visit "
                                      "FROM strip_casino_stats WHERE guild_id=?", (src,)).fetchall():
                    uid, vals, bw, bl, bg, lv = r[0], r[1:8], r[8], r[9], r[10], r[11]
                    cur = conn.execute("SELECT by_game FROM strip_casino_stats WHERE guild_id=? AND user_id=?", (dst, uid)).fetchone()
                    if cur is None:
                        conn.execute(f"INSERT INTO strip_casino_stats (guild_id, user_id, {', '.join(cols)}, biggest_win, biggest_loss, by_game, last_visit) "
                                     f"VALUES (?, ?, {', '.join('?' for _ in cols)}, ?, ?, ?, ?)", (dst, uid, *vals, bw, bl, bg, lv))
                    else:
                        try:
                            games = json.dumps(_merge_games(json.loads(cur[0] or "{}"), json.loads(bg or "{}")))
                        except ValueError:
                            games = cur[0]
                        conn.execute("UPDATE strip_casino_stats SET " + ", ".join(f"{c} = {c} + ?" for c in cols)
                                     + ", biggest_win = MAX(biggest_win, ?), biggest_loss = MAX(biggest_loss, ?), by_game = ?, "
                                     "last_visit = MAX(last_visit, ?) WHERE guild_id=? AND user_id=?", (*vals, bw, bl, games, lv, dst, uid))
                conn.execute("DELETE FROM strip_casino_stats WHERE guild_id=?", (src,))
            if _has(conn, "strip_casino_rl_bets"):   # a roulette spin in the air: the stake goes back
                for bid, uid, amount in conn.execute(
                        "SELECT id, user_id, amount FROM strip_casino_rl_bets WHERE guild_id=? AND status='live'", (src,)).fetchall():
                    _add_cash(db, conn, dst, uid, amount)
                    s["bets_refunded"] += amount
                    conn.execute("UPDATE strip_casino_rl_bets SET status='cleared' WHERE id=?", (bid,))
            conn.execute("INSERT INTO strip_wallet_merges (src, dst, done, summary) VALUES (?, ?, ?, ?)",
                         (src, dst, time.time(), json.dumps(s)))
            conn.commit()
            return {"merged": True, "summary": s}
        except BaseException:
            conn.rollback()
            raise
