"""Crime for THE STRIP game: the money rules behind what players do with their characters.

The game decides what happened in the world (who was standing where, how well the lock was picked, whether the police
caught up). This module decides what it is worth and what it costs, with the same odds, fines and bail as the Discord
commands in stripworld.py, so the two never drift apart.

* ``job``     a street crime (pickpocket, car break-in, hold-up, a crew's vault job). Skill from the minigame nudges
              the odds. Getting caught costs a fine; jail only follows if the police actually arrest you.
* ``rob``     take another player's cash on hand, face to face. Padlocks and the bank protect exactly as in Discord.
* ``arrest``  the police caught you: jail for a number of minutes (a tarot reading still saves you).
* ``bail``    pay your own bond, or a friend's.
* ``illicit`` cash from the fence or from selling contraband, capped per sale and per hour.
"""
from __future__ import annotations

import random
import time

from . import stripworld as sw

HOUR = 3600
# kind: base chance, pay low, pay high, cooldown (s), fine low, fine high
JOBS = {
    "pickpocket": (0.50, 60, 220, 90, 60, 150),
    "carbreak": (0.45, 150, 450, 240, 120, 300),
    "holdup": (0.38, 500, 1_400, 900, 250, 600),
    "vault": (0.32, 2_500, 6_000, 3 * HOUR, 600, 1_500),
}
SKILL_BONUS = 0.25             # a perfect minigame adds this to the base chance
JOB_PER_HOUR = 4_000           # street crime takings per player per hour (the vault is outside this)
ARREST_MIN, ARREST_MAX = 5, 60
ILLICIT_MAX = 2_500            # biggest single sale to the fence or a buyer
ILLICIT_PER_HOUR = 8_000
BOUNTY_MAX = 600               # an officer's reward for one collar
BOUNTY_PER_HOUR = 2_400


class CrimeError(Exception):
    def __init__(self, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


class CrimeService:
    def __init__(self, svc, rng: random.Random | None = None, clock=time.time) -> None:
        self.svc, self.db = svc, svc.db
        self.rng = rng or random.Random()
        self.now = clock
        self._cool: dict[tuple[int, int, str], float] = {}
        self._takes: dict[tuple[int, int, str], list[tuple[float, int]]] = {}

    def _free(self, gid: int, uid: int) -> dict:
        w = self.svc.guard(gid, uid)
        if w.get("jail_until", 0) > self.now():
            raise CrimeError("jailed", "You're in jail.", until=w["jail_until"])
        return w

    def _room(self, gid: int, uid: int, bucket: str, cap: int) -> int:
        now = self.now()
        recent = [(t, a) for t, a in self._takes.get((gid, uid, bucket), []) if now - t < HOUR]
        self._takes[(gid, uid, bucket)] = recent
        return max(0, cap - sum(a for _, a in recent))

    def _took(self, gid: int, uid: int, bucket: str, amount: int) -> None:
        self._takes.setdefault((gid, uid, bucket), []).append((self.now(), amount))

    def job(self, gid: int, uid: int, kind: str, skill: float = 0.0, share: float = 1.0, roll: float | None = None) -> dict:
        """`roll` lets a crew share one throw of the dice, so the vault opens for all of them or none."""
        if kind not in JOBS:
            raise CrimeError("bad_args", "That's not a job anyone's heard of.")
        self._free(gid, uid)
        base, lo, hi, cool, flo, fhi = JOBS[kind]
        now = self.now()
        ready = self._cool.get((gid, uid, kind), 0) + cool
        if ready > now:
            raise CrimeError("cooldown", "Too hot. Lay low a while.", ready_at=ready)
        self._cool[(gid, uid, kind)] = now
        skill = max(0.0, min(1.0, float(skill)))
        lucky = "lucky" in self.db.econ_buffs(gid, uid, now)
        chance = min(0.95, base + skill * SKILL_BONUS + (sw.LUCK_BONUS if lucky else 0.0))
        throw = self.rng.random() if roll is None else max(0.0, min(1.0, float(roll)))
        if throw < chance:
            pay = int(self.rng.randint(lo, hi) * self.svc.happy(gid) * max(0.1, min(1.0, float(share))))
            if kind != "vault":
                pay = min(pay, self._room(gid, uid, "job", JOB_PER_HOUR))
                self._took(gid, uid, "job", pay)
            if pay > 0:
                self.db.econ_adjust(gid, uid, cash=pay)
            return {"success": True, "payout": pay, "kind": kind, "ready_at": now + cool}
        fine = self.db.econ_move(gid, uid, None, self.rng.randint(flo, fhi))
        return {"success": False, "fine": fine, "kind": kind, "ready_at": now + cool}

    def rob(self, gid: int, uid: int, target: int, skill: float = 0.0) -> dict:
        w = self._free(gid, uid)
        if target == uid:
            raise CrimeError("bad_args", "Pick someone else.")
        now = self.now()
        if w["rob_at"] and now - w["rob_at"] < sw.ROB_COOLDOWN:
            raise CrimeError("cooldown", "Too hot to rob again.", ready_at=w["rob_at"] + sw.ROB_COOLDOWN)
        t = self.db.econ_wallet(gid, target)
        if w["cash"] < sw.ROB_MIN_CASH:
            raise CrimeError("broke", f"Carry at least {sw.ROB_MIN_CASH:,}: if you get caught, you pay a fine.")
        if t["cash"] < sw.ROB_MIN_CASH:
            raise CrimeError("poor_target", "They have barely anything on them. (The bank is off-limits.)")
        self.db.econ_adjust(gid, uid, rob_at=now)
        if self.db.econ_items(gid, target).get("padlock"):
            self.db.econ_item_add(gid, target, "padlock", -1)
            fine = self.db.econ_move(gid, uid, target, sw.rob_fine(w["cash"]))
            return {"success": False, "padlock": True, "fine": fine}
        lucky = "lucky" in self.db.econ_buffs(gid, uid, now)
        skill = max(0.0, min(1.0, float(skill)))
        if self.rng.random() < min(0.95, sw.odds(sw.ROB_CHANCE, lucky) + skill * 0.2):
            take = self.db.econ_move(gid, target, uid, sw.rob_take(self.rng, t["cash"]))
            return {"success": True, "payout": take}
        fine = self.db.econ_move(gid, uid, target, sw.rob_fine(w["cash"]))
        return {"success": False, "fine": fine}

    def arrest(self, gid: int, uid: int, minutes: float) -> dict:
        self.svc.guard(gid, uid)
        now = self.now()
        if "foresight" in self.db.econ_buffs(gid, uid, now):
            return {"jailed": False, "foresight": True}
        minutes = max(ARREST_MIN, min(ARREST_MAX, float(minutes)))
        until = now + minutes * 60
        self.db.econ_adjust(gid, uid, jail_until=until)
        return {"jailed": True, "until": until, "minutes": minutes}

    def bail(self, gid: int, uid: int, target: int | None = None) -> dict:
        self.svc.guard(gid, uid)
        who = target or uid
        t = self.db.econ_wallet(gid, who)
        left = t.get("jail_until", 0) - self.now()
        if left <= 0:
            raise CrimeError("not_jailed", "Nobody to bail out.")
        cost = sw.bail_cost(left)
        if not self.db.econ_spend(gid, uid, cost):
            raise CrimeError("broke", f"Bail is {cost:,}. You don't have that on you.", cost=cost)
        self.db.econ_adjust(gid, who, jail_until=0)
        return {"cost": cost, "freed": who}

    def bail_quote(self, gid: int, uid: int, target: int | None = None) -> dict:
        t = self.db.econ_wallet(gid, target or uid)
        left = t.get("jail_until", 0) - self.now()
        return {"cost": sw.bail_cost(left) if left > 0 else 0, "seconds": max(0, left)}

    def serve(self, gid: int, uid: int, minutes: float) -> dict:
        """Jail work: each finished task takes a few minutes off the sentence."""
        w = self.svc.guard(gid, uid)
        now = self.now()
        if w.get("jail_until", 0) <= now:
            raise CrimeError("not_jailed", "You're not locked up.")
        until = max(now, w["jail_until"] - max(0.0, min(5.0, float(minutes))) * 60)
        self.db.econ_adjust(gid, uid, jail_until=until if until > now else 0)
        return {"until": until if until > now else 0}

    def escape(self, gid: int, uid: int, target: int | None = None, ok: bool = False, penalty: float = 0) -> dict:
        """A jailbreak, by the prisoner or by a friend on the outside. The game decides how it went (and puts the
        police on whoever ran); here a clean break opens the cell and a botched one adds time, at most five minutes."""
        who = target or uid
        if target:
            self.svc.guard(gid, uid)
        t = self.db.econ_wallet(gid, who)
        now = self.now()
        if t.get("jail_until", 0) <= now:
            raise CrimeError("not_jailed", "Nobody's locked up.")
        if ok:
            self.db.econ_adjust(gid, who, jail_until=0)
            return {"escaped": True, "freed": who, "until": 0}
        until = min(now + ARREST_MAX * 60, t["jail_until"] + max(0.0, min(5.0, float(penalty or 0))) * 60)
        self.db.econ_adjust(gid, who, jail_until=until)
        return {"escaped": False, "until": until}

    def illicit(self, gid: int, uid: int, amount: int, reason: str = "") -> dict:
        self._free(gid, uid)
        amount = int(amount)
        if amount <= 0 or amount > ILLICIT_MAX:
            raise CrimeError("bad_amount", "Nobody pays that for anything.")
        paid = min(amount, self._room(gid, uid, "illicit", ILLICIT_PER_HOUR))
        if paid <= 0:
            raise CrimeError("limit", "The street's dry for you this hour. Buyers are nervous.")
        self._took(gid, uid, "illicit", paid)
        self.db.econ_adjust(gid, uid, cash=paid)
        return {"amount": paid, "reason": str(reason)[:40]}

    def bounty(self, gid: int, uid: int, amount: int) -> dict:
        self._free(gid, uid)
        amount = int(amount)
        if amount <= 0 or amount > BOUNTY_MAX:
            raise CrimeError("bad_amount", "The city doesn't pay that.")
        paid = min(amount, self._room(gid, uid, "bounty", BOUNTY_PER_HOUR))
        if paid > 0:
            self._took(gid, uid, "bounty", paid)
            self.db.econ_adjust(gid, uid, cash=paid)
        return {"amount": paid}
