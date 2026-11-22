"""THE STRIP crime rules in SYKE: odds, cooldowns, caps, robbery, arrest, bail, jail work, illicit sales.
Run: pytest --noconftest tests/test_strip_crime.py"""

import random

import pytest

from test_strip_casino import G, U, Clock, env  # noqa: F401  (fixture + stubs)
from syke import stripworld as sw
from syke.strip_crime import ARREST_MAX, ILLICIT_PER_HOUR, JOB_PER_HOUR, JOBS, CrimeError, CrimeService

V = U + 1


class Rig(random.Random):
    """A random source that always rolls `roll` and picks the top of every range."""
    def __init__(self, roll): super().__init__(); self.roll = roll
    def random(self): return self.roll
    def randint(self, a, b): return b
    def uniform(self, a, b): return b


@pytest.fixture()
def crime(env):
    db, svc, _, clock = env
    make = lambda roll: CrimeService(svc, rng=Rig(roll), clock=clock)
    return db, make, clock


def test_job_pays_on_success_and_fines_on_failure(crime):
    db, make, clock = crime
    start = db.econ_wallet(G, U)["cash"]
    r = make(0.0).job(G, U, "pickpocket", 1.0)
    assert r["success"] and r["payout"] == JOBS["pickpocket"][2] and db.econ_wallet(G, U)["cash"] == start + r["payout"]
    clock.t += 1000
    f = make(0.99).job(G, U, "pickpocket", 1.0)
    assert not f["success"] and f["fine"] > 0 and db.econ_wallet(G, U)["cash"] == start + r["payout"] - f["fine"]
    assert db.econ_wallet(G, U)["jail_until"] == 0   # a fine, not jail: jail only follows an arrest


def test_skill_moves_the_odds(crime):
    db, make, clock = crime
    base = JOBS["holdup"][0]
    assert not make(base + 0.05).job(G, U, "holdup", 0.0)["success"]
    clock.t += 5000
    assert make(base + 0.05).job(G, U, "holdup", 1.0)["success"]


def test_cooldown_and_bad_kind(crime):
    db, make, clock = crime
    c = make(0.0)
    c.job(G, U, "carbreak")
    with pytest.raises(CrimeError) as e: c.job(G, U, "carbreak")
    assert e.value.code == "cooldown"
    clock.t += JOBS["carbreak"][3] + 1
    assert c.job(G, U, "carbreak")["success"]
    with pytest.raises(CrimeError): c.job(G, U, "jaywalking")


def test_hourly_cap_on_street_crime(crime):
    db, make, clock = crime
    c = make(0.0); total = 0
    for _ in range(12):
        total += c.job(G, U, "holdup", 1.0)["payout"]; clock.t += JOBS["holdup"][3] + 1
        if clock.t > 0 and total >= JOB_PER_HOUR: break
    assert total <= JOB_PER_HOUR + JOBS["holdup"][2]


def test_rob_follows_the_discord_rules(crime):
    db, make, clock = crime
    db.econ_touch(G, V, 0); db.econ_adjust(G, V, cash=5000); db.econ_adjust(G, U, cash=1000)
    v0, u0 = db.econ_wallet(G, V)["cash"], db.econ_wallet(G, U)["cash"]
    r = make(0.0).rob(G, U, V, 1.0)
    assert r["success"] and db.econ_wallet(G, V)["cash"] == v0 - r["payout"] and db.econ_wallet(G, U)["cash"] == u0 + r["payout"]
    with pytest.raises(CrimeError) as e: make(0.0).rob(G, U, V)
    assert e.value.code == "cooldown"
    clock.t += sw.ROB_COOLDOWN + 1
    db.econ_item_add(G, V, "padlock", 1)
    p = make(0.0).rob(G, U, V, 1.0)
    assert p["padlock"] and not p["success"] and not db.econ_items(G, V).get("padlock")
    clock.t += sw.ROB_COOLDOWN + 1
    db.econ_adjust(G, V, cash=-db.econ_wallet(G, V)["cash"])
    with pytest.raises(CrimeError) as e: make(0.0).rob(G, U, V)
    assert e.value.code == "poor_target"
    with pytest.raises(CrimeError): make(0.0).rob(G, U, U)


def test_arrest_bail_and_jail_work(crime):
    db, make, clock = crime
    c = make(0.5)
    a = c.arrest(G, U, 999)
    assert a["jailed"] and a["minutes"] == ARREST_MAX and db.econ_wallet(G, U)["jail_until"] > clock.t
    with pytest.raises(CrimeError) as e: c.job(G, U, "pickpocket")
    assert e.value.code == "jailed"
    s = c.serve(G, U, 99)   # one task takes at most five minutes off
    assert round((a["until"] - s["until"]) / 60) == 5
    db.econ_adjust(G, U, cash=50_000)
    q = c.bail_quote(G, U); before = db.econ_wallet(G, U)["cash"]
    b = c.bail(G, U)
    assert b["cost"] == q["cost"] and db.econ_wallet(G, U)["cash"] == before - b["cost"] and db.econ_wallet(G, U)["jail_until"] == 0
    with pytest.raises(CrimeError): c.bail(G, U)
    # a friend pays
    db.econ_touch(G, V, 0); c.arrest(G, V, 10); before = db.econ_wallet(G, U)["cash"]
    f = c.bail(G, U, V)
    assert f["freed"] == V and db.econ_wallet(G, V)["jail_until"] == 0 and db.econ_wallet(G, U)["cash"] == before - f["cost"]


def test_illicit_sales_are_capped(crime):
    db, make, clock = crime
    c = make(0.5); start = db.econ_wallet(G, U)["cash"]; paid = 0
    with pytest.raises(CrimeError): c.illicit(G, U, 999_999)
    with pytest.raises(CrimeError): c.illicit(G, U, 0)
    try:
        for _ in range(10): paid += c.illicit(G, U, 2000, "fence")["amount"]
    except CrimeError as e:
        assert e.code == "limit"
    assert paid == ILLICIT_PER_HOUR and db.econ_wallet(G, U)["cash"] == start + paid
    clock.t += 3601
    assert c.illicit(G, U, 500)["amount"] == 500


def test_crew_roll_and_bounty(crime):
    db, make, clock = crime
    c = make(0.99)                      # its own dice would fail
    assert c.job(G, U, "vault", 1.0, roll=0.0)["success"]      # the crew's shared throw decides
    clock.t += JOBS["vault"][3] + 1
    assert not c.job(G, U, "vault", 1.0, roll=0.99)["success"]
    start = db.econ_wallet(G, U)["cash"]
    assert c.bounty(G, U, 360)["amount"] == 360 and db.econ_wallet(G, U)["cash"] == start + 360
    with pytest.raises(CrimeError): c.bounty(G, U, 99_999)
    total = 360
    for _ in range(10): total += c.bounty(G, U, 600)["amount"]
    assert total == 2_400
