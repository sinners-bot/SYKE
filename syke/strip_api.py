"""THE STRIP game API: lets THE STRIP Discord Activity (the walkable pixel-art game) use SYKE's economy.

SYKE stays the bank. The game's server calls these endpoints on a player's behalf, and every
rule, price, cooldown and payout here comes from economy.py and stripworld.py. So `!work` in chat
and a shift in the game share one wallet, one timer and one set of odds.

Off unless SYKE_STRIP_API_SECRET is set. Every request must carry `Authorization: Bearer <secret>`,
and only the game server knows the secret (players never talk to this API directly).

    POST /strip/api/<op>   JSON body: {"guild_id": ..., "user_id": ..., ...op arguments}
    GET  /strip/api/health

Ops: state, daily, work, buy, use, pawn, spend, grant, catalog, bank, goal_reward, and the casino
(strip_casino.py): casino_exchange, casino_slots, casino_bj_start, casino_bj_act, casino_bj_state,
casino_visit, casino_stats; table ops (guild only): casino_roulette_round, casino_bj_sweep, casino_recent.
Every successful reply includes the player's fresh `state`, so the game can redraw its HUD.
"""

from __future__ import annotations

import hmac
import logging
import random
import time

from aiohttp import web

from . import economy as eco
from . import stripworld as sw
from .casino_art import tier_for
from .strip_casino import CasinoError, CasinoService

log = logging.getLogger("syke.strip_api")

WORK_BONUS = (0.8, 1.2)    # how much a good or bad shift in the game can nudge SYKE's pay
SPEND_MAX = 100_000        # biggest single cosmetic purchase (motel furniture, makeovers...)
GRANT_MAX = 100            # biggest single in-game find (a wallet in a dumpster)
GRANT_PER_HOUR = 300       # total in-game finds per player per hour
GOAL_REWARD = 500          # one-time "new in town" reward, at most once per player per server
GAME_ONLY_ITEMS = {"paintjob"}   # needs Discord roles: use it in chat


class StripError(Exception):
    def __init__(self, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


class StripService:
    """The economy actions the game can take, written against SYKE's storage and rules."""

    def __init__(self, storage, rng: random.Random | None = None, clock=time.time) -> None:
        self.db = storage
        self.rng = rng or random.Random()
        self.now = clock
        self._grants: dict[tuple[int, int], list[tuple[float, int]]] = {}

    # -- shared gates (mirror Economy._guard / _free) --
    def cfg(self, gid: int) -> dict:
        return self.db.get_config(gid, eco.CONFIG_KEY)

    def house(self) -> dict:
        return self.db.get_config(0, eco.HOUSE_KEY)

    def happy(self, gid: int) -> float:
        cfg = self.cfg(gid)
        return float(cfg.get("happy_mult", 1.0)) if cfg.get("happy_until", 0) > self.now() else 1.0

    def guard(self, gid: int, uid: int) -> dict:
        if not self.cfg(gid).get("open"):
            raise StripError("closed", "The Strip is dark here. An admin can light it up with !economy on.")
        if uid in (self.house().get("banned") or []):
            raise StripError("banned", "The House has banned you from the Strip.")
        return self.db.econ_touch(gid, uid, eco.STARTING_CASH)

    def free(self, w: dict) -> None:
        if w.get("jail_until", 0) > self.now():
            raise StripError("jailed", "You're in jail. Post bail or wait it out.", until=w["jail_until"])

    def tip_store(self, gid: int, store: str, amount: int) -> None:
        if amount > 0 and self.db.econ_shop(gid, store):
            self.db.econ_shop_tip(gid, store, amount)

    # -- reads --
    def state(self, gid: int, uid: int) -> dict:
        if not self.cfg(gid).get("open"):
            return {"open": False}
        w = self.guard(gid, uid)
        now = self.now()
        net = w["cash"] + w["bank"] + w["chips"]
        jail = w.get("jail_until", 0)
        return {
            "open": True,
            "wallet": {k: w[k] for k in ("cash", "chips", "bank", "streak")},
            "net": net, "tier": tier_for(net)[0], "rank": self.db.econ_rank(gid, uid),
            "items": self.db.econ_items(gid, uid),
            "buffs": self.db.econ_buffs(gid, uid, now),
            "jail_until": jail if jail > now else 0,
            "daily_ready_at": w["daily_at"] + eco.DAILY_COOLDOWN if w["daily_at"] else 0,
            "work_ready_at": w["work_at"] + eco.WORK_COOLDOWN if w["work_at"] else 0,
            "happy": self.happy(gid),
            "rewarded": bool(self.db.get_config(gid, "strip_game").get(f"goal_{uid}")),
            "now": now,
        }

    def catalog(self) -> dict:
        return {
            "items": {i.key: {"name": i.name, "price": i.price, "store": i.store, "blurb": i.blurb,
                              "buff": i.buff, "hours": i.hours, "passive": i.passive,
                              "pawn": sw.pawn_value(i.key)} for i in sw.ITEMS},
            "stores": {s.key: {"name": s.name, "price": s.price, "blurb": s.blurb} for s in sw.STORES},
            "work": {"low": eco.WORK_LOW, "high": eco.WORK_HIGH, "cooldown": eco.WORK_COOLDOWN},
        }

    # -- earning (mirror Economy.daily / Economy.work) --
    def daily(self, gid: int, uid: int) -> dict:
        w = self.guard(gid, uid)
        now = self.now()
        if w["daily_at"] and now - w["daily_at"] < eco.DAILY_COOLDOWN:
            raise StripError("cooldown", "Already claimed today.", until=w["daily_at"] + eco.DAILY_COOLDOWN)
        streak = eco.next_streak(w["daily_at"], now, w["streak"])
        amount = int(eco.daily_reward(streak) * self.happy(gid))
        self.db.econ_adjust(gid, uid, cash=amount, daily_at=now, streak=streak)
        return {"amount": amount, "streak": streak}

    def work(self, gid: int, uid: int, bonus: float = 1.0) -> dict:
        w = self.guard(gid, uid)
        self.free(w)
        now = self.now()
        if w["work_at"] and now - w["work_at"] < eco.WORK_COOLDOWN:
            raise StripError("cooldown", "You're beat. Next shift soon.", until=w["work_at"] + eco.WORK_COOLDOWN)
        did, base = eco.work_shift(self.rng)
        boost = self.happy(gid)
        perks = []
        for buff in self.db.econ_buffs(gid, uid, now):
            if buff in sw.WORK_BUFFS:
                boost *= sw.WORK_BUFFS[buff]
                perks.append(buff)
        bonus = min(max(float(bonus), WORK_BONUS[0]), WORK_BONUS[1])
        amount = int(base * boost * bonus)
        self.db.econ_adjust(gid, uid, cash=amount, work_at=now)
        return {"amount": amount, "did": did.replace("**", ""), "boost": round(boost, 2), "bonus": round(bonus, 2),
                "perks": perks}

    # -- shops (mirror StripWorld.buy / use / pawn) --
    def buy(self, gid: int, uid: int, item: str, qty: int = 1) -> dict:
        self.guard(gid, uid)
        it = sw.ITEM_BY_KEY.get(str(item))
        if it is None:
            raise StripError("unknown_item", "Never heard of it.")
        qty = max(1, min(int(qty), 50))
        total = it.price * qty
        if not self.db.econ_spend(gid, uid, total):
            raise StripError("broke", f"{qty}x {it.name} costs {total:,}. You don't have that on you.")
        self.db.econ_item_add(gid, uid, it.key, qty)
        self.tip_store(gid, it.store, int(total * sw.OWNER_CUT))
        return {"item": it.key, "qty": qty, "total": total}

    def use(self, gid: int, uid: int, item: str) -> dict:
        w = self.guard(gid, uid)
        key = str(item)
        held = self.db.econ_items(gid, uid)
        if not held.get(key):
            raise StripError("no_item", "You don't have that.")
        if key in sw.RECORD_BY_KEY:
            raise StripError("record", "It's a record. Spin it at home, or pawn it.")
        it = sw.ITEM_BY_KEY.get(key)
        if it is None:
            raise StripError("unknown_item", "Never heard of it.")
        if it.passive:
            raise StripError("passive", f"Your {it.name} works automatically.")
        if key in GAME_ONLY_ITEMS:
            raise StripError("chat_only", "Use your Paint Job in Discord: !use paintjob #ff3ea5")
        now = self.now()
        if key == "jailcard":
            if w["jail_until"] <= now:
                raise StripError("not_jailed", "You're not locked up. Save it for when you need it.")
            self.db.econ_item_add(gid, uid, key, -1)
            self.db.econ_adjust(gid, uid, jail_until=0)
            return {"result": "freed"}
        self.db.econ_item_add(gid, uid, key, -1)
        if key == "scratch":
            prize = sw.scratch(self.rng)
            if prize:
                self.db.econ_adjust(gid, uid, cash=prize)
            return {"result": "scratch", "prize": prize}
        if key == "energy":
            self.db.econ_adjust(gid, uid, work_at=0)
            return {"result": "energy"}
        if key == "bag":
            got = sw.open_bag(self.rng)
            self.db.econ_item_add(gid, uid, got, 1)
            return {"result": "bag", "got": got, "label": sw.label(got)}
        if key == "crate":
            rec = sw.open_crate(self.rng)
            self.db.econ_item_add(gid, uid, rec, 1)
            _, title, rarity, value = sw.RECORD_BY_KEY[rec]
            return {"result": "crate", "got": rec, "title": title, "rarity": rarity, "value": value,
                    "pawn": sw.pawn_value(rec)}
        out = {"result": "buff", "buff": it.buff, "hours": it.hours}
        if it.buff:
            self.db.econ_buff(gid, uid, it.buff, now + it.hours * sw.HOUR)
        if key == "cookie":
            out["fortune"] = self.rng.choice(sw.FORTUNES)
        return out

    def pawn(self, gid: int, uid: int, item: str, qty: int = 1) -> dict:
        self.guard(gid, uid)
        key = str(item)
        have = self.db.econ_items(gid, uid).get(key, 0)
        if not have:
            raise StripError("no_item", "You don't have that.")
        qty = int(qty)
        if qty <= 0 or qty > have:
            raise StripError("bad_qty", f"You have {have}.")
        each = sw.pawn_value(key)
        total = each * qty
        self.db.econ_item_add(gid, uid, key, -qty)
        self.db.econ_adjust(gid, uid, cash=total)
        self.tip_store(gid, "pawn", int(total * sw.OWNER_CUT))
        return {"item": key, "qty": qty, "total": total}

    # -- game-only money (cosmetics out, small finds in) --
    def spend(self, gid: int, uid: int, amount: int, reason: str = "") -> dict:
        self.guard(gid, uid)
        amount = int(amount)
        if amount <= 0 or amount > SPEND_MAX:
            raise StripError("bad_amount", "That's not a price.")
        if not self.db.econ_spend(gid, uid, amount):
            raise StripError("broke", f"That costs {amount:,}. You don't have that on you.")
        return {"amount": amount, "reason": str(reason)[:40]}

    def bank(self, gid: int, uid: int, action: str, amount: int) -> dict:
        """ATM: the same as !deposit / !withdraw (no fee)."""
        w = self.guard(gid, uid)
        amount = int(amount)
        if action not in ("deposit", "withdraw"):
            raise StripError("bad_args", "Deposit or withdraw?")
        have = w["cash"] if action == "deposit" else w["bank"]
        if amount <= 0 or amount > have:
            raise StripError("broke", f"You only have {have:,} {'on hand' if action == 'deposit' else 'banked'}.")
        if action == "deposit":
            self.db.econ_adjust(gid, uid, cash=-amount, bank=amount)
        else:
            self.db.econ_adjust(gid, uid, cash=amount, bank=-amount)
        return {"action": action, "amount": amount}

    def grant(self, gid: int, uid: int, amount: int, reason: str = "") -> dict:
        self.guard(gid, uid)
        amount = int(amount)
        if amount <= 0 or amount > GRANT_MAX:
            raise StripError("bad_amount", "Too good to be true.")
        now = self.now()
        recent = [(t, a) for t, a in self._grants.get((gid, uid), []) if now - t < 3600]
        if sum(a for _, a in recent) + amount > GRANT_PER_HOUR:
            raise StripError("limit", "The Strip's tapped out for you this hour.")
        recent.append((now, amount))
        self._grants[(gid, uid)] = recent
        self.db.econ_adjust(gid, uid, cash=amount)
        return {"amount": amount, "reason": str(reason)[:40]}

    def goal_reward(self, gid: int, uid: int) -> dict:
        self.guard(gid, uid)
        key = f"goal_{uid}"
        if self.db.get_config(gid, "strip_game").get(key):
            raise StripError("already", "You already got your welcome money.")
        self.db.update_config(gid, "strip_game", **{key: True})
        self.db.econ_adjust(gid, uid, cash=GOAL_REWARD)
        return {"amount": GOAL_REWARD}


def _id(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 2**63 else None


class StripAPI:
    """aiohttp front door for StripService."""

    def __init__(self, storage, secret: str) -> None:
        self.svc = StripService(storage)
        self.casino = CasinoService(storage, self.svc.guard)
        self.secret = secret

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/strip/api/health", self.health)
        app.router.add_post("/strip/api/{op}", self.handle)

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "strip_api"})

    def _ops(self):
        s, c = self.svc, self.casino
        return {
            "state": lambda g, u, b: {},
            "daily": lambda g, u, b: s.daily(g, u),
            "work": lambda g, u, b: s.work(g, u, b.get("bonus", 1.0)),
            "buy": lambda g, u, b: s.buy(g, u, b.get("item"), b.get("qty", 1)),
            "use": lambda g, u, b: s.use(g, u, b.get("item")),
            "pawn": lambda g, u, b: s.pawn(g, u, b.get("item"), b.get("qty", 1)),
            "spend": lambda g, u, b: s.spend(g, u, b.get("amount", 0), b.get("reason", "")),
            "grant": lambda g, u, b: s.grant(g, u, b.get("amount", 0), b.get("reason", "")),
            "goal_reward": lambda g, u, b: s.goal_reward(g, u),
            "bank": lambda g, u, b: s.bank(g, u, b.get("action"), b.get("amount", 0)),
            # casino (every money move carries a request id `rid` and happens exactly once)
            "casino_exchange": lambda g, u, b: c.exchange(g, u, b.get("action"), b.get("amount"), b.get("rid")),
            "casino_slots": lambda g, u, b: c.slots(g, u, b.get("bet"), b.get("rid"), b.get("machine", ""), b.get("name", "")),
            "casino_bj_start": lambda g, u, b: c.bj_start(g, u, b.get("table"), b.get("bet"), b.get("rid"), bool(b.get("vip_ok")), b.get("name", "")),
            "casino_bj_act": lambda g, u, b: c.bj_act(g, u, b.get("hand_id"), b.get("action"), b.get("rid"), b.get("name", "")),
            "casino_bj_state": lambda g, u, b: c.bj_state(g, u),
            "casino_visit": lambda g, u, b: c.visit(g, u, b.get("rid")),
            "casino_stats": lambda g, u, b: c.stats(g, u),
        }

    def _table_ops(self):
        c = self.casino
        return {
            "casino_roulette_round": lambda g, b: c.roulette_round(g, b.get("round_id"), b.get("bets"), b.get("table", "roulette")),
            "casino_bj_sweep": lambda g, b: c.bj_sweep(g),
            "casino_recent": lambda g, b: c.recent(g),
        }

    async def handle(self, request: web.Request) -> web.Response:
        given = request.headers.get("Authorization", "")
        if not self.secret or not hmac.compare_digest(given.encode(), f"Bearer {self.secret}".encode()):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        op = request.match_info.get("op", "")
        if op == "catalog":
            return web.json_response({"ok": True, **self.svc.catalog()})
        fn, table_fn = self._ops().get(op), self._table_ops().get(op)
        if fn is None and table_fn is None:
            return web.json_response({"ok": False, "error": "unknown_op"}, status=404)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        if table_fn is not None:   # whole-table ops: a server, no single player
            gid = _id(body.get("guild_id"))
            if gid is None:
                return web.json_response({"ok": False, "error": "bad_ids"}, status=400)
            try:
                return web.json_response({"ok": True, **table_fn(gid, body)})
            except (StripError, CasinoError) as e:
                return web.json_response({"ok": False, "error": e.code, "message": e.message, **e.extra})
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "bad_args"}, status=400)
            except Exception:
                log.exception("Strip API %s failed", op)
                return web.json_response({"ok": False, "error": "server_error"}, status=500)
        gid, uid = _id(body.get("guild_id")), _id(body.get("user_id"))
        if gid is None or uid is None:
            return web.json_response({"ok": False, "error": "bad_ids"}, status=400)
        try:
            result = fn(gid, uid, body)
            return web.json_response({"ok": True, **result, "state": self.svc.state(gid, uid)})
        except (StripError, CasinoError) as e:
            state = None
            try:
                state = self.svc.state(gid, uid)
            except StripError:
                pass
            return web.json_response({"ok": False, "error": e.code, "message": e.message, **e.extra, "state": state})
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "bad_args"}, status=400)
        except Exception:
            log.exception("Strip API %s failed", op)
            return web.json_response({"ok": False, "error": "server_error"}, status=500)


async def start_standalone(storage, secret: str, port: int) -> web.AppRunner:
    """Run the API on its own little web server (used when the owner dashboard is off)."""
    app = web.Application()
    StripAPI(storage, secret).add_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Strip API listening on port %s", port)
    return runner
