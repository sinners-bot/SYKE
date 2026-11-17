"""THE STRIP lifestyle rewards in SYKE: each key pays once, ever; a daily cap; bad input refused.
Run: pytest --noconftest tests/test_strip_life.py"""

import threading

import pytest

from test_strip_casino import G, U, Clock, env  # noqa: F401  (fixture + stubs)
from syke.strip_life import DAILY_CAP, LifeError, LifeService


@pytest.fixture()
def life(env):
    db, svc, _, clock = env
    return db, LifeService(db, svc.guard, clock=clock), clock


def test_each_key_pays_once(life):
    db, L, _ = life
    start = db.econ_wallet(G, U)["cash"]
    r = L.reward(G, U, "daily:2026-11-17:visit3", 150)
    assert r["amount"] == 150 and db.econ_wallet(G, U)["cash"] == start + 150
    again = L.reward(G, U, "daily:2026-11-17:visit3", 150)
    assert again["replayed"] and db.econ_wallet(G, U)["cash"] == start + 150


def test_concurrent_claims_pay_once(life):
    db, L, _ = life
    start = db.econ_wallet(G, U)["cash"]
    out = []
    ts = [threading.Thread(target=lambda: out.append(L.reward(G, U, "weekly:2026-W47:race", 800))) for _ in range(8)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert db.econ_wallet(G, U)["cash"] == start + 800 and sum(1 for r in out if not r.get("replayed")) == 1


def test_daily_cap_and_bad_input(life):
    db, L, clock = life
    paid = 0
    for i in range(10):
        try:
            paid += L.reward(G, U, f"monthly:k{i}", 1500)["amount"]
        except LifeError as e:
            assert e.code == "cap"
    assert paid <= DAILY_CAP
    for bad in ((" bad key ", 10), ("ok-key", 0), ("ok-key", 999_999), ("ok-key", "lots")):
        with pytest.raises(LifeError):
            L.reward(G, U, *bad)
