"""THE STRIP kitchen, settled by SYKE: groceries, cooking, eating, giving, and food sold by player businesses.

Ingredients (ing_*) and dishes (dish_*) are ordinary items in the one SYKE inventory (econ_items): they show in
the bag, can be kept in a home's storage box and traded on the Market. The game server runs the cooking itself
(the steps, the stars, the recipe book) and asks SYKE to settle each result here, in one transaction:

* buy(): groceries at SYKE's own prices (plus a delivery fee when ordered by phone).
* cook(): the ingredients come out of the bag and the dish goes in, or nothing changes (and a kitchen fee, if any).
* consume(): eat a dish, or set it out for everyone at a home.
* give(): a dish to another player.
* stock() / unstock() / menu() / order(): the owner of a food business (the Golden Dragon, the General Store) puts
  dishes on its menu at a price; a customer pays the owner (less the Strip's 5%) and the dish moves to them.

Every change carries a request id and happens exactly once.
"""
from __future__ import annotations

import json
import re
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS strip_kitchen_requests (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, rid TEXT NOT NULL, op TEXT NOT NULL, reply TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id, rid)
);
CREATE TABLE IF NOT EXISTS strip_kitchen_stock (
    guild_id INTEGER NOT NULL, store TEXT NOT NULL, dish TEXT NOT NULL, owner INTEGER NOT NULL, qty INTEGER NOT NULL, price INTEGER NOT NULL,
    PRIMARY KEY (guild_id, store, dish)
);
CREATE TABLE IF NOT EXISTS strip_kitchen_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, op TEXT NOT NULL, detail TEXT NOT NULL, ts REAL NOT NULL
);
"""
RID = re.compile(r"^[A-Za-z0-9_:.\-]{6,80}$")
INGREDIENTS = {
    "bun": ("Burger buns", "🍞", 15),
    "patty": ("Beef patty", "🥩", 40),
    "cheese": ("Cheese", "🧀", 20),
    "lettuce": ("Lettuce", "🥬", 10),
    "tomato": ("Tomato", "🍅", 10),
    "onion": ("Onion", "🧅", 8),
    "bacon": ("Bacon", "🥓", 35),
    "egg": ("Eggs", "🥚", 12),
    "bread": ("Bread", "🍞", 15),
    "ham": ("Ham", "🍖", 30),
    "dough": ("Pizza dough", "🫓", 25),
    "sauce": ("Tomato sauce", "🥫", 15),
    "pepperoni": ("Pepperoni", "🍕", 30),
    "mushroom": ("Mushrooms", "🍄", 15),
    "pasta": ("Spaghetti", "🍝", 20),
    "cream": ("Cream", "🥛", 20),
    "garlic": ("Garlic", "🧄", 8),
    "chicken": ("Chicken", "🍗", 45),
    "flour": ("Flour", "🌾", 12),
    "sugar": ("Sugar", "🍬", 8),
    "butter": ("Butter", "🧈", 15),
    "milk": ("Milk", "🥛", 12),
    "berries": ("Berries", "🫐", 25),
    "chocolate": ("Chocolate", "🍫", 30),
    "coffee": ("Coffee beans", "☕", 20),
    "lemon": ("Lemons", "🍋", 10),
    "ice": ("Ice", "🧊", 5),
    "mint": ("Mint", "🌿", 10),
    "banana": ("Bananas", "🍌", 12),
    "potato": ("Potatoes", "🥔", 10),
    "avocado": ("Avocado", "🥑", 30),
}
DISHES = {
    "burger": ("Classic burger", "🍔", 140),
    "baconburger": ("Bacon double", "🍔", 220),
    "mushburger": ("Mushroom melt", "🍔", 150),
    "margherita": ("Margherita", "🍕", 160),
    "pepperoni": ("Pepperoni pizza", "🍕", 200),
    "funghi": ("Funghi", "🍕", 190),
    "marinara": ("Spaghetti marinara", "🍝", 130),
    "carbonara": ("Carbonara", "🍝", 190),
    "alfredo": ("Chicken alfredo", "🍝", 210),
    "garden": ("Garden salad", "🥗", 90),
    "caesar": ("Caesar salad", "🥗", 160),
    "avosalad": ("Avocado bowl", "🥗", 170),
    "grilledcheese": ("Grilled cheese", "🥪", 80),
    "club": ("Club sandwich", "🥪", 160),
    "blt": ("BLT", "🥪", 130),
    "pancakes": ("Pancake stack", "🍳", 120),
    "baconeggs": ("Bacon and eggs", "🍳", 140),
    "hashbrowns": ("Hash browns", "🍳", 90),
    "choccake": ("Chocolate cake", "🍰", 180),
    "berrypie": ("Berry pie", "🍰", 170),
    "sundae": ("Ice cream sundae", "🍰", 150),
    "lemonade": ("Lemonade", "🥤", 60),
    "milkshake": ("Chocolate milkshake", "🥤", 90),
    "icedcoffee": ("Iced coffee", "🥤", 70),
    "mocktail": ("Strip mojito (no rum)", "🥤", 80),
    "bananabread": ("Banana bread", "🍰", 140),
}

FOOD_BIZ = {"dragon": None, "general": {"sandwiches", "breakfast", "desserts", "drinks", "salads"}}
CATEGORY = {"burger": "burgers", "baconburger": "burgers", "mushburger": "burgers", "margherita": "pizza", "pepperoni": "pizza", "funghi": "pizza",
            "marinara": "pasta", "carbonara": "pasta", "alfredo": "pasta", "garden": "salads", "caesar": "salads", "avosalad": "salads",
            "grilledcheese": "sandwiches", "club": "sandwiches", "blt": "sandwiches", "pancakes": "breakfast", "baconeggs": "breakfast",
            "hashbrowns": "breakfast", "choccake": "desserts", "berrypie": "desserts", "sundae": "desserts", "bananabread": "desserts",
            "lemonade": "drinks", "milkshake": "drinks", "icedcoffee": "drinks", "mocktail": "drinks"}
BIZ_FEE = 0.05
PRICE_MAX = 2000
FEE_MAX = 100
QTY_MAX = 50
MENU_MAX = 12


def is_food(key: str) -> bool:
    key = str(key)
    return (key.startswith("ing_") and key[4:] in INGREDIENTS) or (key.startswith("dish_") and key[5:] in DISHES)


def label(key: str) -> str:
    key = str(key)
    if key.startswith("ing_") and key[4:] in INGREDIENTS:
        n, e, _ = INGREDIENTS[key[4:]]
        return f"{e} {n}"
    if key.startswith("dish_") and key[5:] in DISHES:
        n, e, _ = DISHES[key[5:]]
        return f"{e} {n}"
    return key


def value(key: str) -> int:
    """What a food item is worth to a pawn shop: half price for groceries, nothing for cooked food (a perfect
    cook makes two portions, so paying for dishes would turn groceries into free money)."""
    key = str(key)
    if key.startswith("ing_") and key[4:] in INGREDIENTS:
        return INGREDIENTS[key[4:]][2] // 2
    return 0


class KitchenError(Exception):
    def __init__(self, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


class KitchenService:
    def __init__(self, storage, guard, clock=time.time) -> None:
        self.db, self.guard, self.now = storage, guard, clock
        with storage._lock:
            storage._conn.executescript(SCHEMA)
            storage._conn.commit()

    # ---------------------------------------------------------------- plumbing
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

    def _once(self, gid, uid, rid, op, do):
        rid = str(rid or "")
        if not RID.match(rid):
            raise KitchenError("bad_rid", "Missing request id.")

        def run(conn):
            row = conn.execute("SELECT op, reply FROM strip_kitchen_requests WHERE guild_id=? AND user_id=? AND rid=?", (gid, uid, rid)).fetchone()
            if row:
                if row[0] != op:
                    raise KitchenError("rid_reused", "That request id was already used.")
                return {**json.loads(row[1]), "replayed": True}
            reply = do(conn)
            conn.execute("INSERT INTO strip_kitchen_requests VALUES (?, ?, ?, ?, ?, ?)", (gid, uid, rid, op, json.dumps(reply), self.now()))
            conn.execute("INSERT INTO strip_kitchen_log (guild_id, user_id, op, detail, ts) VALUES (?, ?, ?, ?, ?)", (gid, uid, op, json.dumps(reply)[:400], self.now()))
            return reply
        return self._tx(run)

    @staticmethod
    def _held(conn, gid, uid, item) -> int:
        row = conn.execute("SELECT qty FROM econ_items WHERE guild_id=? AND user_id=? AND item=?", (gid, uid, item)).fetchone()
        return row[0] if row else 0

    def _add(self, conn, gid, uid, item, delta) -> int:
        new = self._held(conn, gid, uid, item) + delta
        if new < 0:
            raise KitchenError("no_item", f"You don't have enough {label(item)}.", item=item)
        conn.execute("INSERT INTO econ_items VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id, item) DO UPDATE SET qty=excluded.qty", (gid, uid, item, new))
        return new

    def _pay(self, conn, gid, uid, amount):
        w = self.db._wallet_row(conn, gid, uid)
        if w["cash"] < amount:
            raise KitchenError("broke", f"That's {amount:,}. You have {w['cash']:,} on you.", have=w["cash"])
        w["cash"] -= amount
        self.db._write_wallet(conn, gid, uid, w)

    def _credit(self, conn, gid, uid, amount):
        w = self.db._wallet_row(conn, gid, uid)
        w["cash"] += amount
        w["seen"] = 1
        self.db._write_wallet(conn, gid, uid, w)

    @staticmethod
    def _items(raw, kind) -> dict:
        if not isinstance(raw, dict) or not raw or len(raw) > 12:
            raise KitchenError("bad_args", "Which items?")
        out = {}
        for k, q in raw.items():
            key = str(k)
            ok = (kind == "ing" and key.startswith("ing_") and key[4:] in INGREDIENTS) or (kind == "dish" and key.startswith("dish_") and key[5:] in DISHES)
            q = int(q)
            if not ok or q < 1 or q > QTY_MAX:
                raise KitchenError("bad_args", f"Not something from the kitchen: {key}")
            out[key] = q
        return out

    # ---------------------------------------------------------------- ops
    def catalog(self) -> dict:
        return {"ingredients": {k: {"name": n, "emoji": e, "price": p} for k, (n, e, p) in INGREDIENTS.items()},
                "dishes": {k: {"name": n, "emoji": e, "value": p} for k, (n, e, p) in DISHES.items()}}

    def buy(self, gid, uid, items, fee, rid) -> dict:
        self.guard(gid, uid)
        want = self._items(items, "ing")
        fee = max(0, min(FEE_MAX, int(fee or 0)))
        total = sum(INGREDIENTS[k[4:]][2] * q for k, q in want.items()) + fee

        def do(conn):
            self._pay(conn, gid, uid, total)
            for k, q in want.items():
                self._add(conn, gid, uid, k, q)
            return {"bought": want, "total": total, "fee": fee}
        return self._once(gid, uid, rid, "buy", do)

    def cook(self, gid, uid, use, make, fee, rid) -> dict:
        self.guard(gid, uid)
        used, made = self._items(use, "ing"), self._items(make, "dish")
        fee = max(0, min(FEE_MAX, int(fee or 0)))

        def do(conn):
            for k, q in used.items():
                self._add(conn, gid, uid, k, -q)
            if fee:
                self._pay(conn, gid, uid, fee)
            for k, q in made.items():
                self._add(conn, gid, uid, k, q)
            return {"used": used, "made": made, "fee": fee}
        return self._once(gid, uid, rid, "cook", do)

    def consume(self, gid, uid, item, qty, rid) -> dict:
        self.guard(gid, uid)
        took = self._items({item: qty or 1}, "dish")

        def do(conn):
            for k, q in took.items():
                self._add(conn, gid, uid, k, -q)
            return {"consumed": took}
        return self._once(gid, uid, rid, "consume", do)

    def give(self, gid, uid, to, item, qty, rid) -> dict:
        self.guard(gid, uid)
        to = int(to)
        if to == uid:
            raise KitchenError("bad_args", "That's you.")
        self.guard(gid, to)
        moved = self._items({item: qty or 1}, "dish")

        def do(conn):
            for k, q in moved.items():
                self._add(conn, gid, uid, k, -q)
                self._add(conn, gid, to, k, q)
            return {"gave": moved, "to": str(to)}
        return self._once(gid, uid, rid, "give", do)

    def _owner(self, gid, store) -> int | None:
        row = self.db.econ_shop(gid, store)
        return row["owner_id"] if row else None

    def _menu(self, conn, gid, store) -> list:
        return [{"dish": d, "qty": q, "price": p} for d, q, p in conn.execute(
            "SELECT dish, qty, price FROM strip_kitchen_stock WHERE guild_id=? AND store=? AND qty>0 ORDER BY dish", (gid, store))]

    def menu(self, gid, uid, store) -> dict:
        self.guard(gid, uid)
        store = str(store or "")
        if store not in FOOD_BIZ:
            raise KitchenError("not_food", "That storefront doesn't sell food.")
        owner = self._owner(gid, store)
        with self.db._lock:
            items = self._menu(self.db._conn, gid, store)
        return {"store": store, "owner": str(owner) if owner else None, "mine": owner == uid, "menu": items}

    def stock(self, gid, uid, store, item, qty, price) -> dict:
        self.guard(gid, uid)
        store = str(store or "")
        if store not in FOOD_BIZ:
            raise KitchenError("not_food", "That storefront doesn't sell food.")
        if self._owner(gid, store) != uid:
            raise KitchenError("not_yours", "Only the owner can put food on the menu.")
        moved = self._items({item: qty or 1}, "dish")
        cats = FOOD_BIZ[store]
        if cats is not None and any(CATEGORY[k[5:]] not in cats for k in moved):
            raise KitchenError("wrong_food", "That doesn't go on this menu.")
        price = int(price)
        if price < 1 or price > PRICE_MAX:
            raise KitchenError("bad_price", f"Pick a price from 1 to {PRICE_MAX:,}.")

        def run(conn):
            n = conn.execute("SELECT COUNT(*) FROM strip_kitchen_stock WHERE guild_id=? AND store=? AND qty>0", (gid, store)).fetchone()[0]
            for k, q in moved.items():
                cur = conn.execute("SELECT qty FROM strip_kitchen_stock WHERE guild_id=? AND store=? AND dish=?", (gid, store, k)).fetchone()
                if not (cur and cur[0] > 0) and n >= MENU_MAX:
                    raise KitchenError("menu_full", f"A menu holds {MENU_MAX} dishes.")
                self._add(conn, gid, uid, k, -q)
                conn.execute("INSERT INTO strip_kitchen_stock VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id, store, dish) DO UPDATE SET qty=qty+excluded.qty, price=excluded.price, owner=excluded.owner",
                             (gid, store, k, uid, q, price))
            return {"store": store, "menu": self._menu(conn, gid, store)}
        return self._tx(run)

    def unstock(self, gid, uid, store, item) -> dict:
        self.guard(gid, uid)
        store, item = str(store or ""), str(item or "")

        def run(conn):
            row = conn.execute("SELECT owner, qty FROM strip_kitchen_stock WHERE guild_id=? AND store=? AND dish=?", (gid, store, item)).fetchone()
            if not row or row[1] <= 0:
                raise KitchenError("none", "That's not on the menu.")
            if row[0] != uid and self._owner(gid, store) != uid:
                raise KitchenError("not_yours", "Only the owner can change the menu.")
            conn.execute("UPDATE strip_kitchen_stock SET qty=0 WHERE guild_id=? AND store=? AND dish=?", (gid, store, item))
            self._add(conn, gid, uid, item, row[1])
            return {"store": store, "returned": row[1], "menu": self._menu(conn, gid, store)}
        return self._tx(run)

    def order(self, gid, uid, store, item, price, rid) -> dict:
        """A customer buys one portion at the price they were shown; the owner is paid (less the Strip's cut)."""
        self.guard(gid, uid)
        store, item = str(store or ""), str(item or "")

        def do(conn):
            row = conn.execute("SELECT owner, qty, price FROM strip_kitchen_stock WHERE guild_id=? AND store=? AND dish=?", (gid, store, item)).fetchone()
            if not row or row[1] <= 0:
                raise KitchenError("sold_out", "Sold out.")
            owner, _, p = row
            if price is not None and int(price) != p:
                raise KitchenError("price_changed", "The price just changed.", price=p)
            if owner == uid:
                raise KitchenError("own_food", "That's your own food. Take it off the menu instead.")
            self._pay(conn, gid, uid, p)
            self._credit(conn, gid, owner, p - int(p * BIZ_FEE))
            conn.execute("UPDATE strip_kitchen_stock SET qty=qty-1 WHERE guild_id=? AND store=? AND dish=?", (gid, store, item))
            self._add(conn, gid, uid, item, 1)
            return {"store": store, "dish": item, "price": p, "owner": str(owner), "paid_owner": p - int(p * BIZ_FEE)}
        return self._once(gid, uid, rid, "order", do)
