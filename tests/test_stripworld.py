"""Shops, crime & jail, storefront ownership, the House, and the /strip + /casino slash groups."""

import random
import sqlite3
import time
import types
from dataclasses import replace

import pytest

from syke import economy as econ
from syke import stripworld as sw
from syke.bot import Syke
from syke.config import load_settings
from syke.strip_slash import StripSlash
from test_economy import FakeAuthor, FakeCtx

GID = 42


class Rigged(random.Random):
    """random() returns scripted values (then 0.99); everything else behaves normally."""

    def __init__(self, *rolls):
        super().__init__(0)
        self.rolls = list(rolls)

    def random(self):
        return self.rolls.pop(0) if self.rolls else 0.99


async def world(tmp_path, owners=frozenset({1})):
    settings = replace(load_settings(), db_path=str(tmp_path / "s.db"), ai_provider="none", web_enabled=False,
                       owner_ids=owners)
    bot = Syke(settings)
    bot._connection.user = types.SimpleNamespace(id=999, mention="<@999>", name="SYKE")
    eco, strip = econ.Economy(bot), sw.StripWorld(bot)
    await bot.add_cog(eco)
    await bot.add_cog(strip)
    from syke.strip_feed import StripFeed
    await bot.add_cog(StripFeed(bot))
    bot.storage.update_config(GID, econ.CONFIG_KEY, open=True)
    return eco, strip, bot.storage


def wallet(db, uid=1):
    return db.econ_wallet(GID, uid)


def last(ctx):
    e = ctx.replies[-1]
    return f"{e.title} {e.description}"


# ── pure rules ───────────────────────────────────────────────────────────────
def test_storefront_economics():
    video = sw.STORE_BY_KEY["video"]
    assert sw.shop_income(video, 1) == 60 and sw.shop_income(video, 3) == 120
    assert sw.upgrade_cost(video, 1) == 4_800
    row = {"register": 50, "level": 1, "collected_at": 0.0}
    assert sw.pending(row, video, 10 * sw.HOUR) == 50 + 600
    assert sw.pending(row, video, 999 * sw.HOUR) == 50 + 60 * sw.REGISTER_CAP_HOURS   # a register fills up


def test_lookups_are_forgiving():
    assert sw.find_store("golden dragon").key == "dragon"
    assert sw.find_store("Video 2000").key == "video"
    assert sw.find_store("bail").key == "bail"
    assert sw.find_item("lo mein").key == "lomein"
    assert sw.find_item("Get Out of Jail").key == "jailcard"
    assert sw.find_store("nowhere") is None and sw.find_item("unicorn") is None


def test_shop_gambles_keep_an_edge():
    rng = random.Random(1)
    n = 40_000
    assert sum(sw.scratch(rng) for _ in range(n)) / n < sw.ITEM_BY_KEY["scratch"].price
    assert sum(sw.pawn_value(sw.open_crate(rng)) for _ in range(n)) / n < sw.ITEM_BY_KEY["crate"].price
    assert sum(sw.ITEM_BY_KEY[sw.open_bag(rng)].price for _ in range(n)) / n < sw.ITEM_BY_KEY["bag"].price


def test_every_item_lives_in_a_real_store_with_a_known_buff():
    for item in sw.ITEMS:
        assert item.store in sw.STORE_BY_KEY
        assert item.buff is None or item.buff in sw.BUFF_INFO


def test_bail_and_odds():
    assert sw.bail_cost(0) == 250
    assert sw.bail_cost(10 * 60) == 250 + 25 * 10
    assert sw.odds(0.9, True) == 0.95                   # never a sure thing


# ── shops ────────────────────────────────────────────────────────────────────
async def test_buy_takes_cash_and_tips_the_store_owner(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 7, cash=50_000)
    assert db.econ_shop_buy(GID, "general", 7, 18_000, time.time())
    db.econ_adjust(GID, 1, cash=1_000)
    ctx = FakeCtx()
    await strip.buy.callback(strip, ctx, item="padlock")
    assert wallet(db)["cash"] == 200 and db.econ_items(GID, 1) == {"padlock": 1}
    assert db.econ_shop(GID, "general")["register"] == 80          # 10% of the sale


async def test_buy_without_cash_changes_nothing(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 1, cash=100)
    ctx = FakeCtx()
    await strip.buy.callback(strip, ctx, item="charm")
    assert "not enough" in last(ctx).lower()
    assert wallet(db)["cash"] == 100 and not db.econ_items(GID, 1)


async def test_food_boosts_work_pay(tmp_path, monkeypatch):
    eco, strip, db = await world(tmp_path)
    monkeypatch.setattr(econ, "work_shift", lambda rng: ("did a thing", 200))
    db.econ_adjust(GID, 1, cash=1_000)
    ctx = FakeCtx()
    await strip.buy.callback(strip, ctx, item="lo mein")
    await strip.use.callback(strip, ctx, item="lo mein")
    before = wallet(db)["cash"]
    await eco.work.callback(eco, ctx)
    assert wallet(db)["cash"] - before == 250                      # 200 × 1.25


async def test_energy_drink_resets_the_work_cooldown(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 1, cash=500, work_at=time.time())
    db.econ_item_add(GID, 1, "energy")
    ctx = FakeCtx()
    await strip.use.callback(strip, ctx, item="energy")
    assert wallet(db)["work_at"] == 0
    await eco.work.callback(eco, ctx)
    assert "shift done" in last(ctx).lower()


async def test_crates_give_records_you_can_pawn(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 1, cash=0)
    db.econ_item_add(GID, 1, "crate")
    ctx = FakeCtx()
    await strip.use.callback(strip, ctx, item="crate")
    held = db.econ_items(GID, 1)
    (record,) = held
    assert record in sw.RECORD_BY_KEY
    await strip.pawn.callback(strip, ctx, item=f"{record} all")
    assert wallet(db)["cash"] == sw.pawn_value(record) and not db.econ_items(GID, 1)


async def test_paint_job_needs_manage_roles_and_keeps_the_item(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_item_add(GID, 1, "paintjob")
    ctx = FakeCtx()
    ctx.guild.me = types.SimpleNamespace(guild_permissions=types.SimpleNamespace(manage_roles=False))
    await strip.use.callback(strip, ctx, item="paintjob #ff3ea5")
    assert "manage roles" in last(ctx).lower()
    assert db.econ_items(GID, 1) == {"paintjob": 1}
    assert sw.parse_hex("#FF3EA5") == 0xFF3EA5 and sw.parse_hex("pink") is None


# ── crime & jail ─────────────────────────────────────────────────────────────
async def test_rob_success_takes_pocket_cash_only(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 1, cash=500)
    db.econ_adjust(GID, 2, cash=1_000, bank=50_000)
    strip.rng = Rigged(0.0)                                         # succeed; take uses uniform()
    await strip.rob.callback(strip, FakeCtx(), FakeAuthor(2))
    taken = 1_000 - wallet(db, 2)["cash"]
    assert 100 <= taken <= 300 and wallet(db)["cash"] == 500 + taken
    assert wallet(db, 2)["bank"] == 50_000                          # the bank is safe


async def test_padlock_stops_a_robbery(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 1, cash=1_000)
    db.econ_adjust(GID, 2, cash=1_000)
    db.econ_item_add(GID, 2, "padlock")
    strip.rng = Rigged(0.0)
    ctx = FakeCtx()
    await strip.rob.callback(strip, ctx, FakeAuthor(2))
    assert "padlock" in last(ctx).lower()
    assert wallet(db, 2)["cash"] == 1_000 + 150 and wallet(db)["cash"] == 850   # fine paid to the victim
    assert not db.econ_items(GID, 2)                                # the padlock is spent


async def test_failed_rob_fines_and_jails(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 1, cash=1_000)
    db.econ_adjust(GID, 2, cash=1_000)
    strip.rng = Rigged(0.99)
    await strip.rob.callback(strip, FakeCtx(), FakeAuthor(2))
    assert wallet(db)["cash"] == 850 and wallet(db, 2)["cash"] == 1_150
    assert wallet(db)["jail_until"] > time.time()


async def test_jail_blocks_earning_and_gambling_until_bailed(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 1, cash=5_000, chips=500)
    strip.rng = Rigged(0.99)
    ctx = FakeCtx()
    await strip.crime.callback(strip, ctx)
    assert "busted" in last(ctx).lower() and wallet(db)["jail_until"] > time.time()
    for attempt in (eco.work.callback(eco, ctx), eco.slots.callback(eco, ctx, "50"),
                    strip.crime.callback(strip, ctx)):
        await attempt
        assert "jail" in last(ctx).lower()
    # a friend who owns Bail Bonds gets a cut of the bail
    db.econ_adjust(GID, 9, cash=70_000)
    assert db.econ_shop_buy(GID, "bail", 9, 60_000, time.time())
    friend = FakeCtx(uid=3)
    db.econ_adjust(GID, 3, cash=5_000)
    left = wallet(db)["jail_until"] - time.time()
    await strip.bail.callback(strip, friend, FakeAuthor(1))
    cost = sw.bail_cost(left)
    assert wallet(db)["jail_until"] == 0
    assert abs((5_000 - wallet(db, 3)["cash"]) - cost) <= 25        # (a second may tick by)
    assert db.econ_shop(GID, "bail")["register"] > 0


async def test_foresight_keeps_you_out_of_jail(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 1, cash=2_000)
    db.econ_buff(GID, 1, "foresight", time.time() + 3600)
    strip.rng = Rigged(0.99)
    ctx = FakeCtx()
    await strip.crime.callback(strip, ctx)
    assert "saw it coming" in last(ctx).lower() and wallet(db)["jail_until"] == 0


async def test_jail_card_walks_you_out(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 1, jail_until=time.time() + 1800)
    db.econ_item_add(GID, 1, "jailcard")
    await strip.use.callback(strip, FakeCtx(), item="jail card")
    assert wallet(db)["jail_until"] == 0 and not db.econ_items(GID, 1)


async def test_heist_takes_half_the_register(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 7, cash=20_000)
    db.econ_shop_buy(GID, "general", 7, 18_000, time.time())
    db.econ_shop_update(GID, "general", register=1_000)
    db.econ_adjust(GID, 1, cash=500)
    strip.rng = Rigged(0.0)
    await strip.heist.callback(strip, FakeCtx(), store="general store")
    assert wallet(db)["cash"] == 1_000
    assert db.econ_shop(GID, "general")["register"] == 500


async def test_cant_heist_your_own_store(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 1, cash=20_000)
    db.econ_shop_buy(GID, "general", 1, 18_000, time.time())
    ctx = FakeCtx()
    await strip.heist.callback(strip, ctx, store="general")
    assert "your store" in last(ctx).lower()


# ── property ─────────────────────────────────────────────────────────────────
async def test_buy_upgrade_cashup_and_sell_a_storefront(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 1, cash=30_000)
    ctx = FakeCtx()
    await strip.property_buy.callback(strip, ctx, store="video 2000")
    assert db.econ_shop(GID, "video")["owner_id"] == 1 and wallet(db)["cash"] == 22_000
    # someone else can't buy it now
    db.econ_adjust(GID, 2, cash=30_000)
    other = FakeCtx(uid=2)
    await strip.property_buy.callback(strip, other, store="video")
    assert "already owned" in last(other).lower()
    # ten hours pass: the register fills
    db.econ_shop_update(GID, "video", collected_at=time.time() - 10 * sw.HOUR)
    await strip.property_upgrade.callback(strip, ctx, store="video")
    row = db.econ_shop(GID, "video")
    assert row["level"] == 2 and row["register"] >= 599          # old-rate takings were locked in
    before = wallet(db)["cash"]
    await strip.cashup.callback(strip, ctx)
    assert wallet(db)["cash"] - before >= 599 and db.econ_shop(GID, "video")["register"] == 0
    await strip.property_sell.callback(strip, ctx, store="video")
    assert db.econ_shop(GID, "video") is None
    assert wallet(db)["cash"] >= before + 599 + int((8_000 + 4_800) * sw.SELL_BACK)


async def test_property_map_renders(tmp_path):
    eco, strip, db = await world(tmp_path)
    ctx = FakeCtx()
    ctx.guild.get_member = lambda uid: None
    await strip.property_group.callback(strip, ctx)
    assert ctx.sent[-1].kwargs["file"].filename == "the-strip.png"


# ── the House ────────────────────────────────────────────────────────────────
async def test_house_is_owner_only(tmp_path):
    eco, strip, db = await world(tmp_path, owners=frozenset({99}))
    ctx = FakeCtx(uid=1)
    ctx.bot = eco.bot
    with pytest.raises(econ.NotTheHouse):
        await eco.house_give.checks[0](ctx)


async def test_house_controls(tmp_path):
    eco, strip, db = await world(tmp_path)
    ctx = FakeCtx()
    target = FakeAuthor(5)
    await eco.house_give.callback(eco, ctx, target, 1_000, "chips", None)
    assert wallet(db, 5)["chips"] == 1_000
    await eco.house_set.callback(eco, ctx, target, "cash", 77, None)
    assert wallet(db, 5)["cash"] == 77
    await eco.house_give.callback(eco, ctx, target, 500, "cash", "123")      # another server by ID
    assert db.econ_wallet(123, 5)["cash"] == econ.STARTING_CASH + 500
    await eco.house_ban.callback(eco, ctx, target)
    banned = FakeCtx(uid=5)
    await eco.daily.callback(eco, banned)
    assert "86" in last(banned)
    await eco.house_unban.callback(eco, ctx, target)
    await eco.house_jail.callback(eco, ctx, target, 10, None)
    assert wallet(db, 5)["jail_until"] > time.time()
    await eco.house_free.callback(eco, ctx, target, None)
    assert wallet(db, 5)["jail_until"] == 0


async def test_house_happy_hour_and_maintenance(tmp_path):
    eco, strip, db = await world(tmp_path)
    ctx = FakeCtx()
    await eco.house_happy.callback(eco, ctx, 2.0, 1.0, None)
    assert eco.happy(GID) == 2.0
    await eco.daily.callback(eco, ctx)
    assert wallet(db)["cash"] == econ.STARTING_CASH + 2 * econ.daily_reward(1)
    await eco.house_maintenance.callback(eco, ctx, "on")
    await eco.slots.callback(eco, ctx, "50")
    assert "maintenance" in last(ctx).lower()
    await eco.house_maintenance.callback(eco, ctx, "off")
    await eco.house_happy.callback(eco, ctx, 1.0, 1.0, None)
    assert eco.happy(GID) == 1.0


async def test_house_wipe_needs_confirmation(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 1, cash=10_000)
    db.econ_shop_buy(GID, "video", 1, 8_000, time.time())
    ctx = FakeCtx()
    await eco.house_wipe.callback(eco, ctx, "", None)
    assert wallet(db)["cash"] == 2_000
    await eco.house_wipe.callback(eco, ctx, "confirm", None)
    assert wallet(db)["seen"] == 0 and db.econ_shop(GID, "video") is None


# ── slash groups & storage ───────────────────────────────────────────────────
async def test_every_slash_subcommand_routes_to_a_real_command(tmp_path, monkeypatch):
    eco, strip, db = await world(tmp_path)
    slash = StripSlash(eco.bot)
    called = []

    async def fake_from_interaction(interaction):
        return interaction.ctx

    monkeypatch.setattr("discord.ext.commands.Context.from_interaction", fake_from_interaction)
    for group in (slash.strip, slash.casino):
        for sub in group.commands:
            ctx = FakeCtx()
            interaction = types.SimpleNamespace(ctx=ctx)
            args = {}
            for p in sub.parameters:
                if p.required:
                    args[p.name] = FakeAuthor(2) if p.name == "member" else ("100" if p.name != "qty" else 1)
            await sub.callback(slash, interaction, **args)
            called.append(sub.name)
            assert ctx.replies, f"/{group.name} {sub.name} didn't answer"
    assert len(called) == len(slash.strip.commands) + len(slash.casino.commands)
    assert len(slash.strip.commands) <= 25 and len(slash.casino.commands) <= 25   # Discord's per-group cap


def test_old_databases_gain_the_new_wallet_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE econ_wallets (guild_id INTEGER, user_id INTEGER, cash INTEGER NOT NULL DEFAULT 0, "
                 "chips INTEGER NOT NULL DEFAULT 0, bank INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0, "
                 "daily_at REAL NOT NULL DEFAULT 0, work_at REAL NOT NULL DEFAULT 0, hustle_at REAL NOT NULL DEFAULT 0, "
                 "seen INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
    conn.execute("INSERT INTO econ_wallets (guild_id, user_id, cash, seen) VALUES (42, 1, 900, 1)")
    conn.commit()
    conn.close()
    from syke.storage import Storage
    db = Storage(str(path))
    w = db.econ_wallet(42, 1)
    assert w["cash"] == 900 and w["jail_until"] == 0
    db.econ_adjust(42, 1, jail_until=5.0, rob_at=1.0)
    assert db.econ_wallet(42, 1)["jail_until"] == 5.0


def test_free_text_parsing():
    assert sw.split_tail("lo mein 2", r"\d+") == ("lo mein", "2")
    assert sw.split_tail("lo mein", r"\d+") == ("lo mein", None)
    assert sw.split_tail("paintjob #FF3EA5", r"#?[0-9a-f]{6}") == ("paintjob", "#FF3EA5")
    assert sw.split_tail("crystal ball all", r"\d+|all") == ("crystal ball", "all")
    assert sw.find_item("lo").key == "lomein"                 # a prefix beats a substring ("padLOck")
    assert sw.find_item("lock").key == "padlock"
    assert sw.find_store("rose").key == "rose"


async def test_buy_quantity_and_multiword_names(tmp_path):
    eco, strip, db = await world(tmp_path)
    db.econ_adjust(GID, 1, cash=1_000)
    await strip.buy.callback(strip, FakeCtx(), item="scratch ticket 3")
    assert db.econ_items(GID, 1) == {"scratch": 3} and wallet(db)["cash"] == 700
