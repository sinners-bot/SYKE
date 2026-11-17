"""THE STRIP game API: the economy actions the walkable game uses, checked against SYKE's own rules.

These tests only need SYKE's storage and the economy/stripworld rules. If discord.py or aiohttp
isn't installed (e.g. a slim test box), a permissive stand-in is used just for importing.
Run on their own with:  pytest --noconftest tests/test_strip_api.py
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
from syke.strip_api import GOAL_REWARD, GRANT_PER_HOUR, StripAPI, StripError, StripService  # noqa: E402

G, U, U2 = 111111, 222222, 333333


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def env(tmp_path):
    db = Storage(str(tmp_path / "s.db"))
    db.update_config(G, eco.CONFIG_KEY, open=True)
    clock = Clock()
    import random
    svc = StripService(db, rng=random.Random(7), clock=clock)
    return db, svc, clock


def test_closed_server_says_so(tmp_path):
    db = Storage(str(tmp_path / "c.db"))
    svc = StripService(db)
    assert svc.state(G, U) == {"open": False}
    with pytest.raises(StripError) as e:
        svc.work(G, U)
    assert e.value.code == "closed"


def test_first_touch_grants_starting_cash_once(env):
    db, svc, _ = env
    s = svc.state(G, U)
    assert s["open"] and s["wallet"]["cash"] == eco.STARTING_CASH
    assert svc.state(G, U)["wallet"]["cash"] == eco.STARTING_CASH
    assert s["tier"] == "BRONZE"


def test_work_pays_in_range_and_shares_the_cooldown(env):
    db, svc, clock = env
    r = svc.work(G, U, bonus=1.0)
    assert eco.WORK_LOW <= r["amount"] <= eco.WORK_HIGH
    assert db.econ_wallet(G, U)["work_at"] == clock.t        # the same field `!work` uses
    with pytest.raises(StripError) as e:
        svc.work(G, U)
    assert e.value.code == "cooldown" and e.value.extra["until"] == clock.t + eco.WORK_COOLDOWN
    clock.t += eco.WORK_COOLDOWN + 1
    assert svc.work(G, U)["amount"] > 0


def test_work_bonus_is_clamped(env):
    db, svc, clock = env
    r = svc.work(G, U, bonus=50)
    assert r["bonus"] == 1.2
    assert r["amount"] <= int(eco.WORK_HIGH * 1.2)


def test_work_respects_buffs_and_jail(env):
    db, svc, clock = env
    db.econ_buff(G, U, "fed", clock.t + 3600)
    assert "fed" in svc.work(G, U)["perks"]
    clock.t += eco.WORK_COOLDOWN + 1
    db.econ_adjust(G, U, jail_until=clock.t + 600)
    with pytest.raises(StripError) as e:
        svc.work(G, U)
    assert e.value.code == "jailed"


def test_daily_streak_and_cooldown(env):
    db, svc, clock = env
    assert svc.daily(G, U)["amount"] == eco.daily_reward(1)
    with pytest.raises(StripError):
        svc.daily(G, U)
    clock.t += eco.DAILY_COOLDOWN + 10
    r = svc.daily(G, U)
    assert r["streak"] == 2 and r["amount"] == eco.daily_reward(2)


def test_buy_use_and_pawn_follow_stripworld(env):
    db, svc, clock = env
    svc.state(G, U)
    svc.buy(G, U, "lomein")
    assert db.econ_items(G, U)["lomein"] == 1
    assert db.econ_wallet(G, U)["cash"] == eco.STARTING_CASH - sw.ITEM_BY_KEY["lomein"].price
    out = svc.use(G, U, "lomein")
    assert out["buff"] == "fed" and "fed" in db.econ_buffs(G, U, clock.t)
    db.econ_adjust(G, U, cash=10_000)
    svc.buy(G, U, "cookie")
    assert svc.use(G, U, "cookie")["fortune"] in sw.FORTUNES
    svc.buy(G, U, "scratch", 3)
    before = db.econ_wallet(G, U)["cash"]
    svc.pawn(G, U, "scratch", 2)
    assert db.econ_wallet(G, U)["cash"] == before + 2 * sw.pawn_value("scratch")
    assert db.econ_items(G, U)["scratch"] == 1


def test_buy_needs_cash_and_known_items(env):
    db, svc, _ = env
    svc.state(G, U)
    with pytest.raises(StripError) as e:
        svc.buy(G, U, "paintjob")                      # 20,000 on 500 starting cash
    assert e.value.code == "broke"
    with pytest.raises(StripError) as e:
        svc.buy(G, U, "unicorn")
    assert e.value.code == "unknown_item"


def test_energy_resets_work_cooldown(env):
    db, svc, clock = env
    svc.work(G, U)
    svc.buy(G, U, "energy")
    svc.use(G, U, "energy")
    assert svc.work(G, U)["amount"] > 0


def test_owner_gets_their_cut(env):
    db, svc, clock = env
    db.econ_touch(G, U2, 100_000)
    assert db.econ_shop_buy(G, "dragon", U2, sw.STORE_BY_KEY["dragon"].price, clock.t)
    svc.state(G, U)
    svc.buy(G, U, "lomein")
    assert db.econ_shop(G, "dragon")["register"] == int(sw.ITEM_BY_KEY["lomein"].price * sw.OWNER_CUT)


def test_paintjob_stays_in_chat(env):
    db, svc, _ = env
    db.econ_touch(G, U, 0)
    db.econ_item_add(G, U, "paintjob", 1)
    with pytest.raises(StripError) as e:
        svc.use(G, U, "paintjob")
    assert e.value.code == "chat_only"
    assert db.econ_items(G, U)["paintjob"] == 1      # not consumed


def test_spend_and_grant_limits(env):
    db, svc, clock = env
    svc.state(G, U)
    svc.spend(G, U, 300, "lamp")
    assert db.econ_wallet(G, U)["cash"] == eco.STARTING_CASH - 300
    with pytest.raises(StripError):
        svc.spend(G, U, 10**9)
    total = 0
    with pytest.raises(StripError) as e:
        for _ in range(10):
            total += svc.grant(G, U, 80)["amount"]
    assert e.value.code == "limit" and total <= GRANT_PER_HOUR
    clock.t += 3601
    assert svc.grant(G, U, 80)["amount"] == 80


def test_goal_reward_only_once(env):
    db, svc, _ = env
    assert svc.goal_reward(G, U)["amount"] == GOAL_REWARD
    with pytest.raises(StripError):
        svc.goal_reward(G, U)
    assert svc.state(G, U)["rewarded"] is True


def test_banned_players_are_refused(env):
    db, svc, _ = env
    db.update_config(0, eco.HOUSE_KEY, banned=[U])
    with pytest.raises(StripError) as e:
        svc.work(G, U)
    assert e.value.code == "banned"


# ── the HTTP layer ──────────────────────────────────────────────────────────
class FakeRequest:
    def __init__(self, op, body, token):
        self.match_info = {"op": op}
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._body = body

    async def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


def _call(api, op, body, token="s3cret"):
    resp = asyncio.run(api.handle(FakeRequest(op, body, token)))
    data = resp.data if hasattr(resp, "data") else json.loads(resp.text)
    return resp.status, data


def test_api_requires_the_secret(env):
    db, _, _ = env
    api = StripAPI(db, "s3cret")
    status, data = _call(api, "state", {"guild_id": G, "user_id": U}, token="nope")
    assert status == 401 and data["error"] == "unauthorized"
    status, data = _call(api, "state", {"guild_id": G, "user_id": U}, token=None)
    assert status == 401


def test_api_round_trip(env):
    db, _, _ = env
    api = StripAPI(db, "s3cret")
    status, data = _call(api, "work", {"guild_id": G, "user_id": U, "bonus": 1.1})
    assert status == 200 and data["ok"] and data["state"]["wallet"]["cash"] == eco.STARTING_CASH + data["amount"]
    status, data = _call(api, "work", {"guild_id": G, "user_id": U})
    assert data["ok"] is False and data["error"] == "cooldown" and data["state"]["open"]
    status, data = _call(api, "buy", {"guild_id": "nope", "user_id": U, "item": "lomein"})
    assert status == 400
    status, data = _call(api, "launder", {"guild_id": G, "user_id": U})
    assert status == 404
