"""THE STRIP casino: the chip cage, slots, roulette and blackjack for the walkable game, settled by SYKE.

SYKE stays the bank. Chips are the same `chips` balance the casino commands in Discord use, and the
cage swaps them 1:1 with cash (economy.CONVERT_FEE), so no money is created here. Odds and payouts
come from economy.py (slots, roulette) and cardgames.py (blackjack), the same code the bot plays with.

Every money move is ONE SQLite transaction (BEGIN IMMEDIATE under the storage write lock):

    1. the request id (rid) is looked up; a repeat returns the stored reply instead of moving money again
    2. balances are checked and changed
    3. an append-only ledger row is written (who, what, how much, balances after)
    4. casino statistics are updated
    5. the reply is stored under the rid

so a request either happens exactly once, completely, or not at all, whatever the network or a restart
does in between. Blackjack hands are stored as (secret seed, actions taken) and rebuilt on demand, so a
hand survives the game server or SYKE restarting; idle hands auto-stand. Roulette rounds are settled by
round id, once.
"""

from __future__ import annotations

import json
import random
import re
import secrets
import time

from . import economy as eco
from .cardgames import Blackjack, Shoe, hand_value
from .strip_games import (SLOT_MACHINES, TableRound, card_code, roulette_colour, roulette_mult, roulette_numbers,
                          slot_config, slot_pay, slot_spin)

SCHEMA = """
CREATE TABLE IF NOT EXISTS strip_casino_requests (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, rid TEXT NOT NULL,
    op TEXT NOT NULL, reply TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id, rid)
);
CREATE TABLE IF NOT EXISTS strip_casino_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, rid TEXT NOT NULL,
    kind TEXT NOT NULL, game TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '',
    chips_delta INTEGER NOT NULL, cash_delta INTEGER NOT NULL,
    wager INTEGER NOT NULL DEFAULT 0, payout INTEGER NOT NULL DEFAULT 0,
    chips_after INTEGER NOT NULL, cash_after INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}', ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS strip_casino_ledger_user ON strip_casino_ledger (guild_id, user_id, id);
CREATE INDEX IF NOT EXISTS strip_casino_ledger_guild ON strip_casino_ledger (guild_id, id);
CREATE TABLE IF NOT EXISTS strip_casino_stats (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    visits INTEGER NOT NULL DEFAULT 0, wagered INTEGER NOT NULL DEFAULT 0, returned INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
    won_total INTEGER NOT NULL DEFAULT 0, lost_total INTEGER NOT NULL DEFAULT 0,
    biggest_win INTEGER NOT NULL DEFAULT 0, biggest_loss INTEGER NOT NULL DEFAULT 0,
    by_game TEXT NOT NULL DEFAULT '{}', last_visit REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS strip_casino_hands (
    hand_id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, table_id TEXT NOT NULL,
    seed INTEGER NOT NULL, bet INTEGER NOT NULL, actions TEXT NOT NULL DEFAULT '[]',
    staked INTEGER NOT NULL, settled INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS strip_casino_hands_open ON strip_casino_hands (guild_id, user_id, settled);
CREATE TABLE IF NOT EXISTS strip_casino_rl_rounds (
    guild_id INTEGER NOT NULL, round_id TEXT NOT NULL, table_id TEXT NOT NULL, status TEXT NOT NULL,
    opened REAL NOT NULL, closes_at REAL, number INTEGER, reply TEXT, spun_at REAL,
    PRIMARY KEY (guild_id, round_id)
);
CREATE INDEX IF NOT EXISTS strip_casino_rl_rounds_open ON strip_casino_rl_rounds (guild_id, table_id, status);
CREATE TABLE IF NOT EXISTS strip_casino_rl_bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, round_id TEXT NOT NULL, user_id INTEGER NOT NULL,
    rid TEXT NOT NULL, space TEXT NOT NULL, amount INTEGER NOT NULL, name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, payout INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS strip_casino_rl_bets_round ON strip_casino_rl_bets (guild_id, round_id);
CREATE TABLE IF NOT EXISTS strip_casino_bjt (
    guild_id INTEGER NOT NULL, round_id TEXT NOT NULL, table_id TEXT NOT NULL, phase TEXT NOT NULL,
    seed INTEGER NOT NULL, seats TEXT NOT NULL DEFAULT '[]', moves TEXT NOT NULL DEFAULT '[]',
    closes_at REAL NOT NULL, turn_deadline REAL, created REAL NOT NULL, updated REAL NOT NULL,
    PRIMARY KEY (guild_id, round_id)
);
CREATE INDEX IF NOT EXISTS strip_casino_bjt_open ON strip_casino_bjt (guild_id, table_id, phase);
CREATE TABLE IF NOT EXISTS strip_casino_rounds (
    guild_id INTEGER NOT NULL, round_id TEXT NOT NULL, number INTEGER NOT NULL, reply TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (guild_id, round_id)
);
"""

EXCHANGE_MAX = 10_000_000
SLOTS_MAX = 100_000
TABLES = {                     # blackjack tables: limits only (same rules and odds everywhere)
    "bj1": (eco.MIN_BET, 100_000), "bj2": (eco.MIN_BET, 100_000), "vip": (1_000, eco.MAX_BET),
}
ROULETTE_TABLES = {"roulette": (eco.MIN_BET, 100_000), "vip_roulette": (1_000, eco.MAX_BET)}
ROULETTE_MAX_BETS = 300        # per round, across all players
RL_MAX_PER_PLAYER = 20         # bets per player per spin
RL_BET_SECS = 20               # the betting clock starts with the first bet of a round
RL_STALE = 120                 # a round its game server abandoned is spun this long after betting closed
TABLE_SEATS = {"bj1": 5, "bj2": 5, "vip": 4}
BJ_BET_SECS = 15               # betting window after the first bet at a blackjack table
TURN_SECS = 20                 # each player's decision clock; when it runs out the hand stands
BJ_STALE = 90                  # a table round nobody has touched this long is finished automatically
BJ_IDLE = 600                  # a hand untouched this long stands automatically
BIG_WIN = 500                  # net win that makes the recent-winners board
# VIP progression by lifetime chips wagered. Status only: it never changes odds or pays out money.
VIP_TIERS = [("Bronze", 0), ("Silver", 25_000), ("Gold", 100_000), ("Platinum", 500_000), ("Diamond", 2_000_000)]
SLOT_KEYS = {"🍒": "cherry", "🔔": "bell", "🍋": "lemon", "💎": "diamond", "7️⃣": "seven", "🎰": "jackpot"}
RID = re.compile(r"^[A-Za-z0-9_:.\-]{6,80}$")


class CasinoError(Exception):
    def __init__(self, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def vip_tier(wagered: int) -> dict:
    cur, nxt = VIP_TIERS[0], None
    for i, (name, need) in enumerate(VIP_TIERS):
        if wagered >= need:
            cur, nxt = (name, need), (VIP_TIERS[i + 1] if i + 1 < len(VIP_TIERS) else None)
    return {"tier": cur[0], "at": cur[1], "level": [t[0] for t in VIP_TIERS].index(cur[0]),
            "next": nxt[0] if nxt else None, "next_at": nxt[1] if nxt else None, "wagered": wagered}


def tier_level(name: str) -> int:
    names = [t[0].lower() for t in VIP_TIERS]
    return names.index(name.lower()) if str(name).lower() in names else 0


def _card(c) -> str:
    return f"{c.rank}{c.suit}"


class CasinoService:
    """Casino actions on SYKE's storage. `guard(gid, uid)` is StripService.guard (economy open, not banned)."""

    def __init__(self, storage, guard, rng: random.Random | None = None, clock=time.time) -> None:
        self.db = storage
        self.guard = guard
        self.rng = rng or secrets.SystemRandom()   # operating-system randomness for every outcome
        self.now = clock
        with storage._lock:
            storage._conn.executescript(SCHEMA)
            storage._conn.commit()

    # ------------------------------------------------------------------ plumbing
    def _tx(self, fn):
        """Run fn(conn) as one transaction under the storage write lock; roll back on any error."""
        st = self.db
        with st._lock:
            conn = st._conn
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                out = fn(conn)
                conn.commit()
                return out
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _rid(rid) -> str:
        rid = str(rid or "")
        if not RID.match(rid):
            raise CasinoError("bad_rid", "Every casino request needs a request id.")
        return rid

    def _once(self, conn, gid: int, uid: int, rid: str, op: str, do) -> dict:
        """Idempotency: the first time a (player, rid) is seen, do it and store the reply; after that, replay it."""
        row = conn.execute("SELECT op, reply FROM strip_casino_requests WHERE guild_id=? AND user_id=? AND rid=?",
                           (gid, uid, rid)).fetchone()
        if row:
            if row[0] != op:
                raise CasinoError("rid_reused", "That request id was already used for something else.")
            return {**json.loads(row[1]), "replayed": True}
        reply = do()
        conn.execute("INSERT INTO strip_casino_requests VALUES (?, ?, ?, ?, ?, ?)",
                     (gid, uid, rid, op, json.dumps(reply), self.now()))
        return reply

    def _wallet(self, conn, gid: int, uid: int) -> dict:
        return self.db._wallet_row(conn, gid, uid)

    def _save(self, conn, gid: int, uid: int, w: dict) -> None:
        if w["chips"] < 0 or w["cash"] < 0:
            raise CasinoError("negative", "That would leave a negative balance.")   # never reached: checked first
        self.db._write_wallet(conn, gid, uid, w)

    def _ledger(self, conn, gid, uid, rid, kind, game, w, chips_delta=0, cash_delta=0, wager=0, payout=0, ref="", **detail):
        conn.execute(
            "INSERT INTO strip_casino_ledger (guild_id, user_id, rid, kind, game, ref, chips_delta, cash_delta, wager, payout, "
            "chips_after, cash_after, detail, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, uid, rid, kind, game, ref, chips_delta, cash_delta, wager, payout, w["chips"], w["cash"],
             json.dumps(detail, ensure_ascii=False), self.now()))

    def _stat(self, conn, gid: int, uid: int, game: str, wager: int, returned: int) -> None:
        """A finished bet: wagered/returned totals, win/loss counts, biggest swings, per-game totals."""
        row = conn.execute("SELECT wagered, returned, wins, losses, won_total, lost_total, biggest_win, biggest_loss, by_game "
                           "FROM strip_casino_stats WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()
        s = dict(zip(("wagered", "returned", "wins", "losses", "won_total", "lost_total", "biggest_win", "biggest_loss", "by_game"),
                     row or (0, 0, 0, 0, 0, 0, 0, 0, "{}")))
        net = returned - wager
        by = json.loads(s["by_game"] or "{}")
        g = by.setdefault(game, {"plays": 0, "wagered": 0, "net": 0})
        g["plays"] += 1; g["wagered"] += wager; g["net"] += net
        s["wagered"] += wager; s["returned"] += returned
        if net > 0:
            s["wins"] += 1; s["won_total"] += net; s["biggest_win"] = max(s["biggest_win"], net)
        elif net < 0:
            s["losses"] += 1; s["lost_total"] += -net; s["biggest_loss"] = max(s["biggest_loss"], -net)
        conn.execute(
            "INSERT INTO strip_casino_stats (guild_id, user_id, wagered, returned, wins, losses, won_total, lost_total, "
            "biggest_win, biggest_loss, by_game) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "wagered=excluded.wagered, returned=excluded.returned, wins=excluded.wins, losses=excluded.losses, "
            "won_total=excluded.won_total, lost_total=excluded.lost_total, biggest_win=excluded.biggest_win, "
            "biggest_loss=excluded.biggest_loss, by_game=excluded.by_game",
            (gid, uid, s["wagered"], s["returned"], s["wins"], s["losses"], s["won_total"], s["lost_total"],
             s["biggest_win"], s["biggest_loss"], json.dumps(by)))

    @staticmethod
    def _amount(value, lo: int, hi: int, what: str = "bet") -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise CasinoError("bad_amount", f"That's not a {what}.")
        if n != value and not (isinstance(value, str) and value.strip().isdigit()):
            raise CasinoError("bad_amount", "Whole chips only.")
        if n < lo or n > hi:
            raise CasinoError("limit", f"The {what} must be between {lo:,} and {hi:,}.", min=lo, max=hi)
        return n

    @staticmethod
    def _name(name) -> str:
        return re.sub(r"[\x00-\x1f<>]", "", str(name or ""))[:24]

    # ------------------------------------------------------------------ the cage
    def exchange(self, gid: int, uid: int, action: str, amount, rid) -> dict:
        self.guard(gid, uid)
        rid = self._rid(rid)
        if action not in ("buy", "sell"):
            raise CasinoError("bad_args", "Buy chips or cash out?")
        amount = self._amount(amount, 1, EXCHANGE_MAX, "amount")

        def do(conn):
            def inner():
                w = self._wallet(conn, gid, uid)
                received, fee = eco.convert(amount)
                if action == "buy":
                    if w["cash"] < amount:
                        raise CasinoError("broke", f"You only have {w['cash']:,} cash on you.", have=w["cash"])
                    w["cash"] -= amount; w["chips"] += received
                    self._save(conn, gid, uid, w)
                    self._ledger(conn, gid, uid, rid, "buy_chips", "cage", w, chips_delta=received, cash_delta=-amount, fee=fee)
                else:
                    if w["chips"] < amount:
                        raise CasinoError("broke", f"You only have {w['chips']:,} chips.", have=w["chips"])
                    w["chips"] -= amount; w["cash"] += received
                    self._save(conn, gid, uid, w)
                    self._ledger(conn, gid, uid, rid, "sell_chips", "cage", w, chips_delta=-amount, cash_delta=received, fee=fee)
                return {"action": action, "amount": amount, "received": received, "fee": fee, "chips": w["chips"], "cash": w["cash"]}
            return self._once(conn, gid, uid, rid, "exchange", inner)
        return self._tx(do)

    # ------------------------------------------------------------------ slots
    def slots(self, gid: int, uid: int, bet, rid, machine: str = "", name: str = "", theme: str = "") -> dict:
        if theme:
            return self.slot_machine(gid, uid, theme, bet, rid, machine, name)
        self.guard(gid, uid)
        rid = self._rid(rid)
        bet = self._amount(bet, eco.MIN_BET, SLOTS_MAX)

        def do(conn):
            def inner():
                w = self._wallet(conn, gid, uid)
                if w["chips"] < bet:
                    raise CasinoError("broke", f"You need {bet:,} chips for that spin. Visit the cashier.", have=w["chips"])
                reels = eco.slot_spin(self.rng)
                mult = eco.slot_multiplier(reels)
                payout = bet * mult
                w["chips"] += payout - bet
                self._save(conn, gid, uid, w)
                keys = [SLOT_KEYS.get(r, "cherry") for r in reels]
                self._ledger(conn, gid, uid, rid, "slots", "slots", w, chips_delta=payout - bet, wager=bet, payout=payout,
                             ref=str(machine)[:16], reels=keys, name=self._name(name))
                self._stat(conn, gid, uid, "slots", bet, payout)
                return {"reels": keys, "mult": mult, "bet": bet, "payout": payout, "net": payout - bet, "chips": w["chips"]}
            return self._once(conn, gid, uid, rid, "slots", inner)
        return self._tx(do)

    def slot_machine(self, gid: int, uid: int, theme: str, bet, rid, machine: str = "", name: str = "") -> dict:
        """A spin on one of the themed machines (strip_games.SLOT_MACHINES): its reels, pay table and limits."""
        self.guard(gid, uid)
        rid = self._rid(rid)
        if theme not in SLOT_MACHINES:
            raise CasinoError("bad_machine", "No such machine.")
        lo, hi = SLOT_MACHINES[theme]["limits"]
        bet = self._amount(bet, lo, hi)

        def do(conn):
            def inner():
                w = self._wallet(conn, gid, uid)
                if w["chips"] < bet:
                    raise CasinoError("broke", f"You need {bet:,} chips for that spin. Visit the cashier.", have=w["chips"])
                reels = slot_spin(theme, self.rng)
                mult, hit = slot_pay(theme, reels)
                payout = bet * mult
                w["chips"] += payout - bet
                self._save(conn, gid, uid, w)
                self._ledger(conn, gid, uid, rid, "slots", "slots", w, chips_delta=payout - bet, wager=bet, payout=payout,
                             ref=str(machine)[:16], reels=reels, theme=theme, hit=hit, name=self._name(name))
                self._stat(conn, gid, uid, "slots", bet, payout)
                return {"theme": theme, "reels": reels, "mult": mult, "hit": hit, "bet": bet, "payout": payout,
                        "net": payout - bet, "chips": w["chips"]}
            return self._once(conn, gid, uid, rid, "slots", inner)
        return self._tx(do)

    @staticmethod
    def config() -> dict:
        """Everything the game needs to show the games: machines and pay tables, table limits and seats, clocks."""
        return {"slots": slot_config(), "blackjack": {t: {"limits": list(TABLES[t]), "seats": TABLE_SEATS[t]} for t in TABLES},
                "roulette": {t: {"limits": list(v), "max_bets": RL_MAX_PER_PLAYER} for t, v in ROULETTE_TABLES.items()},
                "clocks": {"roulette_bet": RL_BET_SECS, "bj_bet": BJ_BET_SECS, "turn": TURN_SECS}}

    # ------------------------------------------------------------------ shared roulette (chips staked when the bet is placed)
    def _rl_row(self, conn, gid: int, table: str):
        return conn.execute("SELECT round_id, status, opened, closes_at FROM strip_casino_rl_rounds "
                            "WHERE guild_id=? AND table_id=? AND status='open' ORDER BY opened DESC LIMIT 1", (gid, table)).fetchone()

    def _rl_view(self, conn, gid: int, table: str, row) -> dict:
        bets = conn.execute("SELECT user_id, name, space, amount FROM strip_casino_rl_bets WHERE guild_id=? AND round_id=? AND status='live' "
                            "ORDER BY id", (gid, row[0])).fetchall() if row else []
        hist = conn.execute("SELECT number FROM strip_casino_rl_rounds WHERE guild_id=? AND table_id=? AND status='spun' "
                            "ORDER BY spun_at DESC LIMIT 20", (gid, table)).fetchall()
        return {"table": table, "round_id": row[0] if row else None, "status": row[1] if row else None,
                "closes_at": row[3] if row else None, "now": self.now(), "history": [h[0] for h in reversed(hist)],
                "bets": [{"user_id": str(b[0]), "name": b[1], "space": b[2], "amount": b[3]} for b in bets]}

    @staticmethod
    def _rl_table(table: str) -> tuple[int, int]:
        if table not in ROULETTE_TABLES:
            raise CasinoError("bad_table", "No such table.")
        return ROULETTE_TABLES[table]

    def rl_open(self, gid: int, table: str) -> dict:
        """The table's open round (made if there is none): exactly one per table."""
        self._rl_table(table)

        def do(conn):
            row = self._rl_row(conn, gid, table)
            if not row:
                rnd = f"{table}-{secrets.token_hex(6)}"
                conn.execute("INSERT INTO strip_casino_rl_rounds (guild_id, round_id, table_id, status, opened) VALUES (?, ?, ?, 'open', ?)",
                             (gid, rnd, table, self.now()))
                row = self._rl_row(conn, gid, table)
            return self._rl_view(conn, gid, table, row)
        return self._tx(do)

    def rl_state(self, gid: int, table: str) -> dict:
        self._rl_table(table)
        return self._tx(lambda conn: self._rl_view(conn, gid, table, self._rl_row(conn, gid, table)))

    def rl_bet(self, gid: int, uid: int, table: str, round_id: str, space, amount, rid, name: str = "", vip_ok: bool = False) -> dict:
        self.guard(gid, uid)
        rid = self._rid(rid)
        lo, hi = self._rl_table(table)
        if table.startswith("vip") and not vip_ok:
            raise CasinoError("vip_only", "That wheel is for VIP members.")
        space = str(space or "").strip().lower()
        if roulette_numbers(space) is None:
            raise CasinoError("bad_space", "That's not a bet on this table.")
        amount = self._amount(amount, lo, hi)

        def do(conn):
            def inner():
                row = self._rl_row(conn, gid, table)
                if not row or row[0] != round_id:
                    raise CasinoError("closed", "No more bets: the wheel has moved on. Bet on the next spin.")
                if row[3] is not None and self.now() >= row[3]:
                    raise CasinoError("closed", "No more bets.")
                mine = conn.execute("SELECT COUNT(*) FROM strip_casino_rl_bets WHERE guild_id=? AND round_id=? AND user_id=? AND status='live'",
                                    (gid, round_id, uid)).fetchone()[0]
                total = conn.execute("SELECT COUNT(*) FROM strip_casino_rl_bets WHERE guild_id=? AND round_id=? AND status='live'",
                                     (gid, round_id)).fetchone()[0]
                if mine >= RL_MAX_PER_PLAYER or total >= ROULETTE_MAX_BETS:
                    raise CasinoError("limit", f"Up to {RL_MAX_PER_PLAYER} bets a spin.")
                w = self._wallet(conn, gid, uid)
                if w["chips"] < amount:
                    raise CasinoError("broke", f"You only have {w['chips']:,} chips.", have=w["chips"])
                w["chips"] -= amount
                self._save(conn, gid, uid, w)
                conn.execute("INSERT INTO strip_casino_rl_bets (guild_id, round_id, user_id, rid, space, amount, name, status) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, 'live')", (gid, round_id, uid, rid, space, amount, self._name(name)))
                self._ledger(conn, gid, uid, rid, "roulette_bet", "roulette", w, chips_delta=-amount, ref=round_id, space=space, table=table)
                if row[3] is None:
                    conn.execute("UPDATE strip_casino_rl_rounds SET closes_at=? WHERE guild_id=? AND round_id=?",
                                 (self.now() + RL_BET_SECS, gid, round_id))
                return {"chips": w["chips"], "round": self._rl_view(conn, gid, table, self._rl_row(conn, gid, table))}
            return self._once(conn, gid, uid, rid, "rl_bet", inner)
        return self._tx(do)

    def rl_clear(self, gid: int, uid: int, table: str, round_id: str, rid) -> dict:
        """Take your bets back while betting is still open."""
        self.guard(gid, uid)
        rid = self._rid(rid)
        self._rl_table(table)

        def do(conn):
            def inner():
                row = self._rl_row(conn, gid, table)
                if not row or row[0] != round_id or (row[3] is not None and self.now() >= row[3]):
                    raise CasinoError("closed", "No more bets: too late to take them back.")
                bets = conn.execute("SELECT id, amount, rid FROM strip_casino_rl_bets WHERE guild_id=? AND round_id=? AND user_id=? AND status='live'",
                                    (gid, round_id, uid)).fetchall()
                w = self._wallet(conn, gid, uid)
                back = sum(b[1] for b in bets)
                if back:
                    w["chips"] += back
                    self._save(conn, gid, uid, w)
                    conn.executemany("UPDATE strip_casino_rl_bets SET status='cleared' WHERE id=?", [(b[0],) for b in bets])
                    self._ledger(conn, gid, uid, rid, "roulette_refund", "roulette", w, chips_delta=back, ref=round_id, bets=len(bets))
                return {"refunded": back, "chips": w["chips"], "round": self._rl_view(conn, gid, table, row)}
            return self._once(conn, gid, uid, rid, "rl_clear", inner)
        return self._tx(do)

    def rl_spin(self, gid: int, table: str, round_id: str) -> dict:
        """Close the round and spin once; every live bet is paid in this one transaction. Replays by round id."""
        self._rl_table(table)

        def do(conn):
            row = conn.execute("SELECT status, closes_at, reply, table_id FROM strip_casino_rl_rounds WHERE guild_id=? AND round_id=?",
                               (gid, str(round_id))).fetchone()
            if not row or row[3] != table:
                raise CasinoError("bad_round", "No such round.")
            if row[0] == "spun":
                return {**json.loads(row[2]), "replayed": True}
            if row[1] is None:
                raise CasinoError("no_bets", "Nobody has bet yet.")
            if self.now() < row[1]:
                raise CasinoError("too_early", "Betting is still open.", wait=row[1] - self.now())
            number = self.rng.randint(0, 36)
            bets = conn.execute("SELECT id, user_id, rid, space, amount, name FROM strip_casino_rl_bets WHERE guild_id=? AND round_id=? "
                                "AND status='live' ORDER BY id", (gid, round_id)).fetchall()
            results = []
            for bid, uid, brid, space, amount, name in bets:
                payout = amount * roulette_mult(space, number)
                w = self._wallet(conn, gid, uid)
                if payout:
                    w["chips"] += payout
                    self._save(conn, gid, uid, w)
                self._ledger(conn, gid, uid, f"spin:{brid}", "roulette", "roulette", w, chips_delta=payout, wager=amount, payout=payout,
                             ref=round_id, space=space, number=number, name=name, table=table)
                self._stat(conn, gid, uid, "roulette", amount, payout)
                conn.execute("UPDATE strip_casino_rl_bets SET status='settled', payout=? WHERE id=?", (payout, bid))
                results.append({"user_id": str(uid), "rid": brid, "name": name, "space": space, "amount": amount,
                                "payout": payout, "net": payout - amount, "chips": w["chips"]})
            reply = {"round_id": round_id, "table": table, "number": number, "color": roulette_colour(number), "results": results}
            conn.execute("UPDATE strip_casino_rl_rounds SET status='spun', number=?, reply=?, spun_at=? WHERE guild_id=? AND round_id=?",
                         (number, json.dumps(reply), self.now(), gid, round_id))
            return reply
        return self._tx(do)

    # ------------------------------------------------------------------ shared blackjack tables (seats play in turn)
    def _bjt_row(self, conn, gid: int, table: str, open_only: bool = True):
        q = ("SELECT round_id, phase, seed, seats, moves, closes_at, turn_deadline, updated FROM strip_casino_bjt WHERE guild_id=? AND table_id=? "
             + ("AND phase != 'done' " if open_only else "") + "ORDER BY created DESC LIMIT 1")
        return conn.execute(q, (gid, table)).fetchone()

    @staticmethod
    def _bjt_game(row) -> TableRound | None:
        if row[1] == "betting":
            return None
        seats = json.loads(row[3])
        g = TableRound(row[2], sorted((s["seat"], s["bet"]) for s in seats))
        for seat, action in json.loads(row[4]):
            g.act(seat, action)
        return g

    def _bjt_view(self, table: str, row, g: TableRound | None) -> dict:
        if not row:
            return {"table": table, "round_id": None, "phase": None, "seats": [], "now": self.now()}
        seats = json.loads(row[3])
        done = g is not None and g.phase == "done"
        out = {"table": table, "round_id": row[0], "phase": row[1], "closes_at": row[5], "turn_deadline": row[6], "now": self.now(),
               "turn_seat": g.seat if g else None, "actions": sorted(g.actions()) if g else [],
               "costs": {a: g.cost(a) for a in ("double", "split") if g and a in g.actions()},
               "dealer": ([card_code(c) for c in g.dealer] if done else [card_code(g.dealer[0]), "??"]) if g else [],
               "dealer_total": (hand_value(g.dealer)[0] if done else g.dealer[0].value) if g else None, "seats": []}
        for s in sorted(seats, key=lambda x: x["seat"]):
            hs = g.hands[s["seat"]] if g else []
            out["seats"].append({
                "seat": s["seat"], "user_id": str(s["uid"]), "name": s["name"], "bet": s["bet"], "staked": s["staked"],
                "active": g.active[s["seat"]] if g else 0,
                "hands": [{"cards": [card_code(c) for c in h.cards], "total": h.total, "soft": h.soft, "bet": h.bet, "doubled": h.doubled,
                           "done": h.done, "outcome": h.outcome, "payout": h.payout} for h in hs],
                "returned": g.returned(s["seat"]) if g else 0})
        return out

    def _bjt_settle(self, conn, gid: int, table: str, round_id: str, seats: list, g: TableRound) -> None:
        for s in seats:
            if s["staked"] != g.wagered(s["seat"]):   # the chips taken must equal what's on the table, or something is wrong
                raise CasinoError("ledger_mismatch", "Table stake mismatch.")
        for s in seats:
            ret = g.returned(s["seat"])
            w = self._wallet(conn, gid, s["uid"])
            w["chips"] += ret
            self._save(conn, gid, s["uid"], w)
            self._ledger(conn, gid, s["uid"], f"bjt:{round_id}:{s['seat']}", "bj_payout", "blackjack", w, chips_delta=ret, wager=s["staked"],
                         payout=ret, ref=round_id, table=table, outcomes=[h.outcome for h in g.hands[s["seat"]]], name=s["name"])
            self._stat(conn, gid, s["uid"], "blackjack", s["staked"], ret)
        conn.execute("UPDATE strip_casino_bjt SET phase='done', turn_deadline=NULL, updated=? WHERE guild_id=? AND round_id=?",
                     (self.now(), gid, round_id))

    def _bjt_after(self, conn, gid: int, table: str, round_id: str, seats: list, moves: list, g: TableRound, turn_changed: bool) -> None:
        if g.phase == "done":
            conn.execute("UPDATE strip_casino_bjt SET seats=?, moves=? WHERE guild_id=? AND round_id=?",
                         (json.dumps(seats), json.dumps(moves), gid, round_id))
            self._bjt_settle(conn, gid, table, round_id, seats, g)
        else:
            conn.execute("UPDATE strip_casino_bjt SET phase='player', seats=?, moves=?, turn_deadline=?, updated=? WHERE guild_id=? AND round_id=?",
                         (json.dumps(seats), json.dumps(moves), self.now() + TURN_SECS, self.now(), gid, round_id))

    @staticmethod
    def _bjt_table(table: str) -> tuple[int, int]:
        if table not in TABLE_SEATS:
            raise CasinoError("bad_table", "No such table.")
        return TABLES[table]

    def bjt_state(self, gid: int, table: str) -> dict:
        """The table's current round, or the last finished one (for the result on screen)."""
        self._bjt_table(table)

        def do(conn):
            row = self._bjt_row(conn, gid, table) or self._bjt_row(conn, gid, table, open_only=False)
            return self._bjt_view(table, row, self._bjt_game(row) if row else None)
        return self._tx(do)

    def bjt_bet(self, gid: int, uid: int, table: str, seat, bet, rid, vip_ok: bool = False, name: str = "") -> dict:
        self.guard(gid, uid)
        rid = self._rid(rid)
        lo, hi = self._bjt_table(table)
        if table == "vip" and not vip_ok:
            raise CasinoError("vip_only", "That table is for VIP members.")
        if not isinstance(seat, int) or not 0 <= seat < TABLE_SEATS[table]:
            raise CasinoError("bad_seat", "Sit down first.")
        bet = self._amount(bet, lo, hi)

        def do(conn):
            def inner():
                row = self._bjt_row(conn, gid, table)
                now = self.now()
                if row and row[1] != "betting":
                    raise CasinoError("closed", "Cards are out. You're in on the next hand.")
                if row and now >= row[5]:
                    raise CasinoError("closed", "Betting just closed. Next hand.")
                seats = json.loads(row[3]) if row else []
                if any(s["uid"] == uid for s in seats):
                    raise CasinoError("already_bet", "Your bet is already on the table.")
                if any(s["seat"] == seat for s in seats):
                    raise CasinoError("seat_taken", "Someone has a bet on that seat.")
                w = self._wallet(conn, gid, uid)
                if w["chips"] < bet:
                    raise CasinoError("broke", f"You need {bet:,} chips. Visit the cashier.", have=w["chips"])
                if not row:
                    round_id = f"{table}-{secrets.token_hex(6)}"
                    conn.execute("INSERT INTO strip_casino_bjt (guild_id, round_id, table_id, phase, seed, closes_at, created, updated) "
                                 "VALUES (?, ?, ?, 'betting', ?, ?, ?, ?)", (gid, round_id, table, secrets.randbits(62), now + BJ_BET_SECS, now, now))
                else:
                    round_id = row[0]
                w["chips"] -= bet
                self._save(conn, gid, uid, w)
                seats.append({"seat": seat, "uid": uid, "name": self._name(name), "bet": bet, "staked": bet})
                conn.execute("UPDATE strip_casino_bjt SET seats=?, updated=? WHERE guild_id=? AND round_id=?", (json.dumps(seats), now, gid, round_id))
                self._ledger(conn, gid, uid, rid, "bj_stake", "blackjack", w, chips_delta=-bet, wager=0, ref=round_id, table=table, seat=seat)
                row = self._bjt_row(conn, gid, table)
                return {"chips": w["chips"], "round": self._bjt_view(table, row, None)}
            return self._once(conn, gid, uid, rid, "bjt_bet", inner)
        return self._tx(do)

    def bjt_unbet(self, gid: int, uid: int, table: str, rid) -> dict:
        """Take your bet back before the cards are dealt."""
        self.guard(gid, uid)
        rid = self._rid(rid)
        self._bjt_table(table)

        def do(conn):
            def inner():
                row = self._bjt_row(conn, gid, table)
                if not row or row[1] != "betting" or self.now() >= row[5]:
                    raise CasinoError("closed", "Too late: the cards are coming.")
                seats = json.loads(row[3])
                mine = [s for s in seats if s["uid"] == uid]
                if not mine:
                    raise CasinoError("no_bet", "You have no bet on this table.")
                w = self._wallet(conn, gid, uid)
                w["chips"] += mine[0]["staked"]
                self._save(conn, gid, uid, w)
                self._ledger(conn, gid, uid, rid, "bj_refund", "blackjack", w, chips_delta=mine[0]["staked"], ref=row[0], table=table)
                rest = [s for s in seats if s["uid"] != uid]
                if rest:
                    conn.execute("UPDATE strip_casino_bjt SET seats=?, updated=? WHERE guild_id=? AND round_id=?", (json.dumps(rest), self.now(), gid, row[0]))
                else:   # nobody left: the round never happened
                    conn.execute("DELETE FROM strip_casino_bjt WHERE guild_id=? AND round_id=?", (gid, row[0]))
                row = self._bjt_row(conn, gid, table)
                return {"chips": w["chips"], "round": self._bjt_view(table, row, None)}
            return self._once(conn, gid, uid, rid, "bjt_unbet", inner)
        return self._tx(do)

    def bjt_deal(self, gid: int, table: str, round_id: str, early: bool = False) -> dict:
        """Close betting and deal (the game server calls this when the clock runs out, or early once every seat has bet)."""
        self._bjt_table(table)

        def do(conn):
            row = conn.execute("SELECT round_id, phase, seed, seats, moves, closes_at, turn_deadline, updated FROM strip_casino_bjt "
                               "WHERE guild_id=? AND round_id=? AND table_id=?", (gid, str(round_id), table)).fetchone()
            if not row:
                raise CasinoError("bad_round", "No such round.")
            if row[1] != "betting":   # already dealt: same answer again
                return {**self._bjt_view(table, row, self._bjt_game(row)), "replayed": True}
            if self.now() < row[5] and not early:
                raise CasinoError("too_early", "Betting is still open.", wait=row[5] - self.now())
            seats = json.loads(row[3])
            row = (row[0], "player", *row[2:])
            g = self._bjt_game(row)
            self._bjt_after(conn, gid, table, round_id, seats, [], g, True)
            row = conn.execute("SELECT round_id, phase, seed, seats, moves, closes_at, turn_deadline, updated FROM strip_casino_bjt "
                               "WHERE guild_id=? AND round_id=?", (gid, round_id)).fetchone()
            return self._bjt_view(table, row, g)
        return self._tx(do)

    def bjt_act(self, gid: int, uid: int, table: str, action: str, rid, name: str = "") -> dict:
        self.guard(gid, uid)
        rid = self._rid(rid)
        self._bjt_table(table)
        action = str(action or "")

        def do(conn):
            def inner():
                row = self._bjt_row(conn, gid, table)
                if not row or row[1] != "player":
                    raise CasinoError("no_hand", "No hand in play at this table.")
                seats, moves = json.loads(row[3]), json.loads(row[4])
                mine = next((s for s in seats if s["uid"] == uid), None)
                if not mine:
                    raise CasinoError("no_hand", "You're not in this hand.")
                g = self._bjt_game(row)
                if g.seat != mine["seat"]:
                    raise CasinoError("not_your_turn", "Wait for your turn.")
                if action not in g.actions():
                    raise CasinoError("bad_action", f"You can't {action} right now.", actions=sorted(g.actions()))
                cost = g.cost(action)
                w = self._wallet(conn, gid, uid)
                if cost:
                    if w["chips"] < cost:
                        raise CasinoError("broke", f"You need {cost:,} more chips to {action}.", have=w["chips"])
                    w["chips"] -= cost
                    self._save(conn, gid, uid, w)
                    mine["staked"] += cost
                    self._ledger(conn, gid, uid, rid, f"bj_{action}", "blackjack", w, chips_delta=-cost, ref=row[0], table=table)
                g.act(mine["seat"], action)
                moves.append([mine["seat"], action])
                self._bjt_after(conn, gid, table, row[0], seats, moves, g, True)
                row2 = conn.execute("SELECT round_id, phase, seed, seats, moves, closes_at, turn_deadline, updated FROM strip_casino_bjt "
                                    "WHERE guild_id=? AND round_id=?", (gid, row[0])).fetchone()
                return {"chips": self._wallet(conn, gid, uid)["chips"], "round": self._bjt_view(table, row2, g)}
            return self._once(conn, gid, uid, rid, "bjt_act", inner)
        return self._tx(do)

    def bjt_timeout(self, gid: int, table: str, round_id: str, force: bool = False) -> dict:
        """The decision clock ran out: the hand whose turn it is stands. (force: finish an abandoned round.)"""
        self._bjt_table(table)

        def do(conn):
            row = conn.execute("SELECT round_id, phase, seed, seats, moves, closes_at, turn_deadline, updated FROM strip_casino_bjt "
                               "WHERE guild_id=? AND round_id=? AND table_id=?", (gid, str(round_id), table)).fetchone()
            if not row:
                raise CasinoError("bad_round", "No such round.")
            if row[1] != "player":
                return self._bjt_view(table, row, self._bjt_game(row) if row[1] == "done" else None)
            if not force and self.now() < (row[6] or 0) - 0.5:
                raise CasinoError("too_early", "There's still time on the clock.", wait=row[6] - self.now())
            seats, moves = json.loads(row[3]), json.loads(row[4])
            g = self._bjt_game(row)
            while g.phase != "done":
                seat = g.seat
                g.act(seat, "stand")
                moves.append([seat, "stand"])
                if not force:
                    break
            self._bjt_after(conn, gid, table, row[0], seats, moves, g, True)
            row = conn.execute("SELECT round_id, phase, seed, seats, moves, closes_at, turn_deadline, updated FROM strip_casino_bjt "
                               "WHERE guild_id=? AND round_id=?", (gid, row[0])).fetchone()
            return {**self._bjt_view(table, row, g), "timed_out": True}
        return self._tx(do)

    def casino_sweep(self, gid: int) -> dict:
        """Finish what an absent game server left behind: deal and stand abandoned blackjack rounds, spin closed wheels."""
        now, done = self.now(), {"bj": 0, "roulette": 0}
        for rnd, table, phase, closes, updated in self.db._read(
                "SELECT round_id, table_id, phase, closes_at, updated FROM strip_casino_bjt WHERE guild_id=? AND phase != 'done'", (gid,)):
            if phase == "betting" and now >= closes + BJ_STALE:
                self.bjt_deal(gid, table, rnd)
                phase = "player"; updated = now - BJ_STALE
            if phase == "player" and now - updated >= BJ_STALE:
                self.bjt_timeout(gid, table, rnd, force=True)
                done["bj"] += 1
        for rnd, table in self.db._read("SELECT round_id, table_id FROM strip_casino_rl_rounds WHERE guild_id=? AND status='open' "
                                        "AND closes_at IS NOT NULL AND closes_at < ?", (gid, now - RL_STALE)):
            self.rl_spin(gid, table, rnd)
            done["roulette"] += 1
        self.bj_sweep(gid)
        return done

    # ------------------------------------------------------------------ roulette (one shared spin per round)
    def roulette_round(self, gid: int, round_id: str, bets: list, table: str = "roulette") -> dict:
        """Settle a whole round: one spin, every bet paid or taken in one transaction. Replays by round id."""
        if not RID.match(str(round_id or "")):
            raise CasinoError("bad_round", "Bad round id.")
        if table not in ROULETTE_TABLES:
            raise CasinoError("bad_table", "No such table.")
        lo, hi = ROULETTE_TABLES[table]
        if not isinstance(bets, list) or len(bets) > ROULETTE_MAX_BETS:
            raise CasinoError("bad_bets", "Bad bet list.")
        clean = []
        for b in bets:
            if not isinstance(b, dict):
                raise CasinoError("bad_bets", "Bad bet.")
            try:
                uid = int(b.get("user_id"))
            except (TypeError, ValueError):
                raise CasinoError("bad_bets", "Bad player.")
            space = str(b.get("space", "")).strip().lower()
            if not eco.valid_roulette_space(space):
                raise CasinoError("bad_bets", f"Not a space on the table: {space}")
            clean.append({"uid": uid, "space": space, "amount": self._amount(b.get("amount"), lo, hi),
                          "rid": self._rid(b.get("rid")), "name": self._name(b.get("name"))})
        for uid in {b["uid"] for b in clean}:
            self.guard(gid, uid)

        def do(conn):
            row = conn.execute("SELECT reply FROM strip_casino_rounds WHERE guild_id=? AND round_id=?", (gid, round_id)).fetchone()
            if row:
                return {**json.loads(row[0]), "replayed": True}
            number = eco.roulette_spin(self.rng)
            results = []
            for b in clean:
                def inner(b=b):
                    w = self._wallet(conn, gid, b["uid"])
                    if w["chips"] < b["amount"]:
                        return {"accepted": False, "reason": "broke", "payout": 0, "net": 0, "chips": w["chips"]}
                    mult = eco.roulette_multiplier(b["space"], number)
                    payout = b["amount"] * mult
                    w["chips"] += payout - b["amount"]
                    self._save(conn, gid, b["uid"], w)
                    self._ledger(conn, gid, b["uid"], b["rid"], "roulette", "roulette", w, chips_delta=payout - b["amount"],
                                 wager=b["amount"], payout=payout, ref=round_id, space=b["space"], number=number, name=b["name"])
                    self._stat(conn, gid, b["uid"], "roulette", b["amount"], payout)
                    return {"accepted": True, "payout": payout, "net": payout - b["amount"], "chips": w["chips"]}
                r = self._once(conn, gid, b["uid"], b["rid"], "roulette", inner)
                results.append({"user_id": str(b["uid"]), "rid": b["rid"], "space": b["space"], "amount": b["amount"], **r})
            reply = {"round_id": round_id, "table": table, "number": number,
                     "color": "green" if number == 0 else ("red" if number in eco.ROULETTE_RED else "black"), "results": results}
            conn.execute("INSERT INTO strip_casino_rounds VALUES (?, ?, ?, ?, ?)", (gid, round_id, number, json.dumps(reply), self.now()))
            return reply
        return self._tx(do)

    # ------------------------------------------------------------------ blackjack
    @staticmethod
    def _apply(g: Blackjack, action: str) -> None:
        if action == "insurance":
            g.decide_insurance(True)
        elif action == "no_insurance":
            g.decide_insurance(False)
        else:
            getattr(g, action)()

    def _rebuild(self, seed: int, bet: int, actions: list) -> Blackjack:
        g = Blackjack(bet, Shoe(rng=random.Random(seed)))
        g.deal()
        for a in actions:
            self._apply(g, a)
        return g

    @staticmethod
    def _view(g: Blackjack, hand_id: str, table: str) -> dict:
        done = g.phase == "done"
        dealer = [_card(c) for c in g.dealer] if done else [_card(g.dealer[0]), "??"]
        return {
            "hand_id": hand_id, "table": table, "phase": g.phase, "active": g.active,
            "hands": [{"cards": [_card(c) for c in h.cards], "total": h.total, "soft": h.soft, "bet": h.bet,
                       "doubled": h.doubled, "done": h.done, "outcome": h.outcome, "payout": h.payout} for h in g.hands],
            "dealer": dealer, "dealer_total": hand_value(g.dealer)[0] if done else g.dealer[0].value,
            "actions": sorted(g.actions()), "wagered": g.wagered, "returned": g.returned if done else 0,
            "net": g.net if done else 0, "insurance": g.insurance_bet, "even_money": g.even_money,
            "costs": {a: g.cost(a) for a in ("double", "split", "insurance") if a in g.actions()},
        }

    def _settle_hand(self, conn, gid, uid, hand_id, table, g: Blackjack, staked: int, rid: str, name: str) -> dict:
        if staked != g.wagered:   # the chips taken must equal what's on the table, or something is wrong
            raise CasinoError("ledger_mismatch", "Hand stake mismatch.")
        w = self._wallet(conn, gid, uid)
        w["chips"] += g.returned
        self._save(conn, gid, uid, w)
        self._ledger(conn, gid, uid, rid, "bj_payout", "blackjack", w, chips_delta=g.returned, wager=g.wagered, payout=g.returned,
                     ref=hand_id, outcomes=[h.outcome for h in g.hands], name=name)
        self._stat(conn, gid, uid, "blackjack", g.wagered, g.returned)
        conn.execute("UPDATE strip_casino_hands SET settled=1, updated=? WHERE hand_id=?", (self.now(), hand_id))
        return w

    def _finish_stale(self, conn, gid: int, uid: int, row) -> None:
        """Auto-stand an idle hand (the player walked off or disconnected) and settle it."""
        hand_id, table, seed, bet, actions, staked = row
        actions = json.loads(actions)
        g = self._rebuild(seed, bet, actions)
        while g.phase != "done":
            a = "no_insurance" if g.phase == "insurance" else "stand"
            self._apply(g, a)
            actions.append(a)
        conn.execute("UPDATE strip_casino_hands SET actions=? WHERE hand_id=?", (json.dumps(actions), hand_id))
        self._settle_hand(conn, gid, uid, hand_id, table, g, staked, f"auto:{hand_id}", "")

    def _open_hand(self, conn, gid: int, uid: int):
        row = conn.execute("SELECT hand_id, table_id, seed, bet, actions, staked, updated FROM strip_casino_hands "
                           "WHERE guild_id=? AND user_id=? AND settled=0", (gid, uid)).fetchone()
        if row and self.now() - row[6] > BJ_IDLE:
            self._finish_stale(conn, gid, uid, row[:6])
            return None
        return row

    def bj_start(self, gid: int, uid: int, table: str, bet, rid, vip_ok: bool = False, name: str = "") -> dict:
        self.guard(gid, uid)
        rid = self._rid(rid)
        if table not in TABLES:
            raise CasinoError("bad_table", "No such table.")
        if table == "vip" and not vip_ok:
            raise CasinoError("vip_only", "That table is for VIP members.")
        lo, hi = TABLES[table]
        bet = self._amount(bet, lo, hi)

        def do(conn):
            def inner():
                row = self._open_hand(conn, gid, uid)
                if row:
                    raise CasinoError("in_hand", "Finish the hand you're playing first.", hand_id=row[0])
                w = self._wallet(conn, gid, uid)
                if w["chips"] < bet:
                    raise CasinoError("broke", f"You need {bet:,} chips. Visit the cashier.", have=w["chips"])
                seed, hand_id, now = secrets.randbits(62), secrets.token_hex(8), self.now()
                g = self._rebuild(seed, bet, [])
                w["chips"] -= bet
                self._save(conn, gid, uid, w)
                self._ledger(conn, gid, uid, rid, "bj_bet", "blackjack", w, chips_delta=-bet, wager=bet, ref=hand_id, table=table)
                conn.execute("INSERT INTO strip_casino_hands VALUES (?, ?, ?, ?, ?, ?, '[]', ?, 0, ?, ?)",
                             (hand_id, gid, uid, table, seed, bet, bet, now, now))
                if g.phase == "done":
                    w = self._settle_hand(conn, gid, uid, hand_id, table, g, bet, rid, self._name(name))
                return {**self._view(g, hand_id, table), "chips": w["chips"]}
            return self._once(conn, gid, uid, rid, "bj_start", inner)
        return self._tx(do)

    def bj_act(self, gid: int, uid: int, hand_id: str, action: str, rid, name: str = "") -> dict:
        self.guard(gid, uid)
        rid = self._rid(rid)
        action = str(action or "")

        def do(conn):
            def inner():
                row = conn.execute("SELECT hand_id, table_id, seed, bet, actions, staked FROM strip_casino_hands "
                                   "WHERE hand_id=? AND guild_id=? AND user_id=? AND settled=0", (str(hand_id), gid, uid)).fetchone()
                if not row:
                    raise CasinoError("no_hand", "That hand is already over.")
                _, table, seed, bet, actions, staked = row
                actions = json.loads(actions)
                g = self._rebuild(seed, bet, actions)
                if action not in g.actions():
                    raise CasinoError("bad_action", f"You can't {action.replace('_', ' ')} right now.", actions=sorted(g.actions()))
                cost = g.cost(action) if action in ("double", "split", "insurance") else 0
                w = self._wallet(conn, gid, uid)
                if cost:
                    if w["chips"] < cost:
                        raise CasinoError("broke", f"You need {cost:,} more chips to {action}.", have=w["chips"])
                    w["chips"] -= cost
                    self._save(conn, gid, uid, w)
                    self._ledger(conn, gid, uid, rid, f"bj_{action}", "blackjack", w, chips_delta=-cost, wager=cost, ref=hand_id)
                self._apply(g, action)
                actions.append(action)
                staked += cost
                conn.execute("UPDATE strip_casino_hands SET actions=?, staked=?, updated=? WHERE hand_id=?",
                             (json.dumps(actions), staked, self.now(), hand_id))
                if g.phase == "done":
                    w = self._settle_hand(conn, gid, uid, hand_id, table, g, staked, rid, self._name(name))
                return {**self._view(g, hand_id, table), "chips": w["chips"]}
            return self._once(conn, gid, uid, rid, "bj_act", inner)
        return self._tx(do)

    def bj_state(self, gid: int, uid: int) -> dict:
        self.guard(gid, uid)

        def do(conn):
            row = self._open_hand(conn, gid, uid)
            if not row:
                return {"hand": None}
            g = self._rebuild(row[2], row[3], json.loads(row[4]))
            return {"hand": self._view(g, row[0], row[1])}
        return self._tx(do)

    def bj_sweep(self, gid: int) -> dict:
        """Auto-stand every idle hand in this server (the game server calls this now and then)."""
        rows = self.db._read("SELECT user_id FROM strip_casino_hands WHERE guild_id=? AND settled=0 AND updated < ?",
                             (gid, self.now() - BJ_IDLE))
        done = 0
        for (uid,) in rows:
            def do(conn, uid=uid):
                return self._open_hand(conn, gid, uid)
            self._tx(do)
            done += 1
        return {"finished": done}

    # ------------------------------------------------------------------ progression & history
    def visit(self, gid: int, uid: int, rid) -> dict:
        self.guard(gid, uid)
        rid = self._rid(rid)

        def do(conn):
            def inner():
                conn.execute("INSERT INTO strip_casino_stats (guild_id, user_id, visits, last_visit) VALUES (?, ?, 1, ?) "
                             "ON CONFLICT(guild_id, user_id) DO UPDATE SET visits = visits + 1, last_visit = excluded.last_visit",
                             (gid, uid, self.now()))
                return {"visited": True}
            return self._once(conn, gid, uid, rid, "visit", inner)
        return self._tx(do)

    def stats(self, gid: int, uid: int) -> dict:
        self.guard(gid, uid)
        row = self.db._read("SELECT visits, wagered, returned, wins, losses, won_total, lost_total, biggest_win, biggest_loss, by_game "
                            "FROM strip_casino_stats WHERE guild_id=? AND user_id=?", (gid, uid))
        keys = ("visits", "wagered", "returned", "wins", "losses", "won_total", "lost_total", "biggest_win", "biggest_loss", "by_game")
        s = dict(zip(keys, row[0])) if row else {k: 0 for k in keys}
        by = json.loads(s.pop("by_game") or "{}") if row else {}
        fav = max(by.items(), key=lambda kv: (kv[1]["plays"], kv[1]["wagered"]))[0] if by else None
        return {"stats": {**s, "by_game": by, "favourite": fav}, "vip": vip_tier(s["wagered"]),
                "history": self._rows("guild_id=? AND user_id=?", (gid, uid), 30),
                "exchanges": self._rows("guild_id=? AND user_id=? AND kind IN ('buy_chips','sell_chips')", (gid, uid), 20)}

    def _rows(self, where: str, params: tuple, limit: int) -> list:
        rows = self.db._read(f"SELECT id, user_id, kind, game, ref, chips_delta, cash_delta, wager, payout, chips_after, cash_after, detail, ts "
                             f"FROM strip_casino_ledger WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit))
        out = []
        for r in rows:
            d = json.loads(r[11] or "{}")
            out.append({"id": r[0], "user_id": str(r[1]), "kind": r[2], "game": r[3], "ref": r[4], "chips": r[5], "cash": r[6],
                        "wager": r[7], "payout": r[8], "chips_after": r[9], "cash_after": r[10], "ts": r[12],
                        **{k: d[k] for k in ("reels", "space", "number", "outcomes", "name", "table", "theme", "hit") if k in d}})
        return out

    def recent(self, gid: int, limit: int = 12) -> dict:
        """The recent-winners board: settled bets that won at least BIG_WIN net."""
        rows = self._rows("guild_id=? AND kind IN ('slots','roulette','bj_payout') AND payout - wager >= ?", (gid, BIG_WIN), limit)
        return {"winners": [{"user_id": r["user_id"], "name": r.get("name", ""), "game": r["game"], "net": r["payout"] - r["wager"],
                             "ts": r["ts"]} for r in rows]}
