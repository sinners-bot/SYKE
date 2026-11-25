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


def test_cage_is_free_by_default():
    assert econ.CONVERT_FEE == 0
    assert econ.convert(1000) == (1000, 0)


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


class FakeMessage:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeCtx:
    def __init__(self, uid=1, gid=42):
        self.guild = types.SimpleNamespace(id=gid, me=None)
        self.author = FakeAuthor(uid)
        self.replies = []
        self.sent = []          # full kwargs of every reply (files, views…)

    async def reply(self, content=None, **kwargs):
        self.replies.append(kwargs.get("embed") if kwargs.get("embed") is not None else content)
        msg = FakeMessage(kwargs)
        self.sent.append(msg)
        return msg

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


def test_commands_blocked_when_an_admin_closes_it(eco):
    eco.bot.storage.update_config(42, econ.CONFIG_KEY, open=False)   # on by default; !economy off closes it
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


@pytest.mark.parametrize("args", [("1000", None), ("buy", "1000")])
def test_chips_swaps_cash_one_to_one(eco, args):
    _open(eco)
    ctx = FakeCtx()
    eco.bot.storage.econ_adjust(42, 1, cash=1000)
    run(eco.chips.callback(eco, ctx, *args))
    w = eco.bot.storage.econ_wallet(42, 1)
    assert w["chips"] == 1000 and w["cash"] == 0     # free: no cut taken
    assert "no fee" in ctx.replies[-1].description.lower()


def test_bare_chips_only_shows_the_cage(eco):
    _open(eco)
    ctx = FakeCtx()
    run(eco.chips.callback(eco, ctx, None, None))
    assert "cash cage" in ctx.replies[-1].title.lower()
    assert eco.bot.storage.econ_wallet(42, 1)["chips"] == 0


def test_cashout_is_free_and_defaults_to_all(eco):
    _open(eco)
    ctx = FakeCtx()
    eco.bot.storage.econ_adjust(42, 1, chips=750)
    run(eco.cashout.callback(eco, ctx))
    w = eco.bot.storage.econ_wallet(42, 1)
    assert w["chips"] == 0 and w["cash"] == 750


def test_bets_top_up_from_cash_automatically(eco, monkeypatch):
    """The bug report: a player with cash but no chips couldn't bet. Now the cage covers it."""
    _open(eco)
    ctx = FakeCtx()                                   # fresh player: 500 starting cash, 0 chips
    monkeypatch.setattr(econ, "slot_spin", lambda rng: ["🍒", "🔔", "💎"])   # a loss
    run(eco.slots.callback(eco, ctx, "100"))
    w = eco.bot.storage.econ_wallet(42, 1)
    assert w["cash"] == econ.STARTING_CASH - 100 and w["chips"] == 0
    assert "cage swapped" in ctx.replies[-1].description
    assert ctx.sent[-1].kwargs["file"].filename == "slots.png"     # result comes with the art


def test_bet_errors_are_specific(eco):
    _open(eco)
    ctx = FakeCtx()
    run(eco.slots.callback(eco, ctx, "5"))
    assert "minimum" in ctx.replies[-1].description.lower()
    run(eco.slots.callback(eco, ctx, "abc"))
    assert "bet a number" in ctx.replies[-1].description.lower()
    run(eco.slots.callback(eco, ctx, "100000"))
    assert "only got" in ctx.replies[-1].description.lower()


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


# ── interactive tables ───────────────────────────────────────────────────────
from syke.cardgames import Card, HiLo, Shoe  # noqa: E402


def C(text):
    return Card(text[:-1], text[-1])


def stack(eco, *cards, gid=42):
    eco.shoes[gid] = Shoe.stacked([C(c) for c in cards])


class FakeResponse:
    def __init__(self):
        self.done = False
        self.edits = []
        self.messages = []

    def is_done(self):
        return self.done

    async def edit_message(self, **kwargs):
        self.done = True
        self.edits.append(kwargs)

    async def send_message(self, content=None, **kwargs):
        self.done = True
        self.messages.append(content)


class FakeInteraction:
    def __init__(self, uid=1):
        self.user = types.SimpleNamespace(id=uid)
        self.response = FakeResponse()


def chips(eco, uid=1):
    return eco.bot.storage.econ_wallet(42, uid)["chips"]


async def _seat(eco, *cards, bank=1000, bet="100"):
    _open(eco)
    eco.bot.storage.econ_adjust(42, 1, chips=bank)
    stack(eco, *cards)
    ctx = FakeCtx()
    await eco.blackjack.callback(eco, ctx, bet)
    return ctx, ctx.sent[-1].kwargs.get("view")


def labels(view):
    return [item.label for item in view.children]


async def test_blackjack_natural_pays_3_to_2_and_frees_the_seat(eco):
    ctx, view = await _seat(eco, "AS", "9H", "KD", "7C")
    assert chips(eco) == 1000 - 100 + 250
    assert (42, 1) not in eco.tables
    assert labels(view) == ["Deal again (100)"]
    assert ctx.sent[-1].kwargs["file"].filename == "blackjack.png"


async def test_blackjack_buttons_match_the_hand(eco):
    _, view = await _seat(eco, "KS", "5H", "QD", "6C", "9S", "10H", "7D")
    assert labels(view) == ["Hit", "Stand", "Double (100)", "Split (100)", "Surrender"]


async def test_blackjack_stand_wins_and_settles(eco):
    _, view = await _seat(eco, "10S", "6H", "8D", "AC")       # 18 vs dealer soft 17 (stands)
    assert chips(eco) == 900 and (42, 1) in eco.tables
    it = FakeInteraction()
    await view.press(it, "stand")
    assert chips(eco) == 1100
    assert it.response.edits[-1]["attachments"][0].filename == "blackjack.png"
    assert (42, 1) not in eco.tables


async def test_blackjack_double_stakes_and_pays_the_extra_bet(eco):
    _, view = await _seat(eco, "6S", "6H", "5D", "10C", "10S", "9H")
    await view.press(FakeInteraction(), "double")
    assert view.game.hands[0].bet == 200
    assert chips(eco) == 1000 - 200 + 400


async def test_blackjack_split_tens_plays_two_hands(eco):
    _, view = await _seat(eco, "KS", "5H", "QD", "6C", "9S", "10H", "7D")
    await view.press(FakeInteraction(), "split")
    assert chips(eco) == 800                                   # second bet on the table
    await view.press(FakeInteraction(), "stand")
    await view.press(FakeInteraction(), "stand")
    assert [h.outcome for h in view.game.hands] == ["win", "win"]
    assert chips(eco) == 1200


async def test_blackjack_insurance_pays_when_dealer_has_it(eco):
    _, view = await _seat(eco, "10S", "AH", "8D", "KC")
    assert labels(view) == ["Insurance (50)", "No thanks"]
    await view.press(FakeInteraction(), "insurance")
    assert view.game.phase == "done"
    assert chips(eco) == 1000                                  # lost 100, insurance won 100


async def test_blackjack_double_without_funds_is_refused(eco):
    _, view = await _seat(eco, "6S", "6H", "5D", "10C", bank=100)
    it = FakeInteraction()
    await view.press(it, "double")
    assert "need 100 more" in it.response.messages[-1]
    assert not view.game.hands[0].doubled and chips(eco) == 0


async def test_only_the_seated_player_can_press(eco):
    _, view = await _seat(eco, "10S", "6H", "8D", "AC")
    stranger = FakeInteraction(uid=2)
    assert await view.interaction_check(stranger) is False
    assert "table" in stranger.response.messages[-1]


async def test_one_table_at_a_time(eco):
    ctx, _ = await _seat(eco, "10S", "6H", "8D", "AC")
    await eco.blackjack.callback(eco, ctx, "100")
    assert "already at a table" in ctx.replies[-1].title.lower()
    assert chips(eco) == 900                                   # second bet never taken


async def test_timeout_auto_stands_and_settles(eco):
    ctx, view = await _seat(eco, "10S", "6H", "8D", "AC")
    await view.on_timeout()
    assert view.game.phase == "done" and chips(eco) == 1100
    assert ctx.sent[-1].edits                                  # message updated with the result


async def test_unload_refunds_chips_still_in_play(eco):
    _, view = await _seat(eco, "10S", "6H", "8D", "AC")
    assert chips(eco) == 900
    await eco.cog_unload()
    assert chips(eco) == 1000 and not eco.tables


async def test_deal_again_rebets_and_reseats(eco):
    _, view = await _seat(eco, "AS", "9H", "KD", "7C", "10S", "6H", "8D", "AC")
    assert chips(eco) == 1150
    await view.press(FakeInteraction(), "again")
    assert chips(eco) == 1050 and view.game.phase == "player"
    assert eco.tables[(42, 1)] is view


class FixedRng(random.Random):
    def __init__(self, ranks):
        super().__init__(0)
        self.queue = []
        for r in ranks:
            self.queue += [r, "S"]

    def choice(self, seq):
        return self.queue.pop(0) if self.queue else super().choice(seq)


async def test_hilo_call_then_cash_out(eco):
    _open(eco)
    eco.bot.storage.econ_adjust(42, 1, chips=1000)
    eco.rng = FixedRng(["5", "9"])
    ctx = FakeCtx()
    await eco.hilo.callback(eco, ctx, "100")
    view = ctx.sent[-1].kwargs["view"]
    assert chips(eco) == 900
    assert [b.disabled for b in view.children] == [False, False, True]   # can't cash out before a call
    await view.press(FakeInteraction(), "higher")
    await view.press(FakeInteraction(), "cash")
    assert view.game.won and chips(eco) == 900 + view.game.payout > 1000


async def test_hilo_timeout_before_any_call_returns_the_bet(eco):
    _open(eco)
    eco.bot.storage.econ_adjust(42, 1, chips=1000)
    ctx = FakeCtx()
    await eco.hilo.callback(eco, ctx, "100")
    await ctx.sent[-1].kwargs["view"].on_timeout()
    assert chips(eco) == 1000 and not eco.tables


async def test_art_failure_falls_back_to_text(eco, monkeypatch):
    from syke import casino_art

    def boom(*a, **k):
        raise RuntimeError("no pixels today")

    monkeypatch.setattr(casino_art, "slots_machine", boom)
    _open(eco)
    ctx = FakeCtx()
    await eco.slots.callback(eco, ctx, "50")
    assert "file" not in ctx.sent[-1].kwargs and "Slots" in ctx.replies[-1].title


# ── custom emojis ────────────────────────────────────────────────────────────
class FakeEmoji:
    def __init__(self, name, eid):
        self.name, self.id = name, eid

    def __str__(self):
        return f"<:{self.name}:{self.id}>"


async def test_emoji_sync_uploads_missing_and_reuses_existing(eco):
    created = []

    async def fetch():
        return [FakeEmoji("strip_chip", 1)]

    async def create(*, name, image):
        assert image[:8] == b"\x89PNG\r\n\x1a\n"
        created.append(name)
        return FakeEmoji(name, 100 + len(created))

    eco.bot.fetch_application_emojis = fetch
    eco.bot.create_application_emoji = create
    await eco.sync_emojis()
    assert eco.e("chip") == "<:strip_chip:1>"                  # reused, not re-uploaded
    assert "strip_chip" not in created and "strip_cash" in created
    assert eco.e("cash").startswith("<:strip_cash:")
    assert str(eco.partial("chip")) == "<:strip_chip:1>"


async def test_emoji_sync_failure_keeps_standard_emoji(eco):
    async def fetch():
        raise RuntimeError("Discord said no")

    eco.bot.fetch_application_emojis = fetch
    await eco.sync_emojis()
    assert eco.e("chip") == econ.EMOJI_FALLBACK["chip"]


async def test_economy_admin_commands_are_owner_only(eco):
    from discord.ext import commands as dcommands
    eco.bot.settings = replace(eco.bot.settings, owner_ids=frozenset({777}))
    admin_perms = types.SimpleNamespace(manage_guild=True, administrator=True)
    names = {"economy", "economy on", "economy off", "economy give", "economy take", "economy reset"}
    gated = [c for c in eco.walk_commands() if c.qualified_name in names]
    assert len(gated) == len(names)
    for command in gated:
        server_admin = types.SimpleNamespace(author=types.SimpleNamespace(id=1, guild_permissions=admin_perms),
                                             bot=eco.bot, guild=types.SimpleNamespace(id=1))
        with pytest.raises(dcommands.CheckFailure):
            for check in command.checks:
                await check(server_admin)                     # a server admin is not enough any more
        owner = types.SimpleNamespace(author=types.SimpleNamespace(id=777), bot=eco.bot,
                                      guild=types.SimpleNamespace(id=1))
        assert all([await check(owner) for check in command.checks])
