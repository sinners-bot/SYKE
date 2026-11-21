"""weekly, jobs, stocks, birthdays, AFK, Wrapped, vibe, catch-up, radio, song quiz and top songs."""

import random
import time
import types
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import discord
import pytest

from syke import economy as econ
from syke import music, music_lists as lists, social, strip_extras as sx
from syke.bot import Syke
from syke.config import load_settings
from syke.music_sources import Track
from syke.tldr import Line
from test_economy import FakeCtx

NS = types.SimpleNamespace
GID = 42


@pytest.fixture()
def bot(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "n.db"), ai_provider="none", web_enabled=False)
    b = Syke(settings)
    b._connection.user = NS(id=999, mention="<@999>", name="SYKE")
    return b


@pytest.fixture()
def strip(bot):
    eco = econ.Economy(bot)
    extras = sx.StripExtras(bot)
    bot.get_cog = lambda name: {"Economy": eco, "StripExtras": extras}.get(name)
    bot.storage.update_config(GID, econ.CONFIG_KEY, open=True)
    return eco, extras


# Weekly and daily streaks

async def test_weekly_pays_grows_and_breaks(strip):
    eco, extras = strip
    ctx = FakeCtx()
    await extras.weekly.callback(extras, ctx)
    first = bot_cash = eco.bot.storage.econ_wallet(GID, 1)["cash"]
    assert "Weekly collected" in ctx.replies[-1].title
    await extras.weekly.callback(extras, ctx)
    assert "Already claimed" in ctx.replies[-1].title and eco.bot.storage.econ_wallet(GID, 1)["cash"] == bot_cash
    eco.bot.storage.econ_adjust(GID, 1, week_at=time.time() - 7 * 86400)
    await extras.weekly.callback(extras, ctx)
    w = eco.bot.storage.econ_wallet(GID, 1)
    assert w["week_streak"] == 2 and w["cash"] - first == sx.weekly_reward(2)
    eco.bot.storage.econ_adjust(GID, 1, week_at=time.time() - 20 * 86400)
    await extras.weekly.callback(extras, ctx)
    assert "lost your 2-week streak" in ctx.replies[-1].description
    assert eco.bot.storage.econ_wallet(GID, 1)["week_streak"] == 1


async def test_losing_the_daily_streak_is_called_out(strip):
    eco, _ = strip
    ctx = FakeCtx()
    eco.bot.storage.econ_touch(GID, 1, econ.STARTING_CASH)
    eco.bot.storage.econ_adjust(GID, 1, daily_at=time.time() - 5 * 86400, streak=6)
    await eco.daily.callback(eco, ctx)
    assert "lost your 6-day streak" in ctx.replies[-1].description


# Jobs

@pytest.mark.parametrize("job", list(sx.JOBS))
def test_every_job_has_exactly_one_right_answer(job):
    rng = random.Random(7)
    for _ in range(40):
        question, options, right = sx.make_task(job, rng, year=2026)
        assert len(options) == 4 and len(set(options)) == 4 and 0 <= right < 4 and question
        if job == "dealer":
            cards = [c.strip("`")[:-1] for c in question.split("\n")[0].split(": ", 1)[1].split()]
            assert options[right] == str(sx.blackjack_total(cards))
        if job == "bouncer":
            ages = [2026 - int(o.rsplit(" ", 1)[1]) for o in options]
            assert [a < 21 for a in ages].count(True) == 1 and ages[right] < 21
        if job == "valet":
            plate = question.split("**`")[1].split("`**")[0]
            assert options[right] == plate


def test_shift_pay_rewards_right_and_fast():
    rng = random.Random(1)
    fast, _ = sx.shift_pay(100, 100, True, 3, rng)
    slow, _ = sx.shift_pay(100, 100, True, 15, rng)
    wrong, _ = sx.shift_pay(100, 100, False, 3, rng)
    assert fast > slow > wrong and sx.blackjack_total(["A", "A", "9"]) == 21


async def test_job_shift_pays_and_unlocks(strip):
    eco, extras = strip
    ctx = FakeCtx()
    eco.bot.storage.econ_touch(GID, 1, econ.STARTING_CASH)
    eco.bot.storage.econ_adjust(GID, 1, job_shifts=9)
    await extras.jobs.callback(extras, ctx, job="dealer")
    assert "Not yet" in ctx.replies[-1].title
    await extras.job.callback(extras, ctx)
    view = ctx.sent[-1].kwargs["view"]
    before = eco.bot.storage.econ_wallet(GID, 1)["cash"]
    card = await view.finish(0, 2.0)
    after = eco.bot.storage.econ_wallet(GID, 1)
    assert after["job_shifts"] == 10 and after["cash"] >= before
    assert any(f.name == "Unlocked" for f in card.fields)
    await extras.job.callback(extras, ctx)
    assert "Off the clock" in ctx.replies[-1].title
    await extras.jobs.callback(extras, ctx, job="card dealer")
    assert extras.job_of(GID, 1) == "dealer"


# Stocks

def test_prices_follow_activity():
    rng = random.Random(3)
    busy = [sx.next_price(100, 60, 20, rng) for _ in range(50)]
    quiet = [sx.next_price(100, 2, 20, rng) for _ in range(50)]
    assert sum(busy) / 50 > 100 > sum(quiet) / 50 and min(quiet) >= 1
    assert sx.sparkline([1, 2, 3, 4]) == "▁▃▅█" and sx.parse_shares("all", 7, 0) == 7


async def test_market_ticks_and_trading(strip):
    eco, extras = strip
    guild = NS(id=GID, voice_channels=[NS(members=[NS(bot=False), NS(bot=False)])])
    for _ in range(30):
        extras._count(GID, "LOL", 5)
    data = extras.tick(guild)
    assert set(data["prices"]) == set(sx.TICKERS) and len(data["history"]["LOL"]) == 1
    ctx = FakeCtx()
    eco.bot.storage.econ_touch(GID, 1, 5_000)
    price = data["prices"]["SYKE"]
    await extras.trade(ctx, "syke", "10", buying=True)
    assert "Bought" in ctx.replies[-1].title
    spent = 5_000 - eco.bot.storage.econ_wallet(GID, 1)["cash"]
    assert spent == pytest.approx(10 * price * 1.01, abs=1)
    assert extras.holdings(GID, 1)["SYKE"][0] == 10
    await extras.trade(ctx, "SYKE", "all", buying=False)
    assert "Sold" in ctx.replies[-1].title and extras.holdings(GID, 1) == {}
    await extras.trade(ctx, "NOPE", "1", buying=True)
    assert "Unknown" in ctx.replies[-1].title
    await extras.stocks.callback(extras, ctx)
    assert "Stock Exchange" in ctx.replies[-1].title


# Birthdays

@pytest.mark.parametrize("text,expected", [
    ("24.12", (12, 24)), ("24/12/1999", (12, 24)), ("24 december", (12, 24)), ("Dec 24th", (12, 24)),
    ("2000-02-29", (2, 29)), ("31.02", None), ("13.13", None), ("soon", None),
])
def test_parse_birthday(text, expected):
    assert social.parse_birthday(text) == expected


def test_birthday_dates():
    assert social.is_birthday(2, 29, date(2027, 2, 28)) and not social.is_birthday(2, 29, date(2028, 2, 28))
    assert social.days_until(12, 24, date(2026, 12, 20)) == 4
    assert social.days_until(1, 1, date(2026, 12, 31)) == 1


class Member:
    def __init__(self, uid, name="Ada"):
        self.id, self.display_name, self.mention = uid, name, f"<@{uid}>"
        self.roles = []
        self.display_avatar = NS(url="https://cdn/a.png")

    async def add_roles(self, *roles, reason=None):
        self.roles += list(roles)

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


async def test_birthdays_are_celebrated_once_and_the_role_expires(bot, strip):
    cog = social.Social(bot)
    sent = []

    async def send(content=None, **kwargs):
        sent.append((content, kwargs))

    role = NS(id=55)
    member = Member(7)
    guild = NS(id=GID, me=NS(guild_permissions=discord.Permissions.none()), get_member=lambda i: member if i == 7 else None,
               get_channel=lambda i: NS(send=send) if i == 10 else None, get_role=lambda i: role if i == 55 else None)
    bot.storage.set_config(GID, social.BDAY_KEY, {"channel": 10, "role": 55, "people": {"7": [11, 21]}})
    now = datetime(2026, 11, 21, 9, 0, tzinfo=ZoneInfo("Europe/Vienna"))
    assert await cog.celebrate(guild, now) == 1
    assert await cog.celebrate(guild, now) == 0           # only once a day
    assert role in member.roles and "Happy birthday" in sent[0][1]["embed"].title
    assert bot.storage.econ_wallet(GID, 7)["cash"] == social.BDAY_GIFT
    data = bot.storage.get_config(GID, social.BDAY_KEY)
    data["holders"]["7"] = time.time() - 1
    bot.storage.set_config(GID, social.BDAY_KEY, data)
    await cog.celebrate(guild, now)
    assert role not in member.roles


# AFK

async def test_afk_tells_pingers_and_welcomes_back(bot):
    cog = social.Social(bot)
    bot.display_prefix = lambda gid: "$"
    data = {"7": {"reason": "dinner", "since": time.time() - 3600, "pings": [], "nick": None}}
    bot.storage.set_config(GID, social.AFK_KEY, data)
    replies = []

    def message(author_id, content, mentions=()):
        async def reply(*args, **kwargs):
            replies.append(kwargs.get("embed"))
        return NS(guild=NS(id=GID, get_member=lambda i: NS(display_name="Ada")), author=NS(id=author_id, bot=False,
                  display_name="Bo"), content=content, mentions=list(mentions), reference=None,
                  channel=NS(id=10), id=900 + author_id, reply=reply)

    await cog.on_message(message(8, "hey <@7>", [NS(id=7)]))
    assert "Ada is AFK" in replies[-1].title and "dinner" in replies[-1].description
    assert len(bot.storage.get_config(GID, social.AFK_KEY)["7"]["pings"]) == 1
    back = message(7, "im back")
    back.author.display_name = "Ada"
    await cog.on_message(back)
    assert "Welcome back" in replies[-1].title and "<@8>" in replies[-1].description
    assert bot.storage.get_config(GID, social.AFK_KEY) == {}


# Wrapped, vibe

def test_wrapped_stats_and_partners():
    tz = ZoneInfo("Europe/Vienna")
    base = datetime(2026, 3, 2, 22, 0, tzinfo=tz).timestamp()
    rows = [(i, 10, "blackjack again 😂 blackjack", base + i * 86400, 2 if i == 3 else 0, 0) for i in range(5)]
    stats = social.wrapped_stats(rows, tz, (2, 10), [("Bo", 4)], {10: "casino"})
    assert stats["messages"] == 5 and stats["top_words"][0] == "blackjack" and stats["emoji"] == "😂"
    assert stats["hour"] == 22 and stats["streak"] == 5 and stats["channel"] == "casino"
    assert stats["best_laughs"] == 2
    activity = [(10, 1, "Me", 0), (10, 2, "Bo", 30), (10, 1, "Me", 60), (10, 3, "Cy", 1000)]
    assert social.talk_partners(activity, 1) == [("Bo", 2)]
    png = social.render_wrapped("Ada", 2026, stats)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_offline_vibe_reads_the_room():
    angry = [Line("A", "stfu you idiot", i) for i in range(20)]
    sweet = [Line("A", "love you guys, thanks ❤️", i) for i in range(20)]
    assert social.offline_vibe(angry)["scores"]["toxic"] > social.offline_vibe(sweet)["scores"]["toxic"]
    assert social.offline_vibe(sweet)["mood"] == "Wholesome" and social.bar(50, 10) == "█████░░░░░"


# Music: radio, quiz, top

def test_quiz_answers():
    assert music.quiz_match("dior", "Dior") and music.quiz_match("its munch", "Munch (Feelin' U)")
    assert music.quiz_match("bohemian rapsody", "Bohemian Rhapsody") and not music.quiz_match("rhapsody", "Africa")
    assert lists.resolve_genre("UK Drill") == "drill" and lists.resolve_genre("lo-fi") == "lofi"
    assert lists.resolve_genre("polka") is None


async def test_radio_fills_from_the_genre_without_repeats(bot):
    cog = music.Music(bot)
    player = music.Player(cog, NS(id=GID, voice_client=None))
    player.radio = "drill"
    await cog.radio_fill(player, 5)
    first = {t.label for t in player.queue}
    assert len(first) == 5 and all(t.query for t in player.queue)
    await cog.radio_fill(player, 5)
    assert len({t.label for t in player.queue}) == 10


async def test_plays_are_counted_for_the_charts(bot):
    cog = music.Music(bot)
    for title, who in [("A", 1), ("A", 2), ("B", 1)]:
        cog.record_play(GID, Track(title=title, url=f"https://youtu.be/{title}", requester=who))
    assert bot.storage.top_songs(GID, 0)[0][:3] == ("A", "https://youtu.be/A", 2)
    assert bot.storage.top_djs(GID, 0)[0] == (1, 2)
    ctx = FakeCtx()
    ctx.prefix = ctx.clean_prefix = "$"
    await cog.do_top(ctx, "songs", "week")
    assert "Most played this week" in ctx.replies[-1].title and "youtu.be/A" in ctx.replies[-1].description
    await cog.do_top(ctx, "djs", "all")
    assert "<@1>" in ctx.replies[-1].description
