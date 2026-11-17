"""THE STRIP — a neon Vegas economy for SYKE.

A server economy themed after a seedy-but-glamorous Vegas strip mall: two
currencies (**cash** you earn and **chips** you gamble), a handful of themed
jobs, risky hustles, a cash cage that takes a cut, and a small casino (slots,
coinflip, roulette). The leaderboard is "Richest on the Strip".

All the money logic — payouts, odds, cooldowns, the house edge — lives in plain
functions up top so they're trivial to unit-test (and so a test can prove the
casino actually keeps a long-run edge). The `Economy` cog is a thin Discord
wrapper over those plus the wallet methods on `Storage`.

The whole system is **off by default** per server; an admin opens the Strip with
`economy on`. That keeps SYKE's economy out of servers that don't want it.
"""

from __future__ import annotations

import math
import random
import time

import discord
from discord import app_commands
from discord.ext import commands

from .moderation import BRAND, OK, WARN, embed, moderator

# ── Currency & flavor ──────────────────────────────────────────────────────
CASH = "💵"
CHIP = "🎰"
BANK_EMOJI = "🏦"
CONFIG_KEY = "economy"          # per-guild config blob: {"open": bool}

STARTING_CASH = 500             # granted once, on a player's first interaction

# Earning
DAILY_BASE = 250
DAILY_STREAK_STEP = 60          # extra per consecutive day
DAILY_STREAK_CAP = 10           # bonus stops growing past this many days
DAILY_COOLDOWN = 22 * 3600      # claimable a bit under 24h so timezones don't drift you out
STREAK_WINDOW = 48 * 3600       # claim within this of your last or the streak resets

WORK_COOLDOWN = 60 * 60
WORK_LOW, WORK_HIGH = 120, 340

HUSTLE_COOLDOWN = 20 * 60

PAY_TAX = 0.03                  # cut taken out of player-to-player transfers (a sink)
CONVERT_FEE = 0.05             # the cash cage's edge on cash<->chips both ways

CHAT_REWARD = 3                 # trickle for being active
CHAT_COOLDOWN = 60

MIN_BET = 10
MAX_BET = 1_000_000

# Themed shifts, straight off the signs on the Strip.
WORK_JOBS = [
    "restocked the shelves at the **General Store**",
    "buffed acrylics all afternoon at **Nails 4 U**",
    "developed someone's deeply questionable vacation photos at **1-Hour Photo**",
    "bussed tables at the **Golden Dragon** (and took home cold lo mein)",
    "rewound tapes at **Video 2000** like it's still 1999",
    "covered the register at **Cash 4 Less** while the owner 'ran an errand'",
    "read a few palms for **Madame Rose** when she stepped out",
    "mixed paint at **Sherwin Williams** and only ruined one shirt",
    "flipped crates at **Pawn Deals** looking for anything worth selling",
    "worked the night shift at the **Dead Air Records** counter",
]

HUSTLE_WINS = [
    "You found a working slot machine someone dumped behind **Pawn Deals** and cashed out {amt}.",
    "You resold a 'barely used' phone from **Cash 4 Less** for {amt}.",
    "You won a scratcher at the **General Store** — {amt}.",
    "You talked a tourist into a {amt} 'guided tour' of the Strip.",
    "A high-roller tipped you {amt} for carrying their bags to the Golden Dragon.",
]
HUSTLE_FLOPS = [
    "You stood outside **Madame Rose** for an hour. Nothing. No money, no fortune.",
    "Your scratcher was a dud. Better luck next time.",
    "The tourist you were hustling turned out to be an off-duty cop. You walked away empty-handed.",
]
HUSTLE_BUSTS = [
    "Security caught you leaning on the slots at the casino and fined you {amt}.",
    "You bought 'designer' shades off a guy by **Video 2000**. Fake. Out {amt}.",
    "You tipped the wrong palm reader and lost {amt}.",
]

# ── Slots ──────────────────────────────────────────────────────────────────
SLOT_SYMBOLS = ["🍒", "🔔", "🍋", "💎", "7️⃣", "🎰"]
SLOT_WEIGHTS = [34, 26, 24, 10, 4, 2]            # sum 100; rarer = richer
# Multipliers are the TOTAL returned on the bet (stake included); 0 means a loss.
SLOT_TRIPLE = {"🎰": 60, "7️⃣": 25, "💎": 12, "🔔": 7, "🍋": 5, "🍒": 5}
SLOT_TWO_CHERRY = 2

# ── Roulette (single-zero / European) ──────────────────────────────────────
ROULETTE_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
ROULETTE_OUTSIDE = ("red", "black", "even", "odd", "low", "high")
COINFLIP_MULT = 1.95


def fmt(amount: int, emoji: str = CASH) -> str:
    """Pretty money, e.g. '💵 1,250'."""
    return f"{emoji} {int(amount):,}"


def daily_reward(streak: int) -> int:
    """Payout for a daily claim, where `streak` is the (new) consecutive-day count, 1-based."""
    steps = min(max(streak, 1) - 1, DAILY_STREAK_CAP)
    return DAILY_BASE + steps * DAILY_STREAK_STEP


def next_streak(last_at: float, now: float, streak: int) -> int:
    """The streak after a claim at `now`, given the previous claim time and streak."""
    if last_at and (now - last_at) <= STREAK_WINDOW:
        return max(1, streak) + 1
    return 1


def work_shift(rng: random.Random) -> tuple[str, int]:
    """A random themed shift: (what you did, how much you earned)."""
    return rng.choice(WORK_JOBS), rng.randint(WORK_LOW, WORK_HIGH)


def hustle(rng: random.Random) -> tuple[int, str]:
    """A risky earn: (delta, flavor). Delta can be negative on a bust, 0 on a flop."""
    roll = rng.random()
    if roll < 0.55:
        amt = rng.randint(90, 280)
        return amt, rng.choice(HUSTLE_WINS).format(amt=fmt(amt))
    if roll < 0.80:
        return 0, rng.choice(HUSTLE_FLOPS)
    loss = rng.randint(40, 130)
    return -loss, rng.choice(HUSTLE_BUSTS).format(amt=fmt(loss))


def convert(amount: int, fee_rate: float = CONVERT_FEE) -> tuple[int, int]:
    """Cash cage conversion: returns (received, fee) for turning `amount` of one currency
    into the other. The fee is the house's cut and rounds in the house's favor."""
    amount = max(0, int(amount))
    fee = math.ceil(amount * fee_rate)
    return max(0, amount - fee), fee


def slot_spin(rng: random.Random) -> list[str]:
    return rng.choices(SLOT_SYMBOLS, weights=SLOT_WEIGHTS, k=3)


def slot_multiplier(reels: list[str]) -> int:
    """Total-return multiplier for a set of reels (0 = loss)."""
    if reels[0] == reels[1] == reels[2]:
        return SLOT_TRIPLE.get(reels[0], 0)
    if reels.count("🍒") == 2:
        return SLOT_TWO_CHERRY
    return 0


def roulette_spin(rng: random.Random) -> int:
    return rng.randint(0, 36)


def roulette_multiplier(space: str, number: int) -> int:
    """Total-return multiplier for a bet on `space` given the winning `number` (0 = loss)."""
    space = space.strip().lower()
    if space.isdigit():
        return 36 if int(space) == number else 0
    if number == 0:
        return 0  # zero loses every outside bet — the house edge
    if space == "red":
        return 2 if number in ROULETTE_RED else 0
    if space == "black":
        return 2 if number not in ROULETTE_RED else 0
    if space == "even":
        return 2 if number % 2 == 0 else 0
    if space == "odd":
        return 2 if number % 2 == 1 else 0
    if space == "low":
        return 2 if 1 <= number <= 18 else 0
    if space == "high":
        return 2 if 19 <= number <= 36 else 0
    return 0


def valid_roulette_space(space: str) -> bool:
    space = (space or "").strip().lower()
    if space in ROULETTE_OUTSIDE:
        return True
    return space.isdigit() and 0 <= int(space) <= 36


def parse_amount(text: str, available: int) -> int | None:
    """Parse a bet/amount: a plain number, or all/max/half, or a k/m suffix. None if invalid."""
    if text is None:
        return None
    t = text.strip().lower().replace(",", "")
    if t in ("all", "max"):
        return available
    if t in ("half",):
        return available // 2
    mult = 1
    if t.endswith("k"):
        mult, t = 1_000, t[:-1]
    elif t.endswith("m"):
        mult, t = 1_000_000, t[:-1]
    try:
        value = int(float(t) * mult)
    except ValueError:
        return None
    return value if value > 0 else None


# ── The cog ────────────────────────────────────────────────────────────────
class Economy(commands.Cog, name="Economy"):
    """THE STRIP: wallets, earning, the cash cage, and the casino."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.rng = random.Random()
        self._chat_at: dict[tuple[int, int], float] = {}

    # -- helpers --
    def _open(self, guild_id: int) -> bool:
        return bool(self.bot.storage.get_config(guild_id, CONFIG_KEY).get("open"))

    def p(self, ctx) -> str:
        return self.bot.display_prefix(ctx.guild.id)

    async def _guard(self, ctx) -> dict | None:
        """Shared entry for player commands: ensures the Strip is open and returns the caller's
        wallet (seeding starting cash on first touch). Replies and returns None if closed."""
        if ctx.guild is None:
            await ctx.reply(embed=embed("Servers only", "The Strip only runs inside a server.",
                                        WARN), ephemeral=True)
            return None
        if not self._open(ctx.guild.id):
            await ctx.reply(embed=embed(f"{CHIP} The Strip is dark",
                                        f"The neon's off here. An admin can light it up with "
                                        f"`{self.p(ctx)}economy on`.", WARN), ephemeral=True)
            return None
        return self.bot.storage.econ_touch(ctx.guild.id, ctx.author.id, STARTING_CASH)

    def _net(self, w: dict) -> int:
        return w["cash"] + w["bank"] + w["chips"]

    # -- balance --
    @commands.hybrid_command(name="balance", aliases=["bal", "wallet"],
                             description="Check your cash, chips and bank on the Strip")
    @app_commands.describe(member="Whose wallet to check (defaults to you)")
    async def balance(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        who = member or ctx.author
        if member is not None and not member.bot:
            w = self.bot.storage.econ_wallet(ctx.guild.id, member.id)
        e = embed(f"{who.display_name}'s wallet", colour=BRAND)
        e.add_field(name="Cash", value=fmt(w["cash"]), inline=True)
        e.add_field(name="Chips", value=fmt(w["chips"], CHIP), inline=True)
        e.add_field(name="Bank", value=fmt(w["bank"], BANK_EMOJI), inline=True)
        e.add_field(name="Net worth", value=f"**{fmt(self._net(w))}**", inline=False)
        if w["streak"]:
            e.set_footer(text=f"🔥 {w['streak']}-day daily streak")
        await ctx.reply(embed=e)

    # -- daily --
    @commands.hybrid_command(name="daily", description="Claim your daily cash (build a streak for more)")
    async def daily(self, ctx: commands.Context) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        now = time.time()
        waited = now - w["daily_at"]
        if w["daily_at"] and waited < DAILY_COOLDOWN:
            ready = int(w["daily_at"] + DAILY_COOLDOWN)
            await ctx.reply(embed=embed("Already claimed",
                                        f"Come back <t:{ready}:R>.", WARN), ephemeral=True)
            return
        streak = next_streak(w["daily_at"], now, w["streak"])
        amount = daily_reward(streak)
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, cash=amount,
                                     daily_at=now, streak=streak)
        body = f"You picked up {fmt(amount)}."
        if streak > 1:
            body += f"\n🔥 **{streak}-day streak** — keep it going for bigger payouts."
        await ctx.reply(embed=embed(f"{CASH} Daily collected", body, OK))

    # -- work --
    @commands.hybrid_command(name="work", aliases=["shift"], description="Clock a shift on the Strip for cash")
    async def work(self, ctx: commands.Context) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        now = time.time()
        if w["work_at"] and now - w["work_at"] < WORK_COOLDOWN:
            ready = int(w["work_at"] + WORK_COOLDOWN)
            await ctx.reply(embed=embed("On the clock already",
                                        f"You're beat. Next shift <t:{ready}:R>.", WARN),
                            ephemeral=True)
            return
        did, amount = work_shift(self.rng)
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, cash=amount, work_at=now)
        await ctx.reply(embed=embed("Shift done 🧾", f"You {did} and earned {fmt(amount)}.", OK))

    # -- hustle --
    @commands.hybrid_command(name="hustle", aliases=["grind"],
                             description="Try a risky hustle — could pay off, could backfire")
    async def hustle_cmd(self, ctx: commands.Context) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        now = time.time()
        if w["hustle_at"] and now - w["hustle_at"] < HUSTLE_COOLDOWN:
            ready = int(w["hustle_at"] + HUSTLE_COOLDOWN)
            await ctx.reply(embed=embed("Lay low a bit",
                                        f"Too hot right now. Try again <t:{ready}:R>.", WARN),
                            ephemeral=True)
            return
        delta, flavor = hustle(self.rng)
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, cash=delta, hustle_at=now)
        colour = OK if delta > 0 else WARN
        await ctx.reply(embed=embed("Hustle", flavor, colour))

    # -- pay --
    @commands.hybrid_command(name="pay", aliases=["give"], description="Send cash to someone (small tax applies)")
    @app_commands.describe(member="Who to pay", amount="How much (a number, or 'all'/'half')")
    async def pay(self, ctx: commands.Context, member: discord.Member, amount: str) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        if member.bot or member.id == ctx.author.id:
            await ctx.reply(embed=embed("Can't pay that", "Pick another (human) member.", WARN),
                            ephemeral=True)
            return
        value = parse_amount(amount, w["cash"])
        if value is None or value <= 0:
            await ctx.reply(embed=embed("How much?", "Give a number, like `pay @user 500`.", WARN),
                            ephemeral=True)
            return
        if value > w["cash"]:
            await ctx.reply(embed=embed("Not enough cash", f"You only have {fmt(w['cash'])}.", WARN),
                            ephemeral=True)
            return
        tax = math.ceil(value * PAY_TAX)
        self.bot.storage.econ_touch(ctx.guild.id, member.id, STARTING_CASH)
        if not self.bot.storage.econ_transfer(ctx.guild.id, ctx.author.id, member.id, value, tax):
            await ctx.reply(embed=embed("Not enough cash", f"You only have {fmt(w['cash'])}.", WARN),
                            ephemeral=True)
            return
        await ctx.reply(embed=embed(f"{CASH} Paid",
                                    f"{ctx.author.mention} → {member.mention}: {fmt(value - tax)} "
                                    f"(after {fmt(tax)} tax).", OK))

    # -- bank --
    @commands.hybrid_command(name="deposit", aliases=["dep"], description="Move cash into your bank")
    @app_commands.describe(amount="How much (a number, or 'all'/'half')")
    async def deposit(self, ctx: commands.Context, amount: str) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        value = parse_amount(amount, w["cash"])
        if value is None or value <= 0 or value > w["cash"]:
            await ctx.reply(embed=embed("Can't deposit that", f"You have {fmt(w['cash'])} on hand.",
                                        WARN), ephemeral=True)
            return
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, cash=-value, bank=value)
        await ctx.reply(embed=embed(f"{BANK_EMOJI} Deposited", f"Banked {fmt(value)}.", OK))

    @commands.hybrid_command(name="withdraw", aliases=["wd"], description="Take cash out of your bank")
    @app_commands.describe(amount="How much (a number, or 'all'/'half')")
    async def withdraw(self, ctx: commands.Context, amount: str) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        value = parse_amount(amount, w["bank"])
        if value is None or value <= 0 or value > w["bank"]:
            await ctx.reply(embed=embed("Can't withdraw that", f"You have {fmt(w['bank'], BANK_EMOJI)} "
                                        "banked.", WARN), ephemeral=True)
            return
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, cash=value, bank=-value)
        await ctx.reply(embed=embed(f"{CASH} Withdrew", f"Took out {fmt(value)}.", OK))

    # -- the cash cage: cash <-> chips --
    @commands.hybrid_group(name="chips", fallback="help", case_insensitive=True,
                           description="The cash cage: swap cash for casino chips and back")
    async def chips(self, ctx: commands.Context) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        pct = int(CONVERT_FEE * 100)
        await ctx.reply(embed=embed(f"{CHIP} The cash cage",
                                    f"You hold {fmt(w['cash'])} and {fmt(w['chips'], CHIP)}.\n\n"
                                    f"`{self.p(ctx)}chips buy <amount>` — cash → chips\n"
                                    f"`{self.p(ctx)}chips cash <amount>` — chips → cash\n\n"
                                    f"The cage takes **{pct}%** each way.", BRAND))

    @chips.command(name="buy", description="Turn cash into chips")
    @app_commands.describe(amount="How much cash to convert (number, or 'all'/'half')")
    async def chips_buy(self, ctx: commands.Context, amount: str) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        value = parse_amount(amount, w["cash"])
        if value is None or value <= 0 or value > w["cash"]:
            await ctx.reply(embed=embed("Can't convert that", f"You have {fmt(w['cash'])}.", WARN),
                            ephemeral=True)
            return
        got, fee = convert(value)
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, cash=-value, chips=got)
        await ctx.reply(embed=embed(f"{CHIP} Bought chips",
                                    f"{fmt(value)} → {fmt(got, CHIP)} (cage took {fmt(fee)}).", OK))

    @chips.command(name="cash", description="Turn chips back into cash")
    @app_commands.describe(amount="How many chips to convert (number, or 'all'/'half')")
    async def chips_cash(self, ctx: commands.Context, amount: str) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        value = parse_amount(amount, w["chips"])
        if value is None or value <= 0 or value > w["chips"]:
            await ctx.reply(embed=embed("Can't convert that", f"You have {fmt(w['chips'], CHIP)}.",
                                        WARN), ephemeral=True)
            return
        got, fee = convert(value)
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, chips=-value, cash=got)
        await ctx.reply(embed=embed(f"{CASH} Cashed out",
                                    f"{fmt(value, CHIP)} → {fmt(got)} (cage took {fmt(fee, CHIP)}).", OK))

    # -- casino --
    def _take_bet(self, ctx, w: dict, amount: str) -> int | None:
        """Validate a chip bet; replies and returns None if it doesn't fly."""
        value = parse_amount(amount, w["chips"])
        if value is None or value < MIN_BET or value > MAX_BET or value > w["chips"]:
            return None
        return value

    async def _bad_bet(self, ctx, w: dict) -> None:
        await ctx.reply(embed=embed("Bad bet",
                                    f"Bet between {MIN_BET:,} and your {w['chips']:,} chips. "
                                    f"Buy chips with `{self.p(ctx)}chips buy`.", WARN), ephemeral=True)

    @commands.hybrid_command(name="slots", aliases=["slot"], description="Pull the neon slots (bets chips)")
    @app_commands.describe(bet="How many chips to bet (number, or 'all'/'half')")
    async def slots(self, ctx: commands.Context, bet: str) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        value = self._take_bet(ctx, w, bet)
        if value is None:
            await self._bad_bet(ctx, w)
            return
        reels = slot_spin(self.rng)
        mult = slot_multiplier(reels)
        won = value * mult
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, chips=won - value)
        line = " ".join(reels)
        if won > value:
            msg = f"**{line}**\n\nYou win {fmt(won - value, CHIP)}! (×{mult})"
            colour = OK
        elif won == value:
            msg = f"**{line}**\n\nPush — you keep your {fmt(value, CHIP)}."
            colour = BRAND
        else:
            msg = f"**{line}**\n\nThe Strip keeps your {fmt(value, CHIP)}."
            colour = WARN
        await ctx.reply(embed=embed(f"{CHIP} Slots", msg, colour))

    @commands.hybrid_command(name="flip", aliases=["coinflip", "cf"],
                             description="Coinflip for chips — heads or tails")
    @app_commands.describe(side="heads or tails", bet="How many chips to bet")
    async def flip(self, ctx: commands.Context, side: str, bet: str) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        pick = side.strip().lower()
        if pick in ("h", "head", "heads"):
            pick = "heads"
        elif pick in ("t", "tail", "tails"):
            pick = "tails"
        else:
            await ctx.reply(embed=embed("Heads or tails?", "Pick `heads` or `tails`.", WARN),
                            ephemeral=True)
            return
        value = self._take_bet(ctx, w, bet)
        if value is None:
            await self._bad_bet(ctx, w)
            return
        result = self.rng.choice(["heads", "tails"])
        if result == pick:
            won = int(value * COINFLIP_MULT)
            self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, chips=won - value)
            await ctx.reply(embed=embed(f"{CHIP} {result.title()}!",
                                        f"You called it — up {fmt(won - value, CHIP)}.", OK))
        else:
            self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, chips=-value)
            await ctx.reply(embed=embed(f"{CHIP} {result.title()}.",
                                        f"Wrong call — down {fmt(value, CHIP)}.", WARN))

    @commands.hybrid_command(name="roulette", aliases=["roul"],
                             description="Spin the wheel: a number (0-36) or red/black/even/odd/low/high")
    @app_commands.describe(space="A number 0-36, or red/black/even/odd/low/high", bet="Chips to bet")
    async def roulette(self, ctx: commands.Context, space: str, bet: str) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        if not valid_roulette_space(space):
            await ctx.reply(embed=embed("Pick a space",
                                        "Bet a number `0`-`36`, or `red`/`black`/`even`/`odd`/`low`/`high`.",
                                        WARN), ephemeral=True)
            return
        value = self._take_bet(ctx, w, bet)
        if value is None:
            await self._bad_bet(ctx, w)
            return
        number = roulette_spin(self.rng)
        mult = roulette_multiplier(space, number)
        won = value * mult
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, chips=won - value)
        colour = "red" if number in ROULETTE_RED else ("green" if number == 0 else "black")
        head = f"The ball lands on **{number}** ({colour})."
        if won > value:
            msg = f"{head}\n\nYou win {fmt(won - value, CHIP)}! (×{mult})"
            tone = OK
        else:
            msg = f"{head}\n\nDown {fmt(value, CHIP)}."
            tone = WARN
        await ctx.reply(embed=embed(f"{CHIP} Roulette", msg, tone))

    # -- leaderboard --
    @commands.hybrid_command(name="rich", aliases=["baltop", "richest"],
                             description="Richest on the Strip")
    async def rich(self, ctx: commands.Context) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        top = self.bot.storage.econ_top(ctx.guild.id, 15)
        if not top:
            await ctx.reply(embed=embed("Richest on the Strip",
                                        "Nobody's got a dime yet. Run `daily` to get started.", BRAND))
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(top):
            tag = medals[i] if i < 3 else f"`#{i + 1}`"
            lines.append(f"{tag} <@{row['user_id']}> — {fmt(row['net'])}")
        await ctx.reply(embed=embed(f"{CHIP} Richest on the Strip", "\n".join(lines), BRAND))

    # -- admin --
    @commands.hybrid_group(name="economy", fallback="status", case_insensitive=True,
                           description="Admin: open/close the Strip and manage wallets")
    @moderator("manage_guild")
    async def economy(self, ctx: commands.Context) -> None:
        state = "**open** 🟢" if self._open(ctx.guild.id) else "**closed** 🔴"
        circ = self.bot.storage.econ_circulation(ctx.guild.id)
        await ctx.reply(embed=embed(f"{CHIP} The Strip — {state}",
                                    f"{circ['players']} players · {fmt(circ['cash'])} cash · "
                                    f"{fmt(circ['chips'], CHIP)} chips · {fmt(circ['bank'], BANK_EMOJI)} banked\n\n"
                                    f"`{self.p(ctx)}economy on` / `off` · `give` · `take` · `reset`", BRAND),
                        ephemeral=True)

    @economy.command(name="on", aliases=["open"], description="Open the Strip in this server")
    @moderator("manage_guild")
    async def economy_on(self, ctx: commands.Context) -> None:
        self.bot.storage.update_config(ctx.guild.id, CONFIG_KEY, open=True)
        await ctx.reply(embed=embed(f"{CHIP} The Strip is OPEN",
                                    f"The neon's on. Everyone starts with {fmt(STARTING_CASH)}. "
                                    f"Try `{self.p(ctx)}daily`, `{self.p(ctx)}work`, `{self.p(ctx)}slots`.", OK))

    @economy.command(name="off", aliases=["close"], description="Close the Strip (balances are kept)")
    @moderator("manage_guild")
    async def economy_off(self, ctx: commands.Context) -> None:
        self.bot.storage.update_config(ctx.guild.id, CONFIG_KEY, open=False)
        await ctx.reply(embed=embed("The Strip is closed",
                                    "Commands are off, but everyone's balances are safe for when you reopen.",
                                    WARN))

    @economy.command(name="give", description="Add funds to a member's wallet")
    @app_commands.describe(member="Who", amount="How much", currency="cash (default), chips or bank")
    @moderator("manage_guild")
    async def economy_give(self, ctx: commands.Context, member: discord.Member, amount: int,
                           currency: str = "cash") -> None:
        cur = currency.strip().lower()
        if cur not in ("cash", "chips", "bank") or amount == 0:
            await ctx.reply(embed=embed("Check that", "Currency is `cash`, `chips` or `bank`, "
                                        "and amount can't be 0.", WARN), ephemeral=True)
            return
        self.bot.storage.econ_touch(ctx.guild.id, member.id, STARTING_CASH)
        self.bot.storage.econ_adjust(ctx.guild.id, member.id, **{cur: amount})
        verb = "Gave" if amount > 0 else "Took"
        emoji = {"cash": CASH, "chips": CHIP, "bank": BANK_EMOJI}[cur]
        await ctx.reply(embed=embed("Done", f"{verb} {member.mention} {fmt(abs(amount), emoji)} {cur}.", OK))

    @economy.command(name="take", description="Remove funds from a member's wallet")
    @app_commands.describe(member="Who", amount="How much", currency="cash (default), chips or bank")
    @moderator("manage_guild")
    async def economy_take(self, ctx: commands.Context, member: discord.Member, amount: int,
                           currency: str = "cash") -> None:
        await self.economy_give(ctx, member, -abs(amount), currency)

    @economy.command(name="reset", description="Wipe a member's wallet (or 'all' for the whole server)")
    @app_commands.describe(member="Who to reset — omit and pass target 'all' to wipe everyone")
    @moderator("manage_guild")
    async def economy_reset(self, ctx: commands.Context, member: discord.Member | None = None,
                            target: str = "") -> None:
        if member is None and target.strip().lower() == "all":
            n = self.bot.storage.econ_reset_guild(ctx.guild.id)
            await ctx.reply(embed=embed("Wiped", f"Cleared {n} wallet(s) on the Strip.", WARN))
            return
        if member is None:
            await ctx.reply(embed=embed("Who?", "Name a member, or pass `all` to wipe everyone.",
                                        WARN), ephemeral=True)
            return
        self.bot.storage.econ_reset(ctx.guild.id, member.id)
        await ctx.reply(embed=embed("Reset", f"{member.mention}'s wallet is wiped.", WARN))

    # -- passive: a tiny trickle for being active --
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        if not (message.content or "").strip():
            return
        if message.content.startswith(self.bot.display_prefix(message.guild.id)):
            return
        if not self._open(message.guild.id):
            return
        key = (message.guild.id, message.author.id)
        now = time.time()
        if now - self._chat_at.get(key, 0) < CHAT_COOLDOWN:
            return
        self._chat_at[key] = now
        self.bot.storage.econ_touch(message.guild.id, message.author.id, STARTING_CASH)
        self.bot.storage.econ_adjust(message.guild.id, message.author.id, cash=CHAT_REWARD)


def help_lines(prefix: str) -> list[str]:
    return [
        f"`{prefix}economy on` — open THE STRIP in your server (admin)",
        f"`{prefix}daily` / `{prefix}work` / `{prefix}hustle` — earn cash",
        f"`{prefix}chips buy` — swap cash for chips at the cage",
        f"`{prefix}slots` / `{prefix}flip` / `{prefix}roulette` — the casino",
        f"`{prefix}rich` — richest on the Strip",
    ]
