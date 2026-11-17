"""THE STRIP economy: pure logic, a provable house edge, storage, and the cog flow."""

import random
import types
from dataclasses import replace

import pytest

from syke import economy as econ
from syke.bot import Syke
from syke.config import load_settings
from test_commands import run


# ── pure logic ──────────────────────────────────────────────────────────────
def test_daily_reward_scales_with_streak_then_caps():
    assert econ.daily_reward(1) == econ.DAILY_BASE
    assert econ.daily_reward(2) == econ.DAILY_BASE + econ.DAILY_STREAK_STEP
    capped = econ.DAILY_BASE + econ.DAILY_STREAK_CAP * econ.DAILY_STREAK_STEP
    assert econ.daily_reward(999) == capped          # bonus stops growing


def test_next_streak_continues_within_window_else_resets():
    now = 1_000_000.0
    assert econ.next_streak(now - 3600, now, 4) == 5                 # claimed recently
    assert econ.next_streak(now - econ.STREAK_WINDOW - 1, now, 4) == 1  # missed the window
    assert econ.next_streak(0, now, 0) == 1                          # first ever claim


def test_convert_fee_rounds_in_the_houses_favor():
    got, fee = econ.convert(1000, 0.05)
    assert (got, fee) == (950, 50)
    got, fee = econ.convert(1, 0.05)
    assert fee == 1 and got == 0                       # tiny amounts still owe the cage


def test_parse_amount_variants():
    assert econ.parse_amount("500", 1000) == 500
    assert econ.parse_amount("all", 1000) == 1000
    assert econ.parse_amount("half", 1000) == 500
    assert econ.parse_amount("1k", 0) == 1000
    assert econ.parse_amount("2.5k", 0) == 2500
    assert econ.parse_amount("1,000", 0) == 1000
    for bad in ("abc", "-5", "0", ""):
        assert econ.parse_amount(bad, 1000) is None


def test_slot_multiplier_rules():
    assert econ.slot_multiplier(["🎰", "🎰", "🎰"]) == econ.SLOT_TRIPLE["🎰"]
    assert econ.slot_multiplier(["🍒", "🍒", "🔔"]) == econ.SLOT_TWO_CHERRY  # exactly two cherries
    assert econ.slot_multiplier(["🍒", "🍒", "🍒"]) == econ.SLOT_TRIPLE["🍒"]  # three beats two
    assert econ.slot_multiplier(["🍒", "🔔", "💎"]) == 0


def test_roulette_multiplier_and_zero_edge():
    assert econ.roulette_multiplier("7", 7) == 36
    assert econ.roulette_multiplier("7", 8) == 0
    assert econ.roulette_multiplier("red", 1) == 2 and econ.roulette_multiplier("black", 1) == 0
    assert econ.roulette_multiplier("even", 4) == 2
    # Zero is the house edge: every outside bet loses on 0.
    for space in ("red", "black", "even", "odd", "low", "high"):
        assert econ.roulette_multiplier(space, 0) == 0


@pytest.mark.parametrize("game", ["slots", "flip", "roulette"])
def test_casino_keeps_a_house_edge(game):
    """Over many bets the house must come out ahead (total returned < total wagered)."""
    rng = random.Random(1234)
    bet, spins, returned = 100, 40_000, 0
    for _ in range(spins):
        if game == "slots":
            returned += bet * econ.slot_multiplier(econ.slot_spin(rng))
        elif game == "flip":
            returned += int(bet * econ.COINFLIP_MULT) if rng.random() < 0.5 else 0
        else:  # roulette, betting red every time
            returned += bet * econ.roulette_multiplier("red", econ.roulette_spin(rng))
    wagered = bet * spins
    assert returned < wagered, f"{game} paid out {returned/wagered:.3f} of wagers (no edge!)"


# ── storage ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def store(tmp_path):
    from syke.storage import Storage
    return Storage(str(tmp_path / "e.db"))


def test_touch_grants_starting_cash_once(store):
    w = store.econ_touch(42, 1, 500)
    assert w["cash"] == 500 and w["seen"] == 1
    w = store.econ_touch(42, 1, 500)                   # second touch grants nothing more
    assert w["cash"] == 500


def test_adjust_clamps_at_zero_and_sets_times(store):
    store.econ_touch(42, 1, 100)
    w = store.econ_adjust(42, 1, cash=-999)            # can't go negative
    assert w["cash"] == 0
    w = store.econ_adjust(42, 1, chips=50, work_at=123.0, streak=3)
    assert w["chips"] == 50 and w["work_at"] == 123.0 and w["streak"] == 3


def test_adjust_rejects_unknown_fields(store):
    with pytest.raises(ValueError):
        store.econ_adjust(42, 1, cash=1, nonsense=9)


def test_transfer_respects_balance_and_tax(store):
    store.econ_adjust(42, 1, cash=1000)
    assert store.econ_transfer(42, 1, 2, 500, tax=15) is True
    assert store.econ_wallet(42, 1)["cash"] == 500     # sender pays full amount
    assert store.econ_wallet(42, 2)["cash"] == 485     # recipient gets amount - tax
    assert store.econ_transfer(42, 1, 2, 999_999) is False   # can't overdraw
    assert store.econ_wallet(42, 1)["cash"] == 500


def test_top_and_circulation(store):
    store.econ_adjust(42, 1, cash=100, chips=50)        # net 150
    store.econ_adjust(42, 2, cash=400)                  # net 400
    store.econ_adjust(42, 3, bank=0)                    # net 0 -> excluded from top
    top = store.econ_top(42, 10)
    assert [r["user_id"] for r in top] == [2, 1]
    circ = store.econ_circulation(42)
    assert circ["players"] == 3 and circ["cash"] == 500 and circ["chips"] == 50


def test_reset(store):
    store.econ_adjust(42, 1, cash=100)
    assert store.econ_reset(42, 1) is True
    assert store.econ_wallet(42, 1)["cash"] == 0


# ── the cog ──────────────────────────────────────────────────────────────────
class FakeAuthor:
    def __init__(self, uid, name="Tester"):
        self.id = uid
        self.bot = False
        self.display_name = name
        self.mention = f"<@{uid}>"


class FakeCtx:
    def __init__(self, uid=1, gid=42):
        self.guild = types.SimpleNamespace(id=gid, me=None)
        self.author = FakeAuthor(uid)
        self.replies = []

    async def reply(self, content=None, **kwargs):
        self.replies.append(kwargs.get("embed") if kwargs.get("embed") is not None else content)
        return types.SimpleNamespace(id=len(self.replies))

    send = reply


@pytest.fixture()
def eco(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "c.db"), ai_provider="none",
                       web_enabled=False)
    bot = Syke(settings)
    bot._connection.user = types.SimpleNamespace(id=999, mention="<@999>", name="SYKE")
    return econ.Economy(bot)


def _open(eco, gid=42):
    eco.bot.storage.update_config(gid, econ.CONFIG_KEY, open=True)


def test_commands_blocked_until_strip_is_open(eco):
    ctx = FakeCtx()
    run(eco.daily.callback(eco, ctx))
    assert "dark" in ctx.replies[-1].title.lower()          # closed notice
    assert eco.bot.storage.econ_wallet(42, 1)["cash"] == 0   # no wallet seeded


def test_open_then_daily_and_work_pay_out(eco):
    _open(eco)
    ctx = FakeCtx()
    run(eco.daily.callback(eco, ctx))
    w = eco.bot.storage.econ_wallet(42, 1)
    # starting cash + first daily
    assert w["cash"] == econ.STARTING_CASH + econ.daily_reward(1)
    assert w["streak"] == 1
    run(eco.daily.callback(eco, ctx))                        # cooldown blocks a second claim
    assert "claimed" in ctx.replies[-1].title.lower()

    before = eco.bot.storage.econ_wallet(42, 1)["cash"]
    run(eco.work.callback(eco, ctx))
    assert eco.bot.storage.econ_wallet(42, 1)["cash"] > before


def test_pay_moves_cash_with_tax(eco, monkeypatch):
    _open(eco)
    ctx = FakeCtx(uid=1)
    eco.bot.storage.econ_adjust(42, 1, cash=1000)
    target = FakeAuthor(2, "Friend")
    run(eco.pay.callback(eco, ctx, target, "500"))
    # Sender already had a wallet (adjust set seen), so no starting grant: 1000 - 500 = 500.
    assert eco.bot.storage.econ_wallet(42, 1)["cash"] == 500
    import math
    tax = math.ceil(500 * econ.PAY_TAX)
    # Recipient is new: seeded with starting cash, then receives amount - tax.
    assert eco.bot.storage.econ_wallet(42, 2)["cash"] == econ.STARTING_CASH + (500 - tax)


def test_chips_buy_applies_cage_fee(eco):
    _open(eco)
    ctx = FakeCtx()
    eco.bot.storage.econ_adjust(42, 1, cash=1000)
    run(eco.chips_buy.callback(eco, ctx, "1000"))
    w = eco.bot.storage.econ_wallet(42, 1)
    got, _ = econ.convert(1000)
    # Wallet pre-existed (no starting grant); all 1000 cash converted to chips.
    assert w["chips"] == got and w["cash"] == 0


def test_slots_win_and_loss_update_chips(eco, monkeypatch):
    _open(eco)
    ctx = FakeCtx()
    eco.bot.storage.econ_adjust(42, 1, chips=1000)
    # Force a jackpot: net gain = bet*(mult-1).
    monkeypatch.setattr(econ, "slot_spin", lambda rng: ["🎰", "🎰", "🎰"])
    run(eco.slots.callback(eco, ctx, "100"))
    mult = econ.SLOT_TRIPLE["🎰"]
    assert eco.bot.storage.econ_wallet(42, 1)["chips"] == 1000 + 100 * (mult - 1)
    # Force a loss.
    monkeypatch.setattr(econ, "slot_spin", lambda rng: ["🍒", "🔔", "💎"])
    before = eco.bot.storage.econ_wallet(42, 1)["chips"]
    run(eco.slots.callback(eco, ctx, "100"))
    assert eco.bot.storage.econ_wallet(42, 1)["chips"] == before - 100


def test_slots_rejects_bet_above_chips(eco):
    _open(eco)
    ctx = FakeCtx()
    eco.bot.storage.econ_adjust(42, 1, chips=50)
    run(eco.slots.callback(eco, ctx, "100"))
    assert "bad bet" in ctx.replies[-1].title.lower()
    assert eco.bot.storage.econ_wallet(42, 1)["chips"] == 50   # unchanged


def test_admin_on_off_and_give(eco):
    ctx = FakeCtx()
    run(eco.economy_on.callback(eco, ctx))
    assert eco._open(42) is True
    target = FakeAuthor(2)
    run(eco.economy_give.callback(eco, ctx, target, 777, "chips"))
    assert eco.bot.storage.econ_wallet(42, 2)["chips"] == 777
    run(eco.economy_off.callback(eco, ctx))
    assert eco._open(42) is False
