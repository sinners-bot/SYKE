"""THE STRIP 3.21: running a storefront, not just owning one.

Owners (see strip_phone.py for buying, upgrading and collecting) can now:
- set their prices: from 25% off to 50% over. Players buying in the store pay that price; the owner's register gets the
  usual 10% cut plus every coin of markup (a discount comes out of the owner's cut first, then the House's). Walk-in
  trade (the hourly income) moves the other way: cheaper brings more people in (stripworld.demand).
- hire players. An owner offers a job; the player accepts from their phone. When staff finish a shift at that
  storefront in the game, the shift's takings (35% of the pay) go into the register and the owner's chosen wage share
  (0 to 35% of the pay) comes back out of it to the worker, on top of their normal pay.

Staff per store: 2 + its level. A player works for one storefront at a time. Nothing here creates money except the
takings, which only come from shifts actually worked.
"""
from __future__ import annotations

import time

from . import stripworld as sw
from .strip_phone import PhoneError

SCHEMA = """
CREATE TABLE IF NOT EXISTS strip_biz_staff (
    guild_id INTEGER NOT NULL, store TEXT NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL,
    since REAL NOT NULL, shifts INTEGER NOT NULL DEFAULT 0, earned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, store, user_id)
);
CREATE INDEX IF NOT EXISTS strip_biz_staff_user ON strip_biz_staff (guild_id, user_id, status);
CREATE TABLE IF NOT EXISTS strip_biz_cfg (
    guild_id INTEGER NOT NULL, store TEXT NOT NULL, wage REAL NOT NULL DEFAULT 0.15,
    week_takings INTEGER NOT NULL DEFAULT 0, week_sales INTEGER NOT NULL DEFAULT 0, week_of INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, store)
);
"""
TAKINGS = 0.35          # a staffed shift puts 35% of its pay into the store's register
WAGE_RANGE = (0.0, 0.35)
OFFER_DAYS = 3


def staff_cap(level: int) -> int:
    return 2 + max(1, int(level))


class BizService:
    def __init__(self, storage, guard, clock=time.time) -> None:
        self.db, self.guard, self.now = storage, guard, clock
        with storage._lock:
            storage._conn.executescript(SCHEMA)
            storage._conn.commit()

    # ---------------------------------------------------------------- helpers
    def _store(self, key) -> sw.Store:
        s = sw.STORE_BY_KEY.get(str(key or ""))
        if s is None:
            raise PhoneError("bad_store", "Which storefront?")
        return s

    def _mine(self, gid: int, uid: int, key) -> tuple[sw.Store, dict]:
        s = self._store(key)
        row = self.db.econ_shop(gid, s.key)
        if not row or row["owner_id"] != uid:
            raise PhoneError("not_yours", f"You don't own {s.name}.")
        return s, row

    def _week(self) -> int:
        return int(self.now() // (7 * 86400))

    def _cfg(self, gid: int, store: str) -> dict:
        r = self.db._read("SELECT wage, week_takings, week_sales, week_of FROM strip_biz_cfg WHERE guild_id=? AND store=?", (gid, store))
        if not r:
            return {"wage": 0.15, "week_takings": 0, "week_sales": 0}
        wage, wt, ws, wk = r[0]
        if wk != self._week():
            wt = ws = 0
        return {"wage": float(wage), "week_takings": int(wt), "week_sales": int(ws)}

    def _bump(self, gid: int, store: str, takings: int = 0, sales: int = 0) -> None:
        wk = self._week()
        self.db._write("INSERT INTO strip_biz_cfg (guild_id, store, week_of) VALUES (?, ?, ?) ON CONFLICT(guild_id, store) DO NOTHING", (gid, store, wk))
        self.db._write("UPDATE strip_biz_cfg SET week_takings = CASE WHEN week_of=? THEN week_takings ELSE 0 END + ?, "
                       "week_sales = CASE WHEN week_of=? THEN week_sales ELSE 0 END + ?, week_of=? WHERE guild_id=? AND store=?",
                       (wk, int(takings), wk, int(sales), wk, gid, store))

    def _staff(self, gid: int, store: str) -> list[dict]:
        rows = self.db._read("SELECT user_id, status, since, shifts, earned FROM strip_biz_staff WHERE guild_id=? AND store=? ORDER BY since", (gid, store))
        out, cutoff = [], self.now() - OFFER_DAYS * 86400
        for uid, status, since, shifts, earned in rows:
            if status == "offer" and since < cutoff:
                self.db._write("DELETE FROM strip_biz_staff WHERE guild_id=? AND store=? AND user_id=? AND status='offer'", (gid, store, uid))
                continue
            out.append({"user": str(uid), "status": status, "since": since, "shifts": shifts, "earned": earned})
        return out

    def job_of(self, gid: int, uid: int) -> dict | None:
        r = self.db._read("SELECT store, since, shifts, earned FROM strip_biz_staff WHERE guild_id=? AND user_id=? AND status='staff'", (gid, uid))
        if not r:
            return None
        store, since, shifts, earned = r[0]
        row = self.db.econ_shop(gid, store)
        if not row:   # the store was sold: the job went with it
            self.db._write("DELETE FROM strip_biz_staff WHERE guild_id=? AND store=?", (gid, store))
            return None
        s = sw.STORE_BY_KEY.get(store)
        return {"store": store, "name": s.name if s else store, "owner": str(row["owner_id"]), "since": since, "shifts": shifts,
                "earned": earned, "wage": self._cfg(gid, store)["wage"]}

    def markups(self, gid: int) -> dict:
        return {k: round(float(r.get("markup") or 0), 2) for k, r in self.db.econ_shops(gid).items() if r.get("markup")}

    # ---------------------------------------------------------------- the owner's side
    def manage(self, gid: int, uid: int, store) -> dict:
        self.guard(gid, uid)
        s, row = self._mine(gid, uid, store)
        cfg = self._cfg(gid, s.key)
        now = self.now()
        return {"store": s.key, "name": s.name, "level": row["level"], "markup": round(float(row.get("markup") or 0), 2),
                "wage": cfg["wage"], "staff": self._staff(gid, s.key), "cap": staff_cap(row["level"]),
                "income": int(sw.shop_income(s, row["level"]) * sw.demand(row.get("markup") or 0)), "base_income": sw.shop_income(s, row["level"]),
                "register": sw.pending(row, s, now), "week_takings": cfg["week_takings"], "week_sales": cfg["week_sales"],
                "markup_range": list(sw.MARKUP_RANGE), "wage_range": list(WAGE_RANGE), "takings": TAKINGS}

    def set_prices(self, gid: int, uid: int, store, markup=None, wage=None) -> dict:
        self.guard(gid, uid)
        s, row = self._mine(gid, uid, store)
        now = self.now()
        if markup is not None:
            m = round(max(sw.MARKUP_RANGE[0], min(sw.MARKUP_RANGE[1], float(markup))), 2)
            # settle the register at the old price level first, so a change never rewrites what was already earned
            self.db.econ_shop_update(gid, s.key, register=sw.pending(row, s, now), collected_at=now, markup=m)
        if wage is not None:
            w = round(max(WAGE_RANGE[0], min(WAGE_RANGE[1], float(wage))), 2)
            self.db._write("INSERT INTO strip_biz_cfg (guild_id, store, wage, week_of) VALUES (?, ?, ?, ?) "
                           "ON CONFLICT(guild_id, store) DO UPDATE SET wage=excluded.wage", (gid, s.key, w, self._week()))
        return self.manage(gid, uid, s.key)

    def hire(self, gid: int, uid: int, store, target) -> dict:
        self.guard(gid, uid)
        s, row = self._mine(gid, uid, store)
        try:
            tid = int(target)
        except (TypeError, ValueError):
            raise PhoneError("bad_args", "Who?")
        if tid == uid:
            raise PhoneError("self", "You already run the place.")
        staff = self._staff(gid, s.key)
        if any(x["user"] == str(tid) for x in staff):
            raise PhoneError("already", "They're already on your list.")
        if len(staff) >= staff_cap(row["level"]):
            raise PhoneError("full", f"{s.name} can take {staff_cap(row['level'])} staff at level {row['level']}. Upgrade for more.")
        self.db._write("INSERT INTO strip_biz_staff (guild_id, store, user_id, status, since) VALUES (?, ?, ?, 'offer', ?)", (gid, s.key, tid, self.now()))
        return {"store": s.key, "name": s.name, "target": str(tid)}

    def fire(self, gid: int, uid: int, store, target) -> dict:
        self.guard(gid, uid)
        s, _ = self._mine(gid, uid, store)
        n = self.db._write("DELETE FROM strip_biz_staff WHERE guild_id=? AND store=? AND user_id=?", (gid, s.key, int(target)))
        if not n:
            raise PhoneError("gone", "They don't work there.")
        return {"store": s.key, "name": s.name, "target": str(int(target))}

    # ---------------------------------------------------------------- the worker's side
    def jobs(self, gid: int, uid: int) -> dict:
        self.guard(gid, uid)
        rows = self.db._read("SELECT store, since FROM strip_biz_staff WHERE guild_id=? AND user_id=? AND status='offer'", (gid, uid))
        offers, cutoff = [], self.now() - OFFER_DAYS * 86400
        for store, since in rows:
            row = self.db.econ_shop(gid, store)
            if since < cutoff or not row:
                self.db._write("DELETE FROM strip_biz_staff WHERE guild_id=? AND store=? AND user_id=? AND status='offer'", (gid, store, uid))
                continue
            s = sw.STORE_BY_KEY.get(store)
            offers.append({"store": store, "name": s.name if s else store, "owner": str(row["owner_id"]), "wage": self._cfg(gid, store)["wage"], "since": since})
        return {"offers": offers, "job": self.job_of(gid, uid)}

    def accept(self, gid: int, uid: int, store) -> dict:
        self.guard(gid, uid)
        s = self._store(store)
        r = self.db._read("SELECT 1 FROM strip_biz_staff WHERE guild_id=? AND store=? AND user_id=? AND status='offer'", (gid, s.key, uid))
        if not r or not self.db.econ_shop(gid, s.key):
            raise PhoneError("gone", "That offer isn't open any more.")
        self.db._write("DELETE FROM strip_biz_staff WHERE guild_id=? AND user_id=? AND status='staff'", (gid, uid))   # one job at a time
        self.db._write("UPDATE strip_biz_staff SET status='staff', since=? WHERE guild_id=? AND store=? AND user_id=?", (self.now(), gid, s.key, uid))
        return {"store": s.key, "name": s.name, "owner": str(self.db.econ_shop(gid, s.key)["owner_id"])}

    def decline(self, gid: int, uid: int, store) -> dict:
        self.guard(gid, uid)
        s = self._store(store)
        self.db._write("DELETE FROM strip_biz_staff WHERE guild_id=? AND store=? AND user_id=? AND status='offer'", (gid, s.key, uid))
        return {"store": s.key}

    def quit(self, gid: int, uid: int) -> dict:
        self.guard(gid, uid)
        job = self.job_of(gid, uid)
        if not job:
            raise PhoneError("none", "You don't work for anyone.")
        self.db._write("DELETE FROM strip_biz_staff WHERE guild_id=? AND user_id=? AND status='staff'", (gid, uid))
        return {"store": job["store"], "name": job["name"], "owner": job["owner"]}

    # ---------------------------------------------------------------- money moving through a store
    def shift(self, gid: int, uid: int, store, amount: int) -> dict | None:
        """A shift worked at `store` paid `amount`. If the worker is staff there: takings in, wage out."""
        if not store or amount <= 0:
            return None
        row = self.db.econ_shop(gid, str(store))
        if not row or row["owner_id"] == uid:
            return None
        if not self.db._read("SELECT 1 FROM strip_biz_staff WHERE guild_id=? AND store=? AND user_id=? AND status='staff'", (gid, str(store), uid)):
            return None
        takings = int(amount * TAKINGS)
        wage = min(takings, int(amount * self._cfg(gid, str(store))["wage"]))
        self.db.econ_shop_tip(gid, str(store), takings - wage)
        if wage:
            self.db.econ_adjust(gid, uid, cash=wage)
        self.db._write("UPDATE strip_biz_staff SET shifts=shifts+1, earned=earned+? WHERE guild_id=? AND store=? AND user_id=?", (wage, gid, str(store), uid))
        self._bump(gid, str(store), takings=takings - wage)
        s = sw.STORE_BY_KEY.get(str(store))
        return {"store": str(store), "name": s.name if s else str(store), "owner": str(row["owner_id"]), "wage": wage, "takings": takings - wage}

    def sale(self, gid: int, uid: int, store, base: int) -> tuple[int, int]:
        """A sale of `base` (list price) in `store`: what the buyer pays, and what goes into the owner's register."""
        row = self.db.econ_shop(gid, str(store or "")) if store else None
        if not row:
            return base, 0
        m = max(sw.MARKUP_RANGE[0], min(sw.MARKUP_RANGE[1], float(row.get("markup") or 0)))
        if row["owner_id"] == uid:   # owners pay list price in their own store (no discount to themselves)
            return base, 0
        price = max(1, int(round(base * (1 + m))))
        return price, int(base * sw.OWNER_CUT) + (price - base)   # a discount below the 10% cut comes out of the register

    def record_sale(self, gid: int, store, cut: int) -> None:
        if not store or not cut:
            return
        if cut > 0:
            self.db.econ_shop_tip(gid, str(store), cut)
        else:   # a sale below the owner's cut: the difference comes out of the register (as far as it goes)
            self.db._write("UPDATE econ_shops SET register = MAX(0, register + ?) WHERE guild_id=? AND shop=?", (int(cut), gid, str(store)))
        self._bump(gid, str(store), sales=cut)
