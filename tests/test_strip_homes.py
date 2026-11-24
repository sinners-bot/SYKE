"""THE STRIP homes in SYKE: purchases (exactly once) and the storage box (items move, never copy).
Run: pytest --noconftest tests/test_strip_homes.py"""

import threading

import pytest

from test_strip_casino import G, U, U2, Clock, env  # noqa: F401  (fixture + stubs)
from syke.strip_homes import HomeError, HomeService


@pytest.fixture()
def homes(env):
    db, svc, _, clock = env
    return db, HomeService(db, svc.guard, clock=clock)


def test_purchase_once_and_ledger(homes):
    db, h = homes
    start = db.econ_wallet(G, U)["cash"]
    r = h.purchase(G, U, 4000, "property", "rid-home-1", {"prop": "trailer-3"})
    assert r["cash"] == start - 4000 and db.econ_wallet(G, U)["cash"] == start - 4000
    again = h.purchase(G, U, 4000, "property", "rid-home-1")
    assert again["replayed"] and db.econ_wallet(G, U)["cash"] == start - 4000
    with pytest.raises(HomeError) as e:
        h.purchase(G, U, 10_000_000, "property", "rid-home-2")
    assert e.value.code == "bad_amount"
    with pytest.raises(HomeError) as e:
        h.purchase(G, U, 200_000, "property", "rid-home-3")
    assert e.value.code == "broke" and db.econ_wallet(G, U)["cash"] == start - 4000
    rows = db._read("SELECT kind, amount, cash_after FROM strip_home_ledger WHERE guild_id=? AND user_id=?", (G, U))
    assert rows == [("property", 4000, start - 4000)]


def test_concurrent_copies_charge_once(homes):
    db, h = homes
    start = db.econ_wallet(G, U)["cash"]
    out = []
    ts = [threading.Thread(target=lambda: out.append(h.purchase(G, U, 1000, "furniture", "rid-same-1"))) for _ in range(8)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert db.econ_wallet(G, U)["cash"] == start - 1000 and sum(1 for r in out if not r.get("replayed")) == 1


def test_store_and_take_move_items(homes):
    db, h = homes
    db.econ_item_add(G, U, "cookie", 5)
    r = h.store(G, U, "trailer-3", "cookie", 3, 10, "rid-st-1")
    assert r["box"] == {"cookie": 3} and db.econ_items(G, U)["cookie"] == 2
    assert h.store(G, U, "trailer-3", "cookie", 3, 10, "rid-st-1")["replayed"] and db.econ_items(G, U)["cookie"] == 2
    with pytest.raises(HomeError) as e:
        h.store(G, U, "trailer-3", "cookie", 5, 10, "rid-st-2")
    assert e.value.code == "no_item"
    with pytest.raises(HomeError) as e:
        h.store(G, U, "trailer-3", "cookie", 2, 4, "rid-st-3")
    assert e.value.code == "full" and db.econ_items(G, U)["cookie"] == 2
    with pytest.raises(HomeError) as e:
        h.store(G, U, "trailer-3", "nonsense", 1, 10, "rid-st-4")
    assert e.value.code == "not_storable"
    r = h.take(G, U, "trailer-3", "cookie", 2, "rid-tk-1")
    assert r["box"] == {"cookie": 1} and db.econ_items(G, U)["cookie"] == 4
    with pytest.raises(HomeError):
        h.take(G, U, "trailer-3", "cookie", 2, "rid-tk-2")
    # another player's box is theirs
    assert h.storage(G, U2, "trailer-3")["box"] == {}
    # moving out returns everything
    r = h.release(G, U, "trailer-3")
    assert r["returned"] == {"cookie": 1} and db.econ_items(G, U)["cookie"] == 5 and h.storage(G, U, "trailer-3")["box"] == {}


def test_concurrent_takes_never_duplicate(homes):
    db, h = homes
    db.econ_item_add(G, U, "cookie", 1)
    h.store(G, U, "apt", "cookie", 1, 10, "rid-cc-1")
    ok = []
    def take(i):
        try:
            h.take(G, U, "apt", "cookie", 1, f"rid-cc-take-{i}"); ok.append(i)
        except HomeError:
            pass
    ts = [threading.Thread(target=take, args=(i,)) for i in range(8)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert len(ok) == 1 and db.econ_items(G, U)["cookie"] == 1


def test_split_purchase_both_or_neither(homes):
    db, h = homes
    a0, b0 = db.econ_wallet(G, U)["cash"], db.econ_wallet(G, U2)["cash"]
    r = h.purchase_split(G, U, U2, 1001, "upgrade", "rid-split-1", {"prop": "lot-1"})
    assert (r["mine"], r["theirs"]) == (501, 500)
    assert db.econ_wallet(G, U)["cash"] == a0 - 501 and db.econ_wallet(G, U2)["cash"] == b0 - 500
    assert h.purchase_split(G, U, U2, 1001, "upgrade", "rid-split-1")["replayed"] and db.econ_wallet(G, U2)["cash"] == b0 - 500
    db.econ_adjust(G, U2, cash=-(b0 - 500))   # the partner is broke now
    with pytest.raises(HomeError) as e:
        h.purchase_split(G, U, U2, 1000, "upgrade", "rid-split-2")
    assert e.value.code == "partner_broke" and db.econ_wallet(G, U)["cash"] == a0 - 501   # and nobody paid
    with pytest.raises(HomeError):
        h.purchase_split(G, U, U, 1000, "upgrade", "rid-split-3")


def test_transfer_from_partner_box_is_logged_and_never_copies(homes):
    db, h = homes
    db.econ_item_add(G, U2, "cookie", 2)
    h.store(G, U2, "lot-1", "cookie", 2, 10, "rid-ps-1")
    out = []
    ts = [threading.Thread(target=lambda i=i: out.append(_try(lambda: h.transfer(G, U, U2, "lot-1", "cookie", 1, f"rid-tr-{i}")))) for i in range(5)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert sum(1 for r in out if r) == 2 and db.econ_items(G, U).get("cookie", 0) == 2
    assert h.storage_of(G, U, U2, "lot-1")["box"] == {}
    rows = db._read("SELECT from_user, to_user, item, qty FROM strip_home_item_log WHERE guild_id=?", (G,))
    assert rows == [(U2, U, "cookie", 1), (U2, U, "cookie", 1)]


def _try(fn):
    try:
        return fn()
    except HomeError:
        return None


def test_resell_pays_half_once_and_only_for_purchases(homes):
    db, h = homes
    start = db.econ_wallet(G, U)["cash"]
    h.purchase(G, U, 900, "furniture", "rid-furn-1", {"type": "sofa"})
    h.purchase(G, U, 4000, "property", "rid-prop-1")
    r = h.resell(G, U, "rid-furn-1")
    assert r["amount"] == 450 and r["paid"] == 900 and db.econ_wallet(G, U)["cash"] == start - 900 - 4000 + 450
    again = h.resell(G, U, "rid-furn-1")
    assert again["replayed"] and db.econ_wallet(G, U)["cash"] == start - 900 - 4000 + 450
    for rid, code in [("rid-prop-1", "not_bought"), ("rid-never", "not_bought")]:
        with pytest.raises(HomeError) as e:
            h.resell(G, U, rid)
        assert e.value.code == code
    with pytest.raises(HomeError) as e:          # someone else's purchase is not yours to sell
        h.resell(G, U2, "rid-furn-1")
    assert e.value.code == "not_bought"


def test_resell_a_set_piece_by_piece(homes):
    db, h = homes
    start = db.econ_wallet(G, U)["cash"]
    h.purchase(G, U, 3000, "furniture", "rid-set-1", {"collection": "x", "pieces": ["a", "b", "c"]})
    assert h.resell(G, U, "rid-set-1", 0, 3)["amount"] == 500
    assert h.resell(G, U, "rid-set-1", 0, 3)["replayed"]
    assert h.resell(G, U, "rid-set-1", 2, 3)["amount"] == 500
    assert db.econ_wallet(G, U)["cash"] == start - 3000 + 1000
    with pytest.raises(HomeError):
        h.resell(G, U, "rid-set-1", 3, 3)
    assert h.resell(G, U, "rid-set-1", 1)["amount"] == 500          # piece count read from the purchase
