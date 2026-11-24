"""THE STRIP homes: purchases for properties and furniture, and household storage, settled by SYKE.

The game server owns the map of who lives where (properties, furniture placement). Money and items stay
here, in SYKE, the single source of truth:

* purchase(): a price set by the game server (property, rent, furniture, upgrade), paid from cash on hand.
  Idempotent by request id (a retry never charges twice) and written to an append-only ledger.
* store() / take(): move items between the player's bag (econ_items) and a home's storage box. The item
  leaves one place and arrives in the other in the same transaction, so nothing can be duplicated.
* release(): when someone moves out, everything in that home's storage goes back to their bag.
* purchase_split(): a shared household's bill split between the two partners (larger half to the one
  asking), both paid in one transaction or neither.
* transfer(): a household member with storage permission takes an item from their partner's box at the
  shared home. Logged with who, from whom and when.
"""

from __future__ import annotations

import json
import re
import time

from . import stripworld as sw

SCHEMA = """
CREATE TABLE IF NOT EXISTS strip_home_storage (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, home TEXT NOT NULL, item TEXT NOT NULL, qty INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, home, item)
);
CREATE TABLE IF NOT EXISTS strip_home_requests (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, rid TEXT NOT NULL, op TEXT NOT NULL, reply TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id, rid)
);
CREATE TABLE IF NOT EXISTS strip_home_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, rid TEXT NOT NULL,
    kind TEXT NOT NULL, amount INTEGER NOT NULL, cash_after INTEGER NOT NULL, detail TEXT NOT NULL DEFAULT '{}', ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS strip_home_ledger_user ON strip_home_ledger (guild_id, user_id, id);
CREATE TABLE IF NOT EXISTS strip_home_item_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, home TEXT NOT NULL, from_user INTEGER NOT NULL, to_user INTEGER NOT NULL,
    item TEXT NOT NULL, qty INTEGER NOT NULL, rid TEXT NOT NULL, ts REAL NOT NULL
);
"""
PURCHASE_MAX = 1_000_000          # the most expensive thing on the Strip (a penthouse)
RESELL_RATE = 0.5                 # selling furniture back pays half what you paid
RESELLABLE = ("furniture",)
STORE_MAX_QTY = 99
RID = re.compile(r"^[A-Za-z0-9_:.\-]{6,80}$")
HOME = re.compile(r"^[A-Za-z0-9_:\-]{1,60}$")


class HomeError(Exception):
    def __init__(self, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def eligible(item: str) -> bool:
    """Items that can be kept at home: anything from the shops or a record."""
    from .strip_kitchen import is_food   # groceries and cooked food keep at home too
    return item in sw.ITEM_BY_KEY or item in getattr(sw, "RECORD_BY_KEY", {}) or is_food(item)


class HomeService:
    def __init__(self, storage, guard, clock=time.time) -> None:
        self.db, self.guard, self.now = storage, guard, clock
        with storage._lock:
            storage._conn.executescript(SCHEMA)
            storage._conn.commit()

    def _tx(self, fn):
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

    def _once(self, conn, gid, uid, rid, op, do):
        rid = str(rid or "")
        if not RID.match(rid):
            raise HomeError("bad_rid", "Every home purchase needs a request id.")
        row = conn.execute("SELECT op, reply FROM strip_home_requests WHERE guild_id=? AND user_id=? AND rid=?", (gid, uid, rid)).fetchone()
        if row:
            if row[0] != op:
                raise HomeError("rid_reused", "That request id was already used.")
            return {**json.loads(row[1]), "replayed": True}
        reply = do()
        conn.execute("INSERT INTO strip_home_requests VALUES (?, ?, ?, ?, ?, ?)", (gid, uid, rid, op, json.dumps(reply), self.now()))
        return reply

    @staticmethod
    def _home(home) -> str:
        home = str(home or "")
        if not HOME.match(home):
            raise HomeError("bad_home", "Which home?")
        return home

    @staticmethod
    def _qty(qty) -> int:
        try:
            q = int(qty)
        except (TypeError, ValueError):
            raise HomeError("bad_qty", "How many?")
        if q < 1 or q > STORE_MAX_QTY:
            raise HomeError("bad_qty", "How many?")
        return q

    def _held(self, conn, gid, uid, item) -> int:
        row = conn.execute("SELECT qty FROM econ_items WHERE guild_id=? AND user_id=? AND item=?", (gid, uid, item)).fetchone()
        return row[0] if row else 0

    def _bag_add(self, conn, gid, uid, item, delta) -> int:
        new = self._held(conn, gid, uid, item) + delta
        if new < 0:
            raise HomeError("no_item", "You don't have that.")
        conn.execute("INSERT INTO econ_items VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id, item) DO UPDATE SET qty=excluded.qty", (gid, uid, item, new))
        return new

    def _box(self, conn, gid, uid, home) -> dict:
        return {i: q for i, q in conn.execute("SELECT item, qty FROM strip_home_storage WHERE guild_id=? AND user_id=? AND home=? AND qty > 0", (gid, uid, home))}

    # ---------------------------------------------------------------- money
    def purchase(self, gid: int, uid: int, amount, kind: str, rid, detail: dict | None = None) -> dict:
        self.guard(gid, uid)
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            raise HomeError("bad_amount", "That's not a price.")
        if amount < 1 or amount > PURCHASE_MAX:
            raise HomeError("bad_amount", "That's not a price.")
        kind = re.sub(r"[^a-z_]", "", str(kind or "purchase"))[:24] or "purchase"

        def do(conn):
            def inner():
                w = self.db._wallet_row(conn, gid, uid)
                if w["cash"] < amount:
                    raise HomeError("broke", f"That costs {amount:,}. You have {w['cash']:,} on you.", have=w["cash"])
                w["cash"] -= amount
                self.db._write_wallet(conn, gid, uid, w)
                conn.execute("INSERT INTO strip_home_ledger (guild_id, user_id, rid, kind, amount, cash_after, detail, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (gid, uid, str(rid), kind, amount, w["cash"], json.dumps(detail or {})[:400], self.now()))
                return {"amount": amount, "kind": kind, "cash": w["cash"]}
            return self._once(conn, gid, uid, rid, "purchase", inner)
        return self._tx(do)

    def purchase_split(self, gid: int, uid: int, partner, amount, kind: str, rid, detail: dict | None = None) -> dict:
        """Both partners pay half (the asker the larger half), in one transaction: both pay or nobody does."""
        self.guard(gid, uid)
        try:
            partner, amount = int(partner), int(amount)
        except (TypeError, ValueError):
            raise HomeError("bad_amount", "That's not a price.")
        if partner == uid:
            raise HomeError("bad_partner", "Split with whom?")
        self.guard(gid, partner)
        if amount < 2 or amount > PURCHASE_MAX:
            raise HomeError("bad_amount", "That's not a price.")
        kind = re.sub(r"[^a-z_]", "", str(kind or "purchase"))[:24] or "purchase"
        mine, theirs = (amount + 1) // 2, amount // 2

        def do(conn):
            def inner():
                a, b = self.db._wallet_row(conn, gid, uid), self.db._wallet_row(conn, gid, partner)
                if a["cash"] < mine:
                    raise HomeError("broke", f"Your half is {mine:,}. You have {a['cash']:,} on you.", have=a["cash"])
                if b["cash"] < theirs:
                    raise HomeError("partner_broke", f"Your partner's half is {theirs:,} and they don't have it on them.")
                for who, w, part in ((uid, a, mine), (partner, b, theirs)):
                    w["cash"] -= part
                    self.db._write_wallet(conn, gid, who, w)
                    conn.execute("INSERT INTO strip_home_ledger (guild_id, user_id, rid, kind, amount, cash_after, detail, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (gid, who, str(rid), kind, part, w["cash"], json.dumps({**(detail or {}), "split_with": partner if who == uid else uid})[:400], self.now()))
                return {"amount": amount, "mine": mine, "theirs": theirs, "kind": kind, "cash": a["cash"]}
            return self._once(conn, gid, uid, rid, "purchase_split", inner)
        return self._tx(do)

    # ---------------------------------------------------------------- the storage box
    def storage(self, gid: int, uid: int, home) -> dict:
        self.guard(gid, uid)
        home = self._home(home)
        return self._tx(lambda conn: {"home": home, "box": self._box(conn, gid, uid, home),
                                      "bag": {i: q for i, q in conn.execute("SELECT item, qty FROM econ_items WHERE guild_id=? AND user_id=? AND qty > 0", (gid, uid))}})

    def store(self, gid: int, uid: int, home, item, qty, cap, rid) -> dict:
        self.guard(gid, uid)
        home, item, qty = self._home(home), str(item or ""), self._qty(qty)
        cap = max(0, min(10_000, int(cap or 0)))
        if not eligible(item):
            raise HomeError("not_storable", "That can't be stored.")

        def do(conn):
            def inner():
                box = self._box(conn, gid, uid, home)
                if sum(box.values()) + qty > cap:
                    raise HomeError("full", f"The storage is full ({cap} slots). Upgrade it or make room.", cap=cap)
                if self._held(conn, gid, uid, item) < qty:
                    raise HomeError("no_item", "You don't have that many.")
                self._bag_add(conn, gid, uid, item, -qty)
                conn.execute("INSERT INTO strip_home_storage VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, user_id, home, item) DO UPDATE SET qty = qty + excluded.qty", (gid, uid, home, item, qty))
                return {"home": home, "box": self._box(conn, gid, uid, home)}
            return self._once(conn, gid, uid, rid, "store", inner)
        return self._tx(do)

    def take(self, gid: int, uid: int, home, item, qty, rid) -> dict:
        self.guard(gid, uid)
        home, item, qty = self._home(home), str(item or ""), self._qty(qty)

        def do(conn):
            def inner():
                box = self._box(conn, gid, uid, home)
                if box.get(item, 0) < qty:
                    raise HomeError("no_item", "That isn't in storage.")
                conn.execute("UPDATE strip_home_storage SET qty = qty - ? WHERE guild_id=? AND user_id=? AND home=? AND item=?", (qty, gid, uid, home, item))
                self._bag_add(conn, gid, uid, item, qty)
                return {"home": home, "box": self._box(conn, gid, uid, home)}
            return self._once(conn, gid, uid, rid, "take", inner)
        return self._tx(do)

    def resell(self, gid: int, uid: int, rid, part=None, parts=None) -> dict:
        """Sell a piece back: pays RESELL_RATE of what this player paid in that purchase (found by its request id), once.
        A purchase that bought several pieces at once (a furniture set) is sold a piece at a time: `part` of `parts`
        (when `parts` is not given, the number of pieces recorded with the purchase).
        Only things this player bought from a shop can be sold: gifts and rewards have no purchase to point to."""
        self.guard(gid, uid)
        rid = str(rid or "")
        if not RID.match(rid):
            raise HomeError("bad_rid", "Which purchase?")
        try:
            parts = None if parts is None else max(1, min(50, int(parts)))
            part = None if part is None else int(part)
        except (TypeError, ValueError):
            raise HomeError("bad_args", "Which piece?")
        key = ("resell:" + rid + ("" if part is None else ":%d" % part))[:80]

        def do(conn):
            def inner():
                row = conn.execute("SELECT amount, kind, detail FROM strip_home_ledger WHERE guild_id=? AND user_id=? AND rid=? AND kind IN (%s) ORDER BY id LIMIT 1"
                                   % ",".join("?" * len(RESELLABLE)), (gid, uid, rid, *RESELLABLE)).fetchone()
                if not row:
                    raise HomeError("not_bought", "Only things you bought can be sold back.")
                n = parts
                if part is None:
                    n = 1
                elif n is None:
                    try:
                        n = len(json.loads(row[2] or "{}").get("pieces") or [])
                    except (ValueError, AttributeError):
                        n = 0
                if not n or (part is not None and not 0 <= part < n):
                    raise HomeError("bad_args", "Which piece?")
                amount = int(row[0] / n * RESELL_RATE)
                if amount < 1:
                    raise HomeError("worthless", "Nobody will pay for that.")
                w = self.db._wallet_row(conn, gid, uid)
                w["cash"] += amount
                self.db._write_wallet(conn, gid, uid, w)
                conn.execute("INSERT INTO strip_home_ledger (guild_id, user_id, rid, kind, amount, cash_after, detail, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (gid, uid, key, "resell", -amount, w["cash"], json.dumps({"of": rid, "part": part, "parts": n, "paid": row[0]}), self.now()))
                return {"amount": amount, "paid": int(row[0] // n), "cash": w["cash"]}
            return self._once(conn, gid, uid, key, "resell", inner)
        return self._tx(do)

    def release(self, gid: int, uid: int, home) -> dict:
        """Moving out: everything in that home's box goes back to the bag."""
        self.guard(gid, uid)
        home = self._home(home)

        def do(conn):
            box = self._box(conn, gid, uid, home)
            for item, qty in box.items():
                self._bag_add(conn, gid, uid, item, qty)
            conn.execute("DELETE FROM strip_home_storage WHERE guild_id=? AND user_id=? AND home=?", (gid, uid, home))
            return {"home": home, "returned": box}
        return self._tx(do)

    def transfer(self, gid: int, uid: int, owner, home, item, qty, rid) -> dict:
        """A household member takes an item from their partner's box at the shared home (permission checked by the game server)."""
        self.guard(gid, uid)
        try:
            owner = int(owner)
        except (TypeError, ValueError):
            raise HomeError("bad_partner", "Whose box?")
        self.guard(gid, owner)
        home, item, qty = self._home(home), str(item or ""), self._qty(qty)

        def do(conn):
            def inner():
                box = self._box(conn, gid, owner, home)
                if box.get(item, 0) < qty:
                    raise HomeError("no_item", "That isn't in storage.")
                conn.execute("UPDATE strip_home_storage SET qty = qty - ? WHERE guild_id=? AND user_id=? AND home=? AND item=?", (qty, gid, owner, home, item))
                self._bag_add(conn, gid, uid, item, qty)
                conn.execute("INSERT INTO strip_home_item_log (guild_id, home, from_user, to_user, item, qty, rid, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (gid, home, owner, uid, item, qty, str(rid), self.now()))
                return {"home": home, "box": self._box(conn, gid, owner, home)}
            return self._once(conn, gid, uid, rid, "transfer", inner)
        return self._tx(do)

    def storage_of(self, gid: int, uid: int, owner, home) -> dict:
        """A partner's box at a shared home (read only)."""
        self.guard(gid, uid)
        owner = int(owner)
        home = self._home(home)
        return self._tx(lambda conn: {"home": home, "owner": owner, "box": self._box(conn, gid, owner, home)})
