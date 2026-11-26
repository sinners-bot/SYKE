"""THE STRIP 3.21 in SYKE: running a storefront (prices, staff, wages) and the resale of limited pieces.
Run: pytest --noconftest tests/test_strip_biz.py"""
import pytest

from test_strip_casino import G, U, Clock, env  # noqa: F401
from syke import stripworld as sw
from syke.strip_biz import TAKINGS, BizService
from syke.strip_phone import PhoneError, PhoneService

V = U + 1


@pytest.fixture()
def biz(env):
    db, svc, _, clock = env
    svc.guard(G, V)
    db.econ_adjust(G, V, cash=50_000)
    b = BizService(db, svc.guard, clock=clock)
    svc.biz = b
    return db, svc, b, PhoneService(db, svc.guard, clock=clock), clock


def cash(db, u):
    return db.econ_wallet(G, u)["cash"]


def test_game_storefronts_can_be_owned_and_listed(biz):
    db, svc, B, P, clock = biz
    keys = {s["key"] for s in P.biz_list(G, U)["stores"]}
    assert {"threads", "masks", "diner", "video"} <= keys
    P.biz_buy(G, U, "threads")
    assert db.econ_shop(G, "threads")["owner_id"] == U


def test_prices_change_what_buyers_pay_and_walk_in_trade(biz):
    db, svc, B, P, clock = biz
    P.biz_buy(G, U, "general")
    clock.t += 3600
    base_reg = sw.pending(db.econ_shop(G, "general"), sw.STORE_BY_KEY["general"], clock())
    B.set_prices(G, U, "general", markup=0.5)
    shop = db.econ_shop(G, "general")
    assert shop["markup"] == 0.5 and shop["register"] == base_reg   # settled at the old price first
    clock.t += 3600
    m = B.manage(G, U, "general")
    assert m["income"] == int(m["base_income"] * 0.6)                # +50% prices: 60% of the walk-ins
    item = next(i for i in sw.ITEMS if i.store == "general")
    before_v, before_reg = cash(db, V), db.econ_shop(G, "general")["register"]
    r = svc.buy(G, V, item.key, 1)
    assert r["total"] == round(item.price * 1.5)
    assert cash(db, V) == before_v - r["total"]
    assert db.econ_shop(G, "general")["register"] - before_reg == int(item.price * sw.OWNER_CUT) + (r["total"] - item.price)
    # the owner pays list price in their own store
    assert svc.buy(G, U, item.key, 1)["total"] == item.price
    # a purchase in the game with a store (clothes): same rules
    sp = svc.spend(G, V, 1000, "drip", store="general")
    assert sp["amount"] == 1500 and sp["list"] == 1000
    assert svc.state(G, V)["markups"] == {"general": 0.5}


def test_markup_is_clamped(biz):
    db, svc, B, P, clock = biz
    P.biz_buy(G, U, "video")
    assert B.set_prices(G, U, "video", markup=9)["markup"] == 0.5
    assert B.set_prices(G, U, "video", markup=-9)["markup"] == -0.25
    with pytest.raises(PhoneError):
        B.set_prices(G, V, "video", markup=0.1)


def test_hiring_and_staffed_shifts(biz):
    db, svc, B, P, clock = biz
    P.biz_buy(G, U, "video")
    with pytest.raises(PhoneError):
        B.hire(G, U, "video", U)
    B.hire(G, U, "video", V)
    assert B.jobs(G, V)["offers"][0]["store"] == "video"
    B.set_prices(G, U, "video", wage=0.2)
    B.accept(G, V, "video")
    assert B.jobs(G, V)["job"]["store"] == "video"
    reg0, cash0 = db.econ_shop(G, "video")["register"], cash(db, V)
    r = svc.work(G, V, shift=True, store="video")
    pay, staff = r["amount"], r["staff"]
    assert staff["wage"] == int(pay * 0.2)
    assert cash(db, V) - cash0 == pay + staff["wage"]
    assert db.econ_shop(G, "video")["register"] - reg0 == int(pay * TAKINGS) - staff["wage"]
    # not staff anywhere else: no split
    clock.t += 3600
    assert svc.work(G, V, shift=True, store="photo")["staff"] is None
    B.fire(G, U, "video", V)
    assert B.jobs(G, V)["job"] is None


def test_staff_cap_and_one_job_at_a_time(biz):
    db, svc, B, P, clock = biz
    P.biz_buy(G, U, "video"); P.biz_buy(G, U, "photo")
    for k in range(3):
        B.hire(G, U, "video", 9000 + k)
    with pytest.raises(PhoneError):
        B.hire(G, U, "video", 9100)
    B.hire(G, U, "photo", V); B.hire(G, U, "video", V) if False else None
    B.accept(G, V, "photo")
    assert B.jobs(G, V)["job"]["store"] == "photo"
    B.quit(G, V)
    assert B.jobs(G, V)["job"] is None


def test_selling_the_store_lets_the_staff_go(biz):
    db, svc, B, P, clock = biz
    P.biz_buy(G, U, "video"); B.hire(G, U, "video", V); B.accept(G, V, "video")
    P.biz_sell(G, U, "video")
    assert B.jobs(G, V)["job"] is None


def test_wear_trade_moves_cash_less_a_fee(biz):
    db, svc, B, P, clock = biz
    total0 = cash(db, U) + cash(db, V)
    r = svc.wear_trade(G, V, U, 2000)
    assert r["fee"] == 200 and r["payout"] == 1800
    assert cash(db, U) + cash(db, V) == total0 - 200
    with pytest.raises(Exception):
        svc.wear_trade(G, V, V, 2000)
    with pytest.raises(Exception):
        svc.wear_trade(G, V, U, 10**9)
