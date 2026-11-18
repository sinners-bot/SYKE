"""THE STRIP kitchen in SYKE: groceries at SYKE's prices, cooking swaps ingredients for a dish in one go (or nothing
changes), eating and giving, food on a business's menu (owner paid, the Strip's cut, sold out), request ids,
the bag labels, and no free money from the pawn shop.
Run: pytest --noconftest tests/test_strip_kitchen.py"""
import re
from pathlib import Path

import pytest

from test_strip_casino import G, U, Clock, env  # noqa: F401
from syke import stripworld as sw
from syke.strip_kitchen import BIZ_FEE, DISHES, INGREDIENTS, KitchenError, KitchenService

V = U + 1


@pytest.fixture()
def k(env):
    db, svc, _, clock = env
    svc.guard(G, V)
    db.econ_adjust(G, U, cash=5000)
    return db, KitchenService(db, svc.guard, clock=clock)


def items(db, u=U):
    return db.econ_items(G, u)


def test_buy_cook_eat(k):
    db, K = k
    c0 = db.econ_wallet(G, U)["cash"]
    r = K.buy(G, U, {"ing_bun": 2, "ing_patty": 1, "ing_cheese": 1, "ing_lettuce": 1}, 15, "buy-0001")
    price = 2 * INGREDIENTS["bun"][2] + INGREDIENTS["patty"][2] + INGREDIENTS["cheese"][2] + INGREDIENTS["lettuce"][2]
    assert r["total"] == price + 15 and db.econ_wallet(G, U)["cash"] == c0 - price - 15
    again = K.buy(G, U, {"ing_bun": 2, "ing_patty": 1, "ing_cheese": 1, "ing_lettuce": 1}, 15, "buy-0001")
    assert again["replayed"] and db.econ_wallet(G, U)["cash"] == c0 - price - 15
    K.cook(G, U, {"ing_bun": 1, "ing_patty": 1, "ing_cheese": 1, "ing_lettuce": 1}, {"dish_burger": 2}, 0, "cook-0001")
    have = items(db)
    assert have["dish_burger"] == 2 and have["ing_bun"] == 1 and "ing_patty" not in have
    with pytest.raises(KitchenError) as e:   # not enough: nothing changes
        K.cook(G, U, {"ing_bun": 1, "ing_patty": 1}, {"dish_burger": 1}, 0, "cook-0002")
    assert e.value.code == "no_item" and items(db)["ing_bun"] == 1 and items(db)["dish_burger"] == 2
    K.consume(G, U, "dish_burger", 1, "eat-00001")
    assert items(db)["dish_burger"] == 1


def test_only_kitchen_keys(k):
    db, K = k
    for bad in ({"scratch": 1}, {"ing_gold": 1}, {"ing_bun": 99}):
        with pytest.raises(KitchenError):
            K.buy(G, U, bad, 0, "bad-" + str(len(bad)) + "xxxx" + str(list(bad)[0])[:3])
    with pytest.raises(KitchenError):
        K.cook(G, U, {"ing_bun": 1}, {"scratch": 5}, 0, "cook-bad01")


def test_give_and_menu(k):
    db, K = k
    db.econ_item_add(G, U, "dish_pancakes", 3)
    K.give(G, U, V, "dish_pancakes", 1, "give-0001")
    assert items(db, V)["dish_pancakes"] == 1 and items(db)["dish_pancakes"] == 2
    with pytest.raises(KitchenError) as e:
        K.stock(G, U, "dragon", "dish_pancakes", 1, 100)
    assert e.value.code == "not_yours"
    db.econ_adjust(G, U, cash=100_000)
    from syke.strip_phone import PhoneService
    PhoneService(db, lambda g, u: db.econ_touch(g, u)).biz_buy(G, U, "general")
    with pytest.raises(KitchenError) as e:   # the General Store doesn't do burgers
        db.econ_item_add(G, U, "dish_burger", 1)
        K.stock(G, U, "general", "dish_burger", 1, 100)
    assert e.value.code == "wrong_food"
    m = K.stock(G, U, "general", "dish_pancakes", 2, 150)
    assert m["menu"] == [{"dish": "dish_pancakes", "qty": 2, "price": 150}] and "dish_pancakes" not in items(db)
    db.econ_adjust(G, V, cash=1000)
    owner0, buyer0 = db.econ_wallet(G, U)["cash"], db.econ_wallet(G, V)["cash"]
    o = K.order(G, V, "general", "dish_pancakes", 150, "order-0001")
    assert o["paid_owner"] == 150 - int(150 * BIZ_FEE)
    assert db.econ_wallet(G, V)["cash"] == buyer0 - 150 and db.econ_wallet(G, U)["cash"] == owner0 + o["paid_owner"]
    assert items(db, V)["dish_pancakes"] == 2
    assert K.order(G, V, "general", "dish_pancakes", 150, "order-0001")["replayed"]
    with pytest.raises(KitchenError) as e:
        K.order(G, V, "general", "dish_pancakes", 99, "order-0002")
    assert e.value.code == "price_changed"
    K.order(G, V, "general", "dish_pancakes", 150, "order-0003")
    with pytest.raises(KitchenError) as e:
        K.order(G, V, "general", "dish_pancakes", 150, "order-0004")
    assert e.value.code == "sold_out"
    assert K.menu(G, V, "general")["menu"] == []


def test_labels_and_no_free_money(k):
    assert sw.label("dish_burger") == "🍔 Classic burger" and sw.label("ing_egg") == "🥚 Eggs"
    assert sw.pawn_value("dish_burger") == 0 and sw.pawn_value("ing_egg") == INGREDIENTS["egg"][2] // 2


def test_same_lists_as_the_game():
    src = (Path(__file__).resolve().parents[2] / "hometown-repo" / "shared" / "food.js")
    if not src.exists():
        pytest.skip("the game's repository isn't next to SYKE here")
    js = src.read_text()
    ing = set(re.findall(r"^  (\w+): \['", js.split("export const ING = {")[1].split("};")[0], re.M)) | set(re.findall(r" (\w+): \['", js.split("export const ING = {")[1].split("};")[0]))
    rec = set(re.findall(r"^  (\w+):\s+R\(", js, re.M))
    assert ing == set(INGREDIENTS) and rec == set(DISHES)
