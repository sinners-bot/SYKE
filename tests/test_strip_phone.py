"""THE STRIP phone in SYKE: storefront business and the player marketplace (escrow, one-shot buys, no new money).
Run: pytest --noconftest tests/test_strip_phone.py"""
import threading

import pytest

from test_strip_casino import G, U, Clock, env  # noqa: F401
from syke.strip_phone import FEE, MAX_OPEN, PhoneError, PhoneService

V = U + 1


@pytest.fixture()
def ph(env):
    db, svc, _, clock = env
    svc.guard(G, V)
    return db, PhoneService(db, svc.guard, clock=clock), clock


def money(db):
    return sum(db.econ_wallet(G, u)["cash"] for u in (U, V))


def test_business_buy_upgrade_collect_sell(ph):
    db, P, clock = ph
    db.econ_adjust(G, U, cash=100_000)
    lst = P.biz_list(G, U)["stores"]
    video = next(s for s in lst if s["key"] == "video")
    assert video["owner"] is None and video["price"] == 8000
    before = db.econ_wallet(G, U)["cash"]
    P.biz_buy(G, U, "video")
    assert db.econ_wallet(G, U)["cash"] == before - 8000
    with pytest.raises(PhoneError):
        P.biz_buy(G, V, "video")
    clock.t += 3600
    mine = next(s for s in P.biz_list(G, U)["stores"] if s["key"] == "video")
    assert mine["mine"] and mine["register"] == 60 and mine["upgrade"] == 4800
    up = P.biz_upgrade(G, U, "video")
    assert up["level"] == 2
    got = P.biz_collect(G, U)
    assert got["total"] == 60
    with pytest.raises(PhoneError):
        P.biz_sell(G, V, "video")
    sold = P.biz_sell(G, U, "video")
    assert sold["value"] == int((8000 + 4800) * 0.6)
    assert next(s for s in P.biz_list(G, U)["stores"] if s["key"] == "video")["owner"] is None


def test_market_escrow_buy_and_cancel(ph):
    db, P, _ = ph
    db.econ_item_add(G, U, "charm", 3)
    start = money(db)
    lst = P.market_post(G, U, "charm", 2, 2000)["listing"]
    assert db.econ_items(G, U)["charm"] == 1 and lst["status"] == "open"
    assert any(x["id"] == lst["id"] for x in P.market_browse(G, V)["listings"])
    with pytest.raises(PhoneError):
        P.market_buy(G, U, lst["id"], 2000)          # not your own
    with pytest.raises(PhoneError):
        P.market_buy(G, V, lst["id"], 1)             # price must match what you saw
    db.econ_adjust(G, V, cash=5000)
    start = money(db)
    r = P.market_buy(G, V, lst["id"], 2000)
    assert db.econ_items(G, V)["charm"] == 2 and r["payout"] == 2000 - int(2000 * FEE)
    assert money(db) == start - int(2000 * FEE)      # only the fee leaves; nothing is created
    with pytest.raises(PhoneError):
        P.market_buy(G, V, lst["id"], 2000)
    l2 = P.market_post(G, U, "charm", 1, 500)["listing"]
    P.market_cancel(G, U, l2["id"])
    assert db.econ_items(G, U)["charm"] == 1
    with pytest.raises(PhoneError):
        P.market_cancel(G, U, l2["id"])


def test_market_rules(ph):
    db, P, clock = ph
    with pytest.raises(PhoneError):
        P.market_post(G, U, "charm", 1, 100)          # don't have it
    with pytest.raises(PhoneError):
        P.market_post(G, U, "chips", 1, 100)          # not a tradeable item
    db.econ_item_add(G, U, "scratch", 20)
    for _ in range(MAX_OPEN):
        P.market_post(G, U, "scratch", 1, 50)
    with pytest.raises(PhoneError):
        P.market_post(G, U, "scratch", 1, 50)
    clock.t += 8 * 86400                                # a week later, unsold listings come back
    P.market_browse(G, V)
    assert db.econ_items(G, U)["scratch"] == 20


def test_concurrent_buyers_one_wins(ph):
    db, P, _ = ph
    db.econ_item_add(G, U, "crate", 1)
    lid = P.market_post(G, U, "crate", 1, 300)["listing"]["id"]
    db.econ_adjust(G, V, cash=10_000)
    out = []

    def go():
        try:
            out.append(P.market_buy(G, V, lid, 300))
        except PhoneError as e:
            out.append(e.code)
    ts = [threading.Thread(target=go) for _ in range(6)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert sum(1 for x in out if isinstance(x, dict)) == 1 and db.econ_items(G, V)["crate"] == 1
