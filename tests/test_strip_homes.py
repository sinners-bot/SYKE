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
