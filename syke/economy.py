"""THE STRIP — a neon Vegas economy for SYKE.

A server economy themed after a seedy-but-glamorous Vegas strip mall: two
currencies (**cash** you earn and **chips** you play with), themed jobs, risky
hustles, a free cash cage, and a casino — slots, coinflip, roulette, Hi-Lo and a
casino-accurate blackjack table (see `cardgames.py` for the rules).

Every casino result is a custom-drawn image (`casino_art.py`) and SYKE uploads
its own emoji set (chips, cash, the vault…) as application emojis on startup,
falling back to standard emoji if Discord won't take them.

Bets come out of your chips; if you're short, the cage swaps the difference in
from your cash automatically (1:1, no fee) so a bet never fails just because you
forgot to buy chips first. All money logic lives in plain functions up top, so
it's unit-tested — including a test proving every game keeps a house edge.

The whole system is **off by default** per server; an admin opens the Strip with
`economy on`.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import math
import random
import time

import discord
from discord import app_commands
from discord.ext import commands

from . import casino_art as art
from .cardgames import Blackjack, HiLo, Shoe
from .moderation import BRAND, OK, WARN, embed, moderator

log = logging.getLogger("syke.economy")

# ── Currency & flavor ──────────────────────────────────────────────────────
CASH = "💵"
CHIP = "🪙"
BANK_EMOJI = "🏦"
CONFIG_KEY = "economy"          # per-guild config blob: {"open": bool, "happy_until": ts, "happy_mult": x}
HOUSE_KEY = "strip_house"       # global owner settings, stored under guild id 0: {"banned": [...], "maintenance": bool}
HAPPY_MAX = 5.0

# Fallbacks for SYKE's own emoji set (used until/unless the custom ones upload).
EMOJI_FALLBACK = {"cash": CASH, "chip": CHIP, "bank": BANK_EMOJI, "coin": "🪙", "cards": "🃏",
                  "slots": "🎰", "roulette": "🎡", "crown": "👑", "dice": "🎲"}
EMOJI_PREFIX = "strip_"

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
CONVERT_FEE = 0.0              # the cash cage is free: cash and chips swap 1:1

CHAT_REWARD = 3                 # trickle for being active
CHAT_COOLDOWN = 60

MIN_BET = 10
MAX_BET = 1_000_000

TABLE_TIMEOUT = 120             # seconds of inactivity before a blackjack hand auto-stands
HILO_TIMEOUT = 90

RULES_FOOTER = "6-deck shoe • Blackjack pays 3:2 • Dealer stands on all 17s • Split any pair, 10s included"

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


def hustle(rng: random.Random, luck: float = 0.0, safe: bool = False) -> tuple[int, str]:
    """A risky earn: (delta, flavor). Delta can be negative on a bust, 0 on a flop.
    `luck` raises the win chance; `safe` turns a bust into a flop."""
    roll = rng.random()
    if roll < 0.55 + luck:
        amt = rng.randint(90, 280)
        return amt, rng.choice(HUSTLE_WINS).format(amt=fmt(amt))
    if safe or roll < 0.80 + luck / 2:
        return 0, rng.choice(HUSTLE_FLOPS)
    loss = rng.randint(40, 130)
    return -loss, rng.choice(HUSTLE_BUSTS).format(amt=fmt(loss))


def convert(amount: int, fee_rate: float = CONVERT_FEE) -> tuple[int, int]:
    """Cash cage conversion: returns (received, fee) for swapping `amount` of one currency
    for the other. The cage is free by default (fee 0); any fee rounds in the house's favor."""
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
        return available if available > 0 else None
    if t in ("half",):
        return available // 2 if available // 2 > 0 else None
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


async def render(fn, *args, filename: str, **kwargs) -> discord.File | None:
    """Render an image off the event loop. None (text-only fallback) if drawing fails."""
    try:
        data = await asyncio.to_thread(fn, *args, **kwargs)
    except Exception:
        log.exception("Casino art failed (%s); sending text only", getattr(fn, "__name__", fn))
        return None
    return discord.File(io.BytesIO(data), filename=filename)


class NotTheHouse(commands.CheckFailure):
    """Raised when someone who isn't a SYKE owner tries a House command."""


def house_only():
    """Only SYKE's owners (SYKE_OWNER_IDS) run the House."""
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.author.id in ctx.bot.settings.owner_ids:
            return True
        raise NotTheHouse("Only SYKE's owners can run the House.")
    return commands.check(predicate)


# ── The cog ────────────────────────────────────────────────────────────────
class Economy(commands.Cog, name="Economy"):
    """THE STRIP: wallets, earning, the cash cage, and the casino."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.rng = random.Random()
        self._chat_at: dict[tuple[int, int], float] = {}
        self.icons: dict[str, str] = dict(EMOJI_FALLBACK)
        self._emoji_task: asyncio.Task | None = None
        self.shoes: dict[int, Shoe] = {}                       # one blackjack shoe per server
        self.tables: dict[tuple[int, int], discord.ui.View] = {}  # live games by (guild, player)

    # -- custom emojis --
    def e(self, kind: str) -> str:
        return self.icons.get(kind) or EMOJI_FALLBACK.get(kind, "")

    def money(self, amount: int, kind: str = "cash") -> str:
        return f"{self.e(kind)} **{int(amount):,}**"

    def partial(self, kind: str) -> discord.PartialEmoji | str:
        value = self.e(kind)
        return discord.PartialEmoji.from_str(value) if value.startswith("<") else value

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._emoji_task is None:
            self._emoji_task = asyncio.create_task(self.sync_emojis())

    async def sync_emojis(self) -> None:
        """Upload SYKE's economy emoji set as application emojis (once; reused afterwards)."""
        try:
            existing = {e.name: e for e in await self.bot.fetch_application_emojis()}
            for kind in art.EMOJI_NAMES:
                name = EMOJI_PREFIX + kind
                emoji = existing.get(name)
                if emoji is None:
                    data = await asyncio.to_thread(art.emoji_icon, kind)
                    emoji = await self.bot.create_application_emoji(name=name, image=data)
                self.icons[kind] = str(emoji)
            log.info("Economy emojis ready (%d)", len(art.EMOJI_NAMES))
        except Exception:
            log.warning("Couldn't sync the economy's custom emojis; using standard ones", exc_info=True)

    async def cog_unload(self) -> None:
        # Never strand chips on a table: refund anything still in play.
        for view in list(self.tables.values()):
            with contextlib.suppress(Exception):
                view.refund()
        self.tables.clear()
        if self._emoji_task is not None:
            self._emoji_task.cancel()

    # -- helpers --
    def _open(self, guild_id: int) -> bool:
        return bool(self.bot.storage.get_config(guild_id, CONFIG_KEY).get("open"))

    def house(self) -> dict:
        """The owner's global settings (bans, casino maintenance)."""
        return self.bot.storage.get_config(0, HOUSE_KEY)

    def banned(self, user_id: int) -> bool:
        return user_id in (self.house().get("banned") or [])

    def happy(self, guild_id: int) -> float:
        """Earnings multiplier while a happy hour is running in this server (1.0 otherwise)."""
        cfg = self.bot.storage.get_config(guild_id, CONFIG_KEY)
        if cfg.get("happy_until", 0) > time.time():
            return float(cfg.get("happy_mult", 1.0))
        return 1.0

    def buffs(self, guild_id: int, user_id: int) -> dict[str, float]:
        return self.bot.storage.econ_buffs(guild_id, user_id, time.time())

    async def _free(self, ctx, w: dict) -> bool:
        """Jailed players can't earn or gamble. Replies and returns False if they're locked up."""
        if w.get("jail_until", 0) > time.time():
            await ctx.reply(embed=embed("🚔 You're in jail",
                                        f"Out <t:{int(w['jail_until'])}:R>. Post bail with `{self.p(ctx)}bail`, "
                                        "or get a friend to.", WARN), ephemeral=True)
            return False
        return True

    async def _casino(self, ctx, w: dict) -> bool:
        """Shared gate for casino games: casino not under maintenance, player not jailed."""
        if self.house().get("maintenance"):
            await ctx.reply(embed=embed("🚧 Casino closed for maintenance",
                                        "The tables are being polished. Earning still works — back soon.", WARN),
                            ephemeral=True)
            return False
        return await self._free(ctx, w)

    def p(self, ctx) -> str:
        return self.bot.display_prefix(ctx.guild.id)

    def casino_embed(self, title: str, body: str, colour, image: discord.File | None = None) -> discord.Embed:
        e = embed(title, body, colour)
        e.set_author(name="THE STRIP  •  CASINO")
        if image is not None:
            e.set_image(url=f"attachment://{image.filename}")
        return e

    async def send(self, ctx, e: discord.Embed, image: discord.File | None = None, view=None):
        kwargs = {"embed": e}
        if image is not None:
            kwargs["file"] = image
        if view is not None:
            kwargs["view"] = view
        return await ctx.reply(**kwargs)

    async def _guard(self, ctx) -> dict | None:
        """Shared entry for player commands: ensures the Strip is open and returns the caller's
        wallet (seeding starting cash on first touch). Replies and returns None if closed."""
        if ctx.guild is None:
            await ctx.reply(embed=embed("Servers only", "The Strip only runs inside a server.",
                                        WARN), ephemeral=True)
            return None
        if not self._open(ctx.guild.id):
            await ctx.reply(embed=embed(f"{self.e('slots')} The Strip is dark",
                                        f"The neon's off here. An admin can light it up with "
                                        f"`{self.p(ctx)}economy on`.", WARN), ephemeral=True)
            return None
        if self.banned(ctx.author.id):
            await ctx.reply(embed=embed("🚫 You've been 86'd",
                                        "The House has banned you from the Strip.", WARN), ephemeral=True)
            return None
        return self.bot.storage.econ_touch(ctx.guild.id, ctx.author.id, STARTING_CASH)

    def _net(self, w: dict) -> int:
        return w["cash"] + w["bank"] + w["chips"]

    async def _place_bet(self, ctx, w: dict, text: str) -> tuple[int, int] | None:
        """Validate and take a bet from chips (topped up from cash at the free cage).
        Returns (bet, cash_converted) — or replies with the problem and returns None."""
        bankroll = w["chips"] + w["cash"]
        value = parse_amount(text, min(bankroll, MAX_BET))
        problem = None
        if value is None:
            problem = (f"Bet a number of chips — like `{self.p(ctx)}slots 100` — or `all` / `half`."
                       if bankroll else "You're broke! Grab your `daily` or clock a `work` shift first.")
        elif value < MIN_BET:
            problem = f"The minimum bet is {self.money(MIN_BET, 'chip')}."
        elif value > MAX_BET:
            problem = f"The table limit is {self.money(MAX_BET, 'chip')}."
        elif value > bankroll:
            problem = (f"You've only got {self.money(w['chips'], 'chip')} chips and {self.money(w['cash'])} "
                       f"cash on you.")
        if problem is None:
            staked = self.bot.storage.econ_stake(ctx.guild.id, ctx.author.id, value)
            if staked is None:
                problem = "You can't cover that bet right now."
            else:
                return value, staked[1]
        await ctx.reply(embed=embed("Bad bet", problem, WARN), ephemeral=True)
        return None

    def _cage_note(self, converted: int) -> str:
        return (f"\n*The cage swapped {self.money(converted)} cash into chips for this bet — no fee.*"
                if converted else "")

    def _pay(self, guild_id: int, user_id: int, chips: int) -> dict:
        return self.bot.storage.econ_adjust(guild_id, user_id, chips=chips)

    # -- balance --
    @commands.hybrid_command(name="balance", with_app_command=False, aliases=["bal", "wallet"],
                             description="Your Players Club card: cash, chips, bank and net worth")
    @app_commands.describe(member="Whose card to check (defaults to you)")
    async def balance(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        who = member or ctx.author
        if member is not None and not member.bot:
            w = self.bot.storage.econ_wallet(ctx.guild.id, member.id)
        rank = self.bot.storage.econ_rank(ctx.guild.id, who.id)
        avatar = None
        with contextlib.suppress(Exception):
            asset = who.display_avatar.with_size(256)
            avatar = await asyncio.wait_for(asset.read(), timeout=4)
        image = await render(art.players_card, name=who.display_name, cash=w["cash"], chips=w["chips"],
                             bank=w["bank"], rank=rank, member_no=int(f"{ctx.guild.id % 10**6}{who.id % 10**10}"),
                             avatar=avatar, streak=w["streak"], filename="players-club.png")
        tier = art.tier_for(self._net(w))[0].title()
        body = (f"{self.money(w['cash'])} cash · {self.money(w['chips'], 'chip')} chips · "
                f"{self.money(w['bank'], 'bank')} banked\nNet worth {self.money(self._net(w))} · **{tier}** member")
        e = embed(f"{who.display_name}'s Players Club card", body, BRAND)
        if image is not None:
            e.set_image(url=f"attachment://{image.filename}")
        await self.send(ctx, e, image)

    # -- daily --
    @commands.hybrid_command(name="daily", with_app_command=False, description="Claim your daily cash (build a streak for more)")
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
        amount = int(daily_reward(streak) * self.happy(ctx.guild.id))
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, cash=amount,
                                     daily_at=now, streak=streak)
        body = f"You picked up {self.money(amount)}."
        if streak > 1:
            body += f"\n🔥 **{streak}-day streak** — keep it going for bigger payouts."
        await ctx.reply(embed=embed(f"{self.e('cash')} Daily collected", body, OK))

    # -- work --
    @commands.hybrid_command(name="work", with_app_command=False, aliases=["shift"], description="Clock a shift on the Strip for cash")
    async def work(self, ctx: commands.Context) -> None:
        w = await self._guard(ctx)
        if w is None or not await self._free(ctx, w):
            return
        now = time.time()
        if w["work_at"] and now - w["work_at"] < WORK_COOLDOWN:
            ready = int(w["work_at"] + WORK_COOLDOWN)
            await ctx.reply(embed=embed("On the clock already",
                                        f"You're beat. Next shift <t:{ready}:R>.", WARN),
                            ephemeral=True)
            return
        did, base = work_shift(self.rng)
        from .stripworld import WORK_BUFFS
        boost = self.happy(ctx.guild.id)
        perks = []
        for buff in self.buffs(ctx.guild.id, ctx.author.id):
            if buff in WORK_BUFFS:
                boost *= WORK_BUFFS[buff]
                perks.append(buff)
        amount = int(base * boost)
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, cash=amount, work_at=now)
        extra = f" *(×{boost:.2f} boost)*" if boost > 1 else ""
        await ctx.reply(embed=embed("Shift done 🧾", f"You {did} and earned {self.money(amount)}.{extra}", OK))

    # -- hustle --
    @commands.hybrid_command(name="hustle", with_app_command=False, aliases=["grind"],
                             description="Try a risky hustle — could pay off, could backfire")
    async def hustle_cmd(self, ctx: commands.Context) -> None:
        w = await self._guard(ctx)
        if w is None or not await self._free(ctx, w):
            return
        now = time.time()
        buffs = self.buffs(ctx.guild.id, ctx.author.id)
        cooldown = HUSTLE_COOLDOWN / (2 if "chill" in buffs else 1)
        if w["hustle_at"] and now - w["hustle_at"] < cooldown:
            ready = int(w["hustle_at"] + cooldown)
            await ctx.reply(embed=embed("Lay low a bit",
                                        f"Too hot right now. Try again <t:{ready}:R>.", WARN),
                            ephemeral=True)
            return
        delta, flavor = hustle(self.rng, luck=0.15 if "lucky" in buffs else 0.0, safe="foresight" in buffs)
        if delta > 0:
            delta = int(delta * self.happy(ctx.guild.id))
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, cash=delta, hustle_at=now)
        colour = OK if delta > 0 else WARN
        await ctx.reply(embed=embed("Hustle", flavor.replace(CASH, self.e("cash")), colour))

    # -- pay --
    @commands.hybrid_command(name="pay", with_app_command=False, aliases=["give"], description="Send cash to someone (small tax applies)")
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
            await ctx.reply(embed=embed("Not enough cash", f"You only have {self.money(w['cash'])}.", WARN),
                            ephemeral=True)
            return
        tax = math.ceil(value * PAY_TAX)
        self.bot.storage.econ_touch(ctx.guild.id, member.id, STARTING_CASH)
        if not self.bot.storage.econ_transfer(ctx.guild.id, ctx.author.id, member.id, value, tax):
            await ctx.reply(embed=embed("Not enough cash", f"You only have {self.money(w['cash'])}.", WARN),
                            ephemeral=True)
            return
        await ctx.reply(embed=embed(f"{self.e('cash')} Paid",
                                    f"{ctx.author.mention} → {member.mention}: {self.money(value - tax)} "
                                    f"(after {self.money(tax)} tax).", OK))

    # -- bank --
    @commands.hybrid_command(name="deposit", with_app_command=False, aliases=["dep"], description="Move cash into your bank")
    @app_commands.describe(amount="How much (a number, or 'all'/'half')")
    async def deposit(self, ctx: commands.Context, amount: str) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        value = parse_amount(amount, w["cash"])
        if value is None or value <= 0 or value > w["cash"]:
            await ctx.reply(embed=embed("Can't deposit that", f"You have {self.money(w['cash'])} on hand.",
                                        WARN), ephemeral=True)
            return
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, cash=-value, bank=value)
        await ctx.reply(embed=embed(f"{self.e('bank')} Deposited", f"Banked {self.money(value)}.", OK))

    @commands.hybrid_command(name="withdraw", with_app_command=False, aliases=["wd"], description="Take cash out of your bank")
    @app_commands.describe(amount="How much (a number, or 'all'/'half')")
    async def withdraw(self, ctx: commands.Context, amount: str) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        value = parse_amount(amount, w["bank"])
        if value is None or value <= 0 or value > w["bank"]:
            await ctx.reply(embed=embed("Can't withdraw that", f"You have {self.money(w['bank'], 'bank')} "
                                        "banked.", WARN), ephemeral=True)
            return
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, cash=value, bank=-value)
        await ctx.reply(embed=embed(f"{self.e('cash')} Withdrew", f"Took out {self.money(value)}.", OK))

    # -- the cash cage: cash <-> chips, free --
    @commands.hybrid_command(name="chips", with_app_command=False, aliases=["cage", "buychips"],
                             description="The cash cage: swap cash for chips (free) — e.g. chips 500")
    @app_commands.describe(action="An amount of cash to swap for chips — or 'buy'/'cash' then an amount",
                           amount="The amount, when you picked buy or cash")
    async def chips(self, ctx: commands.Context, action: str | None = None, amount: str | None = None) -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        act = (action or "").strip().lower()
        if not act:
            p = self.p(ctx)
            await ctx.reply(embed=embed(f"{self.e('chip')} The cash cage",
                                        f"You hold {self.money(w['cash'])} cash and "
                                        f"{self.money(w['chips'], 'chip')} chips.\n\n"
                                        f"`{p}chips 500` — swap cash for chips\n"
                                        f"`{p}cashout 500` — swap chips back to cash\n\n"
                                        "The cage is **free** — 1 cash = 1 chip, both ways. You don't even "
                                        "need to stop here: any bet tops itself up from your cash.", BRAND))
            return
        if act in ("cash", "sell", "out", "cashout"):
            await self._cash_out(ctx, w, amount or "all")
            return
        text = amount if act in ("buy", "in") else action
        value = parse_amount(text or "", w["cash"])
        if value is None or value > w["cash"]:
            await ctx.reply(embed=embed("Can't swap that", f"You have {self.money(w['cash'])} cash.", WARN),
                            ephemeral=True)
            return
        got, _ = convert(value)
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, cash=-value, chips=got)
        await ctx.reply(embed=embed(f"{self.e('chip')} Chips bought",
                                    f"{self.money(value)} cash → {self.money(got, 'chip')} chips. No fee.", OK))

    @commands.hybrid_command(name="cashout", with_app_command=False, aliases=["cashin"], description="Swap your chips back into cash (free)")
    @app_commands.describe(amount="How many chips (a number, or 'all'/'half'; default all)")
    async def cashout(self, ctx: commands.Context, amount: str = "all") -> None:
        w = await self._guard(ctx)
        if w is None:
            return
        await self._cash_out(ctx, w, amount)

    async def _cash_out(self, ctx, w: dict, text: str) -> None:
        value = parse_amount(text, w["chips"])
        if value is None or value > w["chips"]:
            await ctx.reply(embed=embed("Nothing to cash out", f"You have {self.money(w['chips'], 'chip')} chips.",
                                        WARN), ephemeral=True)
            return
        got, _ = convert(value)
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, chips=-value, cash=got)
        await ctx.reply(embed=embed(f"{self.e('cash')} Cashed out",
                                    f"{self.money(value, 'chip')} chips → {self.money(got)} cash. No fee.", OK))

    # -- casino: quick games --
    @commands.hybrid_command(name="slots", with_app_command=False, aliases=["slot"], description="Pull the neon slots")
    @app_commands.describe(bet="How many chips to bet (number, or 'all'/'half')")
    async def slots(self, ctx: commands.Context, bet: str) -> None:
        w = await self._guard(ctx)
        if w is None or not await self._casino(ctx, w):
            return
        placed = await self._place_bet(ctx, w, bet)
        if placed is None:
            return
        value, converted = placed
        reels = slot_spin(self.rng)
        mult = slot_multiplier(reels)
        won = value * mult
        after = self._pay(ctx.guild.id, ctx.author.id, won)
        image = await render(art.slots_machine, reels, bet=value, won=won, multiplier=mult, filename="slots.png")
        line = " ".join(reels)
        if won > value:
            body, colour = f"{line}  —  **×{mult}**! You win {self.money(won - value, 'chip')}.", OK
        elif won == value:
            body, colour = f"{line}  —  push, your {self.money(value, 'chip')} comes back.", BRAND
        else:
            body, colour = f"{line}  —  the Strip keeps your {self.money(value, 'chip')}.", WARN
        body += f"\nChips: {self.money(after['chips'], 'chip')}{self._cage_note(converted)}"
        await self.send(ctx, self.casino_embed(f"{self.e('slots')} Slots", body, colour, image), image)

    @commands.hybrid_command(name="flip", with_app_command=False, aliases=["coinflip", "cf"],
                             description="Coinflip for chips — heads or tails (pays 1.95×)")
    @app_commands.describe(side="heads or tails", bet="How many chips to bet")
    async def flip(self, ctx: commands.Context, side: str, bet: str) -> None:
        w = await self._guard(ctx)
        if w is None or not await self._casino(ctx, w):
            return
        pick = side.strip().lower()
        if pick in ("h", "head", "heads"):
            pick = "heads"
        elif pick in ("t", "tail", "tails"):
            pick = "tails"
        else:
            await ctx.reply(embed=embed("Heads or tails?", f"Try `{self.p(ctx)}flip heads 100`.", WARN),
                            ephemeral=True)
            return
        placed = await self._place_bet(ctx, w, bet)
        if placed is None:
            return
        value, converted = placed
        result = self.rng.choice(["heads", "tails"])
        won = int(value * COINFLIP_MULT) if result == pick else 0
        after = self._pay(ctx.guild.id, ctx.author.id, won)
        image = await render(art.coin_flip, result, pick=pick, bet=value, won=won, filename="coinflip.png")
        if won:
            body, colour = f"**{result.title()}** — you called it, up {self.money(won - value, 'chip')}.", OK
        else:
            body, colour = f"**{result.title()}** — wrong call, down {self.money(value, 'chip')}.", WARN
        body += f"\nChips: {self.money(after['chips'], 'chip')}{self._cage_note(converted)}"
        await self.send(ctx, self.casino_embed(f"{self.e('coin')} Coin flip", body, colour, image), image)

    @commands.hybrid_command(name="roulette", with_app_command=False, aliases=["roul"],
                             description="Spin the wheel: a number (0-36) or red/black/even/odd/low/high")
    @app_commands.describe(space="A number 0-36, or red/black/even/odd/low/high", bet="Chips to bet")
    async def roulette(self, ctx: commands.Context, space: str, bet: str) -> None:
        w = await self._guard(ctx)
        if w is None or not await self._casino(ctx, w):
            return
        if not valid_roulette_space(space):
            await ctx.reply(embed=embed("Pick a space",
                                        "Bet a number `0`-`36`, or `red`/`black`/`even`/`odd`/`low`/`high`.",
                                        WARN), ephemeral=True)
            return
        placed = await self._place_bet(ctx, w, bet)
        if placed is None:
            return
        value, converted = placed
        number = roulette_spin(self.rng)
        mult = roulette_multiplier(space, number)
        won = value * mult
        after = self._pay(ctx.guild.id, ctx.author.id, won)
        image = await render(art.roulette_wheel, number, space=space.strip().lower(), bet=value, won=won,
                             red=ROULETTE_RED, filename="roulette.png")
        colour_name = "red" if number in ROULETTE_RED else ("green" if number == 0 else "black")
        head = f"The ball lands on **{number} {colour_name}**."
        if won > value:
            body, tone = f"{head} You win {self.money(won - value, 'chip')} (×{mult}).", OK
        else:
            body, tone = f"{head} Down {self.money(value, 'chip')}.", WARN
        body += f"\nChips: {self.money(after['chips'], 'chip')}{self._cage_note(converted)}"
        await self.send(ctx, self.casino_embed(f"{self.e('roulette')} Roulette", body, tone, image), image)

    # -- casino: blackjack --
    def _busy(self, ctx) -> bool:
        return (ctx.guild.id, ctx.author.id) in self.tables

    async def _already_playing(self, ctx) -> None:
        await ctx.reply(embed=embed("You're already at a table",
                                    "Finish the hand you're playing first.", WARN), ephemeral=True)

    def shoe_for(self, guild_id: int) -> tuple[Shoe, bool]:
        """The server's shoe, reshuffled between rounds once the cut card has come out."""
        shoe = self.shoes.get(guild_id)
        if shoe is None:
            shoe = self.shoes[guild_id] = Shoe(rng=self.rng)
            return shoe, True
        if shoe.needs_shuffle:
            shoe.shuffle()
            return shoe, True
        return shoe, False

    @commands.hybrid_command(name="blackjack", with_app_command=False, aliases=["bj", "21"],
                             description="Sit down at the blackjack table — real Vegas Strip rules")
    @app_commands.describe(bet="How many chips to bet (number, or 'all'/'half')")
    async def blackjack(self, ctx: commands.Context, bet: str) -> None:
        w = await self._guard(ctx)
        if w is None or not await self._casino(ctx, w):
            return
        if self._busy(ctx):
            await self._already_playing(ctx)
            return
        placed = await self._place_bet(ctx, w, bet)
        if placed is None:
            return
        value, converted = placed
        view = BlackjackView(self, ctx.guild.id, ctx.author, value)
        self.tables[(ctx.guild.id, ctx.author.id)] = view   # before dealing: a natural settles (and frees it)
        view.start_round(converted)
        e, image = await view.frame()
        view.message = await self.send(ctx, e, image, view)

    # -- casino: hi-lo --
    @commands.hybrid_command(name="hilo", with_app_command=False, aliases=["hl", "higherlower"],
                             description="Higher or lower? Build a multiplier, cash out before you miss")
    @app_commands.describe(bet="How many chips to bet (number, or 'all'/'half')")
    async def hilo(self, ctx: commands.Context, bet: str) -> None:
        w = await self._guard(ctx)
        if w is None or not await self._casino(ctx, w):
            return
        if self._busy(ctx):
            await self._already_playing(ctx)
            return
        placed = await self._place_bet(ctx, w, bet)
        if placed is None:
            return
        value, converted = placed
        view = HiLoView(self, ctx.guild.id, ctx.author, HiLo(value, self.rng), converted)
        self.tables[(ctx.guild.id, ctx.author.id)] = view
        e, image = await view.frame()
        view.message = await self.send(ctx, e, image, view)

    # -- leaderboard --
    @commands.hybrid_command(name="rich", with_app_command=False, aliases=["baltop", "richest"],
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
            tier = art.tier_for(row["net"])[0].title()
            lines.append(f"{tag} <@{row['user_id']}> — {self.money(row['net'])} · {tier}")
        await ctx.reply(embed=embed(f"{self.e('crown')} Richest on the Strip", "\n".join(lines), BRAND))

    # -- admin --
    @commands.hybrid_group(name="economy", fallback="status", case_insensitive=True,
                           description="Admin: open/close the Strip and manage wallets")
    @moderator("manage_guild")
    async def economy(self, ctx: commands.Context) -> None:
        state = "**open** 🟢" if self._open(ctx.guild.id) else "**closed** 🔴"
        circ = self.bot.storage.econ_circulation(ctx.guild.id)
        await ctx.reply(embed=embed(f"{self.e('slots')} The Strip — {state}",
                                    f"{circ['players']} players · {self.money(circ['cash'])} cash · "
                                    f"{self.money(circ['chips'], 'chip')} chips · "
                                    f"{self.money(circ['bank'], 'bank')} banked\n\n"
                                    f"`{self.p(ctx)}economy on` / `off` · `give` · `take` · `reset`", BRAND),
                        ephemeral=True)

    @economy.command(name="on", aliases=["open"], description="Open the Strip in this server")
    @moderator("manage_guild")
    async def economy_on(self, ctx: commands.Context) -> None:
        self.bot.storage.update_config(ctx.guild.id, CONFIG_KEY, open=True)
        p = self.p(ctx)
        await ctx.reply(embed=embed(f"{self.e('slots')} The Strip is OPEN",
                                    f"The neon's on. Everyone starts with {self.money(STARTING_CASH)}.\n"
                                    f"**Earn** `{p}daily` `{p}work` `{p}hustle` · **Casino** `{p}blackjack` "
                                    f"`{p}roulette` `{p}slots` `{p}hilo`\n**Shop** `{p}shop` · **Crime** `{p}rob` "
                                    f"`{p}crime` `{p}heist` · **Property** `{p}property` — own a storefront.", OK))

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
        kind = {"cash": "cash", "chips": "chip", "bank": "bank"}[cur]
        await ctx.reply(embed=embed("Done", f"{verb} {member.mention} {self.money(abs(amount), kind)} {cur}.", OK))

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

    # -- the House: SYKE owners run the economy everywhere --
    def _where(self, ctx, server: str | None) -> int | None:
        if server and server.strip().isdigit():
            return int(server)
        return ctx.guild.id if ctx.guild else None

    def _server_name(self, gid: int) -> str:
        g = self.bot.get_guild(gid)
        return f"**{g.name}**" if g else f"server `{gid}`"

    async def _need_server(self, ctx) -> None:
        await ctx.reply(embed=embed("Which server?", "You're in DMs — add the server ID at the end.", WARN),
                        ephemeral=True)

    @commands.hybrid_group(name="house", fallback="status", case_insensitive=True,
                           description="SYKE owners: run the economy in every server")
    @app_commands.default_permissions(administrator=True)
    @house_only()
    async def house_cmd(self, ctx: commands.Context) -> None:
        g = self.bot.storage.econ_global()
        h = self.house()
        opened = sum(1 for cfg in self.bot.storage.configs(CONFIG_KEY).values() if cfg.get("open"))
        lines = [f"**{g['players']:,}** players in **{g['servers']}** servers · Strip open in **{opened}**",
                 f"{self.money(g['cash'])} cash · {self.money(g['chips'], 'chip')} chips · "
                 f"{self.money(g['bank'], 'bank')} banked",
                 f"Casino: {'🚧 **maintenance**' if h.get('maintenance') else '🟢 open'} · "
                 f"86'd players: **{len(h.get('banned') or [])}**"]
        if ctx.guild:
            mult = self.happy(ctx.guild.id)
            lines.append(f"Here: {'🟢 open' if self._open(ctx.guild.id) else '🔴 closed'}"
                         + (f" · 🍸 happy hour ×{mult:g}" if mult > 1 else ""))
        p = self.p(ctx) if ctx.guild else "!"
        lines.append(f"\n`{p}house give/take/set/peek/reset <user>` · `open`/`close`/`wipe` · `ban`/`unban` · "
                     f"`jail`/`free` · `happyhour <x> [hours]` · `maintenance on/off`\n"
                     "*Add a server ID at the end to act on another server.*")
        await ctx.reply(embed=embed("🎩 The House", "\n".join(lines), BRAND), ephemeral=True)

    @house_cmd.command(name="give", description="Add cash, chips or bank to anyone's wallet")
    @app_commands.describe(user="Who", amount="How much (negative takes)", currency="cash, chips or bank",
                           server="Server ID (defaults to this one)")
    @house_only()
    async def house_give(self, ctx: commands.Context, user: discord.User, amount: int, currency: str = "cash",
                         server: str | None = None) -> None:
        gid = self._where(ctx, server)
        cur = currency.strip().lower()
        cur = "chips" if cur.startswith("chip") else cur
        if gid is None:
            await self._need_server(ctx)
            return
        if cur not in ("cash", "chips", "bank") or amount == 0:
            await ctx.reply(embed=embed("Check that", "Currency is `cash`, `chips` or `bank`; amount can't be 0.",
                                        WARN), ephemeral=True)
            return
        self.bot.storage.econ_touch(gid, user.id, STARTING_CASH)
        w = self.bot.storage.econ_adjust(gid, user.id, **{cur: amount})
        kind = {"cash": "cash", "chips": "chip", "bank": "bank"}[cur]
        await ctx.reply(embed=embed("🎩 Done", f"{'Gave' if amount > 0 else 'Took'} {user.mention} "
                                    f"{self.money(abs(amount), kind)} {cur} in {self._server_name(gid)}. "
                                    f"They now have {self.money(w[cur], kind)}.", OK), ephemeral=True)

    @house_cmd.command(name="take", description="Remove cash, chips or bank from anyone's wallet")
    @app_commands.describe(user="Who", amount="How much", currency="cash, chips or bank",
                           server="Server ID (defaults to this one)")
    @house_only()
    async def house_take(self, ctx: commands.Context, user: discord.User, amount: int, currency: str = "cash",
                         server: str | None = None) -> None:
        await self.house_give.callback(self, ctx, user, -abs(amount), currency, server)

    @house_cmd.command(name="set", description="Set someone's cash, chips or bank to an exact amount")
    @app_commands.describe(user="Who", currency="cash, chips or bank", amount="New balance",
                           server="Server ID (defaults to this one)")
    @house_only()
    async def house_set(self, ctx: commands.Context, user: discord.User, currency: str, amount: int,
                        server: str | None = None) -> None:
        gid = self._where(ctx, server)
        cur = currency.strip().lower()
        cur = "chips" if cur.startswith("chip") else cur
        if gid is None:
            await self._need_server(ctx)
            return
        if cur not in ("cash", "chips", "bank") or amount < 0:
            await ctx.reply(embed=embed("Check that", "Currency is `cash`, `chips` or `bank`; amount ≥ 0.", WARN),
                            ephemeral=True)
            return
        self.bot.storage.econ_touch(gid, user.id, STARTING_CASH)
        self.bot.storage.econ_set(gid, user.id, **{cur: amount})
        await ctx.reply(embed=embed("🎩 Set", f"{user.mention}'s {cur} is now **{amount:,}** in "
                                    f"{self._server_name(gid)}.", OK), ephemeral=True)

    @house_cmd.command(name="peek", description="Look at anyone's wallet, items and timers")
    @app_commands.describe(user="Who", server="Server ID (defaults to this one)")
    @house_only()
    async def house_peek(self, ctx: commands.Context, user: discord.User, server: str | None = None) -> None:
        gid = self._where(ctx, server)
        if gid is None:
            await self._need_server(ctx)
            return
        w = self.bot.storage.econ_wallet(gid, user.id)
        items = self.bot.storage.econ_items(gid, user.id)
        now = time.time()
        lines = [f"{self.money(w['cash'])} cash · {self.money(w['chips'], 'chip')} chips · "
                 f"{self.money(w['bank'], 'bank')} bank · net **{self._net(w):,}**",
                 f"Rank #{self.bot.storage.econ_rank(gid, user.id) or '—'} · streak {w['streak']}"]
        if w["jail_until"] > now:
            lines.append(f"🚔 Jailed until <t:{int(w['jail_until'])}:t>")
        if items:
            lines.append("Items: " + ", ".join(f"{k}×{n}" for k, n in items.items()))
        if self.banned(user.id):
            lines.append("🚫 86'd from the Strip")
        await ctx.reply(embed=embed(f"🎩 {user.display_name} in {self._server_name(gid).strip('*')}",
                                    "\n".join(lines), BRAND), ephemeral=True)

    @house_cmd.command(name="reset", description="Wipe one player's wallet")
    @app_commands.describe(user="Who", server="Server ID (defaults to this one)")
    @house_only()
    async def house_reset(self, ctx: commands.Context, user: discord.User, server: str | None = None) -> None:
        gid = self._where(ctx, server)
        if gid is None:
            await self._need_server(ctx)
            return
        self.bot.storage.econ_reset(gid, user.id)
        await ctx.reply(embed=embed("🎩 Reset", f"{user.mention}'s wallet in {self._server_name(gid)} is wiped.",
                                    WARN), ephemeral=True)

    @house_cmd.command(name="wipe", description="Wipe a whole server's economy (wallets, items, property)")
    @app_commands.describe(confirm="Type 'confirm' to really do it", server="Server ID (defaults to this one)")
    @house_only()
    async def house_wipe(self, ctx: commands.Context, confirm: str = "", server: str | None = None) -> None:
        gid = self._where(ctx, server)
        if gid is None:
            await self._need_server(ctx)
            return
        if confirm.strip().lower() != "confirm":
            await ctx.reply(embed=embed("Are you sure?", f"This erases every wallet, item and storefront in "
                                        f"{self._server_name(gid)}. Run it again with `confirm`.", WARN),
                            ephemeral=True)
            return
        n = self.bot.storage.econ_reset_guild(gid)
        shops = self.bot.storage.econ_reset_shops(gid)
        await ctx.reply(embed=embed("🎩 Wiped", f"Cleared {n} wallet(s) and {shops} storefront(s) in "
                                    f"{self._server_name(gid)}.", WARN), ephemeral=True)

    @house_cmd.command(name="open", description="Open the Strip in a server")
    @app_commands.describe(server="Server ID (defaults to this one)")
    @house_only()
    async def house_open(self, ctx: commands.Context, server: str | None = None) -> None:
        await self._house_toggle(ctx, server, True)

    @house_cmd.command(name="close", description="Close the Strip in a server (balances kept)")
    @app_commands.describe(server="Server ID (defaults to this one)")
    @house_only()
    async def house_close(self, ctx: commands.Context, server: str | None = None) -> None:
        await self._house_toggle(ctx, server, False)

    async def _house_toggle(self, ctx, server: str | None, state: bool) -> None:
        gid = self._where(ctx, server)
        if gid is None:
            await self._need_server(ctx)
            return
        self.bot.storage.update_config(gid, CONFIG_KEY, open=state)
        await ctx.reply(embed=embed("🎩 Done", f"The Strip is now {'open 🟢' if state else 'closed 🔴'} in "
                                    f"{self._server_name(gid)}.", OK), ephemeral=True)

    @house_cmd.command(name="ban", aliases=["86"], description="86 someone: ban them from the Strip everywhere")
    @app_commands.describe(user="Who to ban")
    @house_only()
    async def house_ban(self, ctx: commands.Context, user: discord.User) -> None:
        banned = list(self.house().get("banned") or [])
        if user.id not in banned:
            banned.append(user.id)
        self.bot.storage.update_config(0, HOUSE_KEY, banned=banned)
        await ctx.reply(embed=embed("🚫 86'd", f"{user.mention} is banned from the Strip in every server.", WARN),
                        ephemeral=True)

    @house_cmd.command(name="unban", description="Let an 86'd player back on the Strip")
    @app_commands.describe(user="Who to let back in")
    @house_only()
    async def house_unban(self, ctx: commands.Context, user: discord.User) -> None:
        banned = [u for u in (self.house().get("banned") or []) if u != user.id]
        self.bot.storage.update_config(0, HOUSE_KEY, banned=banned)
        await ctx.reply(embed=embed("🎩 Welcome back", f"{user.mention} can play again.", OK), ephemeral=True)

    @house_cmd.command(name="jail", description="Throw someone in jail for N minutes")
    @app_commands.describe(user="Who", minutes="How long", server="Server ID (defaults to this one)")
    @house_only()
    async def house_jail(self, ctx: commands.Context, user: discord.User, minutes: int,
                         server: str | None = None) -> None:
        gid = self._where(ctx, server)
        if gid is None:
            await self._need_server(ctx)
            return
        until = time.time() + max(1, minutes) * 60
        self.bot.storage.econ_touch(gid, user.id, STARTING_CASH)
        self.bot.storage.econ_adjust(gid, user.id, jail_until=until)
        await ctx.reply(embed=embed("🚔 Locked up", f"{user.mention} is jailed until <t:{int(until)}:t>.", WARN),
                        ephemeral=True)

    @house_cmd.command(name="free", description="Release someone from jail")
    @app_commands.describe(user="Who", server="Server ID (defaults to this one)")
    @house_only()
    async def house_free(self, ctx: commands.Context, user: discord.User, server: str | None = None) -> None:
        gid = self._where(ctx, server)
        if gid is None:
            await self._need_server(ctx)
            return
        self.bot.storage.econ_adjust(gid, user.id, jail_until=0)
        await ctx.reply(embed=embed("🔓 Released", f"{user.mention} walks free.", OK), ephemeral=True)

    @house_cmd.command(name="happyhour", aliases=["boost"], description="Multiply everyone's earnings for a while")
    @app_commands.describe(multiplier="e.g. 2 for double (1 ends it)", hours="How long (default 1)",
                           server="Server ID (defaults to this one)")
    @house_only()
    async def house_happy(self, ctx: commands.Context, multiplier: float, hours: float = 1.0,
                          server: str | None = None) -> None:
        gid = self._where(ctx, server)
        if gid is None:
            await self._need_server(ctx)
            return
        mult = max(1.0, min(HAPPY_MAX, multiplier))
        if mult <= 1.0:
            self.bot.storage.update_config(gid, CONFIG_KEY, happy_until=None, happy_mult=None)
            await ctx.reply(embed=embed("🍸 Last call", f"Happy hour is over in {self._server_name(gid)}.", OK),
                            ephemeral=True)
            return
        until = time.time() + max(0.1, min(hours, 72)) * 3600
        self.bot.storage.update_config(gid, CONFIG_KEY, happy_until=until, happy_mult=mult)
        await ctx.reply(embed=embed("🍸 Happy hour!", f"Earnings ×{mult:g} in {self._server_name(gid)} until "
                                    f"<t:{int(until)}:t> — daily, work, hustles, crime and chat.", OK))

    @house_cmd.command(name="maintenance", description="Close every casino table (earning keeps working)")
    @app_commands.describe(state="on or off")
    @house_only()
    async def house_maintenance(self, ctx: commands.Context, state: str) -> None:
        on = state.strip().lower() in ("on", "true", "yes", "1", "close", "closed")
        self.bot.storage.update_config(0, HOUSE_KEY, maintenance=on)
        await ctx.reply(embed=embed("🚧 Maintenance on" if on else "🎰 Casino reopened",
                                    "Every casino table is closed." if on else "The tables are live again.",
                                    WARN if on else OK), ephemeral=True)

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
        self.bot.storage.econ_adjust(message.guild.id, message.author.id,
                                     cash=int(CHAT_REWARD * self.happy(message.guild.id)))


# ── interactive tables ─────────────────────────────────────────────────────
class TableView(discord.ui.View):
    """Shared plumbing for a one-player casino table: owner-only buttons, a lock against
    double clicks, and refunds/settlement so chips are never stranded."""

    def __init__(self, cog: Economy, guild_id: int, player, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.player = player
        self.message: discord.Message | None = None
        self.lock = asyncio.Lock()
        self.settled = False
        self.note = ""

    @property
    def key(self) -> tuple[int, int]:
        return (self.guild_id, self.player.id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            p = self.cog.bot.display_prefix(self.guild_id)
            await interaction.response.send_message(
                f"This is {self.player.display_name}'s table — grab your own seat with `{p}blackjack <bet>` "
                f"or `{p}hilo <bet>`.", ephemeral=True)
            return False
        return True

    def button(self, label: str, style: discord.ButtonStyle, action: str, *, emoji=None,
               disabled: bool = False, row: int = 0) -> None:
        b = discord.ui.Button(label=label, style=style, emoji=emoji, disabled=disabled, row=row)

        async def callback(interaction: discord.Interaction, act=action) -> None:
            await self.press(interaction, act)

        b.callback = callback
        self.add_item(b)

    async def press(self, interaction: discord.Interaction, action: str) -> None:
        raise NotImplementedError

    async def frame(self) -> tuple[discord.Embed, discord.File | None]:
        raise NotImplementedError

    async def redraw(self, interaction: discord.Interaction | None = None) -> None:
        e, image = await self.frame()
        kwargs = {"embed": e, "attachments": [image] if image is not None else [], "view": self}
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(**kwargs)
        elif self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(**kwargs)

    def release(self) -> None:
        if self.cog.tables.get(self.key) is self:
            self.cog.tables.pop(self.key, None)

    def refund(self) -> None:
        raise NotImplementedError


class BlackjackView(TableView):
    def __init__(self, cog: Economy, guild_id: int, player, bet: int) -> None:
        super().__init__(cog, guild_id, player, TABLE_TIMEOUT)
        self.bet = bet
        self.game: Blackjack | None = None
        self.shuffled = False

    @property
    def finished(self) -> bool:
        return self.game is not None and self.game.phase == "done"

    def start_round(self, converted: int) -> None:
        shoe, self.shuffled = self.cog.shoe_for(self.guild_id)
        self.game = Blackjack(self.bet, shoe)
        self.game.deal()
        self.settled = False
        self.note = self.cog._cage_note(converted)
        if self.finished:
            self.settle()
        self.rebuild()

    def rebuild(self) -> None:
        self.clear_items()
        g = self.game
        cog = self.cog
        if g.phase == "insurance":
            cost = g.cost("insurance")
            label = "Even money" if g.even_money else f"Insurance ({cost:,})"
            self.button(label, discord.ButtonStyle.success, "insurance", emoji=cog.partial("chip"))
            self.button("No thanks", discord.ButtonStyle.secondary, "no_insurance")
        elif g.phase == "player":
            acts = g.actions()
            self.button("Hit", discord.ButtonStyle.primary, "hit", emoji="👆")
            self.button("Stand", discord.ButtonStyle.secondary, "stand", emoji="✋")
            if "double" in acts:
                self.button(f"Double ({g.cost('double'):,})", discord.ButtonStyle.success, "double",
                            emoji=cog.partial("chip"))
            if "split" in acts:
                self.button(f"Split ({g.cost('split'):,})", discord.ButtonStyle.success, "split", emoji="✂️")
            if "surrender" in acts:
                self.button("Surrender", discord.ButtonStyle.danger, "surrender", emoji="🏳️", row=1)
        else:
            self.button(f"Deal again ({self.bet:,})", discord.ButtonStyle.success, "again", emoji=cog.partial("cards"))

    async def press(self, interaction: discord.Interaction, action: str) -> None:
        async with self.lock:
            g = self.game
            storage = self.cog.bot.storage
            if action == "again":
                if self.cog.tables.get(self.key) not in (None, self):
                    await interaction.response.send_message("Finish your other game first.", ephemeral=True)
                    return
                staked = storage.econ_stake(self.guild_id, self.player.id, self.bet)
                if staked is None:
                    await interaction.response.send_message(
                        f"You need {self.bet:,} chips (or cash) to go again.", ephemeral=True)
                    return
                self.cog.tables[self.key] = self
                self.start_round(staked[1])
                await self.redraw(interaction)
                return
            available = g.actions()
            if action not in available:
                await interaction.response.send_message("That move isn't available right now.", ephemeral=True)
                return
            cost = g.cost(action)
            self.note = ""
            if cost:
                staked = storage.econ_stake(self.guild_id, self.player.id, cost)
                if staked is None:
                    await interaction.response.send_message(
                        f"You need {cost:,} more chips (or cash) to {action.replace('_', ' ')}.", ephemeral=True)
                    return
                self.note = self.cog._cage_note(staked[1])
            if action == "insurance":
                g.decide_insurance(True)
            elif action == "no_insurance":
                g.decide_insurance(False)
            else:
                getattr(g, action)()
            if self.finished:
                self.settle()
            self.rebuild()
            await self.redraw(interaction)

    def settle(self) -> None:
        if self.settled or self.game is None:
            return
        self.settled = True
        if self.game.returned:
            self.cog._pay(self.guild_id, self.player.id, self.game.returned)
        self.release()

    def refund(self) -> None:
        if self.settled or self.game is None:
            return
        self.settled = True
        self.cog._pay(self.guild_id, self.player.id, self.game.wagered)
        self.release()

    async def on_timeout(self) -> None:
        async with self.lock:
            g = self.game
            if g is not None and g.phase != "done":
                if g.phase == "insurance":
                    g.decide_insurance(False)
                while g.phase == "player":
                    g.stand()
                self.settle()
                self.note = "\n*Timed out — remaining hands stood automatically.*"
            self.clear_items()
            self.stop()
            await self.redraw()

    def _summary(self) -> str:
        g = self.game
        cog = self.cog
        if g.phase == "insurance":
            return ("Dealer shows an **Ace**. You have **blackjack** — take **even money** (a guaranteed 1:1 "
                    "payout) or play it out?" if g.even_money else
                    f"Dealer shows an **Ace**. Insurance costs {cog.money(g.cost('insurance'), 'chip')} and pays 2:1 "
                    "if the dealer has blackjack.")
        if g.phase == "player":
            h = g.hand
            where = f"Hand {g.active + 1} of {len(g.hands)} · " if len(g.hands) > 1 else ""
            soft = " (soft)" if h.soft and h.total < 21 else ""
            return f"{where}You have **{h.total}{soft}** against the dealer's **{g.upcard}**. Your move."
        lines = []
        for i, h in enumerate(g.hands, 1):
            net = h.payout - h.bet
            label = {"blackjack": "Blackjack!", "win": "Win", "push": "Push", "lose": "Lose", "bust": "Bust",
                     "surrender": "Surrendered"}[h.outcome]
            prefix = f"**Hand {i}** · " if len(g.hands) > 1 else ""
            amount = f"+{net:,}" if net > 0 else f"{net:,}"
            lines.append(f"{prefix}{h.total} vs dealer {hand_total(g)} · {label} ({amount})")
        if g.insurance_bet:
            ins = g.insurance_payout - g.insurance_bet
            lines.append(f"Insurance · {'paid' if ins > 0 else 'lost'} ({'+' if ins > 0 else ''}{ins:,})")
        net = g.net
        result = (f"You're up {cog.money(net, 'chip')}" if net > 0 else
                  "Push — your bet comes back" if net == 0 else f"You're down {cog.money(-net, 'chip')}")
        chips = cog.bot.storage.econ_wallet(self.guild_id, self.player.id)["chips"]
        return "\n".join(lines) + f"\n\n**{result}.** Chips: {cog.money(chips, 'chip')}"

    async def frame(self) -> tuple[discord.Embed, discord.File | None]:
        g = self.game
        image = await render(art.blackjack_table, g, player_name=self.player.display_name,
                             shoe_left=g.shoe.remaining, filename="blackjack.png")
        if g.phase == "done":
            colour = OK if g.net > 0 else (BRAND if g.net == 0 else WARN)
        else:
            colour = BRAND
        body = self._summary()
        if self.shuffled and g.phase != "done":
            body = "🔀 *Fresh shoe — the dealer shuffled six decks.*\n" + body
        e = self.cog.casino_embed(f"{self.cog.e('cards')} Blackjack · {self.bet:,} chips", body + self.note,
                                  colour, image)
        e.set_footer(text=RULES_FOOTER)
        return e, image


def hand_total(g: Blackjack) -> str:
    from .cardgames import hand_value
    total = hand_value(g.dealer)[0]
    if len(g.dealer) == 2 and total == 21:
        return "blackjack"
    return "bust" if total > 21 else str(total)


class HiLoView(TableView):
    def __init__(self, cog: Economy, guild_id: int, player, game: HiLo, converted: int) -> None:
        super().__init__(cog, guild_id, player, HILO_TIMEOUT)
        self.game = game
        self.note = cog._cage_note(converted)
        self.rebuild()

    def rebuild(self) -> None:
        self.clear_items()
        g = self.game
        if g.done:
            return
        hi, lo = g.step_multiplier("higher"), g.step_multiplier("lower")
        self.button(f"Higher ×{hi:.2f}" if hi else "Higher", discord.ButtonStyle.success, "higher",
                    emoji="⬆️", disabled=not hi)
        self.button(f"Lower ×{lo:.2f}" if lo else "Lower", discord.ButtonStyle.danger, "lower",
                    emoji="⬇️", disabled=not lo)
        self.button(f"Cash out {int(g.bet * g.multiplier):,}", discord.ButtonStyle.secondary, "cash",
                    emoji=self.cog.partial("chip"), disabled=g.steps == 0)

    async def press(self, interaction: discord.Interaction, action: str) -> None:
        async with self.lock:
            g = self.game
            if g.done:
                await interaction.response.send_message("This round is over.", ephemeral=True)
                return
            self.note = ""
            if action == "cash":
                g.cash_out()
            else:
                try:
                    g.guess(action)
                except ValueError:
                    await interaction.response.send_message("You can't call that on this card.", ephemeral=True)
                    return
            if g.done:
                self.settle()
                self.stop()
            self.rebuild()
            await self.redraw(interaction)

    def settle(self) -> None:
        if self.settled:
            return
        self.settled = True
        if self.game.payout:
            self.cog._pay(self.guild_id, self.player.id, self.game.payout)
        self.release()

    def refund(self) -> None:
        if self.settled:
            return
        self.settled = True
        # Mid-run: pay out what they'd built up; before any call: hand the bet back.
        self.game.cash_out()
        self.cog._pay(self.guild_id, self.player.id, self.game.payout)
        self.release()

    async def on_timeout(self) -> None:
        async with self.lock:
            if not self.game.done:
                self.refund()
                self.note = "\n*Timed out — " + ("cashed out for you.*" if self.game.steps else "bet returned.*")
            self.clear_items()
            await self.redraw()

    async def frame(self) -> tuple[discord.Embed, discord.File | None]:
        g = self.game
        cog = self.cog
        image = await render(art.hilo_table, g, higher_mult=g.step_multiplier("higher") if not g.done else 0,
                             lower_mult=g.step_multiplier("lower") if not g.done else 0, filename="hilo.png")
        if not g.done:
            body = (f"The card is **{g.current}**. Higher or lower? (Aces high, ties lose.)\n"
                    f"Multiplier **×{g.multiplier:.2f}** — cash out now for {cog.money(int(g.bet * g.multiplier), 'chip')}.")
            colour = BRAND
        elif g.won:
            body = f"Cashed out at **×{g.multiplier:.2f}** — up {cog.money(g.payout - g.bet, 'chip')}."
            colour = OK
        else:
            body = f"It was **{g.current}** — wrong call. Down {cog.money(g.bet, 'chip')}."
            colour = WARN
        if g.done:
            chips = cog.bot.storage.econ_wallet(self.guild_id, self.player.id)["chips"]
            body += f"\nChips: {cog.money(chips, 'chip')}"
        e = cog.casino_embed(f"{cog.e('cards')} Hi-Lo · {g.bet:,} chips", body + self.note, colour, image)
        e.set_footer(text="Each correct call pays fair odds less 3% • Cash out any time after your first call")
        return e, image


def help_lines(prefix: str) -> list[str]:
    return [
        f"`{prefix}economy on` — open THE STRIP in your server (admin)",
        f"`{prefix}daily` / `{prefix}work` / `{prefix}hustle` — earn cash",
        f"`{prefix}blackjack` / `{prefix}slots` / `{prefix}roulette` / `{prefix}hilo` / `{prefix}flip` — the casino",
        f"`{prefix}balance` — your Players Club card · `{prefix}rich` — richest on the Strip",
    ]
