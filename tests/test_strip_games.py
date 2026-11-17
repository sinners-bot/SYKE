"""THE STRIP casino games (strip_games.py) and their settlement (strip_casino.py): themed slots,
shared roulette with every bet type, and turn-based table blackjack. Run: pytest --noconftest tests/test_strip_games.py"""

import json
import random

import pytest

from test_strip_casino import G, U, U2, Clock, env, wallet  # noqa: F401  (fixture + stubs)
from syke.cardgames import Card, Shoe
from syke.strip_casino import (BJ_BET_SECS, BJ_STALE, RL_BET_SECS, RL_STALE, TURN_SECS, CasinoError, CasinoService)
from syke.strip_games import SLOT_MACHINES, TableRound, roulette_mult, roulette_numbers, slot_pay, slot_rtp

U3 = 444444


def ledger_sum(db, uid):
    return db._read("SELECT COALESCE(SUM(chips_delta),0) FROM strip_casino_ledger WHERE guild_id=? AND user_id=?", (G, uid))[0][0]


def C(code):
    return Card(code[:-1], code[-1])


# ---------------------------------------------------------------- slots
def test_every_machine_pays_less_than_it_takes_but_not_much_less():
    for k in SLOT_MACHINES:
        assert 0.93 < slot_rtp(k) < 0.98, k


def test_slot_pay_tables():
    assert slot_pay("lucky7s", ["triple7"] * 3) == (250, "jackpot")
    assert slot_pay("lucky7s", ["seven", "triple7", "seven"]) == (12, "any")
    assert slot_pay("lucky7s", ["cherry", "lemon", "cherry"]) == (2, "pair")
    assert slot_pay("deadman", ["deadskull"] * 3) == (1300, "jackpot")
    assert slot_pay("neon", ["heart", "star", "dice"]) == (0, "")


def test_themed_spin_is_settled_and_reconciles(env):
    db, _, cas, _ = env
    cas.exchange(G, U, "buy", 50_000, "rid-ts-buy")
    for k, theme in enumerate(SLOT_MACHINES):
        lo, _ = SLOT_MACHINES[theme]["limits"]
        r = cas.slots(G, U, lo, f"rid-ts-{k}", machine=f"s{k}", theme=theme)
        assert r["theme"] == theme and all(x in SLOT_MACHINES[theme]["reels"] for x in r["reels"])
        assert r["payout"] == lo * slot_pay(theme, r["reels"])[0]
    assert wallet(db)[1] == ledger_sum(db, U)
    with pytest.raises(CasinoError) as e:
        cas.slots(G, U, 10, "rid-ts-low", theme="vinnie")
    assert e.value.code == "limit"
    with pytest.raises(CasinoError) as e:
        cas.slots(G, U, 100, "rid-ts-bad", theme="nope")
    assert e.value.code == "bad_machine"


# ---------------------------------------------------------------- roulette rules
def test_every_bet_type_has_the_same_house_edge():
    spaces = [str(n) for n in range(37)] + ["red", "black", "even", "odd", "low", "high", "dozen1", "dozen2", "dozen3", "col1", "col2", "col3"]
    spaces += [f"street:{n}" for n in range(1, 35, 3)] + [f"line:{n}" for n in range(1, 32, 3)]
    spaces += [f"corner:{n}" for n in range(1, 33) if n % 3]
    spaces += ["split:0-1", "split:0-2", "split:0-3"] + [f"split:{n}-{n + 3}" for n in range(1, 34)] + [f"split:{n}-{n + 1}" for n in range(1, 36) if n % 3]
    for sp in spaces:
        assert roulette_numbers(sp), sp
        ev = sum(roulette_mult(sp, n) for n in range(37)) / 37
        assert abs(ev - 36 / 37) < 1e-9, sp
    assert len(spaces) == len(set(spaces)) == 37 + 12 + 12 + 11 + 22 + 3 + 33 + 24
    for bad in ["37", "-1", "07", "split:1-3", "split:3-4", "split:2-1", "street:2", "corner:3", "corner:33", "line:34", "purple", "split:1", ""]:
        assert roulette_numbers(bad) is None, bad
    assert roulette_mult("0", 0) == 36 and roulette_mult("red", 0) == 0 and roulette_mult("dozen2", 13) == 3
    assert roulette_mult("corner:5", 9) == 9 and roulette_mult("split:0-2", 2) == 18 and roulette_mult("line:31", 36) == 6


# ---------------------------------------------------------------- shared roulette
def test_roulette_round_takes_chips_at_the_bet_and_pays_once(env):
    db, _, cas, clock = env
    for u in (U, U2):
        cas.exchange(G, u, "buy", 10_000, f"rid-rl-buy-{u}")
    rnd = cas.rl_open(G, "roulette")
    assert cas.rl_open(G, "roulette")["round_id"] == rnd["round_id"]          # one open round per table
    rid = rnd["round_id"]
    assert rnd["closes_at"] is None
    r = cas.rl_bet(G, U, "roulette", rid, "red", 1000, "rid-rl-1", "Ace")
    assert r["chips"] == 9000 and r["round"]["closes_at"] == clock.t + RL_BET_SECS
    assert cas.rl_bet(G, U, "roulette", rid, "red", 1000, "rid-rl-1")["replayed"]   # same request: once
    cas.rl_bet(G, U, "roulette", rid, "corner:1", 500, "rid-rl-2")
    cas.rl_bet(G, U2, "roulette", rid, "dozen3", 2000, "rid-rl-3")
    with pytest.raises(CasinoError) as e:
        cas.rl_bet(G, U2, "roulette", rid, "split:3-4", 100, "rid-rl-bad")
    assert e.value.code == "bad_space"
    with pytest.raises(CasinoError) as e:
        cas.rl_spin(G, "roulette", rid)
    assert e.value.code == "too_early"
    clock.t += RL_BET_SECS
    with pytest.raises(CasinoError) as e:
        cas.rl_bet(G, U2, "roulette", rid, "red", 100, "rid-rl-late")
    assert e.value.code == "closed"
    res = cas.rl_spin(G, "roulette", rid)
    n = res["number"]
    want = {U: 1000 * roulette_mult("red", n) + 500 * roulette_mult("corner:1", n), U2: 2000 * roulette_mult("dozen3", n)}
    assert wallet(db, U)[1] == 10_000 - 1500 + want[U] and wallet(db, U2)[1] == 10_000 - 2000 + want[U2]
    again = cas.rl_spin(G, "roulette", rid)
    assert again["replayed"] and again["number"] == n and wallet(db, U)[1] == 10_000 - 1500 + want[U]
    with pytest.raises(CasinoError):
        cas.rl_bet(G, U, "roulette", rid, "red", 100, "rid-rl-old")              # a spun round takes no bets
    for u in (U, U2):
        assert wallet(db, u)[1] == ledger_sum(db, u)
    assert cas.rl_open(G, "roulette")["history"][-1] == n


def test_roulette_clear_and_abandoned_round(env):
    db, _, cas, clock = env
    cas.exchange(G, U, "buy", 5_000, "rid-rlc-buy")
    rid = cas.rl_open(G, "roulette")["round_id"]
    cas.rl_bet(G, U, "roulette", rid, "17", 300, "rid-rlc-1")
    cas.rl_bet(G, U, "roulette", rid, "black", 200, "rid-rlc-2")
    r = cas.rl_clear(G, U, "roulette", rid, "rid-rlc-clear")
    assert r["refunded"] == 500 and wallet(db)[1] == 5000 and not r["round"]["bets"]
    cas.rl_bet(G, U, "roulette", rid, "odd", 400, "rid-rlc-3")
    clock.t += RL_BET_SECS + RL_STALE + 1                                         # the game server never came back
    assert cas.casino_sweep(G)["roulette"] == 1
    n = cas.rl_state(G, "roulette")["history"][-1]
    assert wallet(db)[1] == 5000 - 400 + 400 * roulette_mult("odd", n) == ledger_sum(db, U)


def test_vip_wheel_and_limits(env):
    db, _, cas, _ = env
    cas.exchange(G, U, "buy", 5_000, "rid-rlv-buy")
    rid = cas.rl_open(G, "vip_roulette")["round_id"]
    with pytest.raises(CasinoError) as e:
        cas.rl_bet(G, U, "vip_roulette", rid, "red", 1000, "rid-rlv-1")
    assert e.value.code == "vip_only"
    with pytest.raises(CasinoError) as e:
        cas.rl_bet(G, U, "vip_roulette", rid, "red", 100, "rid-rlv-2", vip_ok=True)
    assert e.value.code == "limit"
    with pytest.raises(CasinoError) as e:
        cas.rl_bet(G, U, "vip_roulette", rid, "red", 10_000, "rid-rlv-3", vip_ok=True)
    assert e.value.code in ("broke", "limit")


# ---------------------------------------------------------------- table blackjack rules
def test_table_round_turn_order_split_and_dealer():
    # seat 0: 8 8 (splits), seat 2: 10 9; dealer 6 + 10 -> must hit
    shoe = Shoe.stacked([C("8S"), C("10H"), C("6D"), C("8H"), C("9C"), C("10S"),
                         C("3D"), C("2C"), C("5H"), C("7S")])
    g = TableRound(0, [(0, 100), (2, 50)], shoe=shoe)
    assert g.seat == 0 and "split" in g.actions()
    with pytest.raises(ValueError):
        g.act(2, "stand")
    assert g.cost("split") == 100
    g.act(0, "split")                       # first hand 8 + 3
    assert [c.rank for c in g.hands[0][0].cards] == ["8", "3"]
    g.act(0, "double")                      # 8 3 + 2 = 13 doubled
    assert g.seat == 0 and g.active[0] == 1  # second split hand: 8 + 5
    g.act(0, "stand")
    assert g.seat == 2
    g.act(2, "stand")                       # dealer 16 draws 7 = 23, busts
    assert g.phase == "done" and g.wagered(0) == 300 and g.returned(0) == 600 and g.returned(2) == 100


def test_table_round_dealer_blackjack_ends_it_at_once():
    shoe = Shoe.stacked([C("10S"), C("AH"), C("AS"), C("KC"), C("KD"), C("10H")])
    g = TableRound(0, [(1, 100), (3, 100)], shoe=shoe)
    assert g.phase == "done" and g.dealer_blackjack
    assert g.returned(1) == 0 and g.returned(3) == 100      # 1: 20 loses; 3: blackjack pushes


# ---------------------------------------------------------------- shared tables through the ledger
def play_out(cas, table):
    """Stand every seat in turn, the way timeouts would (each call is one decision)."""
    st = cas.bjt_state(G, table)
    while st["phase"] == "player":
        uid = int(next(s for s in st["seats"] if s["seat"] == st["turn_seat"])["user_id"])
        st = cas.bjt_act(G, uid, table, "stand", f"rid-po-{st['round_id']}-{len(json.dumps(st))}-{random.random()}")["round"]
    return st


def test_table_round_with_two_players_turns_and_settlement(env):
    db, _, cas, clock = env
    for u in (U, U2):
        cas.exchange(G, u, "buy", 10_000, f"rid-bt-buy-{u}")
    r = cas.bjt_bet(G, U, "bj1", 0, 500, "rid-bt-1", name="Ace")
    rid = r["round"]["round_id"]
    assert r["chips"] == 9500 and r["round"]["phase"] == "betting" and r["round"]["closes_at"] == clock.t + BJ_BET_SECS
    assert cas.bjt_bet(G, U, "bj1", 0, 500, "rid-bt-1")["replayed"]
    with pytest.raises(CasinoError) as e:
        cas.bjt_bet(G, U2, "bj1", 0, 500, "rid-bt-2")
    assert e.value.code == "seat_taken"
    cas.bjt_bet(G, U2, "bj1", 3, 1000, "rid-bt-3", name="Bea")
    with pytest.raises(CasinoError) as e:
        cas.bjt_deal(G, "bj1", rid)
    assert e.value.code == "too_early"
    st = cas.bjt_deal(G, "bj1", rid, early=True)
    assert cas.bjt_deal(G, "bj1", rid)["replayed"]                    # dealing twice deals once
    with pytest.raises(CasinoError) as e:
        cas.bjt_bet(G, U3, "bj1", 1, 100, "rid-bt-late")
    assert e.value.code in ("closed", "broke")
    if st["phase"] == "player":
        other = U2 if st["turn_seat"] == 0 else U
        with pytest.raises(CasinoError) as e:
            cas.bjt_act(G, other, "bj1", "stand", "rid-bt-wrong")
        assert e.value.code == "not_your_turn"
        assert st["dealer"][1] == "??"
    st = play_out(cas, "bj1")
    assert st["phase"] == "done" and "??" not in st["dealer"]
    for u in (U, U2):
        assert wallet(db, u)[1] == ledger_sum(db, u)
    got = {s["user_id"]: s["returned"] - s["staked"] for s in st["seats"]}
    assert wallet(db, U)[1] == 10_000 + got[str(U)] and wallet(db, U2)[1] == 10_000 + got[str(U2)]
    # a new hand starts a new round
    assert cas.bjt_bet(G, U, "bj1", 0, 100, "rid-bt-next")["round"]["round_id"] != rid


def test_turn_clock_restart_and_abandoned_tables(env):
    db, _, cas, clock = env
    cas.exchange(G, U, "buy", 10_000, "rid-bc-buy")
    for k in range(30):                                                # find a deal where the player has to decide
        r = cas.bjt_bet(G, U, "bj2", 2, 100, f"rid-bc-{k}")
        st = cas.bjt_deal(G, "bj2", r["round"]["round_id"], early=True)
        if st["phase"] == "player":
            break
    assert st["phase"] == "player" and st["turn_deadline"] == clock.t + TURN_SECS
    with pytest.raises(CasinoError) as e:
        cas.bjt_timeout(G, "bj2", st["round_id"])
    assert e.value.code == "too_early"
    # SYKE restarts: a fresh service sees the same hand
    cas2 = CasinoService(db, cas.guard, clock=clock)
    again = cas2.bjt_state(G, "bj2")
    assert again["round_id"] == st["round_id"] and again["seats"][0]["hands"] == st["seats"][0]["hands"]
    clock.t += TURN_SECS
    done = cas2.bjt_timeout(G, "bj2", st["round_id"])
    assert done["timed_out"] and done["phase"] == "done"
    assert wallet(db)[1] == ledger_sum(db, U)
    # bet, then nobody deals (game server gone): the sweep deals and stands it
    r = cas2.bjt_bet(G, U, "vip", 0, 1000, "rid-bc-vip", vip_ok=True)
    clock.t += BJ_BET_SECS + BJ_STALE + 1
    cas2.casino_sweep(G)
    assert cas2.bjt_state(G, "vip")["phase"] == "done" and wallet(db)[1] == ledger_sum(db, U)


def test_unbet_and_double_cost(env):
    db, _, cas, clock = env
    cas.exchange(G, U, "buy", 1_000, "rid-ub-buy")
    cas.bjt_bet(G, U, "bj1", 1, 600, "rid-ub-1")
    r = cas.bjt_unbet(G, U, "bj1", "rid-ub-2")
    assert r["chips"] == 1000 and r["round"]["round_id"] is None
    cas.exchange(G, U, "buy", 40_000, "rid-ub-buy2")
    for k in range(60):
        r = cas.bjt_bet(G, U, "bj1", 1, 600, f"rid-ub-b{k}")
        st = cas.bjt_deal(G, "bj1", r["round"]["round_id"], early=True)
        if st["phase"] == "player" and "double" in st["actions"]:
            break
        if st["phase"] == "player":
            play_out(cas, "bj1")
    assert "double" in st["actions"]
    cas.exchange(G, U, "sell", wallet(db)[1] - 400, "rid-ub-sell")   # 400 chips left can't double 600
    with pytest.raises(CasinoError) as e:
        cas.bjt_act(G, U, "bj1", "double", "rid-ub-dbl")
    assert e.value.code == "broke"
    cas.exchange(G, U, "buy", 200, "rid-ub-more")
    st = cas.bjt_act(G, U, "bj1", "double", "rid-ub-dbl2")["round"]
    assert st["phase"] == "done" and st["seats"][0]["staked"] == 1200 and wallet(db)[1] == st["seats"][0]["returned"]
    assert wallet(db)[1] == ledger_sum(db, U)
