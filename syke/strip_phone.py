"""THE STRIP phone's money apps, run where the money lives (SYKE):

- Business: the Strip's storefronts (the same ones as !property): see them all, buy, upgrade, empty the
  registers, sell. Same prices and rules as the Discord commands.
- Marketplace: players sell their SYKE items and records to each other. Listing puts the items in escrow
  (out of the seller's inventory); buying moves the cash and the items in one transaction; cancelling
  gives the items back. The Strip keeps a 5% fee. Nothing here creates money or items.
"""
from __future__ import annotations

import time

from . import stripworld as sw

SCHEMA = """
CREATE TABLE IF NOT EXISTS strip_market (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, seller INTEGER NOT NULL, item TEXT NOT NULL,
    qty INTEGER NOT NULL, price INTEGER NOT NULL, created REAL NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    buyer INTEGER, closed REAL
);
CREATE INDEX IF NOT EXISTS strip_market_open ON strip_market (guild_id, status, created);
CREATE INDEX IF NOT EXISTS strip_market_seller ON strip_market (guild_id, seller, status);
"""
FEE = 0.05
MAX_OPEN = 10
PRICE_MAX = 1_000_000
LISTING_DAYS = 7


class PhoneError(Exception):
    def __init__(self, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def tradeable(key: str) -> bool:
    from .strip_kitchen import is_food
    return key in sw.ITEM_BY_KEY or key in sw.RECORD_BY_KEY or is_food(key)


class PhoneService:
    def __init__(self, storage, guard, clock=time.time) -> None:
        self.db, self.guard, self.now = storage, guard, clock
        with storage._lock:
            storage._conn.executescript(SCHEMA)
            storage._conn.commit()

    # ---------------------------------------------------------------- business
    def _store(self, key) -> sw.Store:
        s = sw.STORE_BY_KEY.get(str(key or ""))
        if s is None:
            raise PhoneError("bad_store", "Which storefront?")
        return s

    def biz_list(self, gid: int, uid: int) -> dict:
        self.guard(gid, uid)
        now, owned = self.now(), self.db.econ_shops(gid)
        out = []
        game_only = {g.key for g in sw.GAME_STORES}
        for s in sw.ALL_STORES:
            row = owned.get(s.key)
            lvl = row["level"] if row else 0
            out.append({
                "key": s.key, "name": s.name, "blurb": s.blurb, "price": s.price, "colour": "#%02x%02x%02x" % s.colour,
                "owner": str(row["owner_id"]) if row else None, "mine": bool(row and row["owner_id"] == uid), "level": lvl,
                "income": sw.shop_income(s, max(1, lvl)), "next_income": sw.shop_income(s, lvl + 1) if row and lvl < sw.MAX_LEVEL else None,
                "upgrade": sw.upgrade_cost(s, lvl) if row and lvl < sw.MAX_LEVEL else None,
                "register": sw.pending(row, s, now) if row and row["owner_id"] == uid else None,
                "full_in": max(0, int(row["collected_at"] + sw.REGISTER_CAP_HOURS * 3600 - now)) if row and row["owner_id"] == uid else None,
                "sell_value": int(row["invested"] * sw.SELL_BACK) if row and row["owner_id"] == uid else None,
                "markup": round(float(row.get("markup") or 0), 2) if row else 0, "game_only": s.key in game_only,
            })
        return {"stores": out, "max_level": sw.MAX_LEVEL, "cap_hours": sw.REGISTER_CAP_HOURS}

    def biz_buy(self, gid: int, uid: int, store) -> dict:
        w = self.guard(gid, uid)
        s = self._store(store)
        row = self.db.econ_shop(gid, s.key)
        if row:
            raise PhoneError("taken", f"{s.name} already has an owner.")
        if not self.db.econ_shop_buy(gid, s.key, uid, s.price, self.now()):
            raise PhoneError("broke", f"{s.name} costs {s.price:,}. You have {w['cash']:,} on you.")
        return {"store": s.key, "name": s.name, "price": s.price}

    def _mine(self, gid: int, uid: int, store) -> tuple[sw.Store, dict]:
        s = self._store(store)
        row = self.db.econ_shop(gid, s.key)
        if not row or row["owner_id"] != uid:
            raise PhoneError("not_yours", f"You don't own {s.name}.")
        return s, row

    def biz_upgrade(self, gid: int, uid: int, store) -> dict:
        self.guard(gid, uid)
        s, row = self._mine(gid, uid, store)
        if row["level"] >= sw.MAX_LEVEL:
            raise PhoneError("maxed", f"{s.name} is already level {sw.MAX_LEVEL}.")
        cost = sw.upgrade_cost(s, row["level"])
        if not self.db.econ_spend(gid, uid, cost):
            raise PhoneError("broke", f"Upgrading {s.name} costs {cost:,}.")
        now = self.now()
        self.db.econ_shop_update(gid, s.key, level=row["level"] + 1, invested=row["invested"] + cost,
                                 register=sw.pending(row, s, now), collected_at=now)
        return {"store": s.key, "level": row["level"] + 1, "cost": cost, "income": sw.shop_income(s, row["level"] + 1)}

    def biz_collect(self, gid: int, uid: int) -> dict:
        self.guard(gid, uid)
        now, total, lines = self.now(), 0, []
        for key, row in self.db.econ_shops(gid).items():
            if row["owner_id"] != uid:
                continue
            s = sw.STORE_BY_KEY[key]
            amount = sw.pending(row, s, now)
            self.db.econ_shop_update(gid, key, register=0, collected_at=now)
            total += amount
            lines.append({"store": key, "name": s.name, "amount": amount})
        if not lines:
            raise PhoneError("none", "You don't own a storefront yet.")
        self.db.econ_adjust(gid, uid, cash=total)
        return {"total": total, "stores": lines}

    def biz_sell(self, gid: int, uid: int, store) -> dict:
        self.guard(gid, uid)
        s, row = self._mine(gid, uid, store)
        value = int(row["invested"] * sw.SELL_BACK) + sw.pending(row, s, self.now())
        if not self.db.econ_shop_release(gid, s.key):
            raise PhoneError("gone", "That storefront changed hands.")
        try:   # 3.21 the staff go with it (strip_biz.py)
            self.db._write("DELETE FROM strip_biz_staff WHERE guild_id=? AND store=?", (gid, s.key))
        except Exception:
            pass
        self.db.econ_adjust(gid, uid, cash=value)
        return {"store": s.key, "value": value}

    # ---------------------------------------------------------------- marketplace
    def _view(self, r) -> dict:
        key = r[3]
        return {"id": r[0], "seller": str(r[2]), "item": key, "label": sw.label(key), "qty": r[4], "price": r[5],
                "each": round(r[5] / max(1, r[4])), "pawn": sw.pawn_value(key) * r[4], "created": r[6], "status": r[7],
                "buyer": str(r[8]) if r[8] else None}

    def _expire(self, gid: int) -> None:
        """Listings older than a week go back to their sellers."""
        st, cutoff = self.db, self.now() - LISTING_DAYS * 86400
        with st._lock:
            conn = st._conn
            rows = conn.execute("SELECT id, seller, item, qty FROM strip_market WHERE guild_id=? AND status='open' AND created < ?",
                                (gid, cutoff)).fetchall()
            if not rows:
                return
            for lid, seller, item, qty in rows:
                conn.execute("UPDATE strip_market SET status='expired', closed=? WHERE id=?", (self.now(), lid))
                self._give(conn, gid, seller, item, qty)
            conn.commit()

    @staticmethod
    def _give(conn, gid, uid, item, qty) -> None:
        conn.execute("INSERT INTO econ_items VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id, item) DO UPDATE SET qty = qty + excluded.qty",
                     (gid, uid, item, qty))

    def market_browse(self, gid: int, uid: int, item=None) -> dict:
        self.guard(gid, uid)
        self._expire(gid)
        q = "SELECT * FROM strip_market WHERE guild_id=? AND status='open'"
        args: list = [gid]
        if item:
            q += " AND item=?"
            args.append(str(item))
        rows = self.db._read(q + " ORDER BY created DESC LIMIT 80", tuple(args))
        return {"listings": [self._view(r) for r in rows], "fee": FEE, "inventory": self._inventory(gid, uid)}

    def _inventory(self, gid: int, uid: int) -> list[dict]:
        return [{"item": k, "label": sw.label(k), "qty": n, "pawn": sw.pawn_value(k)} for k, n in self.db.econ_items(gid, uid).items() if tradeable(k)]

    def market_mine(self, gid: int, uid: int) -> dict:
        self.guard(gid, uid)
        self._expire(gid)
        rows = self.db._read("SELECT * FROM strip_market WHERE guild_id=? AND seller=? ORDER BY created DESC LIMIT 40", (gid, uid))
        return {"listings": [self._view(r) for r in rows], "inventory": self._inventory(gid, uid), "fee": FEE, "max_open": MAX_OPEN}

    def market_post(self, gid: int, uid: int, item, qty, price) -> dict:
        w = self.guard(gid, uid)
        if w.get("jail_until", 0) > self.now():
            raise PhoneError("jailed", "You can't sell from a jail cell.")
        key = str(item or "")
        if not tradeable(key):
            raise PhoneError("bad_item", "That can't be sold here.")
        qty, price = int(qty), int(price)
        if qty < 1 or qty > 99:
            raise PhoneError("bad_qty", "Sell between 1 and 99.")
        if price < 1 or price > PRICE_MAX:
            raise PhoneError("bad_price", f"Pick a price between 1 and {PRICE_MAX:,}.")
        st = self.db
        with st._lock:
            conn = st._conn
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                n = conn.execute("SELECT COUNT(*) FROM strip_market WHERE guild_id=? AND seller=? AND status='open'", (gid, uid)).fetchone()[0]
                if n >= MAX_OPEN:
                    raise PhoneError("too_many", f"You can have {MAX_OPEN} listings up at once.")
                row = conn.execute("SELECT qty FROM econ_items WHERE guild_id=? AND user_id=? AND item=?", (gid, uid, key)).fetchone()
                if not row or row[0] < qty:
                    raise PhoneError("no_item", f"You have {row[0] if row else 0}.")
                conn.execute("UPDATE econ_items SET qty = qty - ? WHERE guild_id=? AND user_id=? AND item=?", (qty, gid, uid, key))
                cur = conn.execute("INSERT INTO strip_market (guild_id, seller, item, qty, price, created) VALUES (?, ?, ?, ?, ?, ?)",
                                   (gid, uid, key, qty, price, self.now()))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"listing": self._view(self.db._read("SELECT * FROM strip_market WHERE id=?", (cur.lastrowid,))[0])}

    def market_cancel(self, gid: int, uid: int, lid) -> dict:
        self.guard(gid, uid)
        st = self.db
        with st._lock:
            conn = st._conn
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                r = conn.execute("SELECT * FROM strip_market WHERE id=? AND guild_id=?", (int(lid), gid)).fetchone()
                if not r or r[2] != uid:
                    raise PhoneError("not_yours", "That isn't your listing.")
                if r[7] != "open":
                    raise PhoneError("closed", "That listing is already closed.")
                conn.execute("UPDATE strip_market SET status='cancelled', closed=? WHERE id=?", (self.now(), r[0]))
                self._give(conn, gid, uid, r[3], r[4])
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"id": r[0], "item": r[3], "qty": r[4]}

    def market_buy(self, gid: int, uid: int, lid, price) -> dict:
        w = self.guard(gid, uid)
        if w.get("jail_until", 0) > self.now():
            raise PhoneError("jailed", "You can't shop from a jail cell.")
        st = self.db
        with st._lock:
            conn = st._conn
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                r = conn.execute("SELECT * FROM strip_market WHERE id=? AND guild_id=?", (int(lid), gid)).fetchone()
                if not r or r[7] != "open":
                    raise PhoneError("gone", "Someone beat you to it.")
                if r[2] == uid:
                    raise PhoneError("own", "That's your own listing.")
                if int(price) != r[5]:
                    raise PhoneError("changed", "The price changed. Have another look.")
                buyer = st._wallet_row(conn, gid, uid)
                if buyer["cash"] < r[5]:
                    raise PhoneError("broke", f"That costs {r[5]:,}. You have {buyer['cash']:,} on you.")
                buyer["cash"] -= r[5]
                st._write_wallet(conn, gid, uid, buyer)
                seller = st._wallet_row(conn, gid, r[2])
                payout = r[5] - int(r[5] * FEE)
                seller["cash"] += payout
                st._write_wallet(conn, gid, r[2], seller)
                self._give(conn, gid, uid, r[3], r[4])
                conn.execute("UPDATE strip_market SET status='sold', buyer=?, closed=? WHERE id=?", (uid, self.now(), r[0]))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"id": r[0], "item": r[3], "label": sw.label(r[3]), "qty": r[4], "price": r[5], "seller": str(r[2]), "payout": payout}
