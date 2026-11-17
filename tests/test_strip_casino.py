"""THE STRIP casino: chips, slots, roulette and blackjack settled by SYKE, exactly once, auditable.

These tests only need SYKE's storage and the economy/stripworld rules. If discord.py or aiohttp
isn't installed (e.g. a slim test box), a permissive stand-in is used just for importing.
Run on their own with:  pytest --noconftest tests/test_strip_casino.py
"""

import asyncio
import importlib.util
import json
import sys
import types

import pytest


def _install_stub(name: str) -> None:
    """A do-anything module: attributes, calls, decorators and base classes all just work."""
    class _Base:
        def __init_subclass__(cls, **kw):
            super().__init_subclass__()

        def __init__(self, *a, **k):
            pass

    class Anything:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, item):
            return Anything()

        def __call__(self, *a, **k):
            return Anything()

        def __mro_entries__(self, bases):
            return (_Base,)

        def __getitem__(self, item):
            return Anything()

        def __or__(self, other):
            return Anything()

        __ror__ = __or__

    class Mod(types.ModuleType):
        def __getattr__(self, item):
            if item.startswith("__"):
                raise AttributeError(item)
            return Anything()

    mod = Mod(name)
    mod.__path__ = []
    sys.modules[name] = mod


_MISSING = {root for root in ("discord", "aiohttp") if root not in sys.modules and importlib.util.find_spec(root) is None}
for _name in ("discord", "discord.ext", "discord.ext.commands", "discord.app_commands", "discord.ui",
              "discord.utils", "aiohttp", "aiohttp.web"):
    if _name.split(".")[0] in _MISSING:
        _install_stub(_name)
if "aiohttp" in _MISSING:
    sys.modules["aiohttp"].web = sys.modules["aiohttp.web"]
    sys.modules["aiohttp.web"].json_response = lambda data, status=200: types.SimpleNamespace(data=data, status=status)

from syke import economy as eco  # noqa: E402
from syke import stripworld as sw  # noqa: E402
from syke.storage import Storage  # noqa: E402
from syke.strip_api import StripAPI, StripService  # noqa: E402
from syke.strip_casino import BJ_IDLE, CasinoError, CasinoService, vip_tier  # noqa: E402


import random
import threading

G, U, U2 = 111111, 222222, 333333


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def env(tmp_path):
    db = Storage(str(tmp_path / "c.db"))
    db.update_config(G, eco.CONFIG_KEY, open=True)
    clock = Clock()
    svc = StripService(db, clock=clock)
    cas = CasinoService(db, svc.guard, rng=random.Random(11), clock=clock)
    svc.state(G, U); svc.state(G, U2)
    db.econ_adjust(G, U, cash=100_000); db.econ_adjust(G, U2, cash=100_000)
    return db, svc, cas, clock


def wallet(db, uid=U):
    w = db.econ_wallet(G, uid)
    return w["cash"], w["chips"]


def ledger_sum(db, uid=U):
    return db._read("SELECT COALESCE(SUM(chips_delta),0), COALESCE(SUM(cash_delta),0) FROM strip_casino_ledger "
                    "WHERE guild_id=? AND user_id=?", (G, uid))[0]


# ---------------------------------------------------------------- the cage
def test_exchange_is_one_to_one_and_conserves_value(env):
    db, _, cas, _ = env
    cash0, chips0 = wallet(db)
    r = cas.exchange(G, U, "buy", 5_000, "rid-buy-1")
    assert r["received"] == 5_000 and r["fee"] == 0
    assert wallet(db) == (cash0 - 5_000, chips0 + 5_000)
    cas.exchange(G, U, "sell", 2_000, "rid-sell-1")
    cash, chips = wallet(db)
    assert cash + chips == cash0 + chips0 and chips == chips0 + 3_000


def test_exchange_replays_instead_of_doubling(env):
    db, _, cas, _ = env
    a = cas.exchange(G, U, "buy", 1_000, "rid-same-123")
    b = cas.exchange(G, U, "buy", 1_000, "rid-same-123")      # the same click arriving twice (retry, double tap)
    assert b["replayed"] and b["chips"] == a["chips"]
    assert wallet(db)[1] == 1_000                              # bought once
    with pytest.raises(CasinoError) as e:
        cas.slots(G, U, 10, "rid-same-123")                    # a rid can't be reused for a different action
    assert e.value.code == "rid_reused"


def test_exchange_rejects_bad_requests_and_insufficient_balances(env):
    db, _, cas, _ = env
    for amount in (0, -5, 10.5, "lots", 10**12):
        with pytest.raises(CasinoError):
            cas.exchange(G, U, "buy", amount, f"rid-bad-{amount}"[:20].replace(".", "-") + "xx")
    with pytest.raises(CasinoError) as e:
        cas.exchange(G, U, "sell", 1, "rid-nochips")
    assert e.value.code == "broke"
    with pytest.raises(CasinoError) as e:
        cas.exchange(G, U, "buy", 10**7, "rid-toomuch")
    assert e.value.code == "broke"
    with pytest.raises(CasinoError) as e:
        cas.exchange(G, U, "buy", 10, "x")                     # no / bad request id
    assert e.value.code == "bad_rid"
    assert wallet(db) == (100_000 + eco.STARTING_CASH, 0)     # nothing moved


# ---------------------------------------------------------------- slots
def test_slots_pay_by_the_bots_table_and_record_everything(env):
    db, _, cas, _ = env
    cas.exchange(G, U, "buy", 10_000, "rid-buy-slots")
    total_net = 0
    for i in range(60):
        r = cas.slots(G, U, 100, f"rid-spin-{i:03d}", machine="s3", name="Ana")
        assert r["payout"] == 100 * r["mult"] and r["net"] == r["payout"] - 100
        assert r["mult"] == eco.slot_multiplier([{v: k for k, v in __import__("syke.strip_casino", fromlist=["SLOT_KEYS"]).SLOT_KEYS.items()}[x] for x in r["reels"]])
        total_net += r["net"]
    assert wallet(db)[1] == 10_000 + total_net
    chips_delta, _ = ledger_sum(db)
    assert chips_delta == 10_000 + total_net                   # the ledger reconciles with the wallet
    s = cas.stats(G, U)
    assert s["stats"]["wagered"] == 6_000 and s["stats"]["favourite"] == "slots"
    assert s["stats"]["wins"] + s["stats"]["losses"] <= 60


def test_slots_need_chips(env):
    _, _, cas, _ = env
    with pytest.raises(CasinoError) as e:
        cas.slots(G, U, 100, "rid-broke-spin")
    assert e.value.code == "broke"


# ---------------------------------------------------------------- roulette
def test_roulette_round_settles_once_and_rejects_short_bets(env):
    db, _, cas, _ = env
    cas.exchange(G, U, "buy", 1_000, "rid-r-buy-1")
    cas.exchange(G, U2, "buy", 50, "rid-r-buy-2")
    bets = [{"user_id": U, "space": "red", "amount": 500, "rid": "rid-bet-u-1", "name": "Ana"},
            {"user_id": U, "space": "17", "amount": 100, "rid": "rid-bet-u-2"},
            {"user_id": U2, "space": "black", "amount": 200, "rid": "rid-bet-u2-1"}]    # U2 only has 50
    r = cas.roulette_round(G, "round-0001", bets)
    n = r["number"]
    res = {x["rid"]: x for x in r["results"]}
    assert res["rid-bet-u2-1"]["accepted"] is False and res["rid-bet-u2-1"]["reason"] == "broke"
    exp_u = 1_000 - 600 + 500 * eco.roulette_multiplier("red", n) + 100 * eco.roulette_multiplier("17", n)
    assert wallet(db)[1] == exp_u and wallet(db, U2)[1] == 50
    again = cas.roulette_round(G, "round-0001", bets)        # the game server retries after a timeout
    assert again["replayed"] and again["number"] == n and wallet(db)[1] == exp_u
    with pytest.raises(CasinoError):
        cas.roulette_round(G, "round-0002", [{"user_id": U, "space": "purple", "amount": 10, "rid": "rid-bet-bad"}])


# ---------------------------------------------------------------- blackjack
def finish(cas, hand, uid=U):
    i = 0
    while hand["phase"] != "done":
        a = "no_insurance" if hand["phase"] == "insurance" else "stand"
        hand = cas.bj_act(G, uid, hand["hand_id"], a, f"rid-act-{hand['hand_id']}-{i}")
        i += 1
    return hand


def test_blackjack_hand_stakes_settles_and_audits(env):
    db, _, cas, _ = env
    cas.exchange(G, U, "buy", 5_000, "rid-bj-buy")
    for k in range(25):
        h = cas.bj_start(G, U, "bj1", 200, f"rid-bj-start-{k}")
        if h["phase"] == "player" and "double" in h["actions"] and k % 3 == 0:
            h = cas.bj_act(G, U, h["hand_id"], "double", f"rid-bj-dbl-{k}")
        h = finish(cas, h)
        paid = sum(x["payout"] for x in h["hands"])
        assert h["returned"] == paid + (h["insurance"] * 3 if h["insurance"] and h["returned"] > paid else 0)
        assert h["net"] == h["returned"] - h["wagered"]
        assert "??" not in h["dealer"]                          # the hole card shows once the hand is over
    chips = wallet(db)[1]
    assert ledger_sum(db)[0] == chips                          # every chip in and out is in the ledger
    assert db._read("SELECT COUNT(*) FROM strip_casino_hands WHERE settled=0", ())[0][0] == 0


def test_blackjack_hand_survives_a_restart(env, tmp_path):
    db, svc, cas, clock = env
    cas.exchange(G, U, "buy", 1_000, "rid-rs-buy")
    h = None
    for k in range(20):                                        # find a hand that's still live
        h = cas.bj_start(G, U, "bj1", 100, f"rid-rs-start-{k}")
        if h["phase"] != "done":
            break
    assert h["phase"] != "done"
    cas2 = CasinoService(Storage(db._path), svc.guard, clock=clock)   # SYKE (or the game server) restarted
    state = cas2.bj_state(G, U)["hand"]
    assert state["hand_id"] == h["hand_id"] and state["hands"][0]["cards"] == h["hands"][0]["cards"]
    done = finish(cas2, state)
    assert done["phase"] == "done"


def test_one_hand_at_a_time_and_idle_hands_auto_stand(env):
    db, _, cas, clock = env
    cas.exchange(G, U, "buy", 1_000, "rid-idle-buy")
    h = None
    for k in range(20):
        h = cas.bj_start(G, U, "bj2", 100, f"rid-idle-start-{k}")
        if h["phase"] != "done":
            break
    with pytest.raises(CasinoError) as e:
        cas.bj_start(G, U, "bj2", 100, "rid-idle-second")
    assert e.value.code == "in_hand"
    clock.t += BJ_IDLE + 1                                     # the player disconnected and never came back
    assert cas.bj_sweep(G)["finished"] == 1
    assert cas.bj_state(G, U)["hand"] is None
    assert ledger_sum(db)[0] == wallet(db)[1]


def test_vip_table_needs_vip_and_has_its_own_limits(env):
    _, _, cas, _ = env
    cas.exchange(G, U, "buy", 5_000, "rid-vip-buy")
    with pytest.raises(CasinoError) as e:
        cas.bj_start(G, U, "vip", 1_000, "rid-vip-1")
    assert e.value.code == "vip_only"
    with pytest.raises(CasinoError) as e:
        cas.bj_start(G, U, "vip", 500, "rid-vip-2", vip_ok=True)
    assert e.value.code == "limit"


def test_actions_are_validated_and_cost_is_taken(env):
    db, _, cas, _ = env
    cas.exchange(G, U, "buy", 100_000, "rid-cost-buy")
    for k in range(40):
        h = cas.bj_start(G, U, "bj1", 200, f"rid-cost-start-{k}")
        if h["phase"] == "player" and "double" in h["actions"]:
            break
        finish(cas, h)
    with pytest.raises(CasinoError) as e:
        cas.bj_act(G, U, h["hand_id"], "steal", "rid-cost-steal")
    assert e.value.code == "bad_action"
    assert h["phase"] == "player" and "double" in h["actions"]
    cas.exchange(G, U, "sell", wallet(db)[1] - 100, "rid-cost-sell")   # leave 100 chips: not enough to double 200
    with pytest.raises(CasinoError) as e:
        cas.bj_act(G, U, h["hand_id"], "double", "rid-cost-dbl")
    assert e.value.code == "broke"
    cas.exchange(G, U, "buy", 100, "rid-cost-buy2")                     # now exactly 200: the double goes through
    h = cas.bj_act(G, U, h["hand_id"], "double", "rid-cost-dbl2")
    assert h["phase"] == "done" and h["wagered"] == 400


# ---------------------------------------------------------------- concurrency
def test_concurrent_duplicates_apply_once_and_balances_never_go_negative(env):
    db, _, cas, _ = env
    cas.exchange(G, U, "buy", 1_000, "rid-conc-buy")
    errors = []

    def same():
        try:
            cas.slots(G, U, 100, "rid-conc-same")
        except Exception as e:                                 # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=same) for _ in range(12)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert not errors
    assert db._read("SELECT COUNT(*) FROM strip_casino_ledger WHERE rid='rid-conc-same'", ())[0][0] == 1

    def spend(i):
        try:
            cas.exchange(G, U, "sell", 300, f"rid-conc-sell-{i}")
        except CasinoError:
            pass

    threads = [threading.Thread(target=spend, args=(i,)) for i in range(10)]
    [t.start() for t in threads]; [t.join() for t in threads]
    cash, chips = wallet(db)
    assert chips >= 0 and ledger_sum(db)[0] == chips


# ---------------------------------------------------------------- progression
def test_visits_are_idempotent_and_vip_tiers_are_status_only(env):
    _, _, cas, _ = env
    cas.visit(G, U, "rid-visit-1"); cas.visit(G, U, "rid-visit-1"); cas.visit(G, U, "rid-visit-2")
    assert cas.stats(G, U)["stats"]["visits"] == 2
    assert vip_tier(0)["tier"] == "Bronze" and vip_tier(25_000)["tier"] == "Silver" and vip_tier(10**9)["next"] is None


def test_recent_winners_and_history(env):
    _, _, cas, _ = env
    cas.exchange(G, U, "buy", 50_000, "rid-hist-buy")
    for i in range(80):
        cas.slots(G, U, 500, f"rid-hist-{i:03d}", name="Ana")
    s = cas.stats(G, U)
    assert s["exchanges"][0]["kind"] == "buy_chips" and len(s["history"]) == 30
    for w in cas.recent(G)["winners"]:
        assert w["net"] >= 500 and w["name"] == "Ana"


# ---------------------------------------------------------------- the HTTP front door
class FakeRequest:
    def __init__(self, op, body, token="s3cret"):
        self.match_info = {"op": op}
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._body = body

    async def json(self):
        return self._body


def _call(api, op, body, token="s3cret"):
    resp = asyncio.run(api.handle(FakeRequest(op, body, token)))
    data = resp.data if hasattr(resp, "data") else json.loads(resp.text)
    return resp.status, data


def test_api_casino_ops_need_the_secret_and_return_state(env):
    db, _, _, _ = env
    api = StripAPI(db, "s3cret")
    status, data = _call(api, "casino_exchange", {"guild_id": G, "user_id": U, "action": "buy", "amount": 100, "rid": "rid-api-1"}, token="no")
    assert status == 401
    status, data = _call(api, "casino_exchange", {"guild_id": G, "user_id": U, "action": "buy", "amount": 100, "rid": "rid-api-1"})
    assert data["ok"] and data["state"]["wallet"]["chips"] == 100
    status, data = _call(api, "casino_exchange", {"guild_id": G, "user_id": U, "action": "buy", "amount": 100, "rid": "rid-api-1"})
    assert data["ok"] and data["replayed"] and data["state"]["wallet"]["chips"] == 100
    status, data = _call(api, "casino_recent", {"guild_id": G})
    assert data["ok"] and "winners" in data
    status, data = _call(api, "casino_slots", {"guild_id": G, "user_id": U, "bet": 5, "rid": "rid-api-2"})
    assert data["ok"] is False and data["error"] == "limit"
