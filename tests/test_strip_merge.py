"""Pointing THE STRIP at a real server's wallets: the game-only wallets are added in once (cash, bank, items, buffs,
storefronts or a pay-back, menu dishes, home storage), the game-only wallets are emptied, and running it again changes nothing.
Run: pytest --noconftest tests/test_strip_merge.py"""
from test_strip_casino import G, U, Clock, env  # noqa: F401
from syke import strip_merge

GAME = 1          # the game-only "guild"
REAL = G          # the real server
V = U + 1


def test_merge_once(env):
    db, svc, _, clock = env
    db.econ_adjust(REAL, U, cash=11_000, bank=2_000)       # what's already in the real wallet
    db.econ_adjust(GAME, U, cash=1700, bank=50)             # earned in the game
    db.econ_adjust(GAME, V, cash=1300)                      # someone with no real wallet yet
    db.econ_item_add(GAME, U, "dish_burger", 2)
    db.econ_item_add(REAL, U, "dish_burger", 1)
    db.econ_buff(GAME, U, "glam", 9e9)
    assert db.econ_shop_buy(GAME, "dragon", U, 1000, 1.0)    # free in the real server: moves
    assert db.econ_shop_buy(GAME, "general", V, 1000, 1.0)
    assert db.econ_shop_buy(REAL, "general", U, 1000, 1.0)   # taken in the real server: V is paid back
    invested = db.econ_shop(GAME, "general")["invested"]
    before_u, before_v = db.econ_wallet(REAL, U), db.econ_wallet(REAL, V)["cash"]
    g_u, g_v = db.econ_wallet(GAME, U), db.econ_wallet(GAME, V)["cash"]
    r = strip_merge.merge(db, GAME, REAL)
    assert r["merged"] and r["summary"]["players"] == 2
    w = db.econ_wallet(REAL, U)
    assert w["cash"] == before_u["cash"] + g_u["cash"] and w["bank"] == before_u["bank"] + g_u["bank"]
    assert db.econ_wallet(REAL, V)["cash"] == before_v + g_v + invested
    assert db.econ_items(REAL, U)["dish_burger"] == 3 and not db.econ_items(GAME, U)
    assert db.econ_wallet(GAME, U)["cash"] == 0 and db.econ_wallet(GAME, V)["cash"] == 0
    assert db.econ_shop(REAL, "dragon")["owner_id"] == U and db.econ_shop(GAME, "dragon") is None
    assert db.econ_shop(REAL, "general")["owner_id"] == U
    assert "glam" in db.econ_buffs(REAL, U, 0)
    again = strip_merge.merge(db, GAME, REAL)
    assert not again["merged"] and again["reason"] == "already"
    assert db.econ_wallet(REAL, U)["cash"] == w["cash"]
    assert strip_merge.status(db, GAME, REAL)["summary"]["cash"] == g_u["cash"] + g_v


def test_merge_through_the_api(tmp_path):
    import test_strip_api  # noqa: F401  (installs the aiohttp stand-ins)
    from syke.storage import Storage
    from syke.strip_api import StripAPI
    db = Storage(str(tmp_path / "m.db"))
    db.econ_adjust(GAME, U, cash=400)
    api = StripAPI(db, "s")
    out = api._table_ops()["strip_merge"](REAL, {"from_guild": GAME})
    assert out["merged"] and db.econ_wallet(REAL, U)["cash"] >= 400
    assert not api._table_ops()["strip_merge"](REAL, {"from_guild": GAME})["merged"]
